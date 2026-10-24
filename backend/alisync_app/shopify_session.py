"""Validate Shopify App Bridge ID tokens without granting AliSync account access."""
import re

import jwt
from fastapi import APIRouter, Header, HTTPException, Depends
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import Shop, User
from .security import account_user
from .shopify_exchange import exchange_token, is_fresh

router = APIRouter(tags=["Shopify embedded session"])


def verify_shopify_session(token: str) -> dict:
    settings = get_settings()
    if not settings.SHOPIFY_CLIENT_ID or not settings.SHOPIFY_CLIENT_SECRET:
        raise HTTPException(503, "Shopify session verification is not configured")
    try:
        claims = jwt.decode(
            token, settings.SHOPIFY_CLIENT_SECRET, algorithms=["HS256"],
            audience=settings.SHOPIFY_CLIENT_ID,
            options={"require": ["exp", "nbf", "iat", "iss", "dest", "aud", "sub"]},
        )
        dest = claims["dest"]
        if not isinstance(dest, str) or not re.fullmatch(r"https://[a-z0-9][a-z0-9-]*\.myshopify\.com", dest):
            raise ValueError("Invalid destination")
        if claims["iss"] != dest + "/admin" or not claims["sub"]:
            raise ValueError("Invalid issuer or subject")
        return {"shop_domain": dest.removeprefix("https://"), "shopify_user_id": claims["sub"]}
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(401, "Invalid or expired Shopify session") from exc


@router.get("/app/auth/shopify-session")
@router.get("/api/auth/shopify-session")
def shopify_session(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Shopify session token required")
    return verify_shopify_session(authorization[7:])


@router.post("/api/auth/shopify-connect")
@router.post("/app/auth/shopify-connect")
def connect_shopify(shopify_token: str = Header(alias="X-Shopify-Session-Token"),
                    user: User = Depends(account_user), db: Session = Depends(get_db)):
    # Require both account ownership and a valid Shopify session. Never accept
    # a caller-supplied shop domain, client ID, or client secret.
    session = verify_shopify_session(shopify_token)
    domain = session["shop_domain"]
    try:
        db.query(User).filter(User.id == user.id).with_for_update().one()
        shop = db.query(Shop).filter(Shop.user_id == user.id, Shop.shop_domain == domain).with_for_update().first()
        if shop and shop.shopify_client_id and shop.shopify_client_id != get_settings().SHOPIFY_CLIENT_ID:
            raise HTTPException(409, "This store is connected using another app. Its existing connection has been preserved.")
        if not shop:
            shop = Shop(user_id=user.id, shop_domain=domain)
            db.add(shop)
            db.flush()
        if shop.shopify_auth_method == "token_exchange" and shop.is_active and shop.access_token and is_fresh(shop.access_token_expires_at):
            pass
        else:
            exchange_token(shop, shopify_token)
        shop.is_active = True
        shop.uninstalled_at = None
        db.commit()
        return {"connected": True, "shop_id": shop.id, "shop_domain": shop.shop_domain}
    except Exception:
        db.rollback()
        raise
