"""Shopify compliance callbacks: authenticate before parsing or touching data."""
import base64
import hashlib
import hmac
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import Shop, Product, Mapping, PendingImport, SyncLog, Notification, PlanProduct

router = APIRouter(tags=["Shopify privacy webhooks"])
TOPICS = {"customers/data_request", "customers/redact", "shop/redact"}


async def verified_payload(request: Request):
    secret = get_settings().SHOPIFY_CLIENT_SECRET
    if not secret:
        raise HTTPException(503, "Shopify webhook secret is not configured")
    body = await request.body()
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest())
    supplied = request.headers.get("X-Shopify-Hmac-Sha256", "").encode()
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(401, "Invalid Shopify webhook signature")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected a JSON object")
    domain = payload.get("shop_domain")
    if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", domain):
        raise HTTPException(400, "Invalid shop_domain")
    header_domain = request.headers.get("X-Shopify-Shop-Domain")
    if header_domain and header_domain != domain:
        raise HTTPException(400, "Shop domain mismatch")
    return payload


@router.post("/api/webhooks/shopify/privacy")
@router.post("/app/webhooks/shopify/privacy")
async def privacy_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await verified_payload(request)
    topic = request.headers.get("X-Shopify-Topic", "")
    if topic not in TOPICS:
        raise HTTPException(400, "Unsupported privacy topic")
    if topic != "shop/redact":
        # AliSync stores product/supplier data, not Shopify customer or order
        # records. Do not persist the customer PII supplied in these callbacks.
        # Extend this handler if customer/order ingestion is introduced.
        return {"received": True, "customer_data_stored": False}

    settings = get_settings()
    shops = db.query(Shop).filter(
        func.lower(Shop.shop_domain) == payload["shop_domain"],
        or_(Shop.shopify_client_id.is_(None), Shop.shopify_client_id == "",
            Shop.shopify_client_id == settings.SHOPIFY_CLIENT_ID),
    ).with_for_update().all()
    try:
        for shop in shops:
            # Explicit child deletion also works on databases where FK cascade
            # was not enabled. Never call Shopify or delete another shop/account.
            for model in (Notification, SyncLog, PlanProduct, PendingImport, Mapping, Product):
                db.query(model).filter(model.shop_id == shop.id).delete(synchronize_session=False)
            db.delete(shop)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"received": True}
