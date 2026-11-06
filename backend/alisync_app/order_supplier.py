"""Authorized AliExpress DS order adapter. No browser checkout automation."""
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from fastapi import HTTPException
from .aliexpress import _call, get_product
from .auth import get_latest_token, get_aliexpress_credentials

ORDERS_URL = "https://www.aliexpress.com/p/order/index.html"


def money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError()
        return result.quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(409, "Supplier returned an invalid price; refresh the quote")


def items(value, key):
    if isinstance(value, dict):
        value = value.get(key, [])
    return value if isinstance(value, list) else []


def call(db, method, params):
    token = get_latest_token(db)
    app_key, secret, _ = get_aliexpress_credentials(db)
    try:
        body = _call(method, params, token.access_token, app_key, secret)
    except Exception as exc:
        # Never echo request payloads, tokens or customer addresses in errors.
        raise HTTPException(502, "AliExpress response unavailable. Refresh status before retrying.") from exc
    if not isinstance(body, dict) or body.get("error_response"):
        raise HTTPException(502, "AliExpress rejected the request. Check API permissions and authorization.")
    envelope = body.get(method.replace(".", "_") + "_response", {})
    if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), dict):
        raise HTTPException(502, "AliExpress returned an unsupported response")
    if str(envelope.get("rsp_code", "200")) not in ("200", "0", ""):
        raise HTTPException(502, "AliExpress could not verify this order")
    return envelope["result"]


def safe_payment_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and not parsed.username and not parsed.password and (host == "aliexpress.com" or host.endswith(".aliexpress.com")):
        return value
    return None


def quote(db, product_id, sku_id, quantity, address, ship_from):
    raw = get_product(product_id, db, ship_to_country=address["country"])
    sku = next((s for s in raw.get("skus", []) if str(s.get("sku_id")) == str(sku_id)), None)
    if not sku or not sku.get("sku_attr"):
        raise HTTPException(409, "The exact mapped supplier variant is unavailable. Remap this product.")
    if sku.get("stock") is None or int(sku["stock"]) < quantity:
        raise HTTPException(409, "The mapped variant has insufficient or unverified stock")
    unit = money(sku.get("sale_price") if sku.get("sale_price") is not None else sku.get("price"))
    currency = sku.get("currency") or raw.get("currency")
    if not currency:
        raise HTTPException(409, "Supplier price currency is unavailable")
    request = {"country_code": address["country"], "product_id": product_id,
               "product_num": quantity, "send_goods_country_code": ship_from,
               "price": str(unit), "price_currency": currency}
    freight = call(db, "aliexpress.logistics.buyer.freight.calculate", {
        "param_aeop_freight_calculate_for_buyer_d_t_o": json.dumps(request)})
    if freight.get("success") is not True:
        raise HTTPException(409, "No verified shipping quote for this destination")
    methods = []
    for entry in items(freight.get("aeop_freight_calculate_result_for_buyer_d_t_o_list", []), "aeop_freight_calculate_result_for_buyer_dto"):
        cost = entry.get("freight") or {}
        if str(entry.get("error_code", 0)) != "0" or cost.get("currency_code") != currency or not entry.get("service_name"):
            continue
        methods.append({"code": entry["service_name"], "cost": str(money(cost.get("amount"))),
                        "delivery": str(entry.get("estimated_delivery_time") or "Not provided")})
    if not methods:
        raise HTTPException(409, "No supported shipping method returned for this destination")
    return {"product_id": str(product_id), "sku_id": str(sku_id), "sku_attr": sku["sku_attr"],
            "title": raw.get("title"), "variant": sku.get("label"), "image": sku.get("image") or raw.get("main_image"),
            "unit_price": str(unit), "stock": int(sku["stock"]), "quantity": quantity, "currency": currency, "methods": methods,
            "ship_from": ship_from, "product_url": f"https://www.aliexpress.com/item/{product_id}.html"}


def create(db, prepared, address, reference):
    # The documented endpoint returns order IDs, not a guaranteed cashier URL.
    payload = {"logistics_address": {
        "contact_person": address["name"], "full_name": address["name"], "address": address["address1"],
        "address2": address.get("address2", ""), "city": address["city"], "province": address.get("province", ""),
        "country": address["country"], "zip": address.get("zip", ""), "mobile_no": address["phone"],
        "phone_country": address["phone_country"], "locale": "en_US"},
        "product_items": [{"product_id": prepared["product_id"], "sku_attr": prepared["sku_attr"],
            "product_count": prepared["quantity"], "logistics_service_name": prepared["shipping"]["code"],
            "order_memo": reference}]}
    result = call(db, "aliexpress.trade.buy.placeorder", {
        "param_place_order_request4_open_api_d_t_o": json.dumps(payload)})
    ids = items(result.get("order_list"), "number")
    if result.get("is_success") is not True or len(ids) != 1 or not re.fullmatch(r"\d+", str(ids[0])):
        raise HTTPException(502, "Supplier creation could not be confirmed. Check AliExpress orders before any retry.")
    return {"order_id": str(ids[0]), "payment_url": safe_payment_url(result.get("payment_url") or result.get("pay_url"))}


def status(db, order_id):
    result = call(db, "aliexpress.ds.trade.order.get", {"order_id": order_id})
    raw = str(result.get("order_status") or "").strip().upper()
    logistics = str(result.get("logistics_status") or "").strip().upper()
    states = {"PLACE_ORDER_SUCCESS": "awaiting_payment", "WAIT_BUYER_PAY": "awaiting_payment",
              "IN_CANCEL": "processing", "WAIT_SELLER_SEND_GOODS": "ordered", "SELLER_PART_SEND_GOODS": "processing",
              "WAIT_BUYER_ACCEPT_GOODS": "shipped", "FUND_PROCESSING": "processing",
              "FINISH": "processing", "IN_ISSUE": "processing", "CANCELLED": "cancelled", "CANCELED": "cancelled"}
    state = states.get(raw)
    if state is None:
        raise HTTPException(409, "Supplier returned an unrecognized status; no state was changed")
    tracking = [{"number": str(t.get("logistics_no", "")), "carrier": str(t.get("logistics_service", ""))}
                for t in items(result.get("logistics_info_list"), "ae_order_logistics_info") if t.get("logistics_no")]
    # FINISH alone can also represent a closed/refunded order. It is not delivery proof.
    if logistics == "BUYER_ACCEPT_GOODS":
        state = "delivered"
    elif logistics == "SELLER_SEND_GOODS" and state != "cancelled":
        state = "shipped"
    paid = raw in {"WAIT_SELLER_SEND_GOODS", "WAIT_BUYER_ACCEPT_GOODS", "SELLER_PART_SEND_GOODS", "FUND_PROCESSING"} or state in {"shipped", "delivered"}
    amount = result.get("order_amount") or {}
    return {"status": state, "raw_status": raw, "paid": paid, "tracking": tracking,
            "total": str(money(amount["amount"])) if amount.get("amount") is not None else None,
            "currency": amount.get("currency_code"),
            "children": items(result.get("child_order_list"), "ae_child_order_info"),
            "payment_url": safe_payment_url(result.get("payment_url") or result.get("pay_url"))}
