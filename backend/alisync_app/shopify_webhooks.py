"""Shopify compliance callbacks: authenticate before parsing or touching data."""
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import (Shop, User, Product, Mapping, PendingImport, SyncLog, Notification, PlanProduct,
    StoreOrder, StoreOrderLine, OrderActivity, OrderSyncState, OrderPrivacyRequest, OrderFulfillment,
    OrderWebhookTask, OrderRedactedCustomer)

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
    domain = (request.headers.get("X-Shopify-Shop-Domain")
              if request.headers.get("X-Shopify-Topic") in {"app/uninstalled", "app_subscriptions/update", "orders/create", "orders/updated", "orders/cancelled", "orders/paid"} else payload.get("shop_domain"))
    if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", domain):
        raise HTTPException(400, "Invalid shop_domain")
    header_domain = request.headers.get("X-Shopify-Shop-Domain")
    if header_domain and header_domain != domain:
        raise HTTPException(400, "Shop domain mismatch")
    return payload


@router.post("/api/webhooks/shopify/uninstalled")
@router.post("/app/webhooks/shopify/uninstalled")
async def uninstall_webhook(request: Request, db: Session = Depends(get_db)):
    await verified_payload(request)
    if request.headers.get("X-Shopify-Topic") != "app/uninstalled":
        raise HTTPException(400, "Unsupported uninstall topic")
    shops = db.query(Shop).filter(
        Shop.shop_domain == request.headers["X-Shopify-Shop-Domain"],
        or_(Shop.shopify_client_id.is_(None), Shop.shopify_client_id == "",
            Shop.shopify_client_id == get_settings().SHOPIFY_CLIENT_ID)).all()
    for shop in shops:
        shop.is_active = False
        shop.access_token = None
        shop.shopify_refresh_token_encrypted = None
        shop.uninstalled_at = datetime.now(timezone.utc)
        shop.subscription_id = None
        shop.subscription_status = "none"
        shop.plan_name = "free"
    db.flush()
    db.commit()
    return {"received": True}


@router.post("/api/webhooks/shopify/subscriptions")
@router.post("/app/webhooks/shopify/subscriptions")
async def subscription_webhook(request: Request, db: Session = Depends(get_db)):
    await verified_payload(request)
    if request.headers.get("X-Shopify-Topic") != "app_subscriptions/update":
        raise HTTPException(400, "Unsupported subscription topic")
    shops = db.query(Shop).filter(
        Shop.shop_domain == request.headers["X-Shopify-Shop-Domain"], Shop.is_active == True,
        or_(Shop.shopify_client_id.is_(None), Shop.shopify_client_id == "",
            Shop.shopify_client_id == get_settings().SHOPIFY_CLIENT_ID)).all()
    from .billing import reconcile
    for shop in shops:
        # Fetch current state so delayed events cannot restore an obsolete plan.
        reconcile(db, shop)
    return {"received": True}


@router.post("/api/webhooks/shopify/orders")
@router.post("/app/webhooks/shopify/orders")
async def order_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await verified_payload(request)
    topic = request.headers.get("X-Shopify-Topic", "")
    if topic not in {"orders/create", "orders/updated", "orders/cancelled", "orders/paid"}:
        raise HTTPException(400, "Unsupported order topic")
    order_id = str(payload.get("id", ""))
    if not order_id.isdigit():
        raise HTTPException(400, "Invalid Shopify order ID")
    shops = db.query(Shop).filter(Shop.shop_domain == request.headers["X-Shopify-Shop-Domain"], Shop.is_active == True,
        or_(Shop.shopify_client_id.is_(None), Shop.shopify_client_id == "", Shop.shopify_client_id == get_settings().SHOPIFY_CLIENT_ID)).with_for_update().all()
    for shop in shops:
        digest = hashlib.sha256(str(shop.id).encode() + topic.encode() + await request.body()).hexdigest()
        if not db.query(OrderWebhookTask).filter_by(event_key=digest).first():
            db.add(OrderWebhookTask(shop_id=shop.id, event_key=digest, order_gid="gid://shopify/Order/" + order_id))
    db.commit()
    return {"received": True}


@router.post("/api/webhooks/shopify/privacy")
@router.post("/app/webhooks/shopify/privacy")
async def privacy_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await verified_payload(request)
    topic = request.headers.get("X-Shopify-Topic", "")
    if topic not in TOPICS:
        raise HTTPException(400, "Unsupported privacy topic")
    if topic != "shop/redact":
        from .orders import serialized_order
        from .security import encrypt_secret
        shops = db.query(Shop).filter(Shop.shop_domain == payload["shop_domain"],
            or_(Shop.shopify_client_id.is_(None), Shop.shopify_client_id == "", Shop.shopify_client_id == get_settings().SHOPIFY_CLIENT_ID)).with_for_update().all()
        customer_id = str((payload.get("customer") or {}).get("id", ""))
        customer_gid = "gid://shopify/Customer/" + customer_id if customer_id.isdigit() else None
        supplied_ids = payload.get("orders_to_redact", []) if topic == "customers/redact" else payload.get("orders_requested", [])
        order_ids = ["gid://shopify/Order/" + str(v) for v in supplied_ids if str(v).isdigit()]
        stored = False
        for shop in shops:
            from sqlalchemy import false
            rows = db.query(StoreOrder).filter(StoreOrder.shop_id == shop.id,
                or_(StoreOrder.customer_id == customer_gid if customer_gid else false(), StoreOrder.shopify_order_id.in_(order_ids))).all()
            stored = stored or bool(rows)
            if topic == "customers/redact":
                if customer_gid and not db.query(OrderRedactedCustomer).filter_by(shop_id=shop.id, customer_id=customer_gid).first():
                    db.add(OrderRedactedCustomer(shop_id=shop.id, customer_id=customer_gid))
                for row in rows:
                    row.customer_name = None
                    row.address_encrypted = None
                    row.address_override_encrypted = None
                    row.privacy_redacted = True
                    for line in db.query(StoreOrderLine).filter_by(order_id=row.id).all():
                        line.quote_token = None
                # Exports may contain the same customer's details; purge all temporary exports for this store.
                db.query(OrderPrivacyRequest).filter_by(shop_id=shop.id).delete(synchronize_session=False)
            elif rows:
                request_key = str(shop.id) + ":" + str((payload.get("data_request") or {}).get("id") or hashlib.sha256(await request.body()).hexdigest())
                if not db.query(OrderPrivacyRequest).filter_by(request_id=request_key).first():
                    export = [serialized_order(db, row, True) for row in rows]
                    db.add(OrderPrivacyRequest(shop_id=shop.id, request_id=request_key,
                        export_encrypted=encrypt_secret(json.dumps(export, default=str))))
        db.commit()
        return {"received": True, "customer_data_stored": stored}

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
            for model in (OrderFulfillment, OrderActivity, StoreOrderLine, StoreOrder, OrderSyncState, OrderPrivacyRequest, OrderWebhookTask, OrderRedactedCustomer, Notification, SyncLog, PlanProduct, PendingImport, Mapping, Product):
                db.query(model).filter(model.shop_id == shop.id).delete(synchronize_session=False)
            db.delete(shop)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"received": True}
