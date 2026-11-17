"""SKU offer pricing from the latest supplier response; no percentage reapplication."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

def amount(value):
    try:
        value = Decimal(str(value))
        return value if value.is_finite() and value >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None

def sku_pricing(sku):
    original, offer = amount(sku.get("price")), amount(sku.get("sale_price"))
    final = offer if offer is not None and (original is None or offer <= original) else original
    percent = None
    if original is not None and original > 0 and final is not None:
        percent = str(((original-final)/original*100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) + "%"
    return {"original_price": str(original) if original is not None else None,
            "final_price": str(final) if final is not None else None, "discount": percent}

def product_pricing(skus, base):
    for sku in skus:
        sku.update(sku_pricing(sku))
    candidates = [s for s in skus if s["final_price"] is not None and s.get("currency")]
    if candidates and len({s["currency"] for s in candidates}) == 1:
        chosen = min(candidates, key=lambda s: Decimal(s["final_price"]))
        return {"original_price":chosen["original_price"],"sale_price":chosen["final_price"],"currency":chosen["currency"],"discount":chosen["discount"]}
    return {"original_price":base.get("original_price"),"sale_price":base.get("sale_price"),"currency":base.get("currency_code"),"discount":base.get("discount")}
