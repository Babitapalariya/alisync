"""
shopify.py – multi-tenant version. Every function takes a `shop` object
(with .shop_domain and .access_token) as its first parameter instead of
reading a single global SHOPIFY_STORE / token.
"""

import re
import time
import requests
from fastapi import HTTPException
from .config import get_settings
from .shopify_auth import shopify_api_error
import time as _time
import threading

settings = get_settings()

_rate_lock = threading.Lock()
_last_call_time = 0.0
MIN_INTERVAL = 0.55  # ~1.8 req/sec, safely under Shopify's 2/sec sustained limit


# ─────────────────────────────────────────────
# CORE HELPERS — now shop-aware
# ─────────────────────────────────────────────
def _base(shop_domain: str) -> str:
    return f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}"


def _h(access_token: str) -> dict:
    return {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}


def _throttle():
    global _last_call_time
    with _rate_lock:
        now = time.time()
        wait = MIN_INTERVAL - (now - _last_call_time)
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.time()


def _shopify_request(method: str, url: str, max_retries: int = 5, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 20)
    for attempt in range(max_retries):
        _throttle()
        res = requests.request(method, url, **kwargs)
        if res.status_code != 429:
            return res
        retry_after = res.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 1.0
        except (ValueError, TypeError):
            delay = 1.0
        delay = max(delay, 0.5) * (attempt + 1)
        print(f"[Shopify][RateLimit] 429 on {method} {url} — retrying in {delay:.1f}s (attempt {attempt+1}/{max_retries})")
        time.sleep(delay)
    print(f"[Shopify][RateLimit] Giving up after {max_retries} retries: {method} {url}")
    return res


def _graphql_url(shop_domain: str) -> str:
    return f"https://{shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json"


def _graphql(shop, query: str, variables: dict = None) -> dict:
    payload = {"query": query, "variables": variables or {}}
    res = _shopify_request("POST", _graphql_url(shop.shop_domain), json=payload, headers=_h(shop.access_token), timeout=30)
    res.raise_for_status()
    data = res.json()
    if "errors" in data:
        raise HTTPException(502, f"Shopify GraphQL error: {data['errors']}")
    return data.get("data", {})


def _variant_gid(variant_id) -> str:
    return f"gid://shopify/ProductVariant/{variant_id}"

def get_variant_sku_details(shop, variants: list) -> dict:
    """Preserve Shopify SKUs; resolve blanks from each variant's saved supplier ID."""
    result = {}
    missing = []
    for variant in variants:
        sku = str(variant.get("sku") or "").strip()
        result[str(variant["id"])] = {"sku": sku, "sku_source": "shopify" if sku else None}
        if not sku:
            missing.append(_variant_gid(variant["id"]))
    query = """
    query VariantSkuDetails($ids: [ID!]!) {
      nodes(ids: $ids) {
        ... on ProductVariant {
          id
          skuId: metafield(namespace: "aliexpress", key: "sku_id") { value }
        }
      }
    }
    """
    for start in range(0, len(missing), 100):
        try:
            data = _graphql(shop, query, {"ids": missing[start:start + 100]})
            for node in data.get("nodes") or []:
                if not node:
                    continue
                variant_id = _numeric_id_from_gid(node["id"])
                sku = str((node.get("skuId") or {}).get("value") or "").strip()
                if variant_id in result and sku and sku.lower() != "none":
                    result[variant_id] = {"sku": sku, "sku_source": "aliexpress"}
        except Exception:
            # A metadata lookup failure must not prevent price/inventory editing.
            continue
    return result


def _product_gid(product_id) -> str:
    return f"gid://shopify/Product/{product_id}"

def _inventory_item_gid(item_id) -> str:
    return f"gid://shopify/InventoryItem/{item_id}"

def _location_gid(location_id) -> str:
    return f"gid://shopify/Location/{location_id}"

def _numeric_id_from_gid(gid: str) -> str:
    return gid.rsplit("/", 1)[-1]


# ─────────────────────────────────────────────
# EXISTENCE & LOOKUP
# ─────────────────────────────────────────────
def search_shopify_product_titles(shop, text: str) -> dict:
    """Read current Shopify titles matching literal title-word prefixes."""
    words = re.findall(r"\w+", text, flags=re.UNICODE)
    if not words:
        return {}
    search = " AND ".join(f"title:{word}*" for word in words)
    query = """
    query SearchProductTitles($search: String!, $after: String) {
      products(first: 100, query: $search, after: $after) {
        nodes { id title }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    titles = {}
    after = None
    while True:
        data = _graphql(shop, query, {"search": search, "after": after})
        products = data["products"]
        for node in products.get("nodes") or []:
            titles[_numeric_id_from_gid(node["id"])] = node["title"]
        page = products["pageInfo"]
        if not page["hasNextPage"]:
            return titles
        cursor = page.get("endCursor")
        if not cursor or cursor == after:
            raise ValueError("Shopify title search pagination did not advance")
        after = cursor


def check_product_exists_in_shopify(shop, title: str) -> bool:
    try:
        res = _shopify_request(
            "GET", f"{_base(shop.shop_domain)}/products.json",
            params={"title": title, "limit": 1, "fields": "id,title"},
            headers=_h(shop.access_token), timeout=15,
        )
        res.raise_for_status()
        return len(res.json().get("products", [])) > 0
    except Exception:
        return False


def get_shopify_product_by_aliexpress_id(shop, aliexpress_id: str) -> dict | None:
    try:
        res = _shopify_request(
            "GET", f"{_base(shop.shop_domain)}/products.json",
            params={
                "fields": "id,title,metafields",
                "metafield[namespace]": "aliexpress",
                "metafield[key]": "product_id",
                "metafield[value]": aliexpress_id
            },
            headers=_h(shop.access_token), timeout=15,
        )
        res.raise_for_status()
        products = res.json().get("products", [])
        return products[0] if products else None
    except Exception:
        return None


def get_all_shopify_imported_products(shop) -> list:
    products = []
    url = f"{_base(shop.shop_domain)}/products.json?limit=250&fields=id,title,tags,status,metafields"
    while url:
        try:
            res = _shopify_request("GET", url, headers=_h(shop.access_token))
            res.raise_for_status()
            data = res.json()
            for p in data.get("products", []):
                if "aliexpress-import" in p.get("tags", ""):
                    ae_id = None
                    for mf in p.get("metafields", []):
                        if mf.get("namespace") == "aliexpress" and mf.get("key") == "product_id":
                            ae_id = mf.get("value")
                            break
                    products.append({
                        "shopify_id": str(p["id"]),
                        "title": p["title"],
                        "aliexpress_id": ae_id,
                        "status": p.get("status", "draft")
                    })
            link_header = res.headers.get("Link", "")
            next_match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
            url = next_match.group(1) if next_match else None
        except Exception as e:
            print(f"[Shopify] Fetch error: {e}")
            break
    return products


# ─────────────────────────────────────────────
# RATING HELPERS
# ─────────────────────────────────────────────
def _upsert_metafield(shop, shopify_id: str, namespace: str, key: str, value: str, mf_type: str) -> bool:
    try:
        res = _shopify_request(
            "GET", f"{_base(shop.shop_domain)}/products/{shopify_id}/metafields.json",
            params={"namespace": namespace, "key": key}, headers=_h(shop.access_token), timeout=15,
        )
        res.raise_for_status()
        existing = res.json().get("metafields", [])
        payload = {"metafield": {"namespace": namespace, "key": key, "value": value, "type": mf_type}}
        if existing:
            mf_id = existing[0]["id"]
            r2 = _shopify_request("PUT", f"{_base(shop.shop_domain)}/metafields/{mf_id}.json", json=payload, headers=_h(shop.access_token))
        else:
            r2 = _shopify_request("POST", f"{_base(shop.shop_domain)}/products/{shopify_id}/metafields.json", json=payload, headers=_h(shop.access_token))
        r2.raise_for_status()
        return True
    except Exception as e:
        print(f"[Shopify] Metafield error: {e}")
        return False


def save_rating_to_shopify(shop, shopify_id: str, rating: str) -> dict:
    if not shopify_id or not rating:
        return {"metafield": False, "tag": False, "description": False}
    r = str(rating).strip()
    rating_json = f'{{"scale_min":"1.0","scale_max":"5.0","value":"{r}"}}'
    mf_ok = _upsert_metafield(shop, shopify_id, "reviews", "rating", rating_json, "rating")
    _upsert_metafield(shop, shopify_id, "custom", "rating", r, "number_decimal")
    return {"metafield": mf_ok, "tag": False, "description": False}


# ─────────────────────────────────────────────
# SKU IMAGE HELPERS
# ─────────────────────────────────────────────
def _upload_image_to_shopify(shop, shopify_product_id: str, image_url: str, alt: str = "") -> int | None:
    try:
        payload = {"image": {"src": image_url, "alt": alt}}
        res = _shopify_request(
            "POST", f"{_base(shop.shop_domain)}/products/{shopify_product_id}/images.json",
            json=payload, headers=_h(shop.access_token), timeout=30,
        )
        res.raise_for_status()
        return res.json().get("image", {}).get("id")
    except Exception as e:
        print(f"[Shopify][Image] Upload failed for {image_url}: {e}")
        return None


def attach_sku_images_to_product(shop, shopify_product_id: str, aliexpress_skus: list, shopify_variants: list) -> int:
    if not aliexpress_skus or not shopify_variants:
        return 0

    locked_variant_ids = get_locked_variant_ids(shop, shopify_product_id, "image")
    ae_sku_by_id = {str(s.get("sku_id")): s for s in aliexpress_skus if s.get("sku_id") is not None}
    variant_ae_map = {}
    for variant in shopify_variants:
        vid = variant["id"]
        try:
            mf_res = _shopify_request(
                "GET", f"{_base(shop.shop_domain)}/variants/{vid}/metafields.json",
                params={"namespace": "aliexpress", "key": "sku_id"}, headers=_h(shop.access_token),
            )
            if mf_res.status_code == 200:
                metafields = mf_res.json().get("metafields", [])
                if metafields:
                    variant_ae_map[vid] = str(metafields[0].get("value"))
        except Exception as e:
            print(f"[Shopify][Image] Metafield lookup failed for variant {vid}: {e}")
    url_to_image_id: dict[str, int] = {}
    attached = 0
    unmatched = []

    for i, shopify_variant in enumerate(shopify_variants):
        if shopify_variant["id"] in locked_variant_ids:
            continue
        ae_sku_id = variant_ae_map.get(shopify_variant["id"])
        ae_sku = ae_sku_by_id.get(ae_sku_id) if ae_sku_id is not None else None
        if ae_sku is None and i < len(aliexpress_skus):
            ae_sku = aliexpress_skus[i]
        if ae_sku is None:
            unmatched.append(shopify_variant["id"])
            continue
        img_url = ae_sku.get("image")
        if not img_url:
            continue

        if img_url not in url_to_image_id:
            image_id = _upload_image_to_shopify(shop, shopify_product_id, img_url, alt=ae_sku.get("label", ""))
            if image_id:
                url_to_image_id[img_url] = image_id
            else:
                continue
        else:
            image_id = url_to_image_id[img_url]

        variant_id = shopify_variant["id"]
        try:
            res = _shopify_request(
                "PUT", f"{_base(shop.shop_domain)}/variants/{variant_id}.json",
                json={"variant": {"id": variant_id, "image_id": image_id}},
                headers=_h(shop.access_token), timeout=15,
            )
            res.raise_for_status()
            attached += 1
            print(f"[Shopify][Image] Variant {variant_id} ← image {image_id} ({ae_sku.get('label','')})")
        except Exception as e:
            print(f"[Shopify][Image] Variant link failed for {variant_id}: {e}")

    if unmatched:
        print(f"[Shopify][Image] {len(unmatched)} variant(s) had no matching AliExpress SKU: {unmatched}")
    return attached


def _get_all_variants_for_image_sync(shop, shopify_product_id: str) -> list:
    query = """
    query ImageSyncVariants($id: ID!, $after: String) {
      product(id: $id) {
        variants(first: 100, after: $after) {
          nodes { id image { id } skuId: metafield(namespace: "aliexpress", key: "sku_id") { value } }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    variants, after = [], None
    while True:
        data = _graphql(shop, query, {"id": _product_gid(shopify_product_id), "after": after})
        product = data.get("product")
        if not product:
            raise RuntimeError(f"Shopify product {shopify_product_id} was not found")
        connection = product.get("variants") or {}
        for node in connection.get("nodes", []):
            image = node.get("image") or {}
            sku_metafield = node.get("skuId") or {}
            variants.append({
                "id": int(_numeric_id_from_gid(node["id"])),
                "image_id": int(_numeric_id_from_gid(image["id"])) if image.get("id") else None,
                "aliexpress_sku_id": str(sku_metafield["value"]) if sku_metafield.get("value") is not None else None,
            })
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return variants
        after = page_info.get("endCursor")
        if not after:
            raise RuntimeError("Shopify variant pagination returned no end cursor")


def backfill_sku_images(shop, shopify_product_id: str, aliexpress_skus: list) -> dict:
    try:
        shopify_variants = _get_all_variants_for_image_sync(shop, shopify_product_id)
    except Exception as e:
        print(f"[Backfill][Image] Fetch variants failed: {e}")
        return {"attached": 0, "skipped": 0, "total_variants": 0}

    locked_variant_ids = get_locked_variant_ids(shop, shopify_product_id, "image")

    needs_image = [
        v for v in shopify_variants
        if not v.get("image_id") and v["id"] not in locked_variant_ids
    ]
    already_has = len(shopify_variants) - len(needs_image)

    if not needs_image:
        return {"attached": 0, "skipped": already_has, "total_variants": len(shopify_variants)}

    url_to_image_id: dict[str, int] = {}
    ae_sku_by_id = {str(s.get("sku_id")): s for s in aliexpress_skus if s.get("sku_id") is not None}
    attached = 0
    unmatched = 0

    for i, shopify_variant in enumerate(shopify_variants):
        if shopify_variant.get("image_id"):
            continue
        if shopify_variant["id"] in locked_variant_ids:
            continue
        ae_sku_id = shopify_variant.get("aliexpress_sku_id")
        ae_sku = ae_sku_by_id.get(ae_sku_id) if ae_sku_id is not None else None
        if ae_sku is None and ae_sku_id is None and i < len(aliexpress_skus):
            ae_sku = aliexpress_skus[i]
        if ae_sku is None:
            unmatched += 1
            continue
        img_url = ae_sku.get("image")
        if not img_url:
            continue

        if img_url not in url_to_image_id:
            image_id = _upload_image_to_shopify(shop, shopify_product_id, img_url, alt=ae_sku.get("label", ""))
            if image_id:
                url_to_image_id[img_url] = image_id
            else:
                continue
        else:
            image_id = url_to_image_id[img_url]

        variant_id = shopify_variant["id"]
        try:
            res2 = _shopify_request(
                "PUT", f"{_base(shop.shop_domain)}/variants/{variant_id}.json",
                json={"variant": {"id": variant_id, "image_id": image_id}},
                headers=_h(shop.access_token), timeout=15,
            )
            res2.raise_for_status()
            attached += 1
            print(f"[Backfill][Image] Variant {variant_id} ← image {image_id}")
        except Exception as e:
            print(f"[Backfill][Image] Link failed {variant_id}: {e}")

    if unmatched:
        print(f"[Backfill][Image] {unmatched} variant(s) had no matching AliExpress SKU")
    return {
        "attached": attached,
        "skipped": already_has,
        "total_variants": len(shopify_variants),
    }


# ─────────────────────────────────────────────
# NORMALIZER & CREATE
# ─────────────────────────────────────────────
def build_supplier_options(skus: list):
    """Preserve named supplier dimensions; never invent missing combinations."""
    if not skus:
        return [], []
    structured = [bool(s.get("options")) for s in skus]
    if not any(structured):
        values = [[s.get("label") or " / ".join(
            p.split(":", 1)[-1].split("#", 1)[-1].strip()
            for p in (s.get("sku_attr") or "").split(";") if ":" in p
        ) or "Default Title"] for s in skus]
        names = ["Variant"]
    else:
        if not all(structured):
            raise HTTPException(400, "Incomplete supplier options; fetch the product again before importing")
        first = skus[0]["options"]
        keys = [str(o["id"]) for o in first]
        names = [o["name"] for o in first]
        if len(keys) > 3 or len(set(keys)) != len(keys) or len(set(names)) != len(names):
            raise HTTPException(400, "Supplier options must have up to three distinct names")
        values = []
        for sku in skus:
            by_id = {str(o["id"]): o["value"] for o in sku["options"]}
            if set(by_id) != set(keys) or len(sku["options"]) != len(keys):
                raise HTTPException(400, "Supplier option groups differ between variants")
            values.append([by_id[key] for key in keys])
    if any(not v or len(v) > 255 for row in values for v in row):
        raise HTTPException(400, "Supplier option values must contain 1 to 255 characters")
    if len({tuple(row) for row in values}) != len(values):
        raise HTTPException(400, "Duplicate supplier option combinations; cannot safely import")
    return [{"name": name, "values": list(dict.fromkeys(row[i] for row in values))}
            for i, name in enumerate(names)], values



def normalize_aliexpress_product(product: dict) -> dict:
    title = product.get("title") or "AliExpress Product"
    base_price = str(product.get("sale_price") or product.get("original_price") or "0")
    rating = str(product.get("avg_rating") or "").strip()
    product_id = product.get("product_id", "")
    product_url = product.get("product_url", "")
    body_html = f"<p>Imported from AliExpress.</p>\n"
    if rating:
        body_html += f'<p><strong>⭐ AliExpress Rating: {rating} / 5</strong></p>\n'
    body_html += f"<p>Product ID: {product_id}</p>\n<p><a href='{product_url}' target='_blank'>View on AliExpress</a></p>"
    tags = ["aliexpress-import"]
    if rating:
        tags.append(f"rating:{rating}")
    skus = product.get("skus") or []
    options, option_values = build_supplier_options(skus)
    variants = []
    for sku, values in zip(skus, option_values):
        sku_price = str(sku.get("sale_price") or sku.get("price") or base_price)
        v = {"price": sku_price, "inventory_management": "shopify", "inventory_quantity": int(sku.get("stock") or 0)}
        sku_id = str(sku.get("sku_id") or "").strip()
        if sku_id and sku_id.lower() != "none":
            v["sku"] = sku_id
        v.update({f"option{i}": value for i, value in enumerate(values, 1)})
        variants.append(v)
    if not variants:
        variants = [{"price": base_price}]
    payload = {
        "title": title,
        "body_html": body_html,
        "vendor": "ugne",
        "product_type": "",
        "status": "draft",
        "tags": ", ".join(tags),
        "variants": variants,
        "metafields": [
            {"namespace": "aliexpress", "key": "product_id", "value": product_id, "type": "single_line_text_field"}
        ]
    }
    if options:
        payload["options"] = options
    imgs = []
    main = product.get("main_image")
    if main:
        imgs.append(main)
    for img in product.get("all_images") or []:
        if img and img not in imgs:
            imgs.append(img)
    if imgs:
        payload["images"] = [{"src": u} for u in imgs]
    return payload


def create_shopify_product(shop, product: dict) -> dict:
    title = product.get("title") or "AliExpress Product"
    if check_product_exists_in_shopify(shop, title):
        raise HTTPException(409, f"Product '{title}' already exists in Shopify.")
    try:
        payload = normalize_aliexpress_product(product)
        res = _shopify_request(
            "POST", f"{_base(shop.shop_domain)}/products.json",
            json={"product": payload}, headers=_h(shop.access_token), timeout=30,
        )
        res.raise_for_status()
        shopify_data = res.json()
        shopify_product = shopify_data["product"]
        shopify_product_id = str(shopify_product["id"])
        shopify_variants   = shopify_product.get("variants", [])

        skus = product.get("skus", [])
        if skus:
            store_aliexpress_sku_ids(shop, shopify_product_id, skus)

        if skus and shopify_variants:
            attached = attach_sku_images_to_product(shop, shopify_product_id, skus, shopify_variants)
            print(f"[Shopify] Attached {attached}/{len(shopify_variants)} SKU images for product {shopify_product_id}")

        return shopify_data
    except HTTPException:
        raise
    except Exception as e:
        response = getattr(e, "response", None)
        detail = shopify_api_error(response, "create the product") if response is not None else str(e)
        raise HTTPException(502, detail)


# ─────────────────────────────────────────────
# UPDATE FUNCTIONS
# ─────────────────────────────────────────────
def update_shopify_product(shop, shopify_product_id: str, updates: dict):
    payload = {}
    if updates.get("title"):
        payload["title"] = updates["title"]
    if updates.get("body_html"):
        payload["body_html"] = updates["body_html"]

    if updates.get("price"):
        try:
            res = _shopify_request(
                "GET", f"{_base(shop.shop_domain)}/products/{shopify_product_id}.json",
                params={"fields": "id,variants"}, headers=_h(shop.access_token), timeout=15,
            )
            res.raise_for_status()
            existing_variants = res.json().get("product", {}).get("variants", [])
            for v in existing_variants:
                try:
                    r_v = _shopify_request(
                        "PUT", f"{_base(shop.shop_domain)}/variants/{v['id']}.json",
                        json={"variant": {"id": v["id"], "price": str(updates["price"])}},
                        headers=_h(shop.access_token), timeout=20,
                    )
                    r_v.raise_for_status()
                except Exception as e:
                    print(f"[Shopify] Variant price update failed for {v['id']}: {e}")
        except Exception as e:
            detail = getattr(e, "response", None)
            detail = detail.text if detail else str(e)
            raise HTTPException(502, f"Failed to fetch existing variants: {detail}")

    if payload:
        try:
            res = _shopify_request(
                "PUT", f"{_base(shop.shop_domain)}/products/{shopify_product_id}.json",
                json={"product": payload}, headers=_h(shop.access_token), timeout=30,
            )
            res.raise_for_status()
        except Exception as e:
            detail = getattr(e, "response", None)
            detail = detail.text if detail else str(e)
            raise HTTPException(502, f"Shopify update failed: {detail}")

    rating = str(updates.get("rating") or "").strip()
    if rating:
        save_rating_to_shopify(shop, shopify_product_id, rating)


def update_shopify_product_price(shop, shopify_product_id: str, new_price: float) -> bool:
    try:
        res = _shopify_request(
            "GET", f"{_base(shop.shop_domain)}/products/{shopify_product_id}.json",
            params={"fields": "id,variants"}, headers=_h(shop.access_token), timeout=15,
        )
        res.raise_for_status()
        variants = res.json().get("product", {}).get("variants", [])
        if not variants:
            return False

        success_count = 0
        for v in variants:
            try:
                r2 = _shopify_request(
                    "PUT", f"{_base(shop.shop_domain)}/variants/{v['id']}.json",
                    json={"variant": {"id": v["id"], "price": str(new_price)}},
                    headers=_h(shop.access_token), timeout=20,
                )
                r2.raise_for_status()
                success_count += 1
            except Exception as e:
                print(f"[Shopify] Price update failed for variant {v['id']}: {e}")

        print(f"[Shopify] Price updated for {success_count}/{len(variants)} variant(s) of {shopify_product_id} to {new_price}")
        return success_count > 0
    except Exception as e:
        print(f"[Shopify] Price update failed: {e}")
        return False


def update_shopify_product_prices_with_skus(shop, shopify_product_id: str, aliexpress_skus: list,
                                            reset_manual_increases: bool = False, error_details: list | None = None,
                                            product_price_increase: float = 0.0) -> str:
    """Sync each unlocked variant from its own supplier price and saved increase."""
    def report(message):
        if error_details is not None:
            error_details.append(message)
    from decimal import Decimal, ROUND_HALF_UP
    from .price_history import observe_supplier_price
    if not aliexpress_skus:
        return "failed"
    try:
        # Fresh snapshot: no stale lock cache and no separate requests per SKU.
        variants = get_variant_sync_state(shop, shopify_product_id)
        if not variants:
            report("No Shopify variants were found for this mapping.")
            return "failed"
        matched = _match_supplier_variants(variants, aliexpress_skus)
        for sku in aliexpress_skus:
            raw = sku.get("sale_price") or sku.get("price")
            if raw is None:
                continue
            price = Decimal(str(raw))
            if not price.is_finite() or price < 0:
                raise ValueError("Invalid AliExpress variant price")

        updates, unmatched = [], []
        for vid, variant in variants.items():
            sku = matched.get(vid)
            raw = (sku.get("sale_price") or sku.get("price")) if sku else None
            if raw is None:
                if reset_manual_increases or not variant["locks"]["price"]:
                    unmatched.append(vid)
                continue
            base, supplier_price = Decimal(str(raw)), sku.get("_supplier_price", raw)
            previous = variant.get("supplier_history")
            repairing_link = sku.get("sku_id") is not None and str(sku["sku_id"]) != str(variant["ae_sku_id"])
            # A repaired link may have observed a different supplier variant.
            # Establish its baseline without claiming that difference was a price move.
            history = observe_supplier_price(None if repairing_link else previous, supplier_price)
            update = {"variant_id": vid}
            reset_unlocked = reset_manual_increases and not variant["locks"]["price"]
            increase = Decimal("0") if reset_unlocked else variant["price_increase"]
            if reset_unlocked and variant["price_increase"]:
                update["price_increase"] = "0.00"
                update["increase_metafield_id"] = variant.get("increase_metafield_id")
            if repairing_link:
                update["ae_sku_id"] = str(sku["sku_id"])
            if history != previous:
                update["supplier_history"] = history
                update["supplier_history_id"] = variant.get("supplier_history_id")
            if variant["locks"]["price"]:
                # The product-wide rule is cleared by explicit sync. Preserve its
                # locked share as a per-variant adjustment without changing price.
                if reset_manual_increases and product_price_increase:
                    update["price_increase"] = str(variant["price_increase"] + Decimal(str(product_price_increase)))
                    update["increase_metafield_id"] = variant.get("increase_metafield_id")
                if len(update) > 1:
                    updates.append(update)
                continue
            final = (base + increase).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if final < 0:
                raise ValueError(f"Negative final price for variant {vid}")
            if Decimal(str(variant["price"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != final:
                update["price"] = str(final)
            if len(update) > 1:
                updates.append(update)
            print(f"[PriceSync] variant={vid} supplier={base} increase={increase} final={final}")
        if unmatched:
            print(f"[PriceSync] No unambiguous AliExpress SKU match for variants {unmatched}")
            report("No matching AliExpress SKU for Shopify variant(s): " + ", ".join(map(str, unmatched)) + ". Check the linked AliExpress product and variant SKU IDs or option names.")
            if reset_manual_increases:
                return "failed"
        if not updates:
            return "failed" if unmatched else "unchanged"
        for offset in range(0, len(updates), 100):
            batch = updates[offset:offset + 100]
            result = bulk_update_variant_prices(shop, shopify_product_id, batch)
            if not result["success"] or result.get("errors") or result["updated"] != len(batch):
                print(f"[PriceSync] Shopify update incomplete: {result}")
                report("Shopify rejected the variant update: " + "; ".join(str(error.get("message", error)) if isinstance(error, dict) else str(error) for error in result.get("errors", [])))
                return "failed"
        return "failed" if unmatched else "updated" if any("price" in row for row in updates) else "unchanged"
    except Exception as exc:
        print(f"[PriceSync] Product {shopify_product_id} failed: {exc}")
        report(str(exc.detail) if isinstance(exc, HTTPException) else "Could not read or update Shopify variant pricing. Check the shop connection and supplier variant prices.")
        return "failed"

def store_aliexpress_sku_ids(shop, shopify_product_id: str, aliexpress_skus: list):
    res = _shopify_request(
        "GET", f"{_base(shop.shop_domain)}/products/{shopify_product_id}.json",
        params={"fields": "id,variants"}, headers=_h(shop.access_token),
    )
    res.raise_for_status()
    variants = res.json()["product"]["variants"]
    for i, variant in enumerate(variants):
        if i < len(aliexpress_skus):
            sku_id = str(aliexpress_skus[i].get("sku_id"))
            mf_url = f"{_base(shop.shop_domain)}/variants/{variant['id']}/metafields.json"
            check = _shopify_request("GET", mf_url, params={"namespace": "aliexpress", "key": "sku_id"}, headers=_h(shop.access_token))
            existing = check.json().get("metafields", [])
            payload = {"metafield": {"namespace": "aliexpress", "key": "sku_id", "value": sku_id, "type": "single_line_text_field"}}
            if existing:
                mf_id = existing[0]["id"]
                _shopify_request("PUT", f"{_base(shop.shop_domain)}/metafields/{mf_id}.json", json=payload, headers=_h(shop.access_token))
            else:
                _shopify_request("POST", mf_url, json=payload, headers=_h(shop.access_token))
    print(f"[Shopify] Stored SKU IDs for {len(aliexpress_skus)} variants")


def increase_shopify_product_price(shop, shopify_product_id: str, increase_by: float) -> bool:
    try:
        res = _shopify_request(
            "GET", f"{_base(shop.shop_domain)}/products/{shopify_product_id}.json",
            params={"fields": "id,variants"}, headers=_h(shop.access_token), timeout=15,
        )
        res.raise_for_status()
        product_data = res.json().get("product", {})
        variants = product_data.get("variants", [])
        if not variants:
            print(f"[Shopify] No variants found for product {shopify_product_id}")
            return False

        success_count = 0
        for variant in variants:
            try:
                current_price = float(variant["price"])
            except (ValueError, TypeError):
                current_price = 0.0
            new_price = current_price + increase_by
            try:
                r2 = _shopify_request(
                    "PUT", f"{_base(shop.shop_domain)}/variants/{variant['id']}.json",
                    json={"variant": {"id": variant["id"], "price": f"{new_price:.2f}"}},
                    headers=_h(shop.access_token), timeout=20,
                )
                r2.raise_for_status()
                success_count += 1
            except Exception as e:
                print(f"[Shopify] Price increase failed for variant {variant['id']}: {e}")

        print(f"[Shopify] Increased {success_count}/{len(variants)} variant(s) by {increase_by} for product {shopify_product_id}")
        return success_count > 0
    except Exception as e:
        print(f"[Shopify] Price increase failed: {e}")
        return False


def update_shopify_product_inventory_with_skus(shop, shopify_product_id: str, aliexpress_skus: list) -> bool:
    """Push AliExpress per-SKU stock via ONE GraphQL mutation per product."""
    stock_by_ae_sku = {}
    for sku in aliexpress_skus:
        ae_sku_id = str(sku.get("sku_id"))
        stock = sku.get("stock")
        if ae_sku_id and ae_sku_id != "None" and stock is not None:
            try:
                stock_by_ae_sku[ae_sku_id] = int(stock)
            except (ValueError, TypeError):
                continue

    if not stock_by_ae_sku and not aliexpress_skus:
        print("[Inventory] No AE stock data to sync")
        return False

    try:
        res = _shopify_request(
            "GET", f"{_base(shop.shop_domain)}/products/{shopify_product_id}.json",
            params={"fields": "id,variants"}, headers=_h(shop.access_token), timeout=15,
        )
        res.raise_for_status()
        shopify_variants = res.json().get("product", {}).get("variants", [])
        if not shopify_variants:
            return False
    except Exception as e:
        print(f"[Inventory] Fetch error: {e}")
        return False

    locked_variant_ids = get_locked_variant_ids(shop, shopify_product_id, "inventory")
    if locked_variant_ids:
        print(f"[Inventory] {len(locked_variant_ids)} variant(s) locked — will be skipped: {locked_variant_ids}")

    try:
        loc_res = _shopify_request("GET", f"{_base(shop.shop_domain)}/locations.json", headers=_h(shop.access_token), timeout=15)
        loc_res.raise_for_status()
        all_locations = loc_res.json().get("locations", [])
    except Exception as e:
        print(f"[Inventory] Failed to fetch locations: {e}")
        return False

    locations = [loc for loc in all_locations if loc.get("active", True)]
    if not locations:
        print("[Inventory] No active Shopify locations found")
        return False
    if len(locations) < len(all_locations):
        skipped_ids = [loc["id"] for loc in all_locations if not loc.get("active", True)]
        print(f"[Inventory] Skipping {len(all_locations) - len(locations)} inactive location(s): {skipped_ids}")

    primary_location_id = locations[0]["id"]
    other_location_ids = [loc["id"] for loc in locations[1:]]

    if len(locations) > 1:
        print(f"[Inventory] Multi-location store ({len(locations)} active). "
              f"Primary={primary_location_id}, zeroing others={other_location_ids}")

    variant_ae_map = {}
    for variant in shopify_variants:
        vid = variant["id"]
        try:
            mf_res = _shopify_request(
                "GET", f"{_base(shop.shop_domain)}/variants/{vid}/metafields.json",
                params={"namespace": "aliexpress", "key": "sku_id"}, headers=_h(shop.access_token),
            )
            if mf_res.status_code == 200:
                mfs = mf_res.json().get("metafields", [])
                if mfs:
                    variant_ae_map[vid] = mfs[0].get("value")
        except Exception as e:
            print(f"[Inventory] Metafield error for variant {vid}: {e}")

    stock_by_label = {}
    for sku in aliexpress_skus:
        label = (sku.get("label") or sku.get("sku_attr") or "").strip().lower()
        stock = sku.get("stock")
        if label and stock is not None:
            try:
                stock_by_label[label] = int(stock)
            except (ValueError, TypeError):
                continue

    def _variant_label(variant: dict) -> str:
        parts = [
            variant.get(f"option{i}")
            for i in (1, 2, 3)
            if variant.get(f"option{i}") and variant.get(f"option{i}") != "Default Title"
        ]
        return " / ".join(parts).strip().lower()

    skipped_locked = 0
    variant_targets = {}

    for i, variant in enumerate(shopify_variants):
        if variant["id"] in locked_variant_ids:
            skipped_locked += 1
            continue

        new_stock = None
        ae_sku_id = variant_ae_map.get(variant["id"])

        if ae_sku_id and ae_sku_id in stock_by_ae_sku:
            new_stock = stock_by_ae_sku[ae_sku_id]
        else:
            label = _variant_label(variant)
            if label and label in stock_by_label:
                new_stock = stock_by_label[label]
            elif i < len(aliexpress_skus):
                stock_val = aliexpress_skus[i].get("stock")
                if stock_val is not None:
                    try:
                        new_stock = int(stock_val)
                    except (ValueError, TypeError):
                        new_stock = None

        if new_stock is None:
            continue

        inventory_item_id = variant.get("inventory_item_id")
        current_stock = variant.get("inventory_quantity")
        if not inventory_item_id:
            continue
        if current_stock == new_stock and len(locations) <= 1:
            continue

        variant_targets[inventory_item_id] = new_stock

    if skipped_locked:
        print(f"[Inventory] Skipped {skipped_locked} locked variant(s) for product {shopify_product_id}")

    if not variant_targets:
        print("[Inventory] No inventory changes needed")
        return False

    stocked_locations = _get_stocked_location_map(shop, list(variant_targets), {loc["id"] for loc in locations})
    bulk_quantities = []
    for inventory_item_id, new_stock in variant_targets.items():
        item_locations = stocked_locations.get(str(inventory_item_id), [])
        if not item_locations:
            print(f"[Inventory] Item {inventory_item_id} is not stocked at any active location; skipped")
            continue
        item_primary = primary_location_id if primary_location_id in item_locations else item_locations[0]
        bulk_quantities.append({
            "inventory_item_id": inventory_item_id,
            "location_id": item_primary,
            "quantity": new_stock,
        })
        for other_loc_id in item_locations:
            if other_loc_id == item_primary:
                continue
            bulk_quantities.append({
                "inventory_item_id": inventory_item_id,
                "location_id": other_loc_id,
                "quantity": 0,
            })

    if not bulk_quantities:
        print("[Inventory] No valid stocked item/location pairs found")
        return False
    result = bulk_set_inventory_quantities(shop, bulk_quantities)
    if result["success"]:
        print(f"[Inventory] Bulk-updated {len(variant_targets)} variant(s) for product {shopify_product_id} "
              f"({len(bulk_quantities)} item/location pairs in 1 API call)")
        return True
    else:
        print(f"[Inventory] Bulk update failed: {result['errors']}")
        return False


def set_product_out_of_stock(shop, shopify_product_id: str) -> bool:
    try:
        res = _shopify_request(
            "GET", f"{_base(shop.shop_domain)}/products/{shopify_product_id}.json",
            params={"fields": "id,variants"}, headers=_h(shop.access_token), timeout=15,
        )
        res.raise_for_status()
        variants = res.json().get("product", {}).get("variants", [])
        if not variants:
            print(f"[DeadStock] No variants found for {shopify_product_id}")
            return False
    except Exception as e:
        print(f"[DeadStock] Fetch variants failed: {e}")
        return False

    try:
        loc_res = _shopify_request("GET", f"{_base(shop.shop_domain)}/locations.json", headers=_h(shop.access_token), timeout=15)
        loc_res.raise_for_status()
        all_locations = loc_res.json().get("locations", [])
    except Exception as e:
        print(f"[DeadStock] Failed to fetch locations: {e}")
        return False

    locations = [loc for loc in all_locations if loc.get("active", True)]
    if not locations:
        print("[DeadStock] No active Shopify locations found")
        return False

    locked_variant_ids = get_locked_variant_ids(shop, shopify_product_id, "inventory")
    if locked_variant_ids:
        print(f"[DeadStock] {len(locked_variant_ids)} variant(s) inventory-locked — will be skipped: {locked_variant_ids}")

    success_count = 0
    skipped_locked = 0
    policy_failures = 0

    for variant in variants:
        if variant["id"] in locked_variant_ids:
            skipped_locked += 1
            continue

        inventory_item_id = variant.get("inventory_item_id")
        if not inventory_item_id:
            continue

        variant_ok = True
        for loc in locations:
            try:
                res2 = _shopify_request(
                    "POST", f"{_base(shop.shop_domain)}/inventory_levels/set.json",
                    json={"location_id": loc["id"], "inventory_item_id": inventory_item_id, "available": 0},
                    headers=_h(shop.access_token), timeout=20,
                )
                if res2.status_code not in (200, 422):
                    variant_ok = False
                    print(f"[DeadStock] Failed to zero variant {variant['id']} @ location {loc['id']}: {res2.text}")
            except Exception as e:
                variant_ok = False
                print(f"[DeadStock] Error zeroing variant {variant['id']} @ location {loc['id']}: {e}")

        try:
            policy_res = _shopify_request(
                "PUT", f"{_base(shop.shop_domain)}/variants/{variant['id']}.json",
                json={"variant": {"id": variant["id"], "inventory_policy": "deny"}},
                headers=_h(shop.access_token), timeout=20,
            )
            if policy_res.status_code != 200:
                variant_ok = False
                policy_failures += 1
                print(f"[DeadStock] Failed to set inventory_policy=deny for variant {variant['id']}: {policy_res.text}")
        except Exception as e:
            variant_ok = False
            policy_failures += 1
            print(f"[DeadStock] Error setting inventory_policy for variant {variant['id']}: {e}")

        if variant_ok:
            success_count += 1

    if skipped_locked:
        print(f"[DeadStock] Skipped {skipped_locked} inventory-locked variant(s) for product {shopify_product_id}")
    if policy_failures:
        print(f"[DeadStock] {policy_failures} variant(s) had inventory_policy update failures")

    print(f"[DeadStock] Zeroed inventory + set deny-policy for {success_count}/{len(variants)} variant(s) of product {shopify_product_id}")
    return success_count > 0


# ─────────────────────────────────────────────
# VARIANT LOCK (manual override protection)
# ─────────────────────────────────────────────
_lock_cache: dict[str, tuple[float, dict]] = {}
_LOCK_CACHE_TTL = 60
LOCK_TYPES = ("price", "inventory", "image")


def get_variant_lock_map(shop, shopify_product_id: str, use_cache: bool = True) -> dict:
    cache_key = f"{shop.shop_domain}:{shopify_product_id}"
    if use_cache:
        cached = _lock_cache.get(cache_key)
        if cached and (_time.time() - cached[0]) < _LOCK_CACHE_TTL:
            return cached[1]

    lock_map = {}
    query = """
    query($id: ID!) {
      product(id: $id) {
        variants(first: 100) {
          edges {
            node {
              id
              metafields(namespace: "sync", first: 10) {
                edges { node { key value } }
              }
            }
          }
        }
      }
    }
    """
    try:
        data = _graphql(shop, query, {"id": _product_gid(shopify_product_id)})
        product = data.get("product") or {}
        edges = product.get("variants", {}).get("edges", [])
        for edge in edges:
            node = edge["node"]
            vid = int(_numeric_id_from_gid(node["id"]))
            flags = {"price": False, "inventory": False, "image": False}
            for mf_edge in node.get("metafields", {}).get("edges", []):
                mf = mf_edge["node"]
                key = mf.get("key", "")
                if key.endswith("_locked") and mf.get("value") == "true":
                    lt = key.replace("_locked", "")
                    if lt in flags:
                        flags[lt] = True
            lock_map[vid] = flags
    except Exception as e:
        print(f"[Lock] GraphQL fetch failed for {shopify_product_id}: {e}")
        return lock_map

    _lock_cache[cache_key] = (_time.time(), lock_map)
    return lock_map


def _invalidate_lock_cache(shop, shopify_product_id: str):
    _lock_cache.pop(f"{shop.shop_domain}:{shopify_product_id}", None)


def get_locked_variant_ids(shop, shopify_product_id: str, lock_type: str) -> set:
    return {vid for vid, flags in get_variant_lock_map(shop, shopify_product_id).items() if flags.get(lock_type)}


def is_variant_locked(shop, variant_id: int, lock_type: str) -> bool:
    try:
        res = _shopify_request(
            "GET", f"{_base(shop.shop_domain)}/variants/{variant_id}/metafields.json",
            params={"namespace": "sync", "key": f"{lock_type}_locked"}, headers=_h(shop.access_token),
        )
        if res.status_code != 200:
            return False
        mfs = res.json().get("metafields", [])
        return bool(mfs) and mfs[0].get("value") == "true"
    except Exception as e:
        print(f"[Lock] Check failed for variant {variant_id} ({lock_type}): {e}")
        return False


def set_variant_lock(shop, variant_id: int, lock_type: str, locked: bool) -> bool:
    if lock_type not in LOCK_TYPES:
        return False
    key = f"{lock_type}_locked"
    try:
        res = _shopify_request(
            "GET", f"{_base(shop.shop_domain)}/variants/{variant_id}/metafields.json",
            params={"namespace": "sync", "key": key}, headers=_h(shop.access_token),
        )
        existing = res.json().get("metafields", []) if res.status_code == 200 else []
        payload = {"metafield": {"namespace": "sync", "key": key, "value": "true" if locked else "false", "type": "single_line_text_field"}}
        if existing:
            r2 = _shopify_request("PUT", f"{_base(shop.shop_domain)}/metafields/{existing[0]['id']}.json", json=payload, headers=_h(shop.access_token))
        else:
            r2 = _shopify_request("POST", f"{_base(shop.shop_domain)}/variants/{variant_id}/metafields.json", json=payload, headers=_h(shop.access_token))
        r2.raise_for_status()
        print(f"[Lock] Variant {variant_id} {lock_type} {'locked' if locked else 'unlocked'}")
        return True
    except Exception as e:
        print(f"[Lock] Failed to set {lock_type} lock for variant {variant_id}: {e}")
        return False


# ─────────────────────────────────────────────
# GRAPHQL BULK OPERATIONS
# ─────────────────────────────────────────────
def bulk_update_variant_prices(shop, shopify_product_id: str, variant_prices: list) -> dict:
    """
    variant_prices: [{"variant_id": 123, "price": "19.99"}, ...]
    Updates ALL variant prices in ONE GraphQL mutation instead of N sequential REST PUTs.
    Returns {"success": bool, "updated": int, "errors": list}
    """
    if not variant_prices:
        return {"success": True, "updated": 0, "errors": []}

    mutation = """
    mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants, allowPartialUpdates: false) {
        productVariants { id price priceIncrease: metafield(namespace: "sync", key: "price_increase") { value }
          aeSku: metafield(namespace: "aliexpress", key: "sku_id") { value } }
        userErrors { field message }
      }
    }
    """
    variants_input = []
    for edit in variant_prices:
        row = {"id": _variant_gid(edit["variant_id"])}
        if "price" in edit:
            row["price"] = str(edit["price"])
        if "price_increase" in edit:
            metafield = {"value": str(edit["price_increase"])}
            if edit.get("increase_metafield_id"):
                metafield["id"] = edit["increase_metafield_id"]
            else:
                metafield.update({"namespace": "sync", "key": "price_increase", "type": "number_decimal"})
            row["metafields"] = [metafield]
        if "supplier_history" in edit:
            import json
            history = {"value": json.dumps(edit["supplier_history"])}
            if edit.get("supplier_history_id"):
                history["id"] = edit["supplier_history_id"]
            else:
                history.update({"namespace": "sync", "key": "supplier_price_history", "type": "json"})
            row.setdefault("metafields", []).append(history)
        if "ae_sku_id" in edit:
            row.setdefault("metafields", []).append({"namespace": "aliexpress", "key": "sku_id",
                "type": "single_line_text_field", "value": str(edit["ae_sku_id"])})
        variants_input.append(row)
    try:
        data = _graphql(shop, mutation, {
            "productId": _product_gid(shopify_product_id),
            "variants": variants_input,
        })
        result = data.get("productVariantsBulkUpdate", {})
        errors = result.get("userErrors", [])
        returned = {row["id"]: row for row in result.get("productVariants", [])}
        updated = len(returned)
        from decimal import Decimal
        for edit in variant_prices:
            if "ae_sku_id" in edit:
                saved = returned.get(_variant_gid(edit["variant_id"]), {})
                if (saved.get("aeSku") or {}).get("value") != str(edit["ae_sku_id"]):
                    errors.append({"message": f"SKU link was not confirmed for variant {edit['variant_id']}"})
            if "price_increase" in edit:
                saved = returned.get(_variant_gid(edit["variant_id"]), {})
                amount = (saved.get("priceIncrease") or {}).get("value")
                if amount is None or Decimal(amount) != Decimal(str(edit["price_increase"])):
                    errors.append({"message": f"Increase was not confirmed for variant {edit['variant_id']}"})
        if errors:
            print(f"[BulkPrice] userErrors: {errors}")
        return {"success": not errors and updated == len(variants_input), "updated": updated, "errors": errors}
    except Exception as e:
        print(f"[BulkPrice] Mutation failed: {e}")
        return {"success": False, "updated": 0, "errors": [str(e)]}


def bulk_set_inventory_quantities(shop, quantities: list) -> dict:
    if not quantities:
        return {"success": True, "errors": []}

    remaining = list(quantities)
    while remaining:
        result = _run_inventory_mutation(shop, remaining)
        if result["success"]:
            return result
        bad_indices = set()
        for err in result["errors"]:
            field = err.get("field", []) if isinstance(err, dict) else []
            if "quantities" in field:
                position = field.index("quantities") + 1
                try:
                    bad_indices.add(int(field[position]))
                except (ValueError, IndexError):
                    pass
        if not bad_indices:
            return result
        print(f"[BulkInventory] Retrying without {len(bad_indices)} invalid item/location entries")
        remaining = [q for i, q in enumerate(remaining) if i not in bad_indices]
    return {"success": False, "errors": ["No valid stocked inventory locations remained"]}


def _get_stocked_location_map(shop, inventory_item_ids: list, active_location_ids: set | None = None) -> dict:
    """Return {inventory_item_id: [location_id]} using Shopify's actual inventory levels."""
    result = {str(item_id): [] for item_id in inventory_item_ids}
    ids = [str(item_id) for item_id in inventory_item_ids if item_id]
    for offset in range(0, len(ids), 50):
        response = _shopify_request(
            "GET", f"{_base(shop.shop_domain)}/inventory_levels.json",
            params={"inventory_item_ids": ",".join(ids[offset:offset + 50]), "limit": 250},
            headers=_h(shop.access_token), timeout=20,
        )
        if response.status_code != 200:
            print(f"[Inventory] Failed to fetch stocked locations: {response.text}")
            continue
        for level in response.json().get("inventory_levels", []):
            item_id, location_id = str(level.get("inventory_item_id")), level.get("location_id")
            if item_id in result and location_id and (active_location_ids is None or location_id in active_location_ids):
                if location_id not in result[item_id]:
                    result[item_id].append(location_id)
    return result


def _run_inventory_mutation(shop, quantities: list) -> dict:
    mutation = """
    mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
      inventorySetQuantities(input: $input) {
        userErrors { field message }
      }
    }
    """
    input_quantities = [
        {
            "inventoryItemId": _inventory_item_gid(q["inventory_item_id"]),
            "locationId": _location_gid(q["location_id"]),
            "quantity": q["quantity"],
        }
        for q in quantities
    ]
    try:
        data = _graphql(shop, mutation, {
            "input": {
                "name": "available",
                "reason": "correction",
                "ignoreCompareQuantity": True,
                "quantities": input_quantities,
            }
        })
        errors = data.get("inventorySetQuantities", {}).get("userErrors", [])
        if errors:
            print(f"[BulkInventory] userErrors: {errors}")
        return {"success": not errors, "errors": errors}
    except Exception as e:
        print(f"[BulkInventory] Mutation failed: {e}")
        return {"success": False, "errors": [str(e)]}


def _set_variant_inventory_levels(shop, shopify_product_id: str, inventory_updates: dict) -> int:
    """inventory_updates: {variant_id:int -> qty:int}. Returns count updated."""
    vres = _shopify_request(
        "GET", f"{_base(shop.shop_domain)}/products/{shopify_product_id}.json",
        params={"fields": "id,variants"}, headers=_h(shop.access_token), timeout=15,
    )
    if vres.status_code != 200:
        return 0
    shopify_variants = vres.json().get("product", {}).get("variants", [])
    variant_inv_item_map = {v["id"]: v.get("inventory_item_id") for v in shopify_variants}

    loc_res = _shopify_request("GET", f"{_base(shop.shop_domain)}/locations.json", headers=_h(shop.access_token), timeout=15)
    all_locations = loc_res.json().get("locations", []) if loc_res.status_code == 200 else []
    locations = [loc for loc in all_locations if loc.get("active", True)]
    if not locations:
        return 0

    item_ids = [item_id for item_id in variant_inv_item_map.values() if item_id]
    stocked_locations = _get_stocked_location_map(shop, item_ids, {loc["id"] for loc in locations})
    quantities = []
    targeted_variants = 0
    for vid, qty in inventory_updates.items():
        inv_item_id = variant_inv_item_map.get(vid)
        if not inv_item_id:
            continue
        item_locations = stocked_locations.get(str(inv_item_id), [])
        if not item_locations:
            continue
        targeted_variants += 1
        primary = locations[0]["id"] if locations[0]["id"] in item_locations else item_locations[0]
        quantities.append({"inventory_item_id": inv_item_id, "location_id": primary, "quantity": qty})
        for location_id in item_locations:
            if location_id != primary:
                quantities.append({"inventory_item_id": inv_item_id, "location_id": location_id, "quantity": 0})

    if not quantities:
        return 0
    result = bulk_set_inventory_quantities(shop, quantities)
    return targeted_variants if result["success"] else 0


def _sku_label_key(label):
    # Preserve option boundaries and punctuation; normalize only presentation.
    label = str(label or "").casefold().translate(str.maketrans({
        "–": "-", "—": "-", "‑": "-", "−": "-",
    }))
    return tuple(sorted(re.sub(r"\s+", "", part) for part in label.split("/")))


def _match_supplier_variants(variants, skus):
    """Return one-to-one matches, never assigning by supplier array position.

    A unique complete option label repairs stale or previously positional links.
    Existing IDs still support custom Shopify titles. Ambiguities fail closed.
    """
    by_label, by_id = {}, {}
    for index, sku in enumerate(skus):
        # Also recognize option values produced by older imports (e.g. 29#72v lcd).
        legacy_label = " / ".join(part.split(":")[-1].strip()
                                  for part in (sku.get("sku_attr") or "").split(";") if ":" in part)
        for label in {_sku_label_key(sku.get("label")), _sku_label_key(legacy_label)}:
            if label != ("",):
                by_label.setdefault(label, []).append(index)
        if sku.get("sku_id") is not None:
            by_id.setdefault(str(sku["sku_id"]), []).append(index)
    proposed = {}
    for vid, variant in variants.items():
        label = _sku_label_key(variant.get("label"))
        candidates = by_label.get(label, [])
        id_candidates = by_id.get(str(variant.get("ae_sku_id")), [])
        if len(candidates) > 1:
            candidates = [i for i in candidates if i in id_candidates]
        elif not candidates:
            candidates = id_candidates
            if not candidates and len(variants) == len(skus) == 1:
                candidates = [0]
        if len(candidates) == 1:
            proposed[vid] = candidates[0]
    counts = {}
    for index in proposed.values():
        counts[index] = counts.get(index, 0) + 1
    return {vid: skus[index] for vid, index in proposed.items() if counts[index] == 1}


def get_variant_sync_state(shop, shopify_product_id: str) -> dict:
    """Read all prices, SKU links and rules together; never default failed reads."""
    query = """
    query($id: ID!, $after: String) {
      product(id: $id) {
        variants(first: 100, after: $after) {
          pageInfo { hasNextPage endCursor }
          edges { node {
            id price selectedOptions { name value }
            aeSku: metafield(namespace: "aliexpress", key: "sku_id") { value }
            increase: metafield(namespace: "sync", key: "price_increase") { id value }
            supplierHistory: metafield(namespace: "sync", key: "supplier_price_history") { id value }
            priceLock: metafield(namespace: "sync", key: "price_locked") { value }
            inventoryLock: metafield(namespace: "sync", key: "inventory_locked") { value }
            imageLock: metafield(namespace: "sync", key: "image_locked") { value }
          } }
        }
      }
    }
    """
    import json
    from decimal import Decimal
    state, cursor = {}, None
    while True:
        data = _graphql(shop, query, {"id": _product_gid(shopify_product_id), "after": cursor})
        if not data.get("product"):
            raise RuntimeError("Shopify variant pricing rules could not be read")
        connection = data["product"]["variants"]
        for edge in connection["edges"]:
            node = edge["node"]
            vid = int(_numeric_id_from_gid(node["id"]))
            increase = Decimal((node.get("increase") or {}).get("value", "0"))
            if not increase.is_finite():
                raise ValueError(f"Invalid saved increase for variant {vid}")
            state[vid] = {
                "id": vid, "price": node["price"],
                "ae_sku_id": (node.get("aeSku") or {}).get("value"),
                "price_increase": increase,
                "supplier_history": json.loads(node["supplierHistory"]["value"]) if node.get("supplierHistory") else None,
                "supplier_history_id": (node.get("supplierHistory") or {}).get("id"),
                "increase_metafield_id": (node.get("increase") or {}).get("id"),
                "label": " / ".join(option["value"] for option in node["selectedOptions"]
                                     if option["value"] != "Default Title"),
                "locks": {field: (node.get(field + "Lock") or {}).get("value") == "true"
                          for field in LOCK_TYPES},
            }
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            return state
        next_cursor = page["endCursor"]
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("Shopify variant pagination did not advance")
        cursor = next_cursor


def get_variant_price_increase_map(shop, shopify_product_id: str) -> dict[int, float]:
    return {vid: float(row["price_increase"])
            for vid, row in get_variant_sync_state(shop, shopify_product_id).items()
            if row["price_increase"]}


def save_variant_price_edits(shop, shopify_product_id: str, edits: list) -> list:
    """Save each final price and its persistent adjustment in the same mutation."""
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    state = get_variant_sync_state(shop, shopify_product_id)
    updates, seen = [], set()
    for edit in edits:
        try:
            vid = int(edit["variant_id"])
            price = Decimal(str(edit["price"]))
            if vid not in state or vid in seen:
                raise ValueError("Variant is missing from this product or duplicated")
            if not price.is_finite() or price < 0:
                raise ValueError("Price must be finite and non-negative")
            price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            previous = state[vid]
            # Older clients may submit an absolute price without an increase.
            # An absolute edit to a locked selling price preserves its markup.
            # Only an explicit increase changes that saved rule while locked.
            # For unlocked variants, derive the adjustment from the current price.
            increase = (Decimal(str(edit["price_increase"])) if "price_increase" in edit
                        else previous["price_increase"] if previous["locks"]["price"]
                        else previous["price_increase"] + price - Decimal(str(previous["price"])))
            if not increase.is_finite():
                raise ValueError("Increase must be finite")
            increase = increase.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            updates.append({"variant_id": vid, "price": str(price), "price_increase": str(increase),
                            "increase_metafield_id": previous.get("increase_metafield_id")})
            seen.add(vid)
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise HTTPException(400, f"Invalid variant price edit: {exc}") from exc
    if not updates:
        raise HTTPException(400, "No variant price edits provided")
    # Validate the complete request before writing any batches.
    for offset in range(0, len(updates), 100):
        result = bulk_update_variant_prices(shop, shopify_product_id, updates[offset:offset + 100])
        if not result["success"]:
            raise HTTPException(502, f"Failed to save variant prices and increases: {result['errors']}")
    _invalidate_lock_cache(shop, shopify_product_id)
    return updates
