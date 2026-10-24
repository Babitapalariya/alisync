from .subscriptions import PLANS, plan_name, reserve_product, require_access, account, subscription_summary
from .models import PlanProduct
import hashlib
import hmac
import re
import secrets
import time
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlencode

import requests
import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException
from iop.base import IopClient, IopRequest
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import AliExpressCredential, AliExpressToken, Mapping, PendingImport, Product, Shop, SyncLog, User
from .schemas import (AliExpressCodeRequest, AliExpressCredentialRequest, ContactNumberRequest, LoginRequest, MappingCreate,
    PlanSelectionRequest, ProductCreate, ProductUpdate, RegisterRequest, ResendVerificationRequest,
    ShopifyCredentialRequest, ShopRequest, TokenRequest, VerifyEmailRequest)
from .security import create_token, current_user, decode_token, decrypt_secret, encrypt_secret, hash_password, verify_password
from .shopify_auth import shopify_token_error
from .shop_context import get_current_shop

router = APIRouter(prefix="/app", tags=["AliSync App"])
settings = get_settings()
oauth_states: dict[str, tuple[int, str]] = {}


def require_shopify_oauth_config() -> None:
    missing = []
    if not settings.SHOPIFY_CLIENT_ID.strip():
        missing.append("SHOPIFY_CLIENT_ID")
    if not settings.SHOPIFY_CLIENT_SECRET.strip():
        missing.append("SHOPIFY_CLIENT_SECRET")
    if not settings.SHOPIFY_REDIRECT_URI.strip():
        missing.append("SHOPIFY_REDIRECT_URI")
    if missing:
        raise HTTPException(
            503,
            "Shopify OAuth is not configured. Set " + ", ".join(missing)
            + " in alisync/backend/.env and restart the AliSync backend.",
        )


def require_aliexpress_oauth_config() -> None:
    missing = []
    if not settings.ALIEXPRESS_APP_KEY.strip():
        missing.append("ALIEXPRESS_APP_KEY")
    if not settings.ALIEXPRESS_APP_SECRET.strip():
        missing.append("ALIEXPRESS_APP_SECRET")
    if not settings.ALIEXPRESS_REDIRECT_URI.strip():
        missing.append("ALIEXPRESS_REDIRECT_URI")
    if missing:
        raise HTTPException(
            503,
            "AliExpress OAuth is not configured. Set " + ", ".join(missing)
            + " in alisync/backend/.env and restart the AliSync backend.",
        )


def owned_shop(db: Session, user: User, shop_id: int) -> Shop:
    shop = db.query(Shop).filter(Shop.id == shop_id, Shop.user_id == user.id, Shop.is_active == True).first()
    if not shop:
        raise HTTPException(404, "Shop not found")
    return shop


@router.get("/health")
def health():
    return {"status": "ok", "application": "AliSync"}


def _verification_hash(email: str, code: str) -> str:
    secret = settings.APP_SECRET or "alisync-development-secret"
    return hashlib.sha256(f"{email}:{code}:{secret}".encode()).hexdigest()


def _send_verification_email(email: str, code: str) -> bool:
    if not settings.SMTP_HOST or not settings.SMTP_FROM_EMAIL:
        print(f"[AliSync DEV] Verification code for {email}: {code}", flush=True)
        return False
    message = EmailMessage()
    message["Subject"] = "Verify your AliSync email"
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = email
    message.set_content(
        f"Your AliSync verification code is {code}. It expires in 10 minutes.\n\n"
        "If you did not create this account, you can ignore this email."
    )
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as smtp:
        if settings.SMTP_USE_TLS:
            smtp.starttls()
        if settings.SMTP_USERNAME:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(message)
    return True


def _issue_verification(user: User, db: Session) -> tuple[str, bool]:
    code = f"{secrets.randbelow(1_000_000):06d}"
    user.verification_code_hash = _verification_hash(user.email, code)
    user.verification_expires_at = datetime.utcnow() + timedelta(minutes=10)
    db.commit()
    try:
        sent = _send_verification_email(user.email, code)
    except Exception as exc:
        print(f"[AliSync EMAIL] Could not send verification email: {exc}", flush=True)
        sent = False
    return code, sent


def _normalize_contact_number(country_code: str, local_number: str,
                              country_iso: str | None = None) -> str:
    '''Validate a phone number and return its canonical E.164 representation.'''
    calling_code = re.sub(r'\D', '', country_code)
    if not calling_code or not re.search(r'\d', local_number):
        raise HTTPException(400, 'Enter a valid country code and contact number')
    try:
        if country_iso:
            phone = phonenumbers.parse(local_number.strip(), country_iso.upper())
        else:
            phone = next(
                (
                    candidate for region in phonenumbers.region_codes_for_country_code(int(calling_code))
                    if (candidate := phonenumbers.parse(local_number.strip(), region))
                    and candidate.country_code == int(calling_code)
                    and phonenumbers.is_valid_number(candidate)
                ),
                None,
            )
    except NumberParseException:
        raise HTTPException(400, 'Enter a real contact number for the selected country')
    if phone is None or phone.country_code != int(calling_code) or not phonenumbers.is_valid_number(phone):
        raise HTTPException(400, 'Enter a real contact number for the selected country')
    return phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164)


@router.get('/auth/phone-countries')
def phone_countries():
    countries = [
        {'iso': region, 'calling_code': f'+{phonenumbers.country_code_for_region(region)}'}
        for region in sorted(phonenumbers.SUPPORTED_REGIONS)
        if phonenumbers.country_code_for_region(region)
    ]
    return {'countries': countries}


@router.post('/auth/validate-contact')
def validate_contact_number(payload: ContactNumberRequest):
    normalized = _normalize_contact_number(
        payload.country_code, payload.contact_number, payload.country_iso
    )
    return {'valid': True, 'contact_number': normalized}


@router.post('/auth/register', status_code=201)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    email = payload.email.lower()
    local_number = re.sub(r'\D', '', payload.contact_number)
    contact_number = _normalize_contact_number(
        payload.country_code, payload.contact_number, payload.country_iso
    )
    if payload.password != payload.confirm_password:
        raise HTTPException(400, "Passwords do not match")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "Email already registered")
    if db.query(User).filter(User.contact_number.in_([contact_number, local_number])).first():
        raise HTTPException(409, 'This contact number is already registered')
    user = User(email=email, contact_number=contact_number,
                password_hash=hash_password(payload.password), email_verified=False,
                subscription_status="none")
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'This email or contact number is already registered')
    db.refresh(user)
    code, sent = _issue_verification(user, db)
    response = {"message": "Verification code sent to your email", "email": user.email,
                "email_sent": sent}
    if not sent and (not settings.SMTP_HOST or not settings.SMTP_FROM_EMAIL):
        response["dev_verification_code"] = code
        response["message"] = "SMTP is not configured; use the development verification code"
    elif not sent:
        response["message"] = "Account created, but the email could not be sent. Try resending the code."
    return response


@router.post("/auth/verify-email")
def verify_email(payload: VerifyEmailRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email.lower(), User.is_active == True).first()
    if not user:
        raise HTTPException(404, "Account not found")
    if user.email_verified:
        return {"access_token": create_token(user.id), "token_type": "bearer"}
    if not user.verification_code_hash or not user.verification_expires_at:
        raise HTTPException(400, "Request a new verification code")
    if user.verification_expires_at < datetime.utcnow():
        raise HTTPException(400, "Verification code expired")
    supplied = _verification_hash(user.email, payload.code)
    if not hmac.compare_digest(supplied, user.verification_code_hash):
        raise HTTPException(400, "Invalid verification code")
    user.email_verified = True
    user.verification_code_hash = None
    user.verification_expires_at = None
    db.commit()
    return {"access_token": create_token(user.id), "token_type": "bearer",
            "user": {"id": user.id, "email": user.email}}


@router.post("/auth/resend-verification")
def resend_verification(payload: ResendVerificationRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email.lower(), User.is_active == True).first()
    if not user:
        raise HTTPException(404, "Account not found")
    if user.email_verified:
        return {"message": "Email is already verified", "email_sent": True}
    code, sent = _issue_verification(user, db)
    response = {"message": "A new verification code was sent", "email_sent": sent}
    if not sent and (not settings.SMTP_HOST or not settings.SMTP_FROM_EMAIL):
        response["dev_verification_code"] = code
    elif not sent:
        response["message"] = "The email could not be sent. Please try again."
    return response


@router.post("/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email.lower(), User.is_active == True).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    if user.email_verified is False:
        raise HTTPException(403, "Verify your email before signing in")
    if user.email_verified is None:
        user.email_verified = True
        db.commit()
    return {"access_token": create_token(user.id), "token_type": "bearer", "user": {"id": user.id, "email": user.email, "full_name": user.full_name}}


@router.get("/auth/google/status")
def google_status():
    configured = bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)
    return {"configured": configured, "redirect_uri": settings.GOOGLE_REDIRECT_URI}


def _google_return(**parameters):
    response = RedirectResponse(settings.FRONTEND_URL.rstrip("/") + "/#" + urlencode(parameters), status_code=303)
    response.delete_cookie("alisync_google_state", path="/app/auth/google", httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/auth/google")
def google_login():
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        return _google_return(auth_error="Google sign-in is not configured yet. Please use email sign-in.")
    if len(settings.APP_SECRET) < 32:
        return _google_return(auth_error="Sign-in is temporarily unavailable.")
    nonce = secrets.token_urlsafe(32)
    payload = f"{int(time.time())}.{nonce}"
    signature = hmac.new(settings.APP_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    state = f"{payload}.{signature}"
    query = urlencode({"client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI, "response_type": "code",
        "scope": "openid email profile", "state": state, "prompt": "select_account"})
    response = RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}", status_code=303)
    response.set_cookie("alisync_google_state", state, max_age=600, httponly=True,
        secure=settings.GOOGLE_REDIRECT_URI.startswith("https://"), samesite="lax", path="/app/auth/google")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None,
                    error: str | None = None, db: Session = Depends(get_db)):
    cookie = request.cookies.get("alisync_google_state", "")
    try:
        timestamp, nonce, signature = (state or "").split(".")
        expected = hmac.new(settings.APP_SECRET.encode(), f"{timestamp}.{nonce}".encode(), hashlib.sha256).hexdigest()
        valid = (len(settings.APP_SECRET) >= 32 and bool(cookie) and hmac.compare_digest(cookie, state)
                 and hmac.compare_digest(signature, expected) and 0 <= time.time() - int(timestamp) <= 600)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        return _google_return(auth_error="Google sign-in expired or could not be verified. Please try again.")
    if error or not code:
        return _google_return(auth_error="Google sign-in was cancelled. You can try again or use email sign-in.")
    try:
        token_response = requests.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code"}, timeout=20)
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]
        profile_response = requests.get("https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
        profile_response.raise_for_status()
        profile = profile_response.json()
    except (requests.RequestException, KeyError, ValueError):
        return _google_return(auth_error="Google sign-in could not be completed. Please try again.")
    email = str(profile.get("email") or "").strip().lower()
    if not profile.get("sub") or not email or profile.get("email_verified") is not True:
        return _google_return(auth_error="Google did not provide a verified email address.")
    user = db.query(User).filter(User.email == email).first()
    if user and not user.is_active:
        return _google_return(auth_error="This account is unavailable. Contact support.")
    if not user:
        user = User(email=email, full_name=profile.get("name"),
                    password_hash=hash_password(secrets.token_urlsafe(32)),
                    email_verified=True, subscription_status="none")
        db.add(user)
    else:
        user.email_verified = True
        if not user.full_name:
            user.full_name = profile.get("name")
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        return _google_return(auth_error="Your account was updated during sign-in. Please try again.")
    return _google_return(auth_token=create_token(user.id))


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "email": user.email, "full_name": user.full_name,
            "contact_number": user.contact_number, "email_verified": user.email_verified is not False,
            "selected_plan": plan_name(user) if user.selected_plan else None,
            "subscription_status": user.subscription_status or "none"}


@router.post("/subscription/select")
def select_subscription(payload: PlanSelectionRequest, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    plans = set(PLANS)
    plan = payload.plan.lower().strip()
    if plan == "starter": plan = "free"
    if get_settings().BILLING_ENABLED and plan != "free":
        raise HTTPException(409, "Paid billing is not connected. Contact support to change your plan.")
    account(db, user.id, lock=True)
    if plan not in plans:
        raise HTTPException(400, "Unknown subscription plan")
    if not user.email_verified:
        raise HTTPException(403, "Verify your email first")
    user.selected_plan = plan
    user.subscription_status = "active_no_billing"
    db.commit()
    return {"message": f"{plan.title()} plan selected", "plan": plan,
            "subscription_status": "active_no_billing", "billing_enabled": False, "payment_required": False}

@router.get("/connections/status")
def connection_status(shop_id: int | None = None, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    shops = db.query(Shop).filter(Shop.user_id == user.id, Shop.is_active == True).order_by(Shop.id).all()
    selected = next((shop for shop in shops if shop.id == shop_id), None) if shop_id else None
    if selected is None and len(shops) == 1:
        selected = shops[0]
    ali_token = db.query(AliExpressToken).filter(AliExpressToken.user_id == user.id).first()
    ali_credentials = db.query(AliExpressCredential).filter(AliExpressCredential.user_id == user.id).first()
    return {
        "shopify": {
            "connected": bool(selected and selected.access_token),
            "pending": not bool(selected and selected.access_token),
            "credentials_configured": bool(selected and selected.shopify_client_id and selected.shopify_client_secret_encrypted),
            "client_id_hint": ("***" + selected.shopify_client_id[-4:]) if selected and selected.shopify_client_id else None,
            "shop_id": selected.id if selected else None,
            "shop_domain": selected.shop_domain if selected else None,
            "shops_count": len(shops),
        },
        "aliexpress": {
            "connected": bool(ali_token and ali_token.access_token),
            "pending": not bool(ali_token and ali_token.access_token),
            "credentials_configured": bool(ali_credentials),
            "app_key_hint": ("***" + ali_credentials.app_key[-4:]) if ali_credentials else None,
            "redirect_uri": ali_credentials.redirect_uri if ali_credentials else None,
        },
        "ready": bool(selected and selected.access_token and ali_token and ali_token.access_token),
    }


@router.get("/dashboard/stats")
def stats(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return {
        "shops": db.query(Shop).filter(Shop.user_id == user.id, Shop.is_active == True).count(),
        "products": db.query(Product).filter(Product.user_id == user.id).count(),
        "mappings": db.query(Mapping).filter(Mapping.user_id == user.id).count(),
        "pending": db.query(PendingImport).filter(PendingImport.user_id == user.id, PendingImport.status == "pending").count(),
    }


@router.get("/shops")
def shops(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.query(Shop).filter(Shop.user_id == user.id).order_by(Shop.id.desc()).all()
    return [{"id": x.id, "shop_domain": x.shop_domain, "connected": bool(x.access_token), "is_active": x.is_active} for x in rows]


@router.post("/shops", status_code=201)
def add_shop(payload: ShopRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    domain = payload.shop_domain.lower().strip().removeprefix("https://").rstrip("/")
    if not domain.endswith(".myshopify.com"):
        raise HTTPException(400, "Shop must end with .myshopify.com")
    shop = db.query(Shop).filter(Shop.user_id == user.id, Shop.shop_domain == domain).first()
    if not shop:
        shop = Shop(user_id=user.id, shop_domain=domain); db.add(shop); db.commit(); db.refresh(shop)
    return {"id": shop.id, "shop_domain": shop.shop_domain}


@router.delete("/shops/{shop_id}")
def remove_shop(shop_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    shop = owned_shop(db, user, shop_id); shop.is_active = False; db.commit()
    return {"message": "Shop disconnected"}


@router.get("/shopify/install/{shop_id}")
def shopify_install(shop_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Connect a shop with the same server-side grant used by the single-user app."""
    shop = owned_shop(db, user, shop_id)
    if not shop.shopify_client_id or not shop.shopify_client_secret_encrypted:
        raise HTTPException(409, "Enter Shopify credentials for this store first")
    client_secret = decrypt_secret(shop.shopify_client_secret_encrypted)
    try:
        response = requests.post(
            f"https://{shop.shop_domain}/admin/oauth/access_token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": shop.shopify_client_id,
                "client_secret": client_secret,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        detail = (shopify_token_error(exc.response, shop.shop_domain)
                  if exc.response is not None else "Could not reach Shopify. Check your network and try again.")
        raise HTTPException(502, detail) from exc
    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(502, "Shopify did not return an access token")
    from datetime import datetime, timedelta, timezone
    shop.access_token = access_token
    granted_scopes = data.get("scope") or ""
    granted = {scope for scope in re.split(r"[\s,]+", granted_scopes) if scope}
    # Shopify's token response can omit or abbreviate the scope list. Ask the
    # authenticated endpoint for the authoritative set before rejecting it.
    try:
        scope_response = requests.get(
            f"https://{shop.shop_domain}/admin/oauth/access_scopes.json",
            headers={"X-Shopify-Access-Token": access_token}, timeout=20,
        )
        scope_response.raise_for_status()
        scope_rows = scope_response.json().get("access_scopes", [])
        authoritative = {row.get("handle") for row in scope_rows if row.get("handle")}
        if authoritative:
            granted = authoritative
    except (requests.RequestException, ValueError, TypeError):
        pass
    # A Shopify write_* scope includes access to read the same resource. Do
    # not reject a valid token merely because Shopify omits the redundant
    # read_* handle from its granted-scope response.
    effective_granted = set(granted)
    effective_granted.update("read_" + scope[6:] for scope in granted if scope.startswith("write_"))
    required = {scope for scope in re.split(r"[\s,]+", settings.SHOPIFY_SCOPES) if scope}
    missing = sorted(required - effective_granted)
    if missing:
        raise HTTPException(
            409,
            "The installed Shopify app is missing required permissions: " + ", ".join(missing)
            + ". Add these scopes to the app version in Shopify Dev Dashboard, release the version, "
              "install or update it on this store, and reconnect in AliSync. Shopify currently reports: "
            + (", ".join(sorted(granted)) if granted else "no granted scopes") + ".",
        )
    granted_scopes = ",".join(sorted(granted))
    shop.scopes = granted_scopes
    shop.scope = granted_scopes
    expires_in = int(data.get("expires_in") or 86399)
    shop.access_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, expires_in - 300))
    db.commit()
    return {
        "connected": True,
        "shop_id": shop.id,
        "shop_domain": shop.shop_domain,
        "expires_in": expires_in,
        "message": "Shopify store connected",
    }


@router.put("/shops/{shop_id}/credentials")
def save_shopify_credentials(shop_id: int, payload: ShopifyCredentialRequest,
                             db: Session = Depends(get_db), user: User = Depends(current_user)):
    shop = owned_shop(db, user, shop_id)
    shop.shopify_client_id = payload.client_id.strip()
    shop.shopify_client_secret_encrypted = encrypt_secret(payload.client_secret)
    # Changing credentials invalidates the token previously issued for another app.
    shop.access_token = None
    shop.access_token_expires_at = None
    shop.shopify_auth_method = "client_credentials"
    shop.shopify_refresh_token_encrypted = None
    shop.shopify_refresh_token_expires_at = None
    db.commit()
    return {"message": "Shopify credentials saved", "shop_id": shop.id,
            "client_id_hint": "***" + shop.shopify_client_id[-4:]}


@router.get("/shopify/callback")
def shopify_callback(request: Request, shop: str, code: str, state: str,
                     hmac_value: str = Query(..., alias="hmac"), db: Session = Depends(get_db)):
    require_shopify_oauth_config()
    context = oauth_states.pop(state, None)
    if not context or context[1] != shop:
        raise HTTPException(403, "Invalid OAuth state")
    params = dict(request.query_params); supplied = params.pop("hmac", "")
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    expected = hmac_module(settings.SHOPIFY_CLIENT_SECRET, message)
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(403, "Invalid Shopify signature")
    response = requests.post(f"https://{shop}/admin/oauth/access_token", json={"client_id": settings.SHOPIFY_CLIENT_ID,
        "client_secret": settings.SHOPIFY_CLIENT_SECRET, "code": code}, timeout=30)
    response.raise_for_status(); data = response.json()
    row = db.query(Shop).filter(Shop.user_id == context[0], Shop.shop_domain == shop).first()
    row.access_token = data["access_token"]; row.access_token_expires_at = None; row.scopes = data.get("scope"); db.commit()
    return RedirectResponse(f"{settings.FRONTEND_URL}?shopify=connected")


def hmac_module(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


@router.get("/login")
def aliexpress_login(db: Session = Depends(get_db), user: User = Depends(current_user)):
    credentials = db.query(AliExpressCredential).filter(AliExpressCredential.user_id == user.id).first()
    if not credentials:
        raise HTTPException(409, "Enter your AliExpress app credentials first")
    state = create_token(user.id, expires_seconds=600)
    query = urlencode({"response_type": "code", "force_auth": "true",
        "redirect_uri": credentials.redirect_uri,
        "client_id": credentials.app_key, "state": state})
    return {"auth_url": f"https://api-sg.aliexpress.com/oauth/authorize?{query}"}


@router.put("/aliexpress/credentials")
def save_aliexpress_credentials(payload: AliExpressCredentialRequest, db: Session = Depends(get_db),
                                 user: User = Depends(current_user)):
    redirect_uri = payload.redirect_uri.strip()
    if not redirect_uri.startswith("https://") and not redirect_uri.startswith("http://localhost"):
        raise HTTPException(400, "Callback URL must use HTTPS (localhost is allowed for development)")
    row = db.query(AliExpressCredential).filter(AliExpressCredential.user_id == user.id).first()
    if not row:
        row = AliExpressCredential(user_id=user.id, app_key=payload.app_key.strip(),
            app_secret_encrypted=encrypt_secret(payload.app_secret), redirect_uri=redirect_uri)
        db.add(row)
    else:
        row.app_key = payload.app_key.strip(); row.app_secret_encrypted = encrypt_secret(payload.app_secret)
        row.redirect_uri = redirect_uri
    db.commit()
    return {"message": "AliExpress credentials saved", "app_key_hint": "***" + row.app_key[-4:],
            "redirect_uri": row.redirect_uri}


def exchange_aliexpress_code(db: Session, user: User, code: str) -> AliExpressToken:
    credentials = db.query(AliExpressCredential).filter(AliExpressCredential.user_id == user.id).first()
    if not credentials:
        raise HTTPException(401, "AliExpress credentials are unavailable")
    try:
        client = IopClient(settings.ALIEXPRESS_API_URL, credentials.app_key,
                           decrypt_secret(credentials.app_secret_encrypted))
        token_request = IopRequest("/auth/token/create", "POST")
        token_request.add_api_param("app_key", credentials.app_key)
        token_request.add_api_param("code", code.strip())
        data = client.execute(token_request).body
    except Exception as exc:
        raise HTTPException(502, f"AliExpress token exchange failed: {exc}") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise HTTPException(502, {"message": "AliExpress did not return an access token", "response": data})
    token = db.query(AliExpressToken).filter(AliExpressToken.user_id == user.id).first()
    if not token:
        token = AliExpressToken(user_id=user.id, access_token=data["access_token"]); db.add(token)
    token.access_token = data.get("access_token") or token.access_token
    token.refresh_token = data.get("refresh_token") or token.refresh_token
    token.expires_in = data.get("expires_in")
    db.commit(); db.refresh(token)
    return token


@router.post("/aliexpress/exchange-code")
def submit_aliexpress_code(payload: AliExpressCodeRequest, db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    token = exchange_aliexpress_code(db, user, payload.code)
    return {"connected": True, "message": "AliExpress connected", "expires_in": token.expires_in}


@router.get("/callback")
def aliexpress_callback(code: str, state: str, db: Session = Depends(get_db)):
    user_id = decode_token(state)
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(401, "User account is unavailable")
    exchange_aliexpress_code(db, user, code)
    return RedirectResponse(f"{settings.FRONTEND_URL}?aliexpress=connected")


@router.post("/refresh-token")
def refresh_aliexpress_token(db: Session = Depends(get_db), user: User = Depends(current_user)):
    token = db.query(AliExpressToken).filter(AliExpressToken.user_id == user.id).first()
    if not token or not token.refresh_token:
        raise HTTPException(404, "No refresh token found. Visit /app/login first.")
    credentials = db.query(AliExpressCredential).filter(AliExpressCredential.user_id == user.id).first()
    if not credentials:
        raise HTTPException(404, "AliExpress credentials are not configured")
    response = requests.post("https://oauth.aliexpress.com/token", data={"grant_type": "refresh_token",
        "client_id": credentials.app_key, "client_secret": decrypt_secret(credentials.app_secret_encrypted),
        "refresh_token": token.refresh_token}, timeout=30)
    if not response.ok:
        raise HTTPException(502, f"AliExpress refresh failed: {response.text}")
    data = response.json(); token.access_token = data.get("access_token") or token.access_token
    token.refresh_token = data.get("refresh_token") or token.refresh_token; token.expires_in = data.get("expires_in")
    db.commit()
    return {"message": "AliExpress token refreshed", "expires_in": token.expires_in}


@router.put("/aliexpress/token")
def save_aliexpress_token(payload: TokenRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    token = db.query(AliExpressToken).filter(AliExpressToken.user_id == user.id).first()
    if not token: token = AliExpressToken(user_id=user.id, access_token=payload.access_token); db.add(token)
    token.access_token = payload.access_token; token.refresh_token = payload.refresh_token; token.expires_in = payload.expires_in
    db.commit(); return {"message": "AliExpress account connected"}


@router.get("/products")
def products(shop_id: int | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
             db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = db.query(Product).filter(Product.user_id == user.id)
    if shop_id is not None: owned_shop(db, user, shop_id); query = query.filter(Product.shop_id == shop_id)
    total = query.count(); rows = query.order_by(Product.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [{"id": x.id, "shop_id": x.shop_id, "aliexpress_id": x.aliexpress_id, "shopify_product_id": x.shopify_product_id,
        "title": x.title, "price": x.price, "currency": x.currency, "status": x.status, "track_price": x.track_price} for x in rows], "total": total}


@router.post("/products", status_code=201)
def create_product(payload: ProductCreate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    owned_shop(db, user, payload.shop_id)
    if db.query(Product).filter(Product.shop_id == payload.shop_id, Product.aliexpress_id == payload.aliexpress_id).first():
        raise HTTPException(409, "Product already exists for this shop")
    reserve_product(db, owned_shop(db, user, payload.shop_id), payload.aliexpress_id)
    row = Product(user_id=user.id, **payload.model_dump()); db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "message": "Product saved"}


@router.get("/products/{product_id}")
def product(product_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.query(Product).filter(Product.id == product_id, Product.user_id == user.id).first()
    if not row: raise HTTPException(404, "Product not found")
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


@router.patch("/products/{product_id}")
def update_product(product_id: int, payload: ProductUpdate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.query(Product).filter(Product.id == product_id, Product.user_id == user.id).first()
    if not row: raise HTTPException(404, "Product not found")
    changes = payload.model_dump(exclude_unset=True)
    if any(key in changes for key in ("price", "track_price", "price_adjustment")):
        require_access(db, owned_shop(db, user, row.shop_id), row.aliexpress_id)
    for key, value in changes.items(): setattr(row, key, value)
    db.commit(); return {"message": "Product updated"}


@router.delete("/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.query(Product).filter(Product.id == product_id, Product.user_id == user.id).first()
    if not row: raise HTTPException(404, "Product not found")
    db.delete(row)
    from alisync_app.subscriptions import release_deleted_slot
    release_deleted_slot(db, row.user_id, row.shop_id, row.aliexpress_id)
    db.commit(); return {"message": "Product deleted"}


@router.get("/mappings")
def mappings(db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    query = db.query(Mapping).filter(Mapping.user_id == shop.user_id, Mapping.shop_id == shop.id)
    return [{column.name: getattr(x, column.name) for column in x.__table__.columns} for x in query.order_by(Mapping.id.desc()).all()]


@router.post("/mappings", status_code=201)
def create_mapping(payload: MappingCreate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    owned_shop(db, user, payload.shop_id)
    aliexpress_id = payload.aliexpress_id.strip()
    shopify_product_id = payload.shopify_product_id.strip()
    if not aliexpress_id or not shopify_product_id:
        raise HTTPException(400, "AliExpress ID and Shopify product ID are required")
    existing_shopify = db.query(Mapping).filter(
        Mapping.shop_id == payload.shop_id, Mapping.shopify_product_id == shopify_product_id
    ).first()
    if existing_shopify:
        raise HTTPException(409, f"Shopify product {shopify_product_id} is already linked to AliExpress "
                                 f"{existing_shopify.aliexpress_id}")
    imported_link = db.query(Product).filter(
        Product.shop_id == payload.shop_id, Product.shopify_product_id == shopify_product_id
    ).first()
    if imported_link and imported_link.aliexpress_id != aliexpress_id:
        raise HTTPException(409, f"Shopify product {shopify_product_id} is already linked to AliExpress "
                                 f"{imported_link.aliexpress_id} in Imported Products")
    values = payload.model_dump(); values.update(aliexpress_id=aliexpress_id, shopify_product_id=shopify_product_id)
    reserve_product(db, owned_shop(db, user, payload.shop_id), aliexpress_id, mapping=True)
    row = Mapping(user_id=user.id, **values); db.add(row)
    try: db.commit(); db.refresh(row)
    except Exception: db.rollback(); raise HTTPException(409, "Mapping already exists")
    return {"id": row.id, "message": "Mapping created"}


@router.delete("/mappings/{mapping_id}")
def delete_mapping(mapping_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    row = db.query(Mapping).filter(Mapping.id == mapping_id, Mapping.user_id == shop.user_id, Mapping.shop_id == shop.id).first()
    if not row: raise HTTPException(404, "Mapping not found")
    db.delete(row)
    from alisync_app.subscriptions import release_deleted_slot
    release_deleted_slot(db, row.user_id, row.shop_id, row.aliexpress_id)
    db.commit(); return {"message": "Mapping deleted"}


@router.get("/pending")
def pending(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.query(PendingImport).filter(PendingImport.user_id == user.id).order_by(PendingImport.id.desc()).all()
    return [{column.name: getattr(x, column.name) for column in x.__table__.columns} for x in rows]


@router.post("/pending/{pending_id}/retry")
def retry_pending(pending_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.query(PendingImport).filter(PendingImport.id == pending_id, PendingImport.user_id == user.id).first()
    if not row: raise HTTPException(404, "Pending import not found")
    row.status = "partial" if row.in_stock_skus else "pending"
    row.retry_count += 1; row.error_message = None; db.commit()
    return {"message": "Import queued for retry"}


@router.get("/sync/logs")
def sync_logs(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.query(SyncLog).filter(SyncLog.user_id == user.id).order_by(SyncLog.id.desc()).limit(limit).all()
    return [{column.name: getattr(x, column.name) for column in x.__table__.columns} for x in rows]


@router.get("/subscription/entitlements")
def subscription_entitlements(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return subscription_summary(db, user)


@router.post("/subscription/products/{entity_type}/{entity_id}/access")
def set_full_product_access(entity_type: str, entity_id: int, payload: dict,
                            db: Session = Depends(get_db), user: User = Depends(current_user)):
    if entity_type not in {"imported", "mapping"}:
        raise HTTPException(400, "Invalid product type")
    if not isinstance(payload.get("enabled"), bool):
        raise HTTPException(400, "enabled must be true or false")
    owner = account(db, user.id, lock=True)
    if plan_name(owner) != "growth":
        raise HTTPException(400, "Choose your 5 full-access products on Growth. Free includes its first 5 products automatically; Pro includes every product.")
    model = Product if entity_type == "imported" else Mapping
    entity = db.query(model).filter_by(id=entity_id, user_id=user.id).first()
    if not entity: raise HTTPException(404, "Product not found")
    from .subscriptions import hydrate, FULL_LIMIT
    hydrate(db, user.id)
    row = db.query(PlanProduct).filter_by(user_id=user.id, shop_id=entity.shop_id, aliexpress_id=entity.aliexpress_id).with_for_update().one()
    if payload["enabled"] and not row.full_access and len(db.query(PlanProduct).filter_by(user_id=user.id, full_access=True).with_for_update().all()) >= 5:
        raise HTTPException(403, FULL_LIMIT)
    row.full_access = payload["enabled"]
    db.commit()
    return {"message": "Full access enabled" if row.full_access else "Full-access slot released", "full_access": row.full_access}
