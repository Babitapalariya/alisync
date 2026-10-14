"""
AliExpress DS Dropshipping API client — fully corrected based on actual API response.

Key findings from debug endpoint:
- Shipping: logistics_info_dto.delivery_time (int, days) + ship_to_country
- Stock: sku.sku_available_stock (NOT ipm_sku_stock which is always null)
- Variant names: sku.ae_sku_property_dtos
- Sales count: ae_item_base_info_dto.sales_count (NOT lastest_volume)
- Extra data: package_info_dto, ae_store_info available
- SKU images: sku.ae_sku_property_dtos[*].sku_property_value_id_long_image (first prop with image)

- SKU list filtering: the DS API returns SKU combinations across EVERY
  ship-from/ship-to location pairing, even when only one ship_to_country is
  requested. The product detail page only shows one combo per real variant
  (whichever location the buyer's country resolves to), so we collapse the
  raw SKU list down to one entry per real variant.
- Stock quirk: stock (sku_available_stock) is tracked PER location pairing,
  not per real variant. A real variant can have genuine stock in one
  warehouse while its specific "ship to <country>"-tagged combo reports 0 —
  AliExpress's own logistics will still source and ship it from wherever
  stock actually exists. So when collapsing location combos per real
  variant, we prefer an in-stock combo over a same-variant combo that
  happens to show 0, rather than blindly keeping whichever combo matches
  the requested ship_to_country. This prevents variants that are genuinely
  orderable (and show as in-stock on the live product page) from being
  reported as out-of-stock and zeroed out in Shopify.
"""

import hashlib
import http.client
import json
import time
import urllib.parse
from fastapi import HTTPException
from sqlalchemy.orm import Session

from .config import get_settings
from .auth import get_aliexpress_credentials, get_latest_token

settings = get_settings()

DOMAIN   = "api-sg.aliexpress.com"
ENDPOINT = "/sync"


def _sign(secret: str, params: dict) -> str:
    keys     = sorted(params.keys())
    sign_str = secret + "".join(f"{k}{params[k]}" for k in keys) + secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()


def _call(method: str, app_params: dict, access_token: str, app_key: str, app_secret: str) -> dict:
    sys_params = {
        "app_key":      app_key,
        "method":       method,
        "timestamp":    str(int(time.time() * 1000)),
        "sign_method":  "md5",
        "v":            "2.0",
        "access_token": access_token,
    }
    sys_params["sign"] = _sign(app_secret, {**sys_params, **app_params})

    url  = ENDPOINT + "?" + urllib.parse.urlencode(sys_params)
    body = urllib.parse.urlencode(app_params)

    conn = http.client.HTTPSConnection(DOMAIN, timeout=30)
    conn.request("POST", url, body=body, headers={
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Cache-Control": "no-cache",
    })
    resp   = conn.getresponse()
    result = resp.read().decode("utf-8")
    conn.close()

    if resp.status != 200:
        raise HTTPException(status_code=502, detail=f"AliExpress HTTP {resp.status}: {result}")

    return json.loads(result)


def _check_error(body: dict):
    if not isinstance(body, dict):
        return
    if "error_response" in body:
        err = body["error_response"]
        raise HTTPException(status_code=400, detail={
            "aliexpress_error_code": err.get("code", ""),
            "aliexpress_message":    err.get("msg") or err.get("message", ""),
            "raw": body,
        })


# ─────────────────────────────────────────────
# Variant name + image resolver
# ─────────────────────────────────────────────

def _resolve_sku_info(sku: dict) -> dict:
    """
    Returns {"label": str, "image": str|None}
    Extracts human-readable label AND the per-variant image from ae_sku_property_dtos.
    The image is taken from the first property that has a non-null image URL.
    """
    props = sku.get("ae_sku_property_dtos", {})
    if isinstance(props, dict):
        prop_list = props.get("ae_sku_property_d_t_o", [])
    elif isinstance(props, list):
        prop_list = props
    else:
        prop_list = []

    if not prop_list:
        raw = sku.get("sku_attr", "")
        parts = [p.split(":")[-1].strip() for p in raw.split(";") if ":" in p]
        label = " / ".join(parts) if parts else raw
        return {"label": label, "image": None}

    parts = []
    options = []
    image_url = None

    for prop in prop_list:
        val = (
            prop.get("property_value_definition_name")
            or prop.get("attr_value")
            or prop.get("sku_property_value")
            or str(prop.get("attr_value_id", ""))
        )
        if val:
            parts.append(str(val).strip())
            options.append({
                "id": str(prop.get("sku_property_id") or prop.get("attr_name_id") or prop.get("sku_property_name") or len(options)),
                "name": str(prop.get("sku_property_name") or prop.get("attr_name") or f"Option {len(options) + 1}"),
                "value": str(val).strip(),
            })

        # Keep the image's option identity so sizes can share their color image.
        img = (
                prop.get("sku_property_value_id_long_image")   # most common DS field
                or prop.get("property_value_id_long_image")
                or prop.get("sku_image")
                or prop.get("image_path")
            )
        if img and isinstance(img, str):
            if img.startswith("//"):
                img = "https:" + img
            if img.startswith(("https://", "http://")):
                if val:
                    options[-1]["image"] = img
                if image_url is None:
                    image_url = img

    label = " / ".join(parts) if parts else sku.get("sku_attr", "")
    return {"label": label, "image": image_url, "options": options}


# Keep the old name as an alias so nothing breaks
def _resolve_sku_label(sku: dict) -> str:
    return _resolve_sku_info(sku)["label"]


# ─────────────────────────────────────────────
# Ship-from location filter + stock-aware collapsing
# ─────────────────────────────────────────────

COUNTRY_CODE_TO_NAME = {
    "US": "United States", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia",
    "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy", "NL": "Netherlands",
    "BE": "Belgium", "PL": "Poland", "SE": "Sweden", "RU": "Russian Federation",
    "BR": "Brazil", "MX": "Mexico", "JP": "Japan", "KR": "South Korea", "SG": "Singapore",
    "NZ": "New Zealand", "ZA": "South Africa", "AE": "United Arab Emirates",
    "SA": "Saudi Arabia", "TR": "Turkey", "TH": "Thailand", "VN": "Vietnam",
    "PH": "Philippines", "ID": "Indonesia", "MY": "Malaysia", "HK": "Hong Kong",
    "TW": "Taiwan", "IL": "Israel", "UA": "Ukraine", "CZ": "Czech Republic",
    "CL": "Chile", "CN": "China Mainland",
}

KNOWN_SHIP_LOCATIONS = {v.lower() for v in COUNTRY_CODE_TO_NAME.values()} | {
    "china mainland", "hong kong", "macau",
}


def _sku_stock_int(sku: dict):
    """Safely coerce a sku's 'stock' field to int, or None if unusable."""
    val = sku.get("stock")
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _filter_skus_by_ship_location(skus: list, ship_to_country: str) -> list:
    """
    Collapses raw SKU combinations down to one entry per REAL variant.

    The DS API frequently returns SKU combos across every ship-from/ship-to
    location pairing even when only one ship_to_country was requested, so a
    product can report far more SKUs (and different stock numbers) than the
    buyer actually sees on the product detail page.

    For each group of combos that share the same underlying variant (same
    label with the trailing location segment removed), this picks:
      1. The combo matching the requested ship_to_country, IF it has stock.
      2. Otherwise, any other location's combo for that same variant that
         has stock (AliExpress logistics can fulfil from any warehouse that
         has it, regardless of which location combo the buyer's country
         happens to map to).
      3. Otherwise, the combo matching the requested ship_to_country even
         with 0/unknown stock, so price/label/image data is still correct.
      4. Otherwise, just the first combo in the group.

    Falls back to returning the unfiltered list if no location dimension
    is detected, or if collapsing would wipe out every SKU (safety net).
    """
    if not skus:
        return skus

    target_name = COUNTRY_CODE_TO_NAME.get((ship_to_country or "US").upper())
    if not target_name:
        return skus  # unrecognized country code — can't safely filter
    target_name_lower = target_name.lower()

    split_labels = []
    for sku in skus:
        label = sku.get("label") or ""
        parts = [p.strip() for p in label.split(" / ")]
        split_labels.append(parts)

    has_location_dimension = any(
        len(parts) >= 2 and parts[-1].lower() in KNOWN_SHIP_LOCATIONS
        for parts in split_labels
    )
    if not has_location_dimension:
        return skus  # no ship-location dimension present, nothing to collapse

    # Group combos by their base (real-variant) label
    groups = {}
    passthrough = []  # entries with no location suffix at all
    for sku, parts in zip(skus, split_labels):
        if len(parts) >= 2 and parts[-1].lower() in KNOWN_SHIP_LOCATIONS:
            base_label = " / ".join(parts[:-1]) or parts[-1]
            groups.setdefault(base_label, []).append((sku, parts))
        else:
            passthrough.append(sku)

    collapsed = []
    for base_label, entries in groups.items():
        target_entry = None
        for sku, parts in entries:
            if parts[-1].lower() == target_name_lower:
                target_entry = sku
                break

        chosen = None

        # 1. Target country combo, if it actually has stock
        if target_entry is not None and (_sku_stock_int(target_entry) or 0) > 0:
            chosen = target_entry

        # 2. Any other location's combo for this same variant that has stock
        if chosen is None:
            for sku, _parts in entries:
                if (_sku_stock_int(sku) or 0) > 0:
                    chosen = sku
                    break

        # 3. Target country combo even without stock, to preserve correct
        #    price/label/image for this ship destination
        if chosen is None and target_entry is not None:
            chosen = target_entry

        # 4. Last resort — just take the first combo in the group
        if chosen is None:
            chosen = entries[0][0]

        new_sku = dict(chosen)
        new_sku["label"] = base_label
        if new_sku.get("options"):
            new_sku["options"] = new_sku["options"][:-1]
        collapsed.append(new_sku)

    result = passthrough + collapsed
    return result if result else skus


# ─────────────────────────────────────────────
# Shipping parser — from logistics_info_dto
# ─────────────────────────────────────────────

def _parse_freight(result: dict) -> dict:
    logistics = result.get("logistics_info_dto", {})

    if logistics:
        days = logistics.get("delivery_time")
        country = logistics.get("ship_to_country", "")

        ae_logistics = logistics.get("ae_logistics_info", [])
        if isinstance(ae_logistics, list) and ae_logistics:
            cheapest = None
            cheapest_price = float("inf")
            for item in ae_logistics:
                if not isinstance(item, dict):
                    continue
                price_info = item.get("freight_amount", {})
                if isinstance(price_info, dict):
                    try:
                        price_val = float(price_info.get("amount", 9999))
                    except (ValueError, TypeError):
                        price_val = 9999
                else:
                    price_val = 9999
                if price_val < cheapest_price:
                    cheapest_price = price_val
                    cheapest = item

            if cheapest:
                price_info = cheapest.get("freight_amount", {})
                amt = float(price_info.get("amount", 0)) if isinstance(price_info, dict) else 0
                est_days = cheapest.get("estimated_delivery_time") or cheapest.get("delivery_days")
                return {
                    "cost":   "Free" if amt == 0 else f"{price_info.get('currency','USD')} {amt:.2f}",
                    "method": cheapest.get("company", "Standard Shipping"),
                    "days":   str(est_days) if est_days else (str(days) if days else ""),
                }

        if days is not None:
            return {
                "cost":   "Calculated at checkout",
                "method": "Standard Shipping",
                "days":   f"{days} days to {country}" if country else f"{days} days",
            }

    props = result.get("ae_item_properties", {})
    freight_tpl = props.get("freight_template", {})
    if freight_tpl and isinstance(freight_tpl, dict):
        cost   = freight_tpl.get("freight")
        method = freight_tpl.get("delivery_time") or freight_tpl.get("company")
        d_days = freight_tpl.get("delivery_days")
        return {
            "cost":   "Free" if cost == 0 else (str(cost) if cost else "Calculated at checkout"),
            "method": str(method) if method else "Standard Shipping",
            "days":   str(d_days) if d_days else "",
        }

    return {
        "cost":   "Calculated at checkout",
        "method": "Standard Shipping",
        "days":   "",
    }


# ─────────────────────────────────────────────
# Inventory parser — using sku_available_stock
# ─────────────────────────────────────────────

def _parse_inventory(result: dict, sku_list: list) -> dict:
    base = result.get("ae_item_base_info_dto", {})

    sales_count = None
    for field in ("sales_count", "lastest_volume", "volume"):
        val = base.get(field)
        if val:
            try:
                sales_count = int(str(val).replace("+", "").replace(",", ""))
                break
            except (ValueError, TypeError):
                pass

    sku_inventory = []
    total_stock = 0
    any_stock = False

    for sku in sku_list:
        stock_val = sku.get("sku_available_stock")
        info = _resolve_sku_info(sku)

        try:
            stock = int(stock_val) if stock_val is not None else None
        except (ValueError, TypeError):
            stock = None

        if stock is not None and stock > 0:
            total_stock += stock
            any_stock = True

        sku_inventory.append({
            "sku_id": sku.get("sku_id"),
            "attr":   info["label"],
            "stock":  stock,
            "sale_price": sku.get("offer_sale_price"),
            "currency": sku.get("currency_code"),
        })

    if any_stock:
        return {
            "total_stock":     total_stock,
            "stock_available": True,
            "stock_source":    "sku_available_stock",
            "sku_inventory":   sku_inventory,
            "sales_count":     sales_count,
            "note":            f"{total_stock} units across {len(sku_inventory)} SKU(s)",
        }

    return {
        "total_stock":     None,
        "stock_available": None,
        "stock_source":    "unavailable",
        "sku_inventory":   sku_inventory,
        "sales_count":     sales_count,
        "note":            (
            "AliExpress DS API does not expose inventory for this product. "
            "This is normal for dropshipping — the supplier fulfils on demand."
        ),
    }


# ─────────────────────────────────────────────
# Product parser
# ─────────────────────────────────────────────

def _parse_product(raw: dict, ship_to_country: str = "US") -> dict:
    result = (
        raw
        .get("aliexpress_ds_product_get_response", {})
        .get("result", {})
    )
    if not result:
        return raw

    product  = result.get("ae_item_base_info_dto", {})
    sku_list = (
        result
        .get("ae_item_sku_info_dtos", {})
        .get("ae_item_sku_info_d_t_o", [])
    )
    images = (
        result
        .get("ae_multimedia_info_dto", {})
        .get("image_urls", "")
    )

    pkg   = result.get("package_info_dto", {})
    store = result.get("ae_store_info", {})

    # Build SKUs with resolved human-readable labels AND per-variant images
    skus = []
    for sku in sku_list:
        info = _resolve_sku_info(sku)
        skus.append({
            "sku_id":       sku.get("sku_id"),
            "sku_attr":     sku.get("sku_attr"),
            "label":        info["label"],
            "options":      info.get("options", []),
            "image":        info["image"],           # ← per-variant image URL
            "price":        sku.get("sku_price"),
            "sale_price":   sku.get("offer_sale_price"),
            "bulk_price":   sku.get("offer_bulk_sale_price"),
            "stock":        sku.get("sku_available_stock"),
            "currency":     sku.get("currency_code"),
        })

    # Collapse SKU combinations for other ship-from warehouses down to one
    # per real variant — the DS API returns combos for every location, but
    # the product page only shows one combo per real variant. Where the
    # requested-country combo shows 0 stock but another location's combo
    # for the same variant has real stock, that in-stock combo is used
    # instead (AliExpress logistics fulfils from wherever stock exists,
    # not strictly the location tied to the buyer's ship-to country).
    skus = _filter_skus_by_ship_location(skus, ship_to_country=ship_to_country)

    # Keep the raw sku_list used for inventory parsing in sync with the
    # filtered skus, so total_stock/sku_inventory reflect the collapsed set.
    kept_ids = {s.get("sku_id") for s in skus}
    filtered_sku_list = [s for s in sku_list if s.get("sku_id") in kept_ids]

    shipping  = _parse_freight(result)
    inventory = _parse_inventory(result, filtered_sku_list)

    return {
        # Identity
        "product_id":   product.get("product_id"),
        "title":        product.get("subject"),
        "category_id":  product.get("category_id"),
        "store_id":     store.get("store_id") or product.get("store_id"),
        "store_name":   store.get("store_name") or product.get("store_name"),

        # Pricing
        "original_price": product.get("original_price"),
        "sale_price":     product.get("sale_price"),
        "currency":       product.get("currency_code"),
        "discount":       product.get("discount"),

        # Media
        "main_image":  images.split(";")[0] if images else product.get("main_image"),
        "all_images":  images.split(";") if images else [],

        # Ratings & Sales
        "avg_rating":    product.get("avg_evaluation_rating"),
        "review_count":  product.get("evaluation_count"),
        "orders":        product.get("sales_count"),

        # Variants / SKUs (collapsed to match the product detail page,
        # stock-aware across ship-from locations, includes per-variant "image")
        "sku_count": len(skus),
        "skus":      skus,

        # Package info
        "package_weight":      pkg.get("gross_weight") or pkg.get("package_weight"),
        "package_length":      pkg.get("package_length"),
        "package_width":       pkg.get("package_width"),
        "package_height":      pkg.get("package_height"),

        # Links
        "product_url": f"https://www.aliexpress.com/item/{product.get('product_id')}.html",

        # Shipping
        "shipping_info": {
            "cost":   shipping["cost"],
            "method": shipping["method"],
            "days":   shipping["days"],
        },

        # Inventory
        "total_stock":     inventory["total_stock"],
        "stock_available": inventory["stock_available"],
        "stock_source":    inventory["stock_source"],
        "stock_note":      inventory["note"],
        "sku_inventory":   inventory["sku_inventory"],
        "sales_count":     inventory["sales_count"],
    }


def _parse_search(raw: dict) -> dict:
    result = (
        raw
        .get("aliexpress_ds_text_search_response", {})
        .get("result", {})
    )
    if not result:
        return raw

    items = result.get("products", {}).get("traffic_product_d_t_o", [])
    clean = []
    for item in items:
        clean.append({
            "product_id":     item.get("product_id"),
            "title":          item.get("product_title"),
            "main_image":     item.get("product_main_image_url"),
            "sale_price":     item.get("target_sale_price"),
            "original_price": item.get("target_original_price"),
            "currency":       item.get("target_sale_price_currency"),
            "discount":       item.get("discount"),
            "orders":         item.get("lastest_volume"),
            "avg_rating":     item.get("evaluate_rate"),
            "store_id":       item.get("store_id"),
            "store_name":     item.get("store_name"),
            "product_url":    f"https://www.aliexpress.com/item/{item.get('product_id')}.html",
        })

    return {
        "total_count": result.get("total_count"),
        "page":        result.get("current_page_no"),
        "page_size":   result.get("current_record_count"),
        "products":    clean,
    }


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def get_product(product_id: str, db: Session = None, ship_to_country: str = "US") -> dict:
    if db is None:
        from .database import SessionLocal
        db = SessionLocal()
        close_db = True
    else:
        close_db = False

    try:
        token = get_latest_token(db)
        app_key, app_secret, _ = get_aliexpress_credentials(db)
        raw   = _call(
            method="aliexpress.ds.product.get",
            app_params={
                "product_id":      product_id,
                "local":           "en_US",
                "ship_to_country": ship_to_country,
                "target_currency": "USD",
            },
            access_token=token.access_token, app_key=app_key, app_secret=app_secret,
        )
        _check_error(raw)
        return _parse_product(raw, ship_to_country=ship_to_country)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if close_db:
            db.close()


def search_products(keyword: str, page: int = 1, page_size: int = 20, db: Session = None) -> dict:
    if db is None:
        from .database import SessionLocal
        db = SessionLocal()
        close_db = True
    else:
        close_db = False

    try:
        token = get_latest_token(db)
        app_key, app_secret, _ = get_aliexpress_credentials(db)
        raw   = _call(
            method="aliexpress.ds.text.search",
            app_params={
                "search_key":      keyword,
                "page_no":         str(page),
                "page_size":       str(page_size),
                "local":           "en_US",
                "ship_to_country": "US",
                "target_currency": "USD",
                "sort":            "SALE_PRICE_ASC",
            },
            access_token=token.access_token, app_key=app_key, app_secret=app_secret,
        )
        _check_error(raw)
        return _parse_search(raw)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if close_db:
            db.close()


def get_shipping_info(product_id: str, country_code: str = "US", db: Session = None) -> dict:
    """Shipping is embedded in the product response — no separate endpoint exists."""
    try:
        product = get_product(product_id, db, ship_to_country=country_code)
        info = product.get("shipping_info", {})
        return {
            "shipping_cost": info.get("cost", "Calculated at checkout"),
            "method":        info.get("method", "Standard Shipping"),
            "days":          info.get("days", ""),
        }
    except Exception:
        return {
            "shipping_cost": "Calculated at checkout",
            "method":        "Standard Shipping",
            "days":          "",
        }
