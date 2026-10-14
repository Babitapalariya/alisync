"""Replace supplier variants without overwriting merchant product content."""
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException
from . import shopify


def replace_supplier_variants(shop, product_id, supplier, supplier_id):
    skus = supplier.get("skus") or []
    if not skus:
        raise HTTPException(400, "Replacement listing has no variants")
    options, values = shopify.build_supplier_options(skus)
    variants, seen = [], set()
    for sku, row in zip(skus, values):
        sid = str(sku.get("sku_id") or "")
        try:
            raw_price = sku.get("sale_price")
            price = Decimal(str(sku.get("price") if raw_price is None else raw_price))
            stock = Decimal(str(sku.get("stock")))
            if not sid or sid in seen or not price.is_finite() or price < 0:
                raise ValueError()
            if not stock.is_finite() or stock < 0 or stock != stock.to_integral_value() or stock > 2147483647:
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            raise HTTPException(400, "Replacement variants need unique SKU IDs, valid prices and whole-number stock")
        seen.add(sid)
        variants.append({
            "price": str(price.quantize(Decimal("0.01"))),
            "optionValues": [{"optionName": option["name"], "name": value} for option, value in zip(options, row)],
            "inventoryItem": {"tracked": True, "sku": sid},
            "inventoryPolicy": "DENY",
            "metafields": [{"namespace": "aliexpress", "key": "sku_id", "type": "single_line_text_field", "value": sid}],
            "inventoryQuantities": [{"name": "available", "quantity": int(stock)}],
        })
    locations = shopify._shopify_request("GET", f"{shopify._base(shop.shop_domain)}/locations.json", headers=shopify._h(shop.access_token), timeout=20)
    locations.raise_for_status()
    active = [loc for loc in locations.json().get("locations", []) if loc.get("active", True) and not loc.get("legacy", False)]
    if not active:
        raise HTTPException(409, "No active Shopify stock location; remap stopped")
    for variant in variants:
        variant["inventoryQuantities"][0]["locationId"] = f"gid://shopify/Location/{active[0]['id']}"
    gid = f"gid://shopify/Product/{product_id}"
    # Supplying only options and variants preserves title, description and other product content.
    payload = {"id": gid, "productOptions": [
        {"name": option["name"], "position": i, "values": [{"name": value} for value in option["values"]]}
        for i, option in enumerate(options, 1)], "variants": variants}
    query = """mutation RemapVariants($input: ProductSetInput!) {
      productSet(input: $input, synchronous: true) {
        product { id } userErrors { field message }
      }
    }"""
    # Never blindly retry a timeout/5xx: the replacement may already have completed.
    response = shopify._shopify_request("POST", shopify._graphql_url(shop.shop_domain),
        json={"query": query, "variables": {"input": payload}}, headers=shopify._h(shop.access_token),
        timeout=120)
    response.raise_for_status()
    result = response.json()
    changed = (result.get("data") or {}).get("productSet") or {}
    errors = result.get("errors") or changed.get("userErrors")
    if errors or (changed.get("product") or {}).get("id") != gid:
        raise HTTPException(502, f"Shopify variant replacement failed: {errors or 'missing product confirmation'}")
    shopify._invalidate_lock_cache(shop, str(product_id))
    # Product metafields use a separate mutation to preserve unrelated existing metafields.
    warnings = []
    try:
        result = shopify._graphql(shop, """mutation RemapSupplier($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) { userErrors { field message } }
        }""", {"metafields": [{"ownerId": gid, "namespace": "aliexpress", "key": "product_id",
                               "type": "single_line_text_field", "value": str(supplier_id)}]})
        if "metafieldsSet" not in result or result["metafieldsSet"].get("userErrors"):
            raise ValueError(str(result))
    except Exception as exc:
        warnings.append("Variants replaced, but supplier product link update failed: " + str(getattr(exc, "detail", exc)))
    metafield_updated = not warnings
    images_updated = True
    image_result = {"attached": 0, "remaining": 0}
    resolved_skus = resolve_supplier_images(skus)
    if resolved_skus:
        try:
            image_result = sync_replacement_images(shop, str(product_id), resolved_skus)
            images_updated = image_result.get("remaining", 0) == 0
            if not images_updated:
                warnings.append("Variants replaced, but some SKU images are missing. Retry Sync images in the edit form.")
        except Exception as exc:
            images_updated = False
            warnings.append("Variants replaced, but SKU image sync failed: " + str(getattr(exc, "detail", exc)))
    return {"shopify_price_updated": True, "shopify_inv_updated": True,
            "shopify_variants_replaced": True, "shopify_metafield_updated": metafield_updated,
            "shopify_images_updated": images_updated, "sku_images": image_result,
            "variant_count": len(variants), "warnings": warnings}


def resolve_supplier_images(skus: list) -> list:
    """Share only images explicitly associated with the same option value."""
    images = {}
    for sku in skus:
        for option in sku.get("options", []):
            if option.get("image"):
                key = (str(option["id"]), option["value"])
                images.setdefault(key, set()).add(option["image"])
    resolved = []
    for sku in skus:
        row = dict(sku)
        if not row.get("image"):
            candidates = set()
            for option in sku.get("options", []):
                candidates.update(images.get((str(option["id"]), option["value"]), set()))
            if len(candidates) == 1:
                row["image"] = candidates.pop()
        resolved.append(row)
    return resolved


def sync_replacement_images(shop, product_id, skus):
    """Attach new images by supplier identity, never by array position."""
    variants = shopify._get_all_variants_for_image_sync(shop, product_id)
    by_sku = {str(v.get("aliexpress_sku_id")): v for v in variants}
    expected = {str(s["sku_id"]) for s in skus}
    if set(by_sku) != expected or len(variants) != len(skus):
        raise ValueError("Replacement SKU list could not be verified; review Shopify variants")
    uploaded, attached, missing = {}, 0, 0
    for sku in skus:
        url = sku.get("image")
        if not url:
            missing += 1
            continue
        if url not in uploaded:
            uploaded[url] = shopify._upload_image_to_shopify(shop, product_id, url, sku.get("label", ""))
        image_id = uploaded[url]
        if not image_id:
            continue
        variant = by_sku[str(sku["sku_id"])]
        response = shopify._shopify_request(
            "PUT", f"{shopify._base(shop.shop_domain)}/variants/{variant['id']}.json",
            json={"variant": {"id": variant["id"], "image_id": image_id}},
            headers=shopify._h(shop.access_token), timeout=30)
        response.raise_for_status()
        if str((response.json().get("variant") or {}).get("image_id")) == str(image_id):
            attached += 1
    return {"attached": attached, "remaining": len(skus) - attached,
            "missing_supplier_images": missing}
