"""Store-isolated Shopify orders, reviewed supplier purchases and partial fulfillment."""
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .database import get_db, SessionLocal
from .config import get_settings
from .models import Shop, Product, Mapping, StoreOrder, StoreOrderLine, OrderActivity, OrderSyncState, OrderPrivacyRequest, OrderFulfillment, OrderWebhookTask, OrderRedactedCustomer
from .security import encrypt_secret, decrypt_secret
from .shop_context import get_current_shop, ensure_shopify_access_token
from .shopify import _graphql, get_variant_sync_state
from . import order_supplier as supplier

router = APIRouter(prefix="/app/orders", tags=["Orders"])
LINE_FIELDS = """id title variantTitle quantity currentQuantity
    product { id } variant { id }
    discountedTotalSet { shopMoney { amount currencyCode } }"""
ORDER_FIELDS = """id name createdAt updatedAt cancelledAt displayFinancialStatus displayFulfillmentStatus
    customer { id displayName } shippingAddress { name address1 address2 city province zip countryCodeV2 phone }
    currentTotalPriceSet { shopMoney { amount currencyCode } }"""
STATES = {"new", "needs_mapping", "ready_to_buy", "creating", "awaiting_payment", "ordered", "processing", "shipped", "delivered", "cancelled", "failed", "uncertain"}


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ident(value):
    return str(value or "").rsplit("/", 1)[-1]


def event(db, order, message, line=None):
    db.add(OrderActivity(shop_id=order.shop_id, order_id=order.id, line_id=line.id if line else None, message=message))


def address_of(order):
    value = order.address_override_encrypted or order.address_encrypted
    return json.loads(decrypt_secret(value)) if value else {}


def validate_address(address):
    fields = ("name", "address1", "city", "country", "phone", "phone_country")
    if any(not str(address.get(k, "")).strip() for k in fields):
        raise HTTPException(409, "Complete the recipient name, street, city, country, phone and phone country code first")
    if not re.fullmatch(r"[A-Z]{2}", address["country"]) or not re.fullmatch(r"\+?\d{1,4}", address["phone_country"]):
        raise HTTPException(400, "Use a two-letter country code and a numeric phone country code")
    if not re.fullmatch(r"[\d ()+.-]{5,30}", address["phone"]):
        raise HTTPException(400, "Enter a valid recipient phone number")


def owned_order(db, shop, order_id, lock=False):
    query = db.query(StoreOrder).filter_by(id=order_id, shop_id=shop.id, user_id=shop.user_id)
    row = (query.with_for_update() if lock else query).first()
    if not row:
        raise HTTPException(404, "Order not found")
    return row


def owned_line(db, shop, line_id):
    row = db.query(StoreOrderLine).filter_by(id=line_id, shop_id=shop.id, user_id=shop.user_id).first()
    if not row:
        raise HTTPException(404, "Order item not found")
    return row, owned_order(db, shop, row.order_id)


def fetch_order(shop, gid):
    after, value, lines = None, None, []
    while True:
        data = _graphql(shop, "query($id:ID!,$after:String){order(id:$id){" + ORDER_FIELDS +
            " lineItems(first:100,after:$after){nodes{" + LINE_FIELDS + "}pageInfo{hasNextPage endCursor}}}}",
            {"id": gid, "after": after})
        value = data.get("order")
        if not value:
            raise HTTPException(404, "Shopify order unavailable. Check order access permissions.")
        connection = value["lineItems"]
        lines.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            value["lines"] = lines
            return value
        after = connection["pageInfo"].get("endCursor")
        if not after:
            raise HTTPException(502, "Shopify order pagination incomplete")


def ingest(db, shop, data):
    # Serialize imports for a store; duplicate webhooks update the same unique order.
    db.query(Shop).filter_by(id=shop.id).with_for_update().one()
    row = db.query(StoreOrder).filter_by(shop_id=shop.id, shopify_order_id=data["id"]).first()
    updated = timestamp(data["updatedAt"])
    if row and aware(row.source_updated_at) > updated:
        return row
    new = row is None
    if new:
        row = StoreOrder(user_id=shop.user_id, shop_id=shop.id, shopify_order_id=data["id"])
    row.name = data["name"]
    customer = data.get("customer") or {}
    if customer.get("id") and db.query(OrderRedactedCustomer).filter_by(shop_id=shop.id, customer_id=customer["id"]).first():
        row.privacy_redacted = True
    if not row.privacy_redacted:
        row.customer_id = customer.get("id")
        row.customer_name = customer.get("displayName") or (data.get("shippingAddress") or {}).get("name") or "Guest"
        address = dict(data.get("shippingAddress") or {})
        address["country"] = address.pop("countryCodeV2", "") or ""
        try:
            import phonenumbers
            phone = phonenumbers.parse(address.get("phone") or "", address["country"] or None)
            if phonenumbers.is_valid_number(phone):
                address["phone_country"] = "+" + str(phone.country_code)
                address["phone"] = str(phone.national_number)
        except Exception:
            pass
        # Preserve incomplete values for merchant correction; never invent phone data.
        row.address_encrypted = encrypt_secret(json.dumps(address))
    row.currency = data["currentTotalPriceSet"]["shopMoney"]["currencyCode"]
    row.total = supplier.money(data["currentTotalPriceSet"]["shopMoney"]["amount"])
    row.payment_status = data.get("displayFinancialStatus")
    row.fulfillment_status = data.get("displayFulfillmentStatus")
    row.cancelled = bool(data.get("cancelledAt"))
    row.placed_at = timestamp(data["createdAt"])
    row.source_updated_at = updated
    row.synced_at = now()
    if new:
        db.add(row)
    db.flush()
    existing = {r.shopify_line_id: r for r in db.query(StoreOrderLine).filter_by(order_id=row.id).all()}
    seen = set()
    for item in data["lines"]:
        seen.add(item["id"])
        line = existing.get(item["id"])
        if line is None:
            line = StoreOrderLine(user_id=shop.user_id, shop_id=shop.id, order_id=row.id, shopify_line_id=item["id"])
            db.add(line)
        quantity = int(item.get("currentQuantity", item["quantity"]))
        if line.quantity is not None and quantity != line.quantity:
            line.quote_token = None
            if line.supplier_order_id or line.status in ("creating", "uncertain"):
                line.error = "Shopify quantity changed after purchase preparation. Review supplier order before fulfillment."
        line.quantity = quantity
        line.product_id = ident((item.get("product") or {}).get("id"))
        line.variant_id = ident((item.get("variant") or {}).get("id"))
        line.title, line.variant_title = item.get("title"), item.get("variantTitle")
        original = max(int(item["quantity"]), 1)
        line.revenue = supplier.money(item["discountedTotalSet"]["shopMoney"]["amount"]) * Decimal(quantity) / Decimal(original)
        if (row.cancelled or quantity == 0) and not line.supplier_order_id and line.status not in ("creating", "uncertain"):
            line.status, line.quote_token = "cancelled", None
    for key, line in existing.items():
        if key not in seen:
            line.quantity, line.quote_token = 0, None
            if not line.supplier_order_id and line.status not in ("creating", "uncertain"):
                line.status = "cancelled"
    if new:
        event(db, row, "Shopify order received")
    db.commit()
    return row


def sync_error_message(exc):
    detail = str(getattr(exc, "detail", ""))
    if "read_customers" in detail and ("Access denied" in detail or "permission" in detail):
        return ("Shopify is missing read_customers permission. Add read_customers to the Shopify app version, "
                "release it, approve the updated permissions for this store, then reconnect Shopify and sync again.")
    if "ACCESS_DENIED" in detail or "Access denied" in detail:
        return "Shopify denied order/customer access. Review app scopes and protected customer data access, approve permissions and reconnect Shopify."
    return "Shopify order sync failed. Check order scopes, customer data access and connection."


def sync_shop(db, shop):
    ensure_shopify_access_token(shop, db)
    state = db.get(OrderSyncState, shop.id)
    if not state:
        state = OrderSyncState(shop_id=shop.id)
        db.add(state); db.commit()
    if state.retry_after and aware(state.retry_after) > now():
        raise HTTPException(429, (state.error or "Order sync failed.") + " Retry after " + aware(state.retry_after).strftime("%H:%M:%S UTC") + ".")
    started = aware(state.window_end) or now()
    state.window_end = started
    db.commit()
    since = aware(state.checked_at) - timedelta(minutes=10) if state.checked_at else started - timedelta(days=60)
    after, count = state.cursor, 0
    try:
        while True:
            data = _graphql(shop, """query($query:String!,$after:String){orders(first:50,after:$after,query:$query,sortKey:UPDATED_AT){
                nodes{id} pageInfo{hasNextPage endCursor}}}""", {"query": f"updated_at:>={since.isoformat()} updated_at:<={started.isoformat()}", "after": after})
            connection = data["orders"]
            for item in connection["nodes"]:
                ingest(db, shop, fetch_order(shop, item["id"]))
                count += 1
            if not connection["pageInfo"]["hasNextPage"]:
                break
            after = connection["pageInfo"].get("endCursor")
            if not after:
                raise HTTPException(502, "Shopify pagination did not return a cursor")
            state.cursor = after
            db.commit()
            if count >= 1000:
                return count
        state.cursor, state.window_end = None, None
        state.checked_at, state.error, state.retry_after = started, None, None
        db.commit()
        return count
    except Exception as exc:
        db.rollback()
        state = db.get(OrderSyncState, shop.id)
        state.error = sync_error_message(exc)
        state.retry_after = now() + timedelta(minutes=5)
        db.commit()
        raise HTTPException(502, state.error)


def mapping_for(db, shop, line):
    candidates = []
    for model in (Product, Mapping):
        candidates.extend(db.query(model).filter_by(shop_id=shop.id, user_id=shop.user_id, shopify_product_id=line.product_id).all())
    ids = {r.aliexpress_id for r in candidates}
    if len(ids) != 1:
        raise HTTPException(409, "Map Supplier Product: this Shopify product needs one unambiguous supplier mapping")
    product_id = str(next(iter(ids)))
    if not product_id.isdigit():
        raise HTTPException(409, "Supplier mapping has an invalid product ID")
    state = get_variant_sync_state(shop, line.product_id)
    variant = state.get(int(line.variant_id)) if line.variant_id.isdigit() else None
    variant = variant or state.get(line.variant_id)
    sku_id = (variant or {}).get("ae_sku_id")
    if not sku_id:
        raise HTTPException(409, "Map Supplier Product: select the exact AliExpress variant before purchase")
    return product_id, str(sku_id)


def purchase_allowed(order, line):
    if order.privacy_redacted:
        raise HTTPException(409, "Customer information has been redacted")
    if order.cancelled or line.quantity <= 0:
        raise HTTPException(409, "Cancelled or removed Shopify items cannot be purchased")
    if order.payment_status not in ("PAID", "PARTIALLY_PAID"):
        raise HTTPException(409, "Confirm the Shopify customer's payment before purchasing")
    if order.fulfillment_status == "FULFILLED":
        raise HTTPException(409, "This Shopify order is already fulfilled")


def make_quote(db, shop, line, order, ship_from, shipping_code=None):
    purchase_allowed(order, line)
    address = address_of(order)
    validate_address(address)
    if not re.fullmatch(r"[A-Z]{2}", ship_from):
        raise HTTPException(400, "Choose the supplier ship-from country")
    product, sku = mapping_for(db, shop, line)
    result = supplier.quote(db, product, sku, line.quantity, address, ship_from)
    selected = next((m for m in result["methods"] if m["code"] == shipping_code), None)
    if shipping_code and not selected:
        raise HTTPException(409, "Selected shipping service is no longer available")
    result["shipping"] = selected or min(result["methods"], key=lambda m: Decimal(m["cost"]))
    result["cost"] = str(Decimal(result["unit_price"]) * line.quantity + Decimal(result["shipping"]["cost"]))
    result["address_hash"] = hashlib.sha256(json.dumps(address, sort_keys=True).encode()).hexdigest()
    result["profit"] = str(Decimal(line.revenue) - Decimal(result["cost"])) if result["currency"] == order.currency else None
    result["margin"] = round(float(Decimal(result["profit"]) / Decimal(line.revenue) * 100), 2) if result["profit"] is not None and line.revenue else None
    return result


def save_quote(db, order, line, quote, from_purchase=False):
    previous = (line.quote or {}).get("cost")
    quote["price_increased"] = bool(previous is not None and Decimal(quote["cost"]) > Decimal(previous))
    eligible = ["creating"] if from_purchase else ["new", "ready_to_buy", "failed", "needs_mapping"]
    changed = db.query(StoreOrderLine).filter(StoreOrderLine.id == line.id,
        StoreOrderLine.status.in_(eligible), StoreOrderLine.supplier_order_id.is_(None)).update({
            "quote": quote, "quote_token": secrets.token_hex(24), "quote_at": now(),
            "supplier_product_id": quote["product_id"], "supplier_sku_id": quote["sku_id"],
            "status": "ready_to_buy", "error": None}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        raise HTTPException(409, "Purchase state changed while quoting. Refresh this order.")
    event(db, order, "Supplier variant, availability and shipping quote verified", line)
    db.commit()
    db.refresh(line)


def quote_signature(quote):
    return {k: quote.get(k) for k in ("product_id", "sku_id", "sku_attr", "unit_price", "quantity", "currency", "shipping", "address_hash", "cost", "ship_from")}


def refresh_supplier(db, shop, line, order):
    if not line.supplier_order_id:
        return
    db.info.update(user_id=shop.user_id, shop_id=shop.id)
    result = supplier.status(db, line.supplier_order_id)
    old = line.status
    old_paid, old_tracking = line.payment_confirmed, line.tracking
    # Never undo a confirmed delivery based on a delayed supplier response.
    if line.status != "delivered" or result["status"] == "delivered":
        line.status = result["status"]
    line.supplier_raw_status = result["raw_status"]
    line.payment_confirmed = line.payment_confirmed or result["paid"]
    line.tracking = result["tracking"] or line.tracking
    line.payment_url = result["payment_url"] or line.payment_url
    line.refreshed_at = now()
    if result.get("total") is not None and result.get("currency"):
        line.supplier_total, line.supplier_currency = Decimal(result["total"]), result["currency"]
    if not old_paid and line.payment_confirmed:
        event(db, order, "AliExpress payment confirmed by supplier API", line)
    if line.tracking and old_tracking != line.tracking:
        event(db, order, "Supplier tracking information received", line)
    if old != line.status:
        event(db, order, "Supplier status verified: " + line.status.replace("_", " "), line)
    db.commit()


def serialized_line(line):
    return {"id": line.id, "title": line.title, "variant": line.variant_title, "quantity": line.quantity,
            "revenue": str(line.revenue), "status": line.status, "product_id": line.supplier_product_id,
            "sku_id": line.supplier_sku_id, "quote": line.quote, "quote_token": line.quote_token,
            "supplier_order_id": line.supplier_order_id, "payment_confirmed": line.payment_confirmed,
            "supplier_total": str(line.supplier_total) if line.supplier_total is not None else None,
            "supplier_currency": line.supplier_currency,
            "payment_url": supplier.safe_payment_url(line.payment_url), "orders_url": supplier.ORDERS_URL,
            "tracking": line.tracking or [], "fulfillment_id": line.fulfillment_id,
            "fulfillment_state": line.fulfillment_state, "error": line.error,
            "supplier_created_at": line.supplier_created_at, "refreshed_at": line.refreshed_at}


def serialized_order(db, row, detail=False):
    lines = db.query(StoreOrderLine).filter_by(order_id=row.id).order_by(StoreOrderLine.id).all()
    states = list(dict.fromkeys(line.status for line in lines))
    result = {"id": row.id, "name": row.name, "customer": row.customer_name or "Redacted",
              "total": str(row.total), "currency": row.currency, "payment": row.payment_status,
              "fulfillment": row.fulfillment_status, "cancelled": row.cancelled, "placed_at": row.placed_at,
              "statuses": states, "items": sum(line.quantity for line in lines), "synced_at": row.synced_at}
    if detail:
        # Listing links need only the saved import mapping, not a live quote or SKU lookup.
        mapped_ids = {}
        product_ids = {line.product_id for line in lines if line.product_id}
        for model in (Product, Mapping):
            for mapped in db.query(model).filter(model.shop_id == row.shop_id,
                    model.user_id == row.user_id, model.shopify_product_id.in_(product_ids)).all():
                mapped_ids.setdefault(str(mapped.shopify_product_id), set()).add(str(mapped.aliexpress_id))
        serialized_lines = []
        for line in lines:
            item = serialized_line(line)
            candidates = mapped_ids.get(str(line.product_id), set())
            supplier_id = line.supplier_product_id or (next(iter(candidates)) if len(candidates) == 1 else None)
            item["supplier_product_url"] = (
                f"https://www.aliexpress.com/item/{supplier_id}.html"
                if supplier_id and str(supplier_id).isdigit() else None)
            serialized_lines.append(item)
        result.update(address=address_of(row), address_overridden=bool(row.address_override_encrypted),
                      lines=serialized_lines, privacy_redacted=row.privacy_redacted)
        result["history"] = [{"message": a.message, "at": a.created_at} for a in db.query(OrderActivity).filter_by(order_id=row.id).order_by(OrderActivity.id.desc()).limit(100)]
    return result


@router.get("")
def list_orders(q: str = "", status: str = "", payment: str = "", fulfillment: str = "", date_from: str = "", date_to: str = "",
                page: int = Query(1, ge=1), db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    query = db.query(StoreOrder).filter_by(shop_id=shop.id, user_id=shop.user_id)
    if q:
        pattern = "%" + q[:150].replace("%", "\\%").replace("_", "\\_") + "%"
        matching = db.query(StoreOrderLine.order_id).filter(StoreOrderLine.shop_id == shop.id, StoreOrderLine.title.ilike(pattern, escape="\\"))
        query = query.filter(or_(StoreOrder.name.ilike(pattern, escape="\\"), StoreOrder.customer_name.ilike(pattern, escape="\\"), StoreOrder.id.in_(matching)))
    if status:
        if status not in STATES:
            raise HTTPException(400, "Unknown supplier status")
        query = query.filter(StoreOrder.id.in_(db.query(StoreOrderLine.order_id).filter_by(shop_id=shop.id, status=status)))
    if payment:
        query = query.filter(StoreOrder.payment_status == payment)
    if fulfillment:
        query = query.filter(StoreOrder.fulfillment_status == fulfillment)
    try:
        if date_from:
            query = query.filter(StoreOrder.placed_at >= timestamp(date_from + "T00:00:00+00:00"))
        if date_to:
            query = query.filter(StoreOrder.placed_at < timestamp(date_to + "T00:00:00+00:00") + timedelta(days=1))
    except ValueError:
        raise HTTPException(400, "Use YYYY-MM-DD dates")
    total = query.count()
    rows = query.order_by(StoreOrder.placed_at.desc(), StoreOrder.id.desc()).offset((page - 1) * 25).limit(25).all()
    base = db.query(StoreOrderLine).filter_by(shop_id=shop.id, user_id=shop.user_id)
    counts = {"to_buy": base.filter(StoreOrderLine.status.in_(["new", "needs_mapping", "ready_to_buy"])).count(),
              "awaiting_payment": base.filter_by(status="awaiting_payment").count(),
              "in_transit": base.filter_by(status="shipped").count(), "attention": base.filter(StoreOrderLine.status.in_(["failed", "uncertain", "needs_mapping"])).count()}
    state = db.get(OrderSyncState, shop.id)
    return {"orders": [serialized_order(db, row) for row in rows], "total": total, "page": page, "counts": counts,
            "last_sync": state.checked_at if state else None, "sync_error": state.error if state else None,
            "purchase_enabled": get_settings().ALIEXPRESS_ORDER_CREATE_ENABLED, "shop_domain": shop.shop_domain}


@router.post("/sync")
def sync_orders(db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    return {"synced": sync_shop(db, shop)}


@router.get("/privacy-requests")
def privacy_requests(db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    return [{"id": r.id, "request_id": r.request_id, "created_at": r.created_at} for r in db.query(OrderPrivacyRequest).filter_by(shop_id=shop.id).all()]


@router.get("/privacy-requests/{request_id}")
def privacy_export(request_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    row = db.query(OrderPrivacyRequest).filter_by(id=request_id, shop_id=shop.id).first()
    if not row:
        raise HTTPException(404, "Privacy request not found")
    return json.loads(decrypt_secret(row.export_encrypted))


@router.get("/report")
def profit_report(db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    rows = db.query(StoreOrderLine, StoreOrder).join(StoreOrder, StoreOrder.id == StoreOrderLine.order_id).filter(
        StoreOrder.shop_id == shop.id, StoreOrder.user_id == shop.user_id).order_by(StoreOrder.id.desc()).limit(5000).all()
    return {"rows": [{"order": order.name, "item": line.title, "quantity": line.quantity,
        "revenue": str(line.revenue), "currency": order.currency, "supplier_currency": (line.quote or {}).get("currency"),
        "estimated_supplier_cost": (line.quote or {}).get("cost"), "estimated_profit": (line.quote or {}).get("profit"),
        "status": line.status} for line, order in rows], "limit": 5000}


@router.get("/{order_id}")
def order_detail(order_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    return serialized_order(db, owned_order(db, shop, order_id), True)


@router.put("/{order_id}/address")
def edit_address(order_id: int, payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    row = owned_order(db, shop, order_id, True)
    if row.privacy_redacted:
        raise HTTPException(409, "Customer details were redacted")
    busy = db.query(StoreOrderLine).filter(StoreOrderLine.order_id == row.id, StoreOrderLine.status.in_(["creating", "uncertain"])).first()
    if busy:
        raise HTTPException(409, "Resolve the pending supplier purchase before editing the address")
    allowed = ("name", "address1", "address2", "city", "province", "zip", "country", "phone", "phone_country")
    address = {key: str(payload.get(key) or "").strip()[:255] for key in allowed}
    address["country"] = address["country"].upper()
    validate_address(address)
    row.address_override_encrypted = encrypt_secret(json.dumps(address))
    for line in db.query(StoreOrderLine).filter_by(order_id=row.id, supplier_order_id=None).all():
        line.quote_token = None
        if line.status == "ready_to_buy":
            line.status = "new"
    event(db, row, "Shipping details corrected for future supplier purchases; Shopify order unchanged")
    db.commit()
    return {"message": "Saved for new supplier purchases only. Existing supplier orders and Shopify are unchanged."}


@router.post("/lines/{line_id}/quote")
def prepare_line(line_id: int, payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    line, order = owned_line(db, shop, line_id)
    if line.supplier_order_id or line.status in ("creating", "uncertain"):
        raise HTTPException(409, "This item already has a purchase or an unresolved purchase attempt")
    ingest(db, shop, fetch_order(shop, order.shopify_order_id))
    try:
        prepared = make_quote(db, shop, line, order, str(payload.get("ship_from", "")).upper(), payload.get("shipping_code"))
        save_quote(db, order, line, prepared)
    except HTTPException as exc:
        db.query(StoreOrderLine).filter(StoreOrderLine.id == line.id,
            StoreOrderLine.status.in_(["new", "ready_to_buy", "failed", "needs_mapping"])).update({
                "error": str(exc.detail), "status": "needs_mapping" if "Map Supplier Product" in str(exc.detail) else "failed",
                "quote_token": None}, synchronize_session=False)
        db.commit()
        raise
    return serialized_line(line)


@router.post("/lines/{line_id}/purchase")
def purchase_line(line_id: int, payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    if not get_settings().ALIEXPRESS_ORDER_CREATE_ENABLED:
        raise HTTPException(409, "Supplier checkout is not enabled yet. The store owner must complete the authorized AliExpress order API verification.")
    line, order = owned_line(db, shop, line_id)
    if line.supplier_order_id:
        return serialized_line(line)
    if payload.get("confirmed") is not True or not payload.get("quote_token"):
        raise HTTPException(400, "Review and confirm the purchase summary first")
    if not line.quote_at or aware(line.quote_at) < now() - timedelta(minutes=10):
        raise HTTPException(409, "Quote expired. Refresh supplier prices and shipping.")
    previous = dict(line.quote or {})
    changed = db.query(StoreOrderLine).filter_by(id=line.id, status="ready_to_buy", quote_token=payload["quote_token"], supplier_order_id=None).update({"status": "creating"}, synchronize_session=False)
    db.commit()
    if changed != 1:
        raise HTTPException(409, "Purchase already started or quote changed. Refresh status; do not create another order.")
    db.refresh(line)
    try:
        ingest(db, shop, fetch_order(shop, order.shopify_order_id))
        latest = make_quote(db, shop, line, order, previous["ship_from"], previous["shipping"]["code"])
        if quote_signature(latest) != quote_signature(previous):
            save_quote(db, order, line, latest, from_purchase=True)
            raise HTTPException(409, "Price, variant, address or shipping changed. Review the refreshed summary and confirm again.")
    except Exception:
        if line.status == "creating":
            line.status, line.quote_token = "failed", None
            db.commit()
        raise
    # Persist the claim before the non-idempotent supplier call. Unknown outcomes
    # never reset to Ready; the merchant must reconcile the existing supplier order.
    try:
        result = supplier.create(db, latest, address_of(order), f"AliSync item {line.id}")
        line.supplier_order_id = result["order_id"]
        line.payment_url = result["payment_url"]
        line.supplier_created_at = now()
        line.status, line.error, line.quote_token = "awaiting_payment", None, None
        event(db, order, "AliExpress order created; payment is not yet confirmed", line)
        db.commit()
    except Exception as exc:
        db.rollback()
        line, order = owned_line(db, shop, line_id)
        line.status, line.quote_token = "uncertain", None
        line.error = "Supplier response uncertain. Check AliExpress orders and attach the existing order ID; do not purchase again."
        event(db, order, "Supplier creation requires reconciliation; automatic retry blocked", line)
        db.commit()
        raise HTTPException(502, line.error) from exc
    return serialized_line(line)


@router.post("/lines/{line_id}/attach")
def attach_supplier_order(line_id: int, payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    line, order = owned_line(db, shop, line_id)
    order_id = str(payload.get("supplier_order_id", ""))
    if not re.fullmatch(r"\d{5,30}", order_id) or payload.get("confirmed") is not True:
        raise HTTPException(400, "Enter an AliExpress order ID and confirm its variant and recipient match this item")
    if not line.supplier_product_id:
        raise HTTPException(409, "Verify the supplier mapping with a quote first")
    if line.status == "creating":
        raise HTTPException(409, "Purchase may still be in progress. Refresh before linking an existing order.")
    if line.supplier_order_id and line.supplier_order_id != order_id:
        raise HTTPException(409, "This item is already linked to another supplier order")
    existing = db.query(StoreOrderLine).filter(StoreOrderLine.user_id == shop.user_id, StoreOrderLine.supplier_order_id == order_id, StoreOrderLine.id != line.id).first()
    if existing:
        raise HTTPException(409, "This supplier order is already linked to another item")
    db.info.update(user_id=shop.user_id, shop_id=shop.id)
    verified = supplier.status(db, order_id)
    children = verified["children"]
    if len(children) != 1 or str(children[0].get("product_id")) != line.supplier_product_id or int(children[0].get("product_count", 0)) != (line.quote or {}).get("quantity", line.quantity):
        raise HTTPException(409, "Supplier order product and quantity do not match this item")
    db.query(Shop).filter_by(id=shop.id).with_for_update().one()
    line.supplier_order_id, line.quote_token = order_id, None
    line.status, line.payment_confirmed = verified["status"], verified["paid"]
    line.tracking, line.payment_url, line.error = verified["tracking"], verified["payment_url"], None
    event(db, order, "Existing AliExpress order linked; merchant confirmed recipient and exact variant", line)
    db.commit()
    return serialized_line(line)


@router.post("/{order_id}/refresh")
def refresh_order(order_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    order = owned_order(db, shop, order_id)
    ingest(db, shop, fetch_order(shop, order.shopify_order_id))
    errors = []
    for line in db.query(StoreOrderLine).filter_by(order_id=order.id).all():
        if line.status == "creating" and line.quote_at and aware(line.quote_at) < now() - timedelta(minutes=15):
            line.status, line.error = "uncertain", "Purchase response not recorded. Reconcile with AliExpress before continuing."
            db.commit()
        try:
            refresh_supplier(db, shop, line, order)
        except HTTPException as exc:
            errors.append(str(exc.detail))
    return {"order": serialized_order(db, order, True), "errors": errors}


def fulfill_line(db, shop, line, order, notify_customer=True):
    if line.fulfillment_state == "complete":
        return
    ingest(db, shop, fetch_order(shop, order.shopify_order_id))
    if order.cancelled or line.quantity <= 0:
        raise HTTPException(409, "Shopify order was cancelled or the item was removed")
    refresh_supplier(db, shop, line, order)
    if line.status not in ("shipped", "delivered") or not line.payment_confirmed or not line.tracking:
        raise HTTPException(409, "Verified supplier payment, shipment and tracking are required before fulfillment")
    if not line.quote or line.quantity != line.quote.get("quantity"):
        raise HTTPException(409, "Shopify quantity differs from the supplier purchase. Review the order in Shopify.")
    attempts = db.query(OrderFulfillment).filter_by(line_id=line.id).all()
    if any(a.state in ("sending", "uncertain") for a in attempts):
        raise HTTPException(409, "A fulfillment response is uncertain. Check Shopify before another fulfillment attempt.")
    if not attempts:
        after, allocations, remaining = None, [], line.quantity
        while True:
            data = _graphql(shop, """query($id:ID!,$after:String){order(id:$id){fulfillmentOrders(first:50,after:$after){
                nodes{id status lineItems(first:250){nodes{id remainingQuantity lineItem{id}}pageInfo{hasNextPage}}}
                pageInfo{hasNextPage endCursor}}}}""", {"id": order.shopify_order_id, "after": after})
            connection = data["order"]["fulfillmentOrders"]
            for fo in connection["nodes"]:
                if fo["status"] not in ("OPEN", "IN_PROGRESS"):
                    continue
                if fo["lineItems"]["pageInfo"]["hasNextPage"]:
                    raise HTTPException(409, "Large fulfillment order requires review in Shopify")
                for item in fo["lineItems"]["nodes"]:
                    if item["lineItem"]["id"] == line.shopify_line_id and remaining > 0:
                        quantity = min(int(item["remainingQuantity"]), remaining)
                        if quantity:
                            allocations.append((fo["id"], item["id"], quantity)); remaining -= quantity
            if not connection["pageInfo"]["hasNextPage"]:
                break
            after = connection["pageInfo"].get("endCursor")
            if not after:
                raise HTTPException(502, "Incomplete Shopify fulfillment pagination")
        if remaining:
            raise HTTPException(409, "Some quantities are already fulfilled or unavailable. Review Shopify fulfillment orders.")
        # Serialize initial plan persistence for concurrent button/background requests.
        db.query(Shop).filter_by(id=shop.id).with_for_update().one()
        attempts = db.query(OrderFulfillment).filter_by(line_id=line.id).all()
        if not attempts:
            for fo_id, item_id, quantity in allocations:
                db.add(OrderFulfillment(shop_id=shop.id, order_id=order.id, line_id=line.id,
                    fulfillment_order_id=fo_id, fulfillment_line_id=item_id, quantity=quantity))
            db.commit()
            attempts = db.query(OrderFulfillment).filter_by(line_id=line.id).all()
    for attempt in attempts:
        if attempt.state == "complete":
            continue
        claimed = db.query(OrderFulfillment).filter_by(id=attempt.id, state="planned").update({"state": "sending"}, synchronize_session=False)
        db.commit()
        if claimed != 1:
            raise HTTPException(409, "Fulfillment is already being processed")
        try:
            tracking = line.tracking or []
            tracking_info = {"numbers": [t["number"] for t in tracking]}
            if len({t.get("carrier") for t in tracking}) == 1:
                tracking_info["company"] = tracking[0].get("carrier")
            result = _graphql(shop, """mutation($fulfillment:FulfillmentInput!){fulfillmentCreate(fulfillment:$fulfillment){
                fulfillment{id} userErrors{field message}}}""", {"fulfillment": {
                    "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": attempt.fulfillment_order_id,
                        "fulfillmentOrderLineItems": [{"id": attempt.fulfillment_line_id, "quantity": attempt.quantity}]}],
                    "trackingInfo": tracking_info, "notifyCustomer": bool(notify_customer)}})["fulfillmentCreate"]
            if result.get("userErrors"):
                attempt.state = "planned"
                db.commit()
                raise HTTPException(409, "Shopify rejected fulfillment. Check fulfillment permissions and the assigned location.")
            if not result.get("fulfillment", {}).get("id"):
                raise RuntimeError("Missing fulfillment confirmation")
            attempt.state, attempt.fulfillment_id = "complete", result["fulfillment"]["id"]
            line.fulfillment_id = attempt.fulfillment_id
            event(db, order, f"Shopify tracking synchronized for {attempt.quantity} unit(s)", line)
            db.commit()
        except HTTPException as exc:
            if exc.status_code == 409:
                raise
            attempt.state, line.fulfillment_state = "uncertain", "uncertain"
            db.commit()
            raise
        except Exception as exc:
            db.rollback()
            attempt = db.get(OrderFulfillment, attempt.id)
            attempt.state, line.fulfillment_state = "uncertain", "uncertain"
            db.commit()
            raise HTTPException(502, "Shopify fulfillment response uncertain. Check Shopify before retrying.") from exc
    line.fulfillment_state = "complete"
    db.commit()
    ingest(db, shop, fetch_order(shop, order.shopify_order_id))


@router.post("/lines/{line_id}/fulfill")
def fulfillment(line_id: int, payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    if payload.get("confirmed") is not True:
        raise HTTPException(400, "Confirm tracking and customer notification before fulfillment")
    line, order = owned_line(db, shop, line_id)
    fulfill_line(db, shop, line, order, payload.get("notify_customer", True))
    return serialized_line(line)


def sync_orders_background():
    if not get_settings().ORDER_SYNC_ENABLED:
        return
    with SessionLocal() as db:
        shops = db.query(Shop).filter_by(is_active=True).all()
        for shop in shops:
            if not shop.access_token:
                continue
            db.info.update(user_id=shop.user_id, shop_id=shop.id)
            try:
                sync_shop(db, shop)
                lines = db.query(StoreOrderLine).filter(StoreOrderLine.shop_id == shop.id,
                    StoreOrderLine.supplier_order_id.isnot(None),
                    StoreOrderLine.status.notin_(["delivered", "cancelled"])).order_by(StoreOrderLine.refreshed_at.asc()).limit(50).all()
                for line in lines:
                    order = db.get(StoreOrder, line.order_id)
                    if order.privacy_redacted:
                        continue
                    refresh_supplier(db, shop, line, order)
                    if get_settings().ORDER_AUTO_FULFILL and line.status in ("shipped", "delivered") and line.fulfillment_state != "complete":
                        fulfill_line(db, shop, line, order)
            except Exception as exc:
                db.rollback()
                state = db.get(OrderSyncState, shop.id)
                if state:
                    state.retry_after = now() + timedelta(minutes=5)
                    if not isinstance(exc, HTTPException) or exc.status_code != 429:
                        state.error = sync_error_message(exc)
                    db.commit()
            # Customer data exports are temporary merchant downloads, not permanent copies.
        db.query(OrderPrivacyRequest).filter(OrderPrivacyRequest.created_at < now() - timedelta(days=30)).delete(synchronize_session=False)
        db.commit()


def process_order_webhooks():
    with SessionLocal() as db:
        tasks = db.query(OrderWebhookTask).filter(or_(OrderWebhookTask.retry_at.is_(None), OrderWebhookTask.retry_at <= now())).order_by(OrderWebhookTask.id).limit(50).all()
        for task in tasks:
            shop = db.get(Shop, task.shop_id)
            if not shop or not shop.is_active:
                db.delete(task); db.commit(); continue
            try:
                db.info.update(user_id=shop.user_id, shop_id=shop.id)
                ensure_shopify_access_token(shop, db)
                ingest(db, shop, fetch_order(shop, task.order_gid))
                db.delete(task); db.commit()
            except Exception:
                db.rollback()
                task.attempts += 1
                task.retry_at = now() + timedelta(minutes=min(60, 2 ** min(task.attempts, 6)))
                db.commit()
