from .subscriptions import require_any_full, require_bulk
from .subscriptions import reserve_product, has_full_access, require_access, require_entity, move_access, subscription_summary, account
# main.py — complete multi-tenant version
import traceback
import copy
import hashlib
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import or_
import math
import re
import requests
from sqlalchemy.sql import func as sqlfunc
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .database import get_db, engine, SessionLocal
from .models import ImportedProduct, Base, PendingImport, ProductMapping, Shop, SyncLog
from .shop_context import get_current_shop
from .aliexpress import get_product, get_shipping_info
from .shopify import (
    create_shopify_product,
    check_product_exists_in_shopify,
    search_shopify_product_titles,
    update_shopify_product_price,
    update_shopify_product_prices_with_skus,
    update_shopify_product_inventory_with_skus,
    store_aliexpress_sku_ids,
    increase_shopify_product_price,
    get_all_shopify_imported_products,
    set_product_out_of_stock,
    backfill_sku_images,
    get_locked_variant_ids,
    is_variant_locked,
    set_variant_lock,
    get_variant_lock_map,
    get_variant_sku_details,
    _invalidate_lock_cache,
    _base, _h, _shopify_request,
    bulk_update_variant_prices,
    get_variant_sync_state,
    save_variant_price_edits,
    bulk_set_inventory_quantities,
    _set_variant_inventory_levels,
)
from .remap_variants import replace_supplier_variants
from .config import get_settings
from .notifications import create_notification, get_preferences

settings = get_settings()
Base.metadata.create_all(bind=engine)

scheduler = None


def _safe_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    multiplier = 1
    if s[-1].upper() == "K":
        multiplier = 1000
        s = s[:-1]
    elif s[-1].upper() == "M":
        multiplier = 1_000_000
        s = s[:-1]
    match = re.search(r"[\d,]+", s)
    if not match:
        return None
    digits = match.group(0).replace(",", "")
    try:
        return int(float(digits) * multiplier)
    except (ValueError, TypeError):
        return None


def _apply_product_price_increase(skus: list, amount, percentage=0) -> list:
    """Keep the supplier observation separate from the product-wide markup."""
    from decimal import Decimal
    adjusted = copy.deepcopy(skus)
    for sku in adjusted:
        price = sku.get("sale_price") or sku.get("price")
        if price is not None and (amount or percentage):
            sku.setdefault("_supplier_price", price)
            sku["sale_price"] = sku["price"] = str(Decimal(str(price)) * (Decimal("1") + Decimal(str(percentage or 0)) / Decimal("100")) + Decimal(str(amount or 0)))
    return adjusted


def _ali_sale_price_display(data: dict) -> dict:
    """Summarize supplier prices for display without changing sync pricing."""
    from decimal import Decimal, InvalidOperation

    skus = data.get("skus") or []
    currency = data.get("currency") or next(
        (sku.get("currency") for sku in skus if sku.get("currency")), None
    )
    price = data.get("sale_price")
    if price is None or price == "":
        prices = []
        for sku in skus:
            value = sku.get("sale_price")
            if value is None or value == "":
                value = sku.get("price")
            try:
                amount = Decimal(str(value))
                if amount.is_finite() and amount >= 0:
                    prices.append(amount)
            except (InvalidOperation, ValueError, TypeError):
                continue
        if prices:
            low, high = min(prices), max(prices)
            price = format(low, "f") if low == high else f"{low:f} - {high:f}"
        else:
            price = data.get("original_price")
    return {"ali_sale_price": price, "currency": currency}


def is_listing_dead(raw_product: dict) -> bool:
    sale_price = raw_product.get("sale_price")
    original_price = raw_product.get("original_price")
    skus = raw_product.get("skus") or []
    if sale_price is None and original_price is None:
        sku_prices = [s.get("sale_price") or s.get("price") for s in skus]
        if not any(sku_prices):
            return True
    return False


def is_definitively_missing(raw_product: dict) -> bool:
    """Identify a successful AliExpress response that contains no product at all."""
    return not any([
        raw_product.get("product_id"), raw_product.get("title"),
        raw_product.get("sale_price"), raw_product.get("original_price"),
        raw_product.get("main_image"), raw_product.get("all_images"),
        raw_product.get("skus"),
    ])


DEAD_CONFIRMATION_CHECKS = 3


def _record_supplier_check(entity, confirmed_signal: bool, failure_reason: str | None = None) -> str:
    """Advance a persisted verification state without acting on one failed check."""
    now = datetime.now(timezone.utc)
    entity.last_verification_at = now
    if not confirmed_signal:
        entity.supplier_status = "active"
        entity.verification_attempts = 0
        entity.last_failure_reason = None
        entity.is_dead_listing = False
        entity.last_successful_sync_at = now
        return entity.supplier_status
    entity.verification_attempts = (entity.verification_attempts or 0) + 1
    entity.last_failure_reason = failure_reason or "Supplier listing returned no purchasable price"
    if entity.verification_attempts == 1:
        entity.supplier_status = "sync_warning"
    elif entity.verification_attempts < DEAD_CONFIRMATION_CHECKS:
        entity.supplier_status = "verification_in_progress"
    else:
        entity.supplier_status = "confirmed_dead"
        entity.is_dead_listing = True
    return entity.supplier_status


def _record_supplier_error(entity, reason: str) -> str:
    entity.last_verification_at = datetime.now(timezone.utc)
    entity.verification_attempts = (entity.verification_attempts or 0) + 1
    entity.last_failure_reason = reason[:2000]
    if entity.verification_attempts >= DEAD_CONFIRMATION_CHECKS:
        entity.supplier_status = "confirmed_dead"
        entity.is_dead_listing = True
    else:
        entity.is_dead_listing = False
        entity.supplier_status = "sync_warning" if entity.verification_attempts == 1 else "supplier_unavailable"
    return entity.supplier_status


def _record_definitive_missing(entity) -> str:
    """A structurally empty DS response is stronger than a transient request error."""
    entity.last_verification_at = datetime.now(timezone.utc)
    entity.verification_attempts = DEAD_CONFIRMATION_CHECKS
    entity.last_failure_reason = "AliExpress returned no product data; the supplier listing is unavailable"
    entity.supplier_status = "confirmed_dead"
    entity.is_dead_listing = True
    return entity.supplier_status


def _protect_confirmed_dead(shop, entity) -> bool:
    if entity.supplier_status != "confirmed_dead" or not entity.auto_inventory_sync or not entity.shopify_product_id:
        return False
    protected = bool(set_product_out_of_stock(shop, entity.shopify_product_id))
    if protected:
        entity.inventory_protected = True
        if hasattr(entity, "total_stock"):
            entity.total_stock = 0
    return protected


def _notify_supplier_state(db: Session, shop: Shop, entity_type: str, entity) -> None:
    status = entity.supplier_status
    if status == "active":
        return
    product_name = (getattr(entity, "custom_title", None) or getattr(entity, "original_title", None)
                    or getattr(entity, "shopify_product_title", None) or f"AliExpress {entity.aliexpress_id}")
    critical = status in {"confirmed_dead", "supplier_unavailable"} and (entity.verification_attempts or 0) >= DEAD_CONFIRMATION_CHECKS
    event_type = "dead_listing" if status == "confirmed_dead" else "repeated_sync_failure" if critical else "sync_failed"
    category = "dead_listings" if status == "confirmed_dead" else "sync"
    priority = "critical" if critical or status == "confirmed_dead" else "warning"
    action = ("Shopify inventory was set to 0; the Shopify product was preserved."
              if entity.inventory_protected else "No Shopify product or inventory change was made.")
    create_notification(db, user_id=shop.user_id, shop_id=shop.id, entity_type=entity_type,
        entity_id=str(entity.id), event_type=event_type, category=category, priority=priority,
        title=("Dead supplier listing confirmed" if status == "confirmed_dead" else
               "Supplier connection problem" if critical else "Supplier sync needs verification"),
        message=(f"{product_name}: supplier status is {status.replace('_', ' ')} "
                 f"after {entity.verification_attempts}/{DEAD_CONFIRMATION_CHECKS} checks."),
        product_name=product_name, product_image=getattr(entity, "main_image", None),
        old_value=None, new_value={"supplier_status": status}, automatic_action=action,
        merchant_action_required=status in {"confirmed_dead", "supplier_unavailable"},
        action_label="Relink Supplier" if status == "confirmed_dead" else "Review",
        action_url="dead-listings", supplier_url=f"https://www.aliexpress.com/item/{entity.aliexpress_id}.html",
        shopify_url=(f"https://{shop.shop_domain}/admin/products/{entity.shopify_product_id}" if entity.shopify_product_id else None),
        dedupe_key=f"supplier:{entity_type}:{entity.id}:{status}")


def _notify_product_changes(db: Session, shop: Shop, product, new_skus: list[dict]) -> bool:
    """Compare supplier snapshots and group normal variant changes per product."""
    old_skus = product.skus or []
    if not old_skus:
        return False
    prefs = get_preferences(db, shop.user_id)
    key = lambda sku: str(sku.get("sku_id") or sku.get("sku_attr") or sku.get("label") or "default")
    old_map = {key(sku): sku for sku in old_skus}
    price_changes, inventory_changes, oos, restored, low = [], [], [], [], []
    for sku in new_skus:
        sku_key = key(sku); old = old_map.get(sku_key)
        if not old:
            continue
        label = sku.get("label") or sku.get("sku_attr") or sku_key
        try:
            old_price = float(old.get("sale_price") or old.get("price")); new_price = float(sku.get("sale_price") or sku.get("price"))
            if old_price != new_price:
                price_changes.append({"variant": label, "old": old_price, "new": new_price,
                                      "percent": round(abs(new_price-old_price)/old_price*100, 2) if old_price else None})
        except (TypeError, ValueError):
            pass
        try:
            old_stock = int(old.get("stock")); new_stock = int(sku.get("stock"))
            if old_stock != new_stock:
                inventory_changes.append({"variant": label, "old": old_stock, "new": new_stock})
            if old_stock > 0 and new_stock == 0: oos.append(label)
            if old_stock == 0 and new_stock > 0: restored.append({"variant": label, "quantity": new_stock})
            if 0 < new_stock <= prefs.low_stock_threshold: low.append({"variant": label, "quantity": new_stock})
        except (TypeError, ValueError):
            pass
    name = product.custom_title or product.original_title or f"AliExpress {product.aliexpress_id}"
    base = dict(user_id=shop.user_id, shop_id=shop.id, entity_type="product", entity_id=str(product.id),
                product_name=name, product_image=product.main_image, action_label="View Product", action_url="products",
                supplier_url=f"https://www.aliexpress.com/item/{product.aliexpress_id}.html",
                shopify_url=(f"https://{shop.shop_domain}/admin/products/{product.shopify_product_id}"
                             if product.shopify_product_id else None))
    pause_price_update = False
    if price_changes:
        largest = max((change.get("percent") or 0 for change in price_changes), default=0)
        major = largest >= prefs.large_price_change_percent
        pause_price_update = major and prefs.pause_large_price_updates
        create_notification(db, **base, event_type="large_price_change" if major else "supplier_price_changed",
            category="pricing", priority="warning" if major else "information",
            title="Major supplier price change" if major else "Supplier price changed",
            message=f"Supplier prices changed for {len(price_changes)} variant(s); largest change: {largest:.1f}%.",
            old_value={"variants": [{"variant": x["variant"], "price": x["old"]} for x in price_changes]},
            new_value={"variants": [{"variant": x["variant"], "price": x["new"]} for x in price_changes]},
            automatic_action=("Shopify price update requires approval." if major and prefs.pause_large_price_updates
                              else "Shopify prices were recalculated using the active pricing rule."),
            merchant_action_required=major and prefs.pause_large_price_updates,
            dedupe_key=f"price:product:{product.id}:{hashlib.sha256(str(price_changes).encode()).hexdigest()[:16]}")
    if oos:
        create_notification(db, **base, event_type="supplier_out_of_stock", category="inventory", priority="critical",
            title="Supplier variant out of stock", message=f"Supplier stock reached 0 for {len(oos)} variant(s): {', '.join(oos[:5])}.",
            old_value={"available": oos}, new_value={"stock": 0},
            automatic_action=("Matching Shopify inventory was synchronized to 0." if product.auto_inventory_sync
                              else "Auto Inventory Sync is off; no Shopify change was made."),
            merchant_action_required=True,
            dedupe_key=f"oos:product:{product.id}:{','.join(sorted(oos))}")
    if restored:
        create_notification(db, **base, event_type="supplier_back_in_stock", category="inventory", priority="information",
            title="Supplier product back in stock", message=f"{len(restored)} variant(s) are available again.",
            old_value={"stock": 0}, new_value={"variants": restored},
            automatic_action=("Shopify inventory was restored." if prefs.auto_restore_inventory
                              else "Auto Restore is off; Shopify inventory was not changed."),
            merchant_action_required=not prefs.auto_restore_inventory,
            dedupe_key=f"restored:product:{product.id}:{','.join(sorted(x['variant'] for x in restored))}")
    if low:
        create_notification(db, **base, event_type="low_supplier_stock", category="inventory", priority="warning",
            title="Low supplier stock", message=f"{len(low)} variant(s) are at or below your {prefs.low_stock_threshold}-unit threshold.",
            new_value={"variants": low}, automatic_action="No forced Shopify change was made.",
            dedupe_key=f"low-stock:product:{product.id}:{hashlib.sha256(str(low).encode()).hexdigest()[:16]}")
    return pause_price_update


def classify_skus(product_data: dict) -> dict:
    skus = product_data.get("skus") or []
    if not skus:
        return {"in_stock": [], "out_of_stock": [], "unknown": [], "any_in_stock": False,
                "all_in_stock": False, "any_oos": False, "no_info": True}
    in_stock, out_of_stock, unknown = [], [], []
    for sku in skus:
        stock_val = sku.get("stock")
        try:
            stock = int(stock_val) if stock_val is not None else None
        except (ValueError, TypeError):
            stock = None
        entry = {"sku_id": sku.get("sku_id"), "label": sku.get("label") or sku.get("sku_attr") or "—",
                  "stock": stock, "sale_price": sku.get("sale_price"), "price": sku.get("price")}
        if stock is None:
            unknown.append(entry)
        elif stock > 0:
            in_stock.append(entry)
        else:
            out_of_stock.append(entry)
    no_info = (len(unknown) == len(skus) and len(out_of_stock) == 0)
    return {"in_stock": in_stock, "out_of_stock": out_of_stock, "unknown": unknown,
            "any_in_stock": len(in_stock) > 0, "all_in_stock": len(out_of_stock) == 0 and len(unknown) == 0,
            "any_oos": len(out_of_stock) > 0, "no_info": no_info}


def upsert_pending(shop, aliexpress_id: str, raw_product: dict, stock_info: dict, status: str, db) -> PendingImport:
    if stock_info["any_oos"]:
        status = "partial" if stock_info["any_in_stock"] else "pending"
    record = db.query(PendingImport).filter(
        PendingImport.shop_id == shop.id, PendingImport.aliexpress_id == aliexpress_id
    ).first()
    if record:
        record.product_data = raw_product
        record.out_of_stock_skus = stock_info["out_of_stock"]
        record.in_stock_skus = stock_info["in_stock"]
        record.status = status
        record.retry_count = 0
    else:
        record = PendingImport(user_id=shop.user_id, shop_id=shop.id, aliexpress_id=aliexpress_id, product_data=raw_product,
                                out_of_stock_skus=stock_info["out_of_stock"], in_stock_skus=stock_info["in_stock"],
                                status=status)
        db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _fill_product_fields(product: ImportedProduct, raw_product: dict):
    product.original_title = raw_product.get("title") or product.original_title or "AliExpress Product"
    product.title = product.custom_title or product.original_title
    product.original_price = raw_product.get("sale_price") or raw_product.get("original_price")
    product.currency = raw_product.get("currency")
    product.main_image = raw_product.get("main_image")
    product.store_name = raw_product.get("store_name")
    product.avg_rating = raw_product.get("avg_rating")
    product.review_count = _safe_int(raw_product.get("review_count"))
    product.orders = _safe_int(raw_product.get("orders"))
    product.sku_count = raw_product.get("sku_count")
    product.skus = raw_product.get("skus")
    product.all_images = raw_product.get("all_images")
    product.track_price = True


def _assert_shopify_link_available(db: Session, shop_id: int, shopify_id: str, aliexpress_id: str,
                                    exclude_product_id: int | None = None) -> None:
    mapping = db.query(ProductMapping).filter(
        ProductMapping.shop_id == shop_id, ProductMapping.shopify_product_id == shopify_id
    ).first()
    if mapping and mapping.aliexpress_id != aliexpress_id:
        raise HTTPException(409, f"Shopify product {shopify_id} is already linked to AliExpress "
                                 f"{mapping.aliexpress_id} in Sync Mappings")
    products = db.query(ImportedProduct).filter(
        ImportedProduct.shop_id == shop_id, ImportedProduct.shopify_product_id == shopify_id
    )
    if exclude_product_id is not None:
        products = products.filter(ImportedProduct.id != exclude_product_id)
    product = products.first()
    if product and product.aliexpress_id != aliexpress_id:
        raise HTTPException(409, f"Shopify product {shopify_id} is already linked to AliExpress "
                                 f"{product.aliexpress_id} in Imported Products")


def import_aliexpress_product_to_shopify(shop, raw_product: dict, db: Session) -> tuple[ImportedProduct, bool]:
    aliexpress_id = raw_product.get("product_id")
    if not aliexpress_id:
        raise HTTPException(400, "Missing product_id in AliExpress data")

    reserve_product(db, shop, aliexpress_id)

    mapping_exists = db.query(ProductMapping).filter(
        ProductMapping.shop_id == shop.id, ProductMapping.aliexpress_id == aliexpress_id
    ).first()
    if mapping_exists:
        raise HTTPException(409, detail=f"AliExpress ID {aliexpress_id} is already mapped to Shopify product "
                                         f"{mapping_exists.shopify_product_id}. Use Sync Mappings instead.")

    existing = db.query(ImportedProduct).filter(
        ImportedProduct.shop_id == shop.id, ImportedProduct.aliexpress_id == aliexpress_id
    ).first()
    if existing and existing.shopify_product_id:
        raise HTTPException(409, "Product already imported to Shopify")

    title = raw_product.get("title")
    if title and check_product_exists_in_shopify(shop, title):
        try:
            res = _shopify_request("GET", f"{_base(shop.shop_domain)}/products.json",
                params={"title": title, "limit": 1, "fields": "id,title,status"}, headers=_h(shop.access_token), timeout=15)
            res.raise_for_status()
            products = res.json().get("products", [])
            if products:
                shopify_id = str(products[0]["id"])
                shopify_status = products[0].get("status", "draft")
                _assert_shopify_link_available(db, shop.id, shopify_id, aliexpress_id,
                                               existing.id if existing else None)
                if existing:
                    existing.shopify_product_id = shopify_id
                    existing.shopify_status = shopify_status
                    _fill_product_fields(existing, raw_product)
                    db.commit(); db.refresh(existing)
                    return existing, False
                else:
                    new_product = ImportedProduct(user_id=shop.user_id, shop_id=shop.id, aliexpress_id=aliexpress_id,
                        shopify_product_id=shopify_id, shopify_status=shopify_status, track_price=True)
                    _fill_product_fields(new_product, raw_product)
                    db.add(new_product); db.commit(); db.refresh(new_product)
                    return new_product, False
        except Exception as e:
            raise HTTPException(409, f"Product '{title}' already exists in Shopify but could not retrieve its ID: {e}")

    shopify_resp = create_shopify_product(shop, raw_product)
    shopify_product_new = shopify_resp.get("product", {})
    shopify_id = str(shopify_product_new.get("id"))
    shopify_status = shopify_product_new.get("status", "draft")

    if existing:
        existing.shopify_product_id = shopify_id
        existing.shopify_status = shopify_status
        _fill_product_fields(existing, raw_product)
        db.commit(); db.refresh(existing)
        return existing, True
    else:
        new_product = ImportedProduct(user_id=shop.user_id, shop_id=shop.id, aliexpress_id=aliexpress_id,
            shopify_product_id=shopify_id, shopify_status=shopify_status, track_price=True)
        _fill_product_fields(new_product, raw_product)
        db.add(new_product); db.commit(); db.refresh(new_product)
        return new_product, True


def sync_product_price(shop, product_id: int, db: Session) -> bool:
    product = db.query(ImportedProduct).filter(
        ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id
    ).first()
    if not product or not has_full_access(db, shop, product.aliexpress_id) or not product.track_price or not product.aliexpress_id:
        return False
    if product.price_mode == 'manual':
        return False

    def record_sync(status: str, message: str):
        db.add(SyncLog(user_id=shop.user_id, shop_id=shop.id, entity_type='product',
                       entity_id=str(product.id), action='synchronize', status=status, message=message))

    try:
        latest_data = get_product(product.aliexpress_id, db)
    except Exception as e:
        _record_supplier_error(product, str(e))
        record_sync('failed', f'Could not fetch supplier product: {e}')
        db.commit()
        print(f"[Sync] Failed to fetch product {product.aliexpress_id}: {e}")
        return False

    supplier_skus = copy.deepcopy(latest_data.get("skus", []))
    new_skus = copy.deepcopy(supplier_skus)
    if not new_skus:
        record_sync('failed', 'Supplier returned no variants')
        db.commit()
        return False

    if _notify_product_changes(db, shop, product, supplier_skus):
        record_sync('failed', 'Synchronization paused by notification rules')
        db.commit()
        return False

    new_skus = _apply_product_price_increase(new_skus, product.price_increase, product.price_percentage)

    if is_listing_dead(latest_data):
        _record_supplier_check(product, True)
        if product.supplier_status == "confirmed_dead":
            try:
                _protect_confirmed_dead(shop, product)
            except Exception as e:
                print(f"[Sync] Failed to protect confirmed dead listing {product.aliexpress_id}: {e}")
        record_sync('failed', 'Supplier listing is unavailable')
        db.commit()
        return False

    _record_supplier_check(product, False)

    shopify_updated = False
    if product.shopify_product_id:
        shopify_updated = update_shopify_product_prices_with_skus(shop, product.shopify_product_id, new_skus) in ("updated", "unchanged")
        if product.auto_inventory_sync:
            inventory_skus = copy.deepcopy(new_skus)
            prefs = get_preferences(db, shop.user_id)
            if not prefs.auto_restore_inventory:
                sku_key = lambda sku: str(sku.get("sku_id") or sku.get("sku_attr") or sku.get("label") or "default")
                old_map = {sku_key(sku): sku for sku in (product.skus or [])}
                for sku in inventory_skus:
                    old = old_map.get(sku_key(sku))
                    try:
                        if old and int(old.get("stock")) == 0 and int(sku.get("stock")) > 0:
                            sku["stock"] = 0
                    except (TypeError, ValueError):
                        pass
            update_shopify_product_inventory_with_skus(shop, product.shopify_product_id, inventory_skus)

    original_price = latest_data.get("sale_price") or latest_data.get("original_price")
    if original_price:
        product.original_price = original_price
    new_total_stock = latest_data.get("total_stock")
    if new_total_stock is not None:
        product.total_stock = new_total_stock
    product.skus = supplier_skus
    record_sync('success' if shopify_updated else 'failed',
                'Price and inventory synchronized' if shopify_updated else 'No Shopify update was completed')
    db.commit()
    return shopify_updated


def sync_existing_shopify_products_to_db(shop):
    db = SessionLocal()
    db.info["shop_id"] = shop.id
    try:
        shopify_products = get_all_shopify_imported_products(shop)
        for sp in shopify_products:
            aliexpress_id = sp.get("aliexpress_id")
            if not aliexpress_id:
                continue
            existing = db.query(ImportedProduct).filter(
                ImportedProduct.shop_id == shop.id, ImportedProduct.aliexpress_id == aliexpress_id
            ).first()
            if existing:
                if not existing.shopify_product_id:
                    _assert_shopify_link_available(db, shop.id, str(sp["shopify_id"]), aliexpress_id, existing.id)
                    existing.shopify_product_id = sp["shopify_id"]
                    existing.shopify_status = sp["status"]
                    db.commit()
                continue
            try:
                raw_product = get_product(aliexpress_id, db)
            except Exception as e:
                print(f"[Sync] Failed to fetch AliExpress product {aliexpress_id}: {e}")
                continue
            try:
                reserve_product(db, shop, aliexpress_id)
                _assert_shopify_link_available(db, shop.id, str(sp["shopify_id"]), aliexpress_id)
                new_product = ImportedProduct(user_id=shop.user_id, shop_id=shop.id, aliexpress_id=aliexpress_id,
                    shopify_product_id=sp["shopify_id"], shopify_status=sp["status"], track_price=True)
                _fill_product_fields(new_product, raw_product)
                new_product.original_title = raw_product.get("title") or sp["title"]
                db.add(new_product); db.commit()
            except Exception as e:
                db.rollback()
                print(f"[Sync] Failed to insert local record for {aliexpress_id}: {e}")
    except Exception as e:
        print(f"[Sync] Error syncing Shopify products for {shop.shop_domain}: {e}")
    finally:
        db.close()


def sync_all_tracked_products():
    db = SessionLocal()
    try:
        shops = db.query(Shop).filter(Shop.is_active == True).all()
        print(f"[Sync] Running for {len(shops)} active shop(s)")
        for shop in shops:
            db.info["shop_id"] = shop.id
            sync_existing_shopify_products_to_db(shop)
            products = db.query(ImportedProduct).filter(
                ImportedProduct.shop_id == shop.id, ImportedProduct.track_price == True
            ).all()
            print(f"[Sync][{shop.shop_domain}] Syncing {len(products)} product(s)")
            for p in products:
                if not has_full_access(db, shop, p.aliexpress_id):
                    continue
                sync_product_price(shop, p.id, db)
                try:
                    existing_pending = db.query(PendingImport).filter(
                        PendingImport.shop_id == shop.id, PendingImport.aliexpress_id == p.aliexpress_id,
                        PendingImport.status.in_(["pending", "partial"])
                    ).first()
                    if not existing_pending:
                        raw = get_product(p.aliexpress_id, db)
                        stock_info = classify_skus(raw)
                        if stock_info["out_of_stock"]:
                            upsert_pending(shop, p.aliexpress_id, raw, stock_info, "pending", db)
                except Exception as e:
                    print(f"[Sync] OOS check failed for {p.aliexpress_id}: {e}")
    except Exception as e:
        print(f"[Sync] Background task error: {e}")
    finally:
        db.close()


def sync_all_mapped_products_background():
    db = SessionLocal()
    try:
        shops = db.query(Shop).filter(Shop.is_active == True).all()
        for shop in shops:
            db.info["shop_id"] = shop.id
            mappings = db.query(ProductMapping).filter(
                ProductMapping.shop_id == shop.id, ProductMapping.track_price == True
            ).all()
            for m in mappings:
                if not has_full_access(db, shop, m.aliexpress_id):
                    continue
                if m.price_mode == "manual":
                    continue
                try:
                    raw = get_product(m.aliexpress_id, db)
                    if is_listing_dead(raw):
                        m.is_dead_listing = True
                        db.commit()
                        try:
                            set_product_out_of_stock(shop, m.shopify_product_id)
                        except Exception as e:
                            print(f"[Hourly] Failed to zero stock for {m.aliexpress_id}: {e}")
                        continue
                    if m.is_dead_listing:
                        m.is_dead_listing = False
                        db.commit()
                    skus = raw.get("skus", [])
                    if not skus:
                        continue
                    skus = _apply_product_price_increase(skus, m.price_increase, m.price_percentage)
                    update_shopify_product_prices_with_skus(shop, m.shopify_product_id, skus)
                    update_shopify_product_inventory_with_skus(shop, m.shopify_product_id, skus)
                    print(f"[Hourly][{shop.shop_domain}] Updated {m.aliexpress_id} (mode={m.price_mode})")
                except Exception as e:
                    print(f"[Hourly] Error for {m.aliexpress_id}: {e}")
    finally:
        db.close()


def process_pending_imports(shop_id: int | None = None):
    from .shop_context import ensure_shopify_access_token
    db = SessionLocal()
    try:
        query = db.query(PendingImport).filter(PendingImport.status.in_(["pending", "partial", "failed"]))
        if shop_id is not None:
            query = query.filter(PendingImport.shop_id == shop_id)
        for pending in query.all():
            shop = db.query(Shop).filter(Shop.id == pending.shop_id,
                Shop.user_id == pending.user_id, Shop.is_active == True).first()
            if not shop or not shop.access_token:
                continue
            db.info["shop_id"] = shop.id
            db.info["user_id"] = shop.user_id
            if not has_full_access(db, shop, pending.aliexpress_id):
                continue
            try:
                raw_product = get_product(pending.aliexpress_id, db)
                stock_info = classify_skus(raw_product)
                pending.product_data = raw_product
                pending.out_of_stock_skus = stock_info["out_of_stock"]
                pending.in_stock_skus = stock_info["in_stock"]
                if stock_info["any_oos"]:
                    pending.status = "partial" if stock_info["any_in_stock"] else "pending"
                pending.last_checked = sqlfunc.now()
                pending.retry_count = (pending.retry_count or 0) + 1
                db.commit()
                imported = db.query(ImportedProduct).filter(
                    ImportedProduct.shop_id == shop.id, ImportedProduct.aliexpress_id == pending.aliexpress_id
                ).first()
                if not imported or not imported.shopify_product_id:
                    if not stock_info["any_in_stock"]:
                        continue
                    shop = ensure_shopify_access_token(shop, db)
                    product, created = import_aliexpress_product_to_shopify(shop, raw_product, db)
                    pending.status = "imported" if stock_info["all_in_stock"] else "partial"
                else:
                    if not imported.auto_inventory_sync:
                        continue
                    skus = raw_product.get("skus") or []
                    if not skus or stock_info["no_info"]:
                        continue
                    shop = ensure_shopify_access_token(shop, db)
                    if not update_shopify_product_inventory_with_skus(shop, imported.shopify_product_id, skus):
                        pending.error_message = "Shopify inventory update failed; will retry automatically."
                        db.commit()
                        continue
                    pending.status = ("imported" if stock_info["all_in_stock"] else
                                      "partial" if stock_info["any_in_stock"] else "pending")
                pending.error_message = None
                db.commit()
            except Exception as exc:
                db.rollback()
                pending.error_message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                db.commit()
                print(f"[Pending] Error processing {pending.aliexpress_id}: {pending.error_message}")
    finally:
        db.close()


def _job_listener(event):
    if event.exception:
        print(f"[Scheduler][ERROR] Job '{event.job_id}' failed: {event.exception}")
    else:
        print(f"[Scheduler][OK] Job '{event.job_id}' completed successfully")


def start_scheduler():
    global scheduler
    print("[Scheduler] Starting scheduler...")
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_listener(_job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        scheduler.add_job(sync_all_tracked_products, trigger=IntervalTrigger(hours=1),
                           id="price_sync_job", replace_existing=True, misfire_grace_time=3600)
        scheduler.add_job(sync_all_mapped_products_background, trigger=IntervalTrigger(hours=1),
                           id="mapped_price_sync_job", replace_existing=True, misfire_grace_time=3600)
        scheduler.add_job(process_pending_imports, trigger=IntervalTrigger(minutes=30),
                           id="pending_import_job", replace_existing=True, misfire_grace_time=1800,
                           next_run_time=datetime.now(timezone.utc), coalesce=True, max_instances=1)
        scheduler.start()
        jobs = scheduler.get_jobs()
        print(f"[Scheduler] Started with {len(jobs)} job(s):")
        for j in jobs:
            print(f"  - {j.id} -> next run at {j.next_run_time}")
    except Exception as e:
        print(f"[Scheduler] ERROR: {e}")
        traceback.print_exc()
        scheduler = None


def shutdown_scheduler():
    if scheduler:
        scheduler.shutdown()
        print("[Scheduler] Stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    shutdown_scheduler()


api = APIRouter()


# ─────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────
@api.get("/dashboard/stats")
def dashboard_stats(db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    q = db.query(ImportedProduct).filter(ImportedProduct.shop_id == shop.id)
    total = q.count()
    draft = q.filter(ImportedProduct.shopify_status == "draft").count()
    active = q.filter(ImportedProduct.shopify_status == "active").count()
    modified_condition = (
        (ImportedProduct.custom_title.isnot(None)) | (ImportedProduct.custom_price.isnot(None)) |
        (ImportedProduct.custom_rating.isnot(None)) | (ImportedProduct.custom_description.isnot(None)) |
        (ImportedProduct.price_mode != 'auto')
    )
    modified = q.filter(modified_condition).count()
    not_dead = or_(ImportedProduct.supplier_status.is_(None), ImportedProduct.supplier_status != 'confirmed_dead')
    overview_dead = q.filter(ImportedProduct.supplier_status == 'confirmed_dead').count()
    overview_modified = q.filter(not_dead, modified_condition).count()
    entitled_ids = [p.id for p in q.all() if has_full_access(db, shop, p.aliexpress_id)]
    overview_auto = q.filter(not_dead, ~modified_condition, ImportedProduct.track_price.is_(True), ImportedProduct.id.in_(entitled_ids)).count()
    overview_paused = q.filter(not_dead, ~modified_condition, or_(ImportedProduct.track_price.is_(False), ~ImportedProduct.id.in_(entitled_ids))).count()
    overview_other = max(0, total - overview_dead - overview_modified - overview_auto - overview_paused)
    mappings = db.query(ProductMapping).filter(ProductMapping.shop_id == shop.id)
    linked_total = total + mappings.count()
    confirmed_dead = q.filter(ImportedProduct.supplier_status == "confirmed_dead").count() + mappings.filter(ProductMapping.supplier_status == "confirmed_dead").count()
    sync_issues = q.filter(ImportedProduct.supplier_status.in_(["sync_warning", "verification_in_progress", "supplier_unavailable"])).count() + mappings.filter(ProductMapping.supplier_status.in_(["sync_warning", "verification_in_progress", "supplier_unavailable"])).count()
    out_of_stock = q.filter(ImportedProduct.total_stock == 0).count()
    return {"total_imported": total, "draft": draft, "active": active, "modified": modified,
            "total_linked": linked_total, "out_of_stock": out_of_stock,
            'confirmed_dead': confirmed_dead, 'sync_issues': sync_issues,
            'status_overview': {'total': total, 'auto_sync': overview_auto, 'paused': overview_paused,
                                'dead': overview_dead, 'modified': overview_modified, 'other': overview_other}}


@api.get('/dashboard/sync-activity')
def dashboard_sync_activity(db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    today = datetime.now(timezone.utc).date()
    first_day = today - timedelta(days=6)
    start = datetime.combine(first_day, datetime.min.time(), tzinfo=timezone.utc)
    rows = db.query(SyncLog).filter(SyncLog.shop_id == shop.id, SyncLog.created_at >= start).all()
    activity = {first_day + timedelta(days=i): {'total': 0, 'success': 0, 'failed': 0} for i in range(7)}
    for row in rows:
        created_at = row.created_at
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        day = created_at.astimezone(timezone.utc).date()
        if day not in activity:
            continue
        activity[day]['total'] += 1
        outcome = 'success' if (row.status or '').lower() in {'success', 'completed', 'ok'} else 'failed'
        activity[day][outcome] += 1
    total = sum(item['total'] for item in activity.values())
    successful = sum(item['success'] for item in activity.values())
    return {'days': [{'date': day.isoformat(), 'label': day.strftime('%a'), **counts} for day, counts in activity.items()],
            'total': total, 'successful': successful, 'failed': total - successful,
            'health_percent': round(successful * 100 / total) if total else None}


@api.get("/pricing-lock-summary/{entity_type}/{entity_id}")
def pricing_lock_summary(entity_type: str, entity_id: int, db: Session = Depends(get_db),
                         shop: Shop = Depends(get_current_shop)):
    model = ImportedProduct if entity_type == "imported" else ProductMapping if entity_type == "mapping" else None
    if model is None:
        raise HTTPException(400, "Invalid product type")
    entity = db.query(model).filter(model.id == entity_id, model.shop_id == shop.id,
                                    model.user_id == shop.user_id).first()
    if not entity:
        raise HTTPException(404, "Product not found")
    if not entity.shopify_product_id:
        return {"total": 0, "price_locked": 0}
    variants = get_variant_sync_state(shop, entity.shopify_product_id)
    return {"total": len(variants),
            "price_locked": sum(bool(v["locks"].get("price")) for v in variants.values())}


@api.get('/dashboard/products')
def list_products(page: int = Query(1, ge=1), page_size: int = Query(5, ge=1, le=100),
                   search: str = Query(None), db: Session = Depends(get_db),
                   shop: Shop = Depends(get_current_shop)):
    query = db.query(ImportedProduct).filter(ImportedProduct.shop_id == shop.id)
    shopify_titles = {}
    search_warning = None
    search = (search or "").strip()
    if search:
        try:
            shopify_titles = search_shopify_product_titles(shop, search)
        except Exception:
            search_warning = "Shopify title search is unavailable. Showing matches from saved titles and IDs."
        term = f"%{search}%"
        query = query.filter(
            (ImportedProduct.original_title.ilike(term)) | (ImportedProduct.custom_title.ilike(term)) |
            (ImportedProduct.aliexpress_id.ilike(term)) | (ImportedProduct.replacement_aliexpress_id.ilike(term)) |
            (ImportedProduct.shopify_product_id.ilike(term)) |
            (ImportedProduct.shopify_product_id.in_(list(shopify_titles)))
        )
    total = query.count()
    pages = math.ceil(total / page_size) if total > 0 else 1
    offset = (page - 1) * page_size
    products = query.order_by(ImportedProduct.imported_at.desc()).offset(offset).limit(page_size).all()

    result = []
    for p in products:
        result.append({
            "id": p.id, "aliexpress_id": p.aliexpress_id,
            "title": shopify_titles.get(str(p.shopify_product_id)) or p.custom_title or p.original_title,
            "original_title": p.original_title, "shopify_title": shopify_titles.get(str(p.shopify_product_id)),
            "price": p.custom_price or p.original_price,
            **_ali_sale_price_display({"original_price": p.original_price, "currency": p.currency, "skus": p.skus}),
            "main_image": p.main_image, "store_name": p.store_name,
            "avg_rating": p.custom_rating or p.avg_rating, "rating": p.custom_rating or p.avg_rating,
            "sku_count": p.sku_count, "shopify_status": p.shopify_status,
            "shopify_product_id": p.shopify_product_id,
            "imported_at": p.imported_at.isoformat() if p.imported_at else None,
            "custom_title": p.custom_title, "custom_price": p.custom_price,
            "custom_rating": p.custom_rating, "custom_description": p.custom_description,
            "track_price": p.track_price, "price_mode": p.price_mode, "price_increase": p.price_increase, "price_percentage": p.price_percentage,
            "replacement_aliexpress_id": p.replacement_aliexpress_id, "is_dead_listing": p.is_dead_listing,
            "skus": p.skus,
        })
    return {"products": result, "total": total, "page": page, "pages": pages, "search_warning": search_warning}


@api.put("/dashboard/products/{product_id}")
def update_product(product_id: int, payload: dict, db: Session = Depends(get_db),
                    shop: Shop = Depends(get_current_shop)):
    p = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not p:
        raise HTTPException(404, "Product not found")
    if payload.get("custom_price") is not None or payload.get("track_price") is True:
        require_access(db, shop, p.aliexpress_id)
    if "custom_title" in payload:
        p.custom_title = payload["custom_title"] or None
    if "custom_price" in payload:
        p.custom_price = payload["custom_price"] or None
    if "custom_rating" in payload:
        try:
            p.custom_rating = float(payload["custom_rating"]) if payload["custom_rating"] else None
        except Exception:
            p.custom_rating = None
    if "custom_description" in payload:
        p.custom_description = payload["custom_description"] or None
    if "track_price" in payload:
        p.track_price = bool(payload["track_price"])
    db.commit(); db.refresh(p)

    shopify_synced = False
    if p.shopify_product_id:
        try:
            from .shopify import update_shopify_product
            update_shopify_product(shop, p.shopify_product_id, {
                "title": p.custom_title or p.original_title,
                "body_html": p.custom_description or "",
                "rating": p.custom_rating or p.avg_rating,
            })
            shopify_synced = True
        except Exception as e:
            print(f"Shopify sync failed: {e}")
    else:
        shopify_synced = None

    return {"message": "Product updated", "shopify_synced": shopify_synced,
            "product": {"id": p.id, "title": p.custom_title or p.original_title, "price": p.custom_price or p.original_price}}


@api.delete("/dashboard/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    p = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not p:
        raise HTTPException(404, "Product not found")
    db.delete(p)
    from alisync_app.subscriptions import release_deleted_slot
    release_deleted_slot(db, p.user_id, p.shop_id, p.aliexpress_id)
    db.commit()
    return {"message": "Deleted from local database"}


@api.post("/dashboard/products/{product_id}/toggle-track-price")
def toggle_track_price(product_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "imported", product_id)
    p = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not p:
        raise HTTPException(404, "Product not found")
    p.track_price = not p.track_price
    db.commit()
    return {"id": p.id, "track_price": p.track_price}


# ─────────────────────────────────────────────
# ALIEXPRESS FETCH & IMPORT
# ─────────────────────────────────────────────
@api.get("/product/{aliexpress_id}")
def fetch_aliexpress_product(aliexpress_id: str, db: Session = Depends(get_db),
                              shop: Shop = Depends(get_current_shop)):
    product = get_product(aliexpress_id, db)
    if is_definitively_missing(product):
        linked = [
            ("product", row) for row in db.query(ImportedProduct).filter(
                ImportedProduct.shop_id == shop.id, ImportedProduct.aliexpress_id == aliexpress_id).all()
        ] + [
            ("mapping", row) for row in db.query(ProductMapping).filter(
                ProductMapping.shop_id == shop.id, ProductMapping.aliexpress_id == aliexpress_id).all()
        ]
        for entity_type, entity in linked:
            if not has_full_access(db, shop, entity.aliexpress_id):
                continue
            _record_definitive_missing(entity)
            _notify_supplier_state(db, shop, entity_type, entity)
        db.commit()
        raise HTTPException(
            404,
            detail={
                "message": "AliExpress product is unavailable and has been moved to Dead Listings"
                           if linked else "AliExpress product is unavailable",
                "aliexpress_id": aliexpress_id,
                "supplier_status": "confirmed_dead",
                "dead_listing_updated": bool(linked),
                "linked_items": len(linked),
            },
        )
    if "shipping_info" not in product or not product["shipping_info"]:
        product["shipping_info"] = {"cost": "Calculated at checkout", "method": "Standard Shipping"}
    reserve_product(db, shop, aliexpress_id)
    return product


@api.post("/import/{aliexpress_id}")
def import_to_shopify(aliexpress_id: str, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    reserve_product(db, shop, aliexpress_id)
    raw_product = get_product(aliexpress_id, db)
    stock_info = classify_skus(raw_product)

    try:
        product, created = import_aliexpress_product_to_shopify(shop, raw_product, db)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(500, f"Import failed: {str(e)}")

    shopify_info = {"id": product.shopify_product_id, "title": product.original_title, "status": product.shopify_status}
    oos = stock_info["out_of_stock"]

    if oos and has_full_access(db, shop, aliexpress_id):
        upsert_pending(shop, aliexpress_id, raw_product, stock_info, "pending", db)
        return {"status": "imported", "product_id": product.id, "out_of_stock_skus": oos,
                "shopify_product": shopify_info,
                "message": f"Imported successfully. {len(oos)} variant(s) out of stock — queued for tracking.",
                "queued_for_inventory_sync": True}

    return {"status": "imported", "product_id": product.id, "out_of_stock_skus": [],
            "shopify_product": shopify_info,
            "message": "Imported successfully" if created else "Linked to existing Shopify product",
            "queued_for_inventory_sync": False}


@api.post("/dashboard/products/{product_id}/sync-price")
def manual_product_price_sync(product_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "imported", product_id)
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        raise HTTPException(400, "This product has no Shopify ID attached")
    if not product.track_price:
        raise HTTPException(400, "Price tracking is disabled for this product")

    latest_data = get_product(product.aliexpress_id, db)
    new_skus = latest_data.get("skus", [])
    if not new_skus:
        raise HTTPException(500, "No SKUs found in AliExpress product")

    result = update_shopify_product_prices_with_skus(shop, product.shopify_product_id,
        new_skus, reset_manual_increases=True, product_price_increase=product.price_increase or 0.0)
    if result == "failed":
        raise HTTPException(502, "Shopify variant price update failed")

    original_price = latest_data.get("sale_price") or latest_data.get("original_price")
    if original_price:
        product.original_price = original_price
    product.price_mode = "auto"
    product.price_increase = 0.0
    product.price_percentage = 0.0
    product.skus = new_skus
    product.custom_price = None
    db.commit()

    return {"message": "Unlocked variants restored to AliExpress prices and their increases cleared. Locked variant prices and increases preserved. Auto mode enabled.", "price_mode": product.price_mode}



@api.post("/dashboard/products/{product_id}/sync-inventory")
def sync_product_inventory(product_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "imported", product_id)
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        raise HTTPException(400, "Product has no Shopify ID linked")

    raw = get_product(product.aliexpress_id, db)
    skus = raw.get("skus", [])
    if not skus:
        raise HTTPException(500, "No SKUs found in AliExpress product")

    inventory_updated = update_shopify_product_inventory_with_skus(shop, product.shopify_product_id, skus)
    new_total_stock = raw.get("total_stock")
    if new_total_stock is not None:
        product.total_stock = new_total_stock
        db.commit()

    return {"message": "Inventory pushed to Shopify successfully" if inventory_updated else "No changes needed",
            "inventory_updated": inventory_updated, "total_stock": new_total_stock}


@api.post("/dashboard/products/{product_id}/sync-images")
def sync_product_images(product_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        raise HTTPException(400, "Product has no Shopify ID linked")

    raw = get_product(product.aliexpress_id, db)
    skus = raw.get("skus", [])
    result = backfill_sku_images(shop, product.shopify_product_id, skus)

    if raw.get("main_image"):
        product.main_image = raw["main_image"]
    if raw.get("all_images"):
        product.all_images = raw["all_images"]
    if skus:
        product.skus = skus
    db.commit()

    return {"message": f"Attached {result['attached']} image(s). {result['skipped']} already had images.", **result}


@api.post("/dashboard/products/{product_id}/increase-price")
def increase_imported_product_price(product_id: int, payload: dict, db: Session = Depends(get_db),
                                     shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "imported", product_id)
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        raise HTTPException(400, "Product has no Shopify ID linked")

    increase_by = payload.get("increase_by")
    if increase_by is None:
        raise HTTPException(400, "increase_by amount is required")
    try:
        increase_by = float(increase_by)
    except ValueError:
        raise HTTPException(400, "Invalid amount")

    old_rule = {"mode": product.price_mode, "fixed_margin": product.price_increase or 0.0}
    product.price_mode = "increase"
    product.price_increase = increase_by
    product.price_percentage = 0.0
    product.custom_price = None

    latest_data = get_product(product.aliexpress_id, db)
    skus = latest_data.get("skus", [])
    if not skus:
        raise HTTPException(500, "No SKUs found in AliExpress product")

    for sku in skus:
        base_price = sku.get("sale_price") or sku.get("price")
        if base_price is not None:
            new_price = float(base_price) + increase_by
            sku.setdefault("_supplier_price", sku.get("sale_price") or sku.get("price"))
            sku["sale_price"] = str(new_price)
            sku["price"] = str(new_price)

    success = update_shopify_product_prices_with_skus(shop, product.shopify_product_id, skus, reset_manual_increases=True) != "failed"
    if not success:
        raise HTTPException(502, "Failed to update Shopify variant prices")

    original_price = latest_data.get("sale_price") or latest_data.get("original_price")
    if original_price:
        product.original_price = original_price
    create_notification(db, user_id=shop.user_id, shop_id=shop.id, entity_type="product", entity_id=str(product.id),
        event_type="pricing_rule_changed", category="pricing", priority="information",
        title="Pricing rule changed", product_name=product.custom_title or product.original_title,
        message=f"Pricing rule changed to supplier price + ${increase_by:.2f}. {len(skus)} variant(s) updated.",
        old_value=old_rule, new_value={"mode": "fixed_margin", "fixed_margin": increase_by},
        automatic_action=f"Recalculated {len(skus)} unlocked Shopify variant price(s).",
        action_label="View Product", action_url="products",
        dedupe_key=f"pricing-rule:product:{product.id}:{increase_by}")
    db.commit()

    return {"message": f"Price set to AliExpress base + ${increase_by:.2f} markup",
            "price_mode": "increase", "price_increase": increase_by, "updated_variants": len(skus)}


@api.post("/dashboard/products/{product_id}/reset-price-mode")
def reset_product_price_mode(product_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "imported", product_id)
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    product.price_mode = "auto"
    product.price_increase = 0.0
    product.price_percentage = 0.0
    product.custom_price = None
    db.commit()
    sync_product_price(shop, product_id, db)
    return {"message": "Price mode reset to auto", "price_mode": "auto"}


@api.get("/dashboard/products/{product_id}/details")
def get_product_details(product_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "imported", product_id)
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not product:
        raise HTTPException(404, "Product not found")

    live_data = None
    fetch_error = None
    try:
        live_data = get_product(product.aliexpress_id, db)
    except Exception as e:
        fetch_error = str(e)

    if live_data:
        shipping = live_data.get("shipping_info", {})
        if shipping.get("cost") and shipping["cost"] != "Calculated at checkout":
            product.shipping_cost = shipping["cost"]
        if shipping.get("method"):
            product.shipping_method = shipping["method"]
        if live_data.get("total_stock") is not None:
            product.total_stock = live_data["total_stock"]
        product.last_shipment_fetch = sqlfunc.now()
        db.commit()

    def _shipping_cost():
        if live_data:
            return live_data.get("shipping_info", {}).get("cost") or product.shipping_cost or "Calculated at checkout"
        return product.shipping_cost or "Calculated at checkout"

    def _shipping_method():
        if live_data:
            return live_data.get("shipping_info", {}).get("method") or product.shipping_method or "Standard Shipping"
        return product.shipping_method or "Standard Shipping"

    def _shipping_days():
        if live_data:
            return live_data.get("shipping_info", {}).get("days") or ""
        return ""

    return {
        "aliexpress_id": product.aliexpress_id,
        **_ali_sale_price_display(live_data if live_data else {
            "original_price": product.original_price, "currency": product.currency, "skus": product.skus,
        }),
        "last_shipment_fetch": product.last_shipment_fetch.isoformat() if product.last_shipment_fetch else None,
        "shipping_cost": _shipping_cost(),
        "shipping_method": _shipping_method(),
        "shipping_days": _shipping_days(),
        "total_stock": live_data.get("total_stock") if live_data else product.total_stock,
        "stock_available": live_data.get("stock_available") if live_data else None,
        "stock_source": live_data.get("stock_source") if live_data else "cached",
        "stock_note": live_data.get("stock_note") if live_data else ("Using cached data — live fetch failed." if fetch_error else None),
        "sku_inventory": live_data.get("sku_inventory") if live_data else [],
        "orders": live_data.get("orders") if live_data else None,
        "fetch_error": fetch_error,
    }


@api.get("/dashboard/products/{product_id}/variants")
def get_product_variants(product_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "imported", product_id)
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        return {"variants": [], "message": "No Shopify product linked"}

    res = _shopify_request("GET", f"{_base(shop.shop_domain)}/products/{product.shopify_product_id}.json",
        params={"fields": "id,title,variants,options"}, headers=_h(shop.access_token), timeout=15)
    if res.status_code != 200:
        raise HTTPException(502, res.text)

    shopify_product = res.json().get("product", {})
    variants = shopify_product.get("variants", [])
    sku_details = get_variant_sku_details(shop, variants)
    pricing = get_variant_sync_state(shop, product.shopify_product_id)
    result = []
    for v in variants:
        label_parts = [v.get(f"option{i}") for i in (1, 2, 3) if v.get(f"option{i}") and v.get(f"option{i}") != "Default Title"]
        result.append({
            "variant_id": v["id"], "label": " / ".join(label_parts) if label_parts else "Default",
            **sku_details[str(v["id"])], "price": v.get("price"),
            "price_increase": float(pricing[v["id"]]["price_increase"]),
            "product_price_increase": float(product.price_increase or 0),
            "supplier_price_change": (pricing[v["id"]].get("supplier_history") or {}).get("change"),
            "supplier_price_changed_at": (pricing[v["id"]].get("supplier_history") or {}).get("changed_at"),
            "compare_at_price": v.get("compare_at_price"), "inventory_quantity": v.get("inventory_quantity"),
        })
    return {"shopify_product_id": product.shopify_product_id, "title": shopify_product.get("title"), "variants": result, "locks": {str(vid): row["locks"] for vid, row in pricing.items()}}



@api.post("/dashboard/products/{product_id}/update-variant-prices")
def update_variant_prices(product_id: int, payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "imported", product_id)
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        raise HTTPException(400, "Product has no Shopify ID linked")

    variants_payload = payload.get("variants", [])
    if not variants_payload:
        raise HTTPException(400, "No variants provided")

    price_updates = save_variant_price_edits(shop, product.shopify_product_id, variants_payload)

    inventory_updates = {}
    for v in variants_payload:
        vid = v.get("variant_id")
        qty = v.get("inventory_quantity")
        if vid is not None and qty is not None:
            try:
                inventory_updates[int(vid)] = int(qty)
            except (ValueError, TypeError):
                continue

    inventory_updated_count = 0
    if inventory_updates:
        inventory_updated_count = _set_variant_inventory_levels(shop, product.shopify_product_id, inventory_updates)

    # Per-variant locks protect edited prices while unlocked variants keep syncing.
    product.price_mode = "variant_manual"
    product.custom_price = None
    db.commit()

    msg = f"Updated {len(price_updates)} variant price(s)"
    if inventory_updates:
        msg += f" · {inventory_updated_count}/{len(inventory_updates)} inventory level(s) updated"

    return {"message": msg, "price_mode": product.price_mode, "updated": len(price_updates), "inventory_updated": inventory_updated_count}



# ─────────────────────────────────────────────
# DEAD LISTING — PRODUCTS
# ─────────────────────────────────────────────
@api.get("/dashboard/products/{product_id}/check-listing")
def check_listing_status(product_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "imported", product_id)
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not product:
        raise HTTPException(404, "Product not found")

    try:
        raw = get_product(product.aliexpress_id, db)
    except Exception as e:
        _record_supplier_error(product, str(e)); db.commit()
        return {**_dead_listing_payload("product", product), "is_dead_listing": False,
                "stock_zeroed_in_shopify": False, "message": "Supplier unavailable. No inventory changes were made."}

    dead = is_listing_dead(raw)
    status = _record_supplier_check(product, dead)
    db.commit()

    stock_zeroed = False
    restored = False

    if status == "confirmed_dead" and product.shopify_product_id:
        try:
            stock_zeroed = _protect_confirmed_dead(shop, product); db.commit()
        except Exception as e:
            print(f"[CheckListing] Failed to zero stock for {product.aliexpress_id}: {e}")
    elif status == "active" and product.shopify_product_id:
        try:
            restored = sync_product_price(shop, product_id, db)
        except Exception as e:
            print(f"[CheckListing] Failed to restore stock for {product.aliexpress_id}: {e}")

    return {
        "aliexpress_id": product.aliexpress_id, "is_dead_listing": status == "confirmed_dead",
        "supplier_status": status, "verification_attempts": product.verification_attempts,
        "verification_required": DEAD_CONFIRMATION_CHECKS,
        "stock_zeroed_in_shopify": stock_zeroed, "stock_restored": restored,
        "sale_price": raw.get("sale_price"), "original_price": raw.get("original_price"),
        "sku_prices": [{"sku_id": s.get("sku_id"), "price": s.get("sale_price") or s.get("price")} for s in (raw.get("skus") or [])],
        "message": ("DEAD listing — inventory zeroed. Please remap." if dead else "Listing is active with valid prices."),
    }


def _validate_remap_id_available(db, shop, new_id):
    """Supplier IDs must be unused across both product tables in this shop."""
    for model, table_label in ((ImportedProduct, "Imported Products"),
                               (ProductMapping, "Product Mappings")):
        conflict = db.query(model).filter(
            model.shop_id == shop.id, model.aliexpress_id == new_id
        ).first()
        if conflict:
            raise HTTPException(409, f"AliExpress ID {new_id} is already used in {table_label}. Choose a different ID to remap.")


@api.post("/dashboard/products/{product_id}/remap-listing")
def remap_listing(product_id: int, payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "imported", product_id, "relink")
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not product:
        raise HTTPException(404, "Product not found")

    new_id = (payload.get("new_aliexpress_id") or "").strip()
    if not new_id:
        raise HTTPException(400, "new_aliexpress_id is required")
    if new_id == product.aliexpress_id:
        raise HTTPException(400, "new_aliexpress_id is the same as the current ID")

    _validate_remap_id_available(db, shop, new_id)

    try:
        raw = get_product(new_id, db)
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch new ID from AliExpress: {e}")

    if is_listing_dead(raw):
        raise HTTPException(400, f"New ID {new_id} also appears dead. Please verify the correct listing ID.")

    if not product.shopify_product_id:
        raise HTTPException(400, "No Shopify product linked; cannot replace variants")
    replacement = dict(raw)
    replacement["skus"] = _apply_product_price_increase(
        raw.get("skus", []), product.price_increase, product.price_percentage)
    old_id = product.aliexpress_id
    move_access(db, shop, old_id, new_id)
    try:
        remap_result = replace_supplier_variants(shop, product.shopify_product_id, replacement, new_id)
    except Exception:
        db.rollback()
        raise
    product.replacement_aliexpress_id = old_id
    product.aliexpress_id = new_id
    product.is_dead_listing = False
    product.supplier_status = "relinked"
    product.verification_attempts = 0
    product.last_failure_reason = None
    product.inventory_protected = False
    # Refresh supplier data without changing merchant title/description or tracking.
    for field in ("original_price", "currency", "main_image", "store_name", "avg_rating",
                  "review_count", "orders", "sku_count", "skus", "all_images", "total_stock"):
        value = raw.get(field)
        if field == "original_price":
            value = raw.get("sale_price") or raw.get("original_price")
        elif field == "sku_count":
            value = len(raw.get("skus", []))
        elif field in ("review_count", "orders"):
            value = _safe_int(value)
        setattr(product, field, value)
    db.commit()

    return {"message": f"Remapped from {old_id} to {new_id}; old variants replaced and title/description preserved.",
            "old_aliexpress_id": old_id, "new_aliexpress_id": new_id,
            "sku_count": len(raw.get("skus", [])), **remap_result}




@api.post("/admin/scan-dead-listings")
def scan_dead_listings(background_tasks: BackgroundTasks, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_any_full(db, shop)
    products = db.query(ImportedProduct).filter(
        ImportedProduct.shop_id == shop.id, ImportedProduct.shopify_product_id.isnot(None)
    ).all()
    products = [item for item in products if has_full_access(db, shop, item.aliexpress_id)]
    if not products:
        raise HTTPException(404, "No imported products found")

    total = len(products)
    snapshots = [{"id": p.id, "aliexpress_id": p.aliexpress_id} for p in products]
    shop_id = shop.id

    def run():
        inner_db = SessionLocal()
        inner_db.info["shop_id"] = shop_id
        dead_count = alive_count = error_count = zeroed_count = 0
        inner_shop = inner_db.query(Shop).filter(Shop.id == shop_id).first()
        try:
            for snap in snapshots:
                if not has_full_access(inner_db, inner_shop, snap["aliexpress_id"]):
                    continue
                try:
                    raw = get_product(snap["aliexpress_id"], inner_db)
                    dead = is_listing_dead(raw)
                    prod = inner_db.query(ImportedProduct).filter(ImportedProduct.id == snap["id"]).first()
                    if prod:
                        prod.is_dead_listing = dead
                        inner_db.commit()
                    if dead:
                        dead_count += 1
                        if prod and prod.shopify_product_id:
                            try:
                                zeroed = set_product_out_of_stock(inner_shop, prod.shopify_product_id)
                                if zeroed:
                                    zeroed_count += 1
                                    prod.total_stock = 0
                                    inner_db.commit()
                            except Exception as e:
                                print(f"[DeadScan] Failed to zero stock for {snap['aliexpress_id']}: {e}")
                    else:
                        alive_count += 1
                except Exception as e:
                    error_count += 1
                    print(f"[DeadScan] Error: {snap['aliexpress_id']}: {e}")
        finally:
            inner_db.close()
            print(f"[DeadScan] Done — alive={alive_count} dead={dead_count} zeroed={zeroed_count} errors={error_count}")

    background_tasks.add_task(run)
    return {"message": f"Dead listing scan started for {total} product(s)", "total": total,
            "note": "Check terminal for progress. Use GET /dead-listings to see results."}


@api.get("/dead-listings")
def get_dead_listings(db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    products = db.query(ImportedProduct).filter(
        ImportedProduct.shop_id == shop.id,
        (ImportedProduct.is_dead_listing == True) | (ImportedProduct.replacement_aliexpress_id.isnot(None))
    ).all()
    return {"count": len(products), "products": [{
        "id": p.id, "aliexpress_id": p.aliexpress_id, "replacement_aliexpress_id": p.replacement_aliexpress_id,
        "title": p.custom_title or p.original_title, "main_image": p.main_image,
        "shopify_product_id": p.shopify_product_id,
        "shopify_url": f"https://{shop.shop_domain}/admin/products/{p.shopify_product_id}" if p.shopify_product_id else None,
        "aliexpress_url": f"https://www.aliexpress.com/item/{p.aliexpress_id}.html",
        "imported_at": p.imported_at.isoformat() if p.imported_at else None,
        "is_still_dead": p.is_dead_listing,
    } for p in products]}


# ─────────────────────────────────────────────
# PRODUCT MAPPINGS
# ─────────────────────────────────────────────
@api.post("/mappings/add")
def add_mapping(aliexpress_id: str, shopify_product_id: str, shopify_product_title: str = None,
                db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    aliexpress_id = aliexpress_id.strip()
    shopify_product_id = shopify_product_id.strip()
    if not aliexpress_id or not shopify_product_id:
        raise HTTPException(400, "AliExpress ID and Shopify product ID are required")
    existing = db.query(ProductMapping).filter(
        ProductMapping.shop_id == shop.id, ProductMapping.aliexpress_id == aliexpress_id
    ).first()
    if existing:
        raise HTTPException(400, "Mapping for this AliExpress ID already exists")
    shopify_mapping = db.query(ProductMapping).filter(
        ProductMapping.shop_id == shop.id, ProductMapping.shopify_product_id == shopify_product_id
    ).first()
    if shopify_mapping:
        raise HTTPException(409, f"Shopify product {shopify_product_id} is already linked to AliExpress "
                                 f"{shopify_mapping.aliexpress_id}")
    imported_link = db.query(ImportedProduct).filter(
        ImportedProduct.shop_id == shop.id, ImportedProduct.shopify_product_id == shopify_product_id
    ).first()
    if imported_link and imported_link.aliexpress_id != aliexpress_id:
        raise HTTPException(409, f"Shopify product {shopify_product_id} is already linked to AliExpress "
                                 f"{imported_link.aliexpress_id} in Imported Products")
    reserve_product(db, shop, aliexpress_id, mapping=True)
    mapping = ProductMapping(user_id=shop.user_id, shop_id=shop.id, aliexpress_id=aliexpress_id, shopify_product_id=shopify_product_id,
                              shopify_product_title=shopify_product_title, track_price=True, price_mode="auto", price_increase=0.0)
    db.add(mapping); db.flush()
    create_notification(db, user_id=shop.user_id, shop_id=shop.id, entity_type="mapping", entity_id=str(mapping.id),
        event_type="product_linked", category="sync", priority="information", title="Product successfully linked",
        product_name=shopify_product_title or f"Shopify product {shopify_product_id}",
        message="The existing Shopify product is now linked to an AliExpress supplier.",
        automatic_action="Supplier linked. Automation follows your plan and product access.", action_label="View Product", action_url="mappings",
        dedupe_key=f"linked:mapping:{mapping.id}")
    db.commit(); db.refresh(mapping)
    return {"message": "Mapping added", "mapping": {"id": mapping.id, "aliexpress_id": mapping.aliexpress_id,
            "shopify_product_id": mapping.shopify_product_id, "title": mapping.shopify_product_title}}


@api.get("/mappings/list")
def list_mappings(page: int = Query(1, ge=1), page_size: int = Query(5, ge=1, le=100),
                   search: str = Query(None), db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    query = db.query(ProductMapping).filter(ProductMapping.shop_id == shop.id, ProductMapping.user_id == shop.user_id)
    if search:
        term = f"%{search}%"
        query = query.filter((ProductMapping.aliexpress_id.ilike(term)) |
                              (ProductMapping.shopify_product_id.ilike(term)) |
                              (ProductMapping.shopify_product_title.ilike(term)))
    total = query.count()
    pages = (total + page_size - 1) // page_size if total > 0 else 1
    offset = (page - 1) * page_size
    mappings = query.order_by(ProductMapping.created_at.desc()).offset(offset).limit(page_size).all()
    return {"mappings": [{
        "id": m.id, "aliexpress_id": m.aliexpress_id, "shopify_product_id": m.shopify_product_id,
        "title": m.custom_title or m.shopify_product_title, "track_price": m.track_price,
        "price_mode": m.price_mode, "price_increase": m.price_increase, "price_percentage": m.price_percentage, "is_dead_listing": m.is_dead_listing,
        "custom_title": m.custom_title, "custom_description": m.custom_description, "custom_rating": m.custom_rating,
    } for m in mappings], "total": total, "page": page, "pages": pages}


def delete_mapping(mapping_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    db.delete(mapping)
    from alisync_app.subscriptions import release_deleted_slot
    release_deleted_slot(db, mapping.user_id, mapping.shop_id, mapping.aliexpress_id)
    db.commit()
    return {"message": "Mapping deleted"}


@api.put("/mappings/{mapping_id}")
def update_mapping(mapping_id: int, payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    if "custom_title" in payload:
        mapping.custom_title = payload["custom_title"] or None
    if "custom_description" in payload:
        mapping.custom_description = payload["custom_description"] or None
    if "custom_rating" in payload:
        try:
            mapping.custom_rating = float(payload["custom_rating"]) if payload["custom_rating"] else None
        except (ValueError, TypeError):
            mapping.custom_rating = None
    db.commit(); db.refresh(mapping)

    shopify_synced = False
    if mapping.shopify_product_id:
        try:
            from .shopify import update_shopify_product
            update_shopify_product(shop, mapping.shopify_product_id, {
                "title": mapping.custom_title, "body_html": mapping.custom_description, "rating": mapping.custom_rating,
            })
            shopify_synced = True
        except Exception as e:
            print(f"[MappingEdit] Shopify sync failed: {e}")
    return {"message": "Mapping updated", "shopify_synced": shopify_synced}


@api.post("/mappings/sync-price")
def sync_mapped_product_price(aliexpress_id: str, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_access(db, shop, aliexpress_id)
    mapping = db.query(ProductMapping).filter(
        ProductMapping.shop_id == shop.id, ProductMapping.aliexpress_id == aliexpress_id
    ).first()
    if not mapping or not mapping.track_price:
        raise HTTPException(404, "Mapping not found or price tracking disabled")

    raw = get_product(aliexpress_id, db)
    if is_listing_dead(raw):
        _record_supplier_check(mapping, True)
        zeroed = False
        if mapping.supplier_status == "confirmed_dead":
            try:
                zeroed = _protect_confirmed_dead(shop, mapping)
            except Exception as e:
                print(f"[SyncMapping] Failed to protect inventory: {e}")
        db.commit()
        raise HTTPException(409, f"Supplier verification {mapping.verification_attempts}/{DEAD_CONFIRMATION_CHECKS}. "
                                 f"Inventory {'protected' if zeroed else 'unchanged'}; Shopify listing preserved.")

    _record_supplier_check(mapping, False); db.commit()

    aliexpress_skus = raw.get("skus", [])
    if not aliexpress_skus:
        raise HTTPException(500, "No SKUs found in AliExpress product")

    errors = []
    price_result = update_shopify_product_prices_with_skus(shop, mapping.shopify_product_id,
        aliexpress_skus, reset_manual_increases=True, error_details=errors,
        product_price_increase=mapping.price_increase or 0.0)
    if price_result == "failed":
        raise HTTPException(502, "; ".join(errors) or "Failed to update Shopify variant prices")
    mapping.price_mode = "auto"
    mapping.price_increase = 0.0
    mapping.price_percentage = 0.0
    db.commit()

    # Price reset is complete even if the separate inventory operation fails.
    inventory_error = None
    try:
        inventory_updated = update_shopify_product_inventory_with_skus(shop, mapping.shopify_product_id, aliexpress_skus)
        if not inventory_updated:
            inventory_error = "Inventory was not updated; retry inventory sync separately."
    except Exception:
        inventory_updated = False
        inventory_error = "Inventory sync failed; retry inventory sync separately."
    message = "Unlocked variants restored to AliExpress prices and their increases cleared. Locked variant prices and increases preserved. Auto mode enabled."
    if inventory_error:
        message += " " + inventory_error
    return {"message": message, "product_id": mapping.shopify_product_id,
            "price_mode": "auto", "price_increase": 0.0,
            "inventory_updated": inventory_updated, "inventory_error": inventory_error}




@api.post("/mappings/{mapping_id}/sync-inventory")
def sync_mapping_inventory(mapping_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "mapping", mapping_id)
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    try:
        raw = get_product(mapping.aliexpress_id, db)
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch AliExpress data: {e}")
    skus = raw.get("skus", [])
    if not skus:
        raise HTTPException(500, "No SKUs found in AliExpress product")
    inventory_updated = update_shopify_product_inventory_with_skus(shop, mapping.shopify_product_id, skus)
    return {"message": "Inventory pushed to Shopify successfully" if inventory_updated else "No inventory changes detected",
            "inventory_updated": inventory_updated, "total_stock": raw.get("total_stock")}


@api.post("/mappings/{mapping_id}/toggle-track")
def toggle_track(mapping_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "mapping", mapping_id)
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    mapping.track_price = not mapping.track_price
    db.commit()
    return {"id": mapping.id, "track_price": mapping.track_price}


@api.post("/mappings/{mapping_id}/increase-price")
def increase_mapping_price(mapping_id: int, payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "mapping", mapping_id)
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    increase_by = payload.get("increase_by")
    if increase_by is None:
        raise HTTPException(400, "increase_by amount is required")
    try:
        increase_by = float(increase_by)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid amount format")

    if not math.isfinite(increase_by):
        raise HTTPException(400, "Increase amount must be finite")

    old_rule = {"mode": mapping.price_mode, "fixed_margin": mapping.price_increase or 0.0}

    raw = get_product(mapping.aliexpress_id, db)
    skus = raw.get("skus", [])
    if not skus:
        raise HTTPException(500, "No SKUs found in AliExpress product")

    for sku in skus:
        base_price = sku.get("sale_price") or sku.get("price")
        if base_price is not None:
            new_price = float(base_price) + increase_by
            sku.setdefault("_supplier_price", sku.get("sale_price") or sku.get("price"))
            sku["sale_price"] = str(new_price)
            sku["price"] = str(new_price)

    errors = []
    success = update_shopify_product_prices_with_skus(shop, mapping.shopify_product_id, skus, error_details=errors, reset_manual_increases=True) != "failed"
    if not success:
        raise HTTPException(502, "; ".join(errors) or "Failed to update Shopify variant prices")
    mapping.price_mode = "increase"
    mapping.price_increase = increase_by
    mapping.price_percentage = 0.0
    db.commit()

    return {"message": f"Markup set to ${increase_by:.2f} above AliExpress base",
            "product_id": mapping.shopify_product_id, "price_mode": "increase", "price_increase": increase_by}



@api.post("/mappings/{mapping_id}/reset-mode")
def reset_mapping_mode(mapping_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "mapping", mapping_id)
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    mapping.price_mode = "auto"
    mapping.price_increase = 0.0
    mapping.price_percentage = 0.0
    db.commit()
    try:
        raw = get_product(mapping.aliexpress_id, db)
        skus = raw.get("skus", [])
        if skus:
            update_shopify_product_prices_with_skus(shop, mapping.shopify_product_id, skus)
    except Exception as e:
        print(f"[ResetMode] Sync failed (non-fatal): {e}")
    return {"message": "Mapping reset to auto sync mode", "price_mode": "auto"}


@api.post("/mappings/{mapping_id}/sync-images")
def sync_mapping_images(mapping_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    try:
        raw = get_product(mapping.aliexpress_id, db)
        skus = raw.get("skus", [])
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch AliExpress data: {e}")
    if not skus:
        raise HTTPException(400, "No SKUs found for this AliExpress product")
    result = backfill_sku_images(shop, mapping.shopify_product_id, skus)
    return {"message": f"Attached {result['attached']} image(s). {result['skipped']} variant(s) already had images.", **result}


@api.get("/mappings/{mapping_id}/check-listing")
def check_mapping_listing_status(mapping_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "mapping", mapping_id)
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    try:
        raw = get_product(mapping.aliexpress_id, db)
    except Exception as e:
        _record_supplier_error(mapping, str(e)); db.commit()
        return {**_dead_listing_payload("mapping", mapping), "is_dead_listing": False,
                "stock_zeroed_in_shopify": False, "message": "Supplier unavailable. No inventory changes were made."}
    dead = is_listing_dead(raw)
    status = _record_supplier_check(mapping, dead)
    create_notification(db, user_id=shop.user_id, shop_id=shop.id, entity_type="mapping", entity_id=str(mapping.id),
        event_type="pricing_rule_changed", category="pricing", priority="information",
        title="Pricing rule changed", product_name=mapping.custom_title or mapping.shopify_product_title,
        message=f"Pricing rule changed to supplier price + ${increase_by:.2f}. {len(skus)} variant(s) updated.",
        old_value=old_rule, new_value={"mode": "fixed_margin", "fixed_margin": increase_by},
        automatic_action=f"Recalculated {len(skus)} unlocked Shopify variant price(s).",
        action_label="View Product", action_url="mappings",
        dedupe_key=f"pricing-rule:mapping:{mapping.id}:{increase_by}")
    db.commit()
    db.add(SyncLog(user_id=shop.user_id, shop_id=shop.id, entity_type="product", entity_id=str(product.id),
                   action="relink", status="success", message=f"Supplier relinked from {old_id} to {new_id}; Shopify listing preserved"))
    create_notification(db, user_id=shop.user_id, shop_id=shop.id, entity_type="product", entity_id=str(product.id),
        event_type="product_relinked", category="dead_listings", priority="information",
        title="Product successfully relinked", product_name=product.custom_title or product.original_title,
        message=f"Supplier changed from {old_id} to {new_id}. The existing Shopify product and pricing rule were preserved.",
        old_value={"aliexpress_id": old_id}, new_value={"aliexpress_id": new_id},
        automatic_action="Price and inventory sync resumed with the new supplier.", action_label="View Product",
        action_url="products", dedupe_key=f"relinked:product:{product.id}:{new_id}")
    db.commit()
    stock_zeroed = False
    if status == "confirmed_dead":
        try:
            stock_zeroed = _protect_confirmed_dead(shop, mapping); db.commit()
        except Exception as e:
            print(f"[CheckMappingListing] Failed to zero stock: {e}")
    return {"aliexpress_id": mapping.aliexpress_id, "is_dead_listing": status == "confirmed_dead",
            "supplier_status": status, "verification_attempts": mapping.verification_attempts,
            "verification_required": DEAD_CONFIRMATION_CHECKS, "stock_zeroed_in_shopify": stock_zeroed,
            "sale_price": raw.get("sale_price"), "original_price": raw.get("original_price"),
            "message": ("DEAD listing — inventory zeroed. Please remap." if dead else "Listing is active with valid prices.")}


@api.post("/mappings/{mapping_id}/remap-listing")
def remap_mapping_listing(mapping_id: int, payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "mapping", mapping_id, "relink")
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")

    new_id = (payload.get("new_aliexpress_id") or "").strip()
    if not new_id:
        raise HTTPException(400, "new_aliexpress_id is required")
    if new_id == mapping.aliexpress_id:
        raise HTTPException(400, "new_aliexpress_id is the same as the current ID")

    _validate_remap_id_available(db, shop, new_id)

    try:
        raw = get_product(new_id, db)
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch new ID from AliExpress: {e}")

    if is_listing_dead(raw):
        raise HTTPException(400, f"New ID {new_id} also appears dead. Please verify the correct listing ID.")

    if not mapping.shopify_product_id:
        raise HTTPException(400, "No Shopify product linked; cannot replace variants")
    replacement = dict(raw)
    replacement["skus"] = _apply_product_price_increase(
        raw.get("skus", []), mapping.price_increase, mapping.price_percentage)
    old_id = mapping.aliexpress_id
    move_access(db, shop, old_id, new_id)
    try:
        remap_result = replace_supplier_variants(shop, mapping.shopify_product_id, replacement, new_id)
    except Exception:
        db.rollback()
        raise
    mapping.replacement_aliexpress_id = old_id
    mapping.aliexpress_id = new_id
    mapping.is_dead_listing = False
    mapping.supplier_status = "relinked"
    mapping.verification_attempts = 0
    mapping.last_failure_reason = None
    mapping.inventory_protected = False
    db.commit()
    db.add(SyncLog(user_id=shop.user_id, shop_id=shop.id, entity_type="mapping", entity_id=str(mapping.id),
                   action="relink", status="success", message=f"Supplier relinked from {old_id} to {new_id}; Shopify listing and pricing rule preserved"))
    create_notification(db, user_id=shop.user_id, shop_id=shop.id, entity_type="mapping", entity_id=str(mapping.id),
        event_type="product_relinked", category="dead_listings", priority="information",
        title="Product successfully relinked", product_name=mapping.custom_title or mapping.shopify_product_title,
        message=f"Supplier changed from {old_id} to {new_id}. The existing Shopify product and pricing rule were preserved.",
        old_value={"aliexpress_id": old_id}, new_value={"aliexpress_id": new_id},
        automatic_action="Supplier link updated. Automation follows your plan and product access.", action_label="View Product",
        action_url="mappings", dedupe_key=f"relinked:mapping:{mapping.id}:{new_id}")
    db.commit()

    return {"message": f"Remapped from {old_id} to {new_id}; old variants replaced and title/description preserved.",
            "old_aliexpress_id": old_id, "new_aliexpress_id": new_id,
            "sku_count": len(raw.get("skus", [])), **remap_result}




@api.post("/admin/scan-dead-mappings")
def scan_dead_mappings(background_tasks: BackgroundTasks, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_any_full(db, shop)
    mappings = db.query(ProductMapping).filter(ProductMapping.shop_id == shop.id).all()
    mappings = [item for item in mappings if has_full_access(db, shop, item.aliexpress_id)]
    if not mappings:
        raise HTTPException(404, "No mappings found")

    total = len(mappings)
    snapshots = [{"id": m.id, "aliexpress_id": m.aliexpress_id, "shopify_product_id": m.shopify_product_id} for m in mappings]
    shop_id = shop.id

    def run():
        inner_db = SessionLocal()
        inner_db.info["shop_id"] = shop_id
        dead_count = alive_count = error_count = zeroed_count = 0
        inner_shop = inner_db.query(Shop).filter(Shop.id == shop_id).first()
        try:
            for snap in snapshots:
                if not has_full_access(inner_db, inner_shop, snap["aliexpress_id"]):
                    continue
                try:
                    raw = get_product(snap["aliexpress_id"], inner_db)
                    dead = is_listing_dead(raw)
                    m = inner_db.query(ProductMapping).filter(ProductMapping.id == snap["id"]).first()
                    if m:
                        m.is_dead_listing = dead
                        inner_db.commit()
                    if dead:
                        dead_count += 1
                        if snap["shopify_product_id"]:
                            try:
                                zeroed = set_product_out_of_stock(inner_shop, snap["shopify_product_id"])
                                if zeroed:
                                    zeroed_count += 1
                            except Exception as e:
                                print(f"[DeadMappingScan] Failed to zero stock for {snap['aliexpress_id']}: {e}")
                    else:
                        alive_count += 1
                except Exception as e:
                    error_count += 1
                    print(f"[DeadMappingScan] Error: {snap['aliexpress_id']}: {e}")
        finally:
            inner_db.close()
            print(f"[DeadMappingScan] Done — alive={alive_count} dead={dead_count} zeroed={zeroed_count} errors={error_count}")

    background_tasks.add_task(run)
    return {"message": f"Dead mapping scan started for {total} mapping(s)", "total": total,
            "note": "Check terminal for progress. Use GET /dead-mappings to see results."}


@api.get("/dead-mappings")
def get_dead_mappings(db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    mappings = db.query(ProductMapping).filter(
        ProductMapping.shop_id == shop.id, ProductMapping.is_dead_listing == True
    ).all()
    return {"count": len(mappings), "mappings": [{
        "id": m.id, "aliexpress_id": m.aliexpress_id, "shopify_product_id": m.shopify_product_id,
        "title": m.shopify_product_title,
        "shopify_url": f"https://{shop.shop_domain}/admin/products/{m.shopify_product_id}" if m.shopify_product_id else None,
        "aliexpress_url": f"https://www.aliexpress.com/item/{m.aliexpress_id}.html",
        "created_at": m.created_at.isoformat() if m.created_at else None,
    } for m in mappings]}


def _dead_listing_payload(entity_type: str, entity) -> dict:
    return {
        "entity_type": entity_type, "id": entity.id,
        "title": getattr(entity, "custom_title", None) or getattr(entity, "original_title", None)
                 or getattr(entity, "shopify_product_title", None),
        "aliexpress_id": entity.aliexpress_id,
        "replacement_aliexpress_id": getattr(entity, "replacement_aliexpress_id", None),
        "shopify_product_id": entity.shopify_product_id,
        "supplier_status": entity.supplier_status,
        "verification_attempts": entity.verification_attempts,
        "verification_required": DEAD_CONFIRMATION_CHECKS,
        "last_verification_at": entity.last_verification_at.isoformat() if entity.last_verification_at else None,
        "last_successful_sync_at": entity.last_successful_sync_at.isoformat() if entity.last_successful_sync_at else None,
        "failure_reason": entity.last_failure_reason,
        "inventory_protected": entity.inventory_protected,
        "action": "relink" if entity.supplier_status == "confirmed_dead" else "review",
    }


@api.get("/supplier-health")
def supplier_health(status: str | None = None, db: Session = Depends(get_db),
                    shop: Shop = Depends(get_current_shop)):
    """One queue for imported and manually linked Shopify products."""
    products = db.query(ImportedProduct).filter(ImportedProduct.shop_id == shop.id).all()
    mappings = db.query(ProductMapping).filter(ProductMapping.shop_id == shop.id).all()
    items = ([_dead_listing_payload("product", row) for row in products] +
             [_dead_listing_payload("mapping", row) for row in mappings])
    if status:
        wanted = {part.strip() for part in status.split(",") if part.strip()}
        items = [item for item in items if item["supplier_status"] in wanted]
    else:
        items = [item for item in items if item["supplier_status"] != "active"
                 or item["replacement_aliexpress_id"]]
    counts = {}
    for item in items:
        counts[item["supplier_status"]] = counts.get(item["supplier_status"], 0) + 1
    return {"count": len(items), "counts": counts, "items": items}


@api.post("/supplier-health/{entity_type}/{entity_id}/verify")
def verify_supplier_listing(entity_type: str, entity_id: int, db: Session = Depends(get_db),
                            shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, entity_type, entity_id)
    model = ImportedProduct if entity_type == "product" else ProductMapping if entity_type == "mapping" else None
    if model is None:
        raise HTTPException(400, "entity_type must be product or mapping")
    entity = db.query(model).filter(model.id == entity_id, model.shop_id == shop.id).first()
    if not entity:
        raise HTTPException(404, "Linked product not found")
    try:
        raw = get_product(entity.aliexpress_id, db)
        status = _record_supplier_check(entity, is_listing_dead(raw))
        protected = _protect_confirmed_dead(shop, entity)
    except Exception as exc:
        status = _record_supplier_error(entity, str(exc))
        protected = False
    _notify_supplier_state(db, shop, entity_type, entity)
    db.commit()
    return {**_dead_listing_payload(entity_type, entity), "protected_now": protected,
            "message": ("Supplier listing confirmed dead; Shopify listing preserved and inventory protected."
                        if status == "confirmed_dead" else
                        "Verification recorded. No destructive action was taken.")}


@api.post("/admin/scan-supplier-health")
def scan_supplier_health(background_tasks: BackgroundTasks, db: Session = Depends(get_db),
                         shop: Shop = Depends(get_current_shop)):
    require_any_full(db, shop)
    entities = ([{"type": "product", "id": row.id} for row in db.query(ImportedProduct).filter(ImportedProduct.shop_id == shop.id).all()] +
                [{"type": "mapping", "id": row.id} for row in db.query(ProductMapping).filter(ProductMapping.shop_id == shop.id).all()])
    if not entities:
        raise HTTPException(404, "No linked products found")
    shop_id = shop.id
    def run():
        inner_db = SessionLocal(); inner_db.info["shop_id"] = shop_id
        inner_shop = inner_db.query(Shop).filter(Shop.id == shop_id).first()
        try:
            for item in entities:
                model = ImportedProduct if item["type"] == "product" else ProductMapping
                entity = inner_db.query(model).filter(model.id == item["id"], model.shop_id == shop_id).first()
                if not entity or not has_full_access(inner_db, inner_shop, entity.aliexpress_id):
                    continue
                try:
                    raw = get_product(entity.aliexpress_id, inner_db)
                    _record_supplier_check(entity, is_listing_dead(raw))
                    _protect_confirmed_dead(inner_shop, entity)
                except Exception as exc:
                    _record_supplier_error(entity, str(exc))
                _notify_supplier_state(inner_db, inner_shop, item["type"], entity)
                inner_db.commit()
        finally:
            inner_db.close()
    background_tasks.add_task(run)
    return {"message": f"Supplier verification started for {len(entities)} linked product(s)",
            "total": len(entities), "note": "View Background job status on the Dead Listings page."}


@api.get("/mappings/{mapping_id}/variants")
def get_mapping_variants(mapping_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "mapping", mapping_id)
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    res = _shopify_request("GET", f"{_base(shop.shop_domain)}/products/{mapping.shopify_product_id}.json",
        params={"fields": "id,title,variants,options"}, headers=_h(shop.access_token), timeout=15)
    if res.status_code != 200:
        raise HTTPException(502, res.text)
    shopify_product = res.json().get("product", {})
    variants = shopify_product.get("variants", [])
    sku_details = get_variant_sku_details(shop, variants)
    pricing = get_variant_sync_state(shop, mapping.shopify_product_id)
    result = []
    for v in variants:
        label_parts = [v.get(f"option{i}") for i in (1, 2, 3) if v.get(f"option{i}") and v.get(f"option{i}") != "Default Title"]
        result.append({"variant_id": v["id"], "label": " / ".join(label_parts) if label_parts else "Default",
                        **sku_details[str(v["id"])], "price": v.get("price"),
                        "price_increase": float(pricing[v["id"]]["price_increase"]),
            "product_price_increase": float(mapping.price_increase or 0),
            "supplier_price_change": (pricing[v["id"]].get("supplier_history") or {}).get("change"),
            "supplier_price_changed_at": (pricing[v["id"]].get("supplier_history") or {}).get("changed_at"),
            "compare_at_price": v.get("compare_at_price"), "inventory_quantity": v.get("inventory_quantity")})
    return {"shopify_product_id": mapping.shopify_product_id, "title": shopify_product.get("title"), "variants": result, "locks": {str(vid): row["locks"] for vid, row in pricing.items()}}



@api.post("/mappings/{mapping_id}/update-variant-prices")
def update_mapping_variant_prices(mapping_id: int, payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "mapping", mapping_id)
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    variants_payload = payload.get("variants", [])
    if not variants_payload:
        raise HTTPException(400, "No variants provided")

    price_updates = save_variant_price_edits(shop, mapping.shopify_product_id, variants_payload)

    inventory_updates = {}
    for v in variants_payload:
        vid, qty = v.get("variant_id"), v.get("inventory_quantity")
        if vid is not None and qty is not None:
            try:
                inventory_updates[int(vid)] = int(qty)
            except (ValueError, TypeError):
                continue
    inventory_updated_count = _set_variant_inventory_levels(shop, mapping.shopify_product_id, inventory_updates) if inventory_updates else 0

    mapping.price_mode = "variant_manual"
    db.commit()

    msg = f"Updated {len(price_updates)} variant price(s)"
    if inventory_updates:
        msg += f" · {inventory_updated_count}/{len(inventory_updates)} inventory level(s) updated"
    return {"message": msg, "price_mode": mapping.price_mode, "updated": len(price_updates), "inventory_updated": inventory_updated_count}



@api.get("/mappings/{mapping_id}/details")
def get_mapping_details(mapping_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "mapping", mapping_id)
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    try:
        live_data = get_product(mapping.aliexpress_id, db)
    except Exception as e:
        return {"aliexpress_id": mapping.aliexpress_id, "fetch_error": str(e),
                "shipping_cost": "Calculated at checkout", "shipping_method": "Standard Shipping",
                "shipping_days": "", "total_stock": None, "stock_available": None, "sku_inventory": [], "orders": None}
    shipping = live_data.get("shipping_info", {})
    return {
        "aliexpress_id": mapping.aliexpress_id,
        **_ali_sale_price_display(live_data),
        "shipping_cost": shipping.get("cost") or "Calculated at checkout",
        "shipping_method": shipping.get("method") or "Standard Shipping",
        "shipping_days": shipping.get("days") or "",
        "total_stock": live_data.get("total_stock"), "stock_available": live_data.get("stock_available"),
        "stock_source": live_data.get("stock_source"), "stock_note": live_data.get("stock_note"),
        "sku_inventory": live_data.get("sku_inventory") or [], "orders": live_data.get("orders"), "fetch_error": None,
    }


# ─────────────────────────────────────────────
# VARIANT LOCKS
# ─────────────────────────────────────────────
@api.post("/dashboard/variants/{variant_id}/toggle-lock/{lock_type}")
def toggle_variant_lock(variant_id: int, lock_type: str, product_id: int = Query(None), mapping_id: int = Query(None),
                         db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    entity = require_entity(db, shop, "mapping" if mapping_id else "imported", mapping_id or product_id)
    if variant_id not in get_variant_sync_state(shop, entity.shopify_product_id):
        raise HTTPException(404, "Variant does not belong to this product")
    if lock_type not in ("price", "inventory", "image"):
        raise HTTPException(400, "lock_type must be one of: price, inventory, image")

    currently_locked = is_variant_locked(shop, variant_id, lock_type)
    new_state = not currently_locked
    if not set_variant_lock(shop, variant_id, lock_type, new_state):
        raise HTTPException(502, "Failed to update variant lock")

    shopify_id = None
    if product_id:
        p = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
        if p:
            shopify_id = p.shopify_product_id
    elif mapping_id:
        m = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
        if m:
            shopify_id = m.shopify_product_id

    if shopify_id:
        _invalidate_lock_cache(shop, shopify_id)

    # Repair records left in product-wide manual mode by the old per-variant
    # edit behavior. The variant lock itself now protects only that price.
    if lock_type == "price" and new_state:
        if product_id:
            p = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
            if p and p.price_mode == "manual":
                p.price_mode = "variant_manual"
                p.custom_price = None
                db.commit()
        elif mapping_id:
            m = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
            if m and m.price_mode == "manual":
                m.price_mode = "variant_manual"
                db.commit()

    return {"variant_id": variant_id, "lock_type": lock_type, "locked": new_state}


@api.get("/dashboard/products/{product_id}/variant-locks")
def get_variant_locks(product_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id, ImportedProduct.shop_id == shop.id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        return {"locks": {}}
    return {"locks": get_variant_lock_map(shop, product.shopify_product_id)}


@api.get("/mappings/{mapping_id}/variant-locks")
def get_mapping_variant_locks(mapping_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id, ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    return {"locks": get_variant_lock_map(shop, mapping.shopify_product_id)}


# ─────────────────────────────────────────────
# BULK INCREASE
# ─────────────────────────────────────────────
@api.post("/dashboard/products/bulk-increase")
def bulk_increase_imported_products(payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    product_ids = payload.get("product_ids", [])
    require_bulk(db, shop, "imported", product_ids)
    increase_by = payload.get("increase_by")
    if not product_ids or increase_by is None:
        raise HTTPException(400, "Missing product_ids or increase_by")
    try:
        increase_by = float(increase_by)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid increase amount")

    products = db.query(ImportedProduct).filter(
        ImportedProduct.shop_id == shop.id, ImportedProduct.id.in_(product_ids),
        ImportedProduct.shopify_product_id.isnot(None)
    ).all()
    if not products:
        raise HTTPException(404, "No valid products found")

    results = []
    for prod in products:
        try:
            require_access(db, shop, prod.aliexpress_id)
            latest_data = get_product(prod.aliexpress_id, db)
            skus = latest_data.get("skus", [])
            if not skus:
                raise ValueError("No AliExpress SKUs found")
            for sku in skus:
                base_price = sku.get("sale_price") or sku.get("price")
                if base_price is not None:
                    new_price = float(base_price) + increase_by
                    sku.setdefault("_supplier_price", sku.get("sale_price") or sku.get("price"))
                    sku["sale_price"] = str(new_price)
                    sku["price"] = str(new_price)
            update_result = update_shopify_product_prices_with_skus(
                shop, prod.shopify_product_id, skus, reset_manual_increases=True
            )
            if update_result == "failed":
                raise ValueError("Shopify rejected the variant price update")
            prod.price_mode = "increase"
            prod.price_increase = increase_by
            prod.price_percentage = 0.0
            prod.custom_price = None
            original_price = latest_data.get("sale_price") or latest_data.get("original_price")
            if original_price:
                prod.original_price = original_price
            results.append({"id": prod.id, "aliexpress_id": prod.aliexpress_id, "success": True})
        except Exception as exc:
            results.append({"id": prod.id, "aliexpress_id": prod.aliexpress_id,
                            "success": False, "error": str(exc)})

    succeeded = sum(1 for result in results if result["success"])
    db.commit()
    if succeeded == 0:
        raise HTTPException(502, detail={"message": "Shopify prices were not updated", "results": results})
    failed = len(results) - succeeded
    message = f"Updated Shopify prices for {succeeded} product(s)"
    if failed:
        message += f"; {failed} failed"
    return {"message": message, "updated": succeeded, "failed": failed, "results": results}

@api.post("/mappings/bulk-increase")
def bulk_increase_mappings(payload: dict, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    mapping_ids = payload.get("mapping_ids", [])
    require_bulk(db, shop, "mapping", mapping_ids)
    increase_by = payload.get("increase_by")
    if not mapping_ids or increase_by is None:
        raise HTTPException(400, "Missing mapping_ids or increase_by")
    try:
        increase_by = float(increase_by)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid increase amount")

    if not math.isfinite(increase_by):
        raise HTTPException(400, "Increase amount must be finite")

    mappings = db.query(ProductMapping).filter(
        ProductMapping.shop_id == shop.id, ProductMapping.id.in_(mapping_ids), ProductMapping.track_price == True
    ).all()
    if not mappings:
        raise HTTPException(404, "No active mappings found")

    results = []
    for m in mappings:
        try:
            increase_mapping_price(m.id, {"increase_by": increase_by}, db, shop)
            success = True
            results.append({"id": m.id, "aliexpress_id": m.aliexpress_id, "shopify_product_id": m.shopify_product_id, "success": success, "price_mode": m.price_mode, "price_increase": m.price_increase})
        except Exception as e:
            results.append({"id": m.id, "aliexpress_id": m.aliexpress_id, "success": False, "error": str(e.detail) if isinstance(e, HTTPException) else str(e)})

    succeeded = sum(row["success"] for row in results)
    return {"message": f"Increased {succeeded}/{len(results)} mapping(s)", "results": results,
            "success_count": succeeded, "failed_count": len(results) - succeeded}



# ─────────────────────────────────────────────
# PENDING IMPORTS
# ─────────────────────────────────────────────
@api.get("/pending/list")
def list_pending_imports(db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    pendings = db.query(PendingImport).filter(PendingImport.shop_id == shop.id).order_by(PendingImport.created_at.desc()).all()
    result = []
    for p in pendings:
        product_data = p.product_data or {}
        oos_skus = p.out_of_stock_skus or []
        ins_skus = p.in_stock_skus or []
        total_skus = len(product_data.get("skus") or []) or (len(oos_skus) + len(ins_skus))
        # Stored workflow status is not proof that every variant is out of stock.
        # Use the latest saved stock lists, including for older misclassified rows.
        unknown_count = max(0, total_skus - len(oos_skus) - len(ins_skus))
        if oos_skus and ins_skus:
            stock_status = "partial"
        elif oos_skus and not unknown_count:
            stock_status = "pending"
        elif unknown_count or not total_skus:
            stock_status = "unknown"
        else:
            stock_status = "imported" if p.status == "imported" else "in_stock"
        result.append({
            "id": p.id, "aliexpress_id": p.aliexpress_id, "title": product_data.get("title") or "—",
            "main_image": product_data.get("main_image"), "total_skus": total_skus,
            "out_of_stock_skus": oos_skus, "in_stock_skus": ins_skus, "status": stock_status,
            "queue_status": p.status, "unknown_stock_count": unknown_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "last_checked": p.last_checked.isoformat() if p.last_checked else None,
            "retry_count": p.retry_count or 0,
        })
    return {"pendings": result}


@api.post("/pending/retry")
def retry_pending_import(aliexpress_id: str, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    pending = db.query(PendingImport).filter(
        PendingImport.shop_id == shop.id, PendingImport.aliexpress_id == aliexpress_id,
        PendingImport.status.in_(["pending", "partial", "failed"])
    ).first()
    if not pending:
        raise HTTPException(404, "No active pending record found")

    require_access(db, shop, aliexpress_id)
    raw = get_product(aliexpress_id, db)
    stock_info = classify_skus(raw)
    pending.product_data = raw
    pending.out_of_stock_skus = stock_info["out_of_stock"]
    pending.in_stock_skus = stock_info["in_stock"]
    if stock_info["any_oos"]:
        pending.status = "partial" if stock_info["any_in_stock"] else "pending"
    pending.last_checked = sqlfunc.now()
    pending.retry_count = (pending.retry_count or 0) + 1
    db.commit()

    imported = db.query(ImportedProduct).filter(
        ImportedProduct.shop_id == shop.id, ImportedProduct.aliexpress_id == aliexpress_id
    ).first()

    if (not imported or not imported.shopify_product_id) and stock_info["any_in_stock"]:
        try:
            product, created = import_aliexpress_product_to_shopify(shop, raw, db)
            pending.status = "imported" if stock_info["all_in_stock"] else "partial"
            db.commit()
            return {"status": "imported", "shopify_product_id": product.shopify_product_id,
                    "message": "Imported; remaining variants are still being tracked." if pending.status == "partial" else "All variants in stock; imported successfully.",
                    "can_retry": pending.status != "imported"}
        except HTTPException as e:
            raise e

    skus = raw.get("skus", [])
    inv_updated = update_shopify_product_inventory_with_skus(shop, imported.shopify_product_id, skus) if skus and not stock_info["no_info"] and imported and imported.shopify_product_id else False

    oos = stock_info["out_of_stock"]
    ins = stock_info["in_stock"]
    if stock_info["all_in_stock"] and inv_updated:
        pending.status = "imported"; db.commit()
        return {"status": "imported", "message": "All variants in stock!", "inventory_updated": inv_updated, "can_retry": False}
    elif ins and oos:
        pending.status = "partial"; db.commit()
    else:
        db.commit()

    oos_labels = [s.get("label") or s.get("sku_id") or "—" for s in oos]
    return {"status": "oos", "out_of_stock_skus": oos, "out_of_stock_labels": oos_labels,
            "in_stock_count": len(ins), "oos_count": len(oos), "inventory_updated": inv_updated, "can_retry": True}


@api.delete("/pending/{pending_id}")
def delete_pending_import(pending_id: int, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    pending = db.query(PendingImport).filter(PendingImport.id == pending_id, PendingImport.shop_id == shop.id).first()
    if not pending:
        raise HTTPException(404, "Pending record not found")
    db.delete(pending); db.commit()
    return {"message": "Pending record deleted"}


@api.post("/admin/process-pending")
def process_pending_now(shop: Shop = Depends(get_current_shop)):
    process_pending_imports(shop.id)
    return {"message": "Pending imports processed"}


@api.post("/admin/requeue-partial-imports")
def requeue_partial_imports(db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    pendings = db.query(PendingImport).filter(PendingImport.shop_id == shop.id, PendingImport.status == "imported").all()
    requeued = 0
    for p in pendings:
        oos = p.out_of_stock_skus or []
        if len(oos) > 0:
            p.status = "partial" if p.in_stock_skus else "pending"
            requeued += 1
    db.commit()
    return {"message": f"Re-queued {requeued} partially-imported products", "requeued": requeued}


# ─────────────────────────────────────────────
# BACKGROUND ADMIN JOBS
# ─────────────────────────────────────────────
@api.post("/admin/backfill-oos-to-pending")
def backfill_oos_to_pending(background_tasks: BackgroundTasks, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_any_full(db, shop)
    shop_id = shop.id

    def run():
        inner_db = SessionLocal()
        inner_db.info["shop_id"] = shop_id
        try:
            inner_shop = inner_db.query(Shop).filter(Shop.id == shop_id).first()
            products = inner_db.query(ImportedProduct).filter(
                ImportedProduct.shop_id == shop_id, ImportedProduct.shopify_product_id.isnot(None)
            ).all()
            print(f"[Backfill] Scanning {len(products)} imported products for OOS variants...")
            queued = skipped = errors = 0
            for prod in products:
                if not has_full_access(inner_db, inner_shop, prod.aliexpress_id):
                    continue
                try:
                    existing = inner_db.query(PendingImport).filter(
                        PendingImport.shop_id == shop_id, PendingImport.aliexpress_id == prod.aliexpress_id
                    ).first()
                    if existing:
                        skipped += 1
                        continue
                    raw = get_product(prod.aliexpress_id, inner_db)
                    stock_info = classify_skus(raw)
                    if stock_info["out_of_stock"]:
                        upsert_pending(inner_shop, prod.aliexpress_id, raw, stock_info, "pending", inner_db)
                        queued += 1
                    else:
                        skipped += 1
                except Exception as e:
                    errors += 1
                    print(f"[Backfill] Error for {prod.aliexpress_id}: {e}")
            print(f"[Backfill] Done — queued={queued}, skipped={skipped}, errors={errors}")
        finally:
            inner_db.close()

    background_tasks.add_task(run)
    return {"message": "Backfill started in background — check terminal for progress"}


@api.post("/admin/backfill-sku-images")
def backfill_sku_images_endpoint(background_tasks: BackgroundTasks, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    shop_id = shop.id

    def run():
        inner_db = SessionLocal()
        inner_db.info["shop_id"] = shop_id
        try:
            inner_shop = inner_db.query(Shop).filter(Shop.id == shop_id).first()
            products = inner_db.query(ImportedProduct).filter(
                ImportedProduct.shop_id == shop_id, ImportedProduct.shopify_product_id.isnot(None)
            ).all()
            total_attached = total_skipped = errors = 0
            for prod in products:
                try:
                    raw = get_product(prod.aliexpress_id, inner_db)
                    skus = raw.get("skus", [])
                    has_images = any(s.get("image") for s in skus)
                    if not has_images:
                        total_skipped += 1
                        continue
                    result = backfill_sku_images(inner_shop, prod.shopify_product_id, skus)
                    total_attached += result["attached"]
                    total_skipped += result["skipped"]
                except Exception as e:
                    errors += 1
                    print(f"[BackfillImages] Error for {prod.aliexpress_id}: {e}")
            print(f"[BackfillImages] Done — attached={total_attached} skipped={total_skipped} errors={errors}")
        finally:
            inner_db.close()

    background_tasks.add_task(run)
    return {"message": "SKU image backfill started in background — check terminal for progress"}


@api.post("/dashboard/products/bulk-sync-inventory")
def bulk_sync_inventory(payload: dict, background_tasks: BackgroundTasks, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    product_ids = payload.get("product_ids", [])
    require_bulk(db, shop, "imported", product_ids)
    query = db.query(ImportedProduct).filter(ImportedProduct.shop_id == shop.id, ImportedProduct.shopify_product_id.isnot(None))
    if product_ids:
        query = query.filter(ImportedProduct.id.in_(product_ids))
    products = query.all()
    products = [item for item in products if has_full_access(db, shop, item.aliexpress_id)]
    if not products:
        raise HTTPException(404, "No valid products found with a linked Shopify ID")

    total = len(products)
    shop_id = shop.id
    product_snapshots = [{"id": p.id, "aliexpress_id": p.aliexpress_id, "shopify_product_id": p.shopify_product_id} for p in products]

    def run():
        inner_db = SessionLocal()
        inner_db.info["shop_id"] = shop_id
        inner_shop = inner_db.query(Shop).filter(Shop.id == shop_id).first()
        success = failed = skipped = 0
        try:
            for snap in product_snapshots:
                if not has_full_access(inner_db, inner_shop, snap["aliexpress_id"]):
                    continue
                try:
                    raw = get_product(snap["aliexpress_id"], inner_db)
                    skus = raw.get("skus", [])
                    if not skus:
                        skipped += 1
                        continue
                    has_stock_data = any(s.get("stock") is not None for s in skus)
                    if not has_stock_data:
                        skipped += 1
                        continue
                    updated = update_shopify_product_inventory_with_skus(inner_shop, snap["shopify_product_id"], skus)
                    new_total = raw.get("total_stock")
                    if new_total is not None:
                        prod = inner_db.query(ImportedProduct).filter(ImportedProduct.id == snap["id"]).first()
                        if prod:
                            prod.total_stock = new_total
                            inner_db.commit()
                    if updated:
                        success += 1
                    else:
                        skipped += 1
                except Exception as e:
                    failed += 1
                    print(f"[BulkInventory] {snap['aliexpress_id']}: {e}")
        finally:
            inner_db.close()
            print(f"[BulkInventory] Done — updated={success} unchanged/skipped={skipped} errors={failed}")

    background_tasks.add_task(run)
    return {"message": f"Bulk inventory sync started for {total} product(s) — running in background",
            "total": total, "note": "Check terminal logs for per-product progress."}


@api.post("/admin/backfill-vendor")
def backfill_vendor(background_tasks: BackgroundTasks, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    products = db.query(ImportedProduct).filter(
        ImportedProduct.shop_id == shop.id, ImportedProduct.shopify_product_id.isnot(None)
    ).all()
    if not products:
        raise HTTPException(404, "No imported products with a Shopify ID found")

    total = len(products)
    snapshots = [{"id": p.id, "aliexpress_id": p.aliexpress_id, "shopify_product_id": p.shopify_product_id} for p in products]
    shop_domain, access_token = shop.shop_domain, shop.access_token

    def run():
        success = failed = 0
        for snap in snapshots:
            try:
                res = _shopify_request("PUT", f"{_base(shop_domain)}/products/{snap['shopify_product_id']}.json",
                    json={"product": {"vendor": "ugne"}}, headers=_h(access_token), timeout=15)
                if res.status_code == 200:
                    success += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                print(f"[VendorBackfill] {snap['aliexpress_id']}: {e}")
        print(f"[VendorBackfill] Done — updated={success} errors={failed}")

    background_tasks.add_task(run)
    return {"message": f"Vendor backfill started for {total} product(s) — running in background", "total": total}


@api.post("/admin/backfill-sku-metafields")
def backfill_sku_metafields(db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    products = db.query(ImportedProduct).filter(
        ImportedProduct.shop_id == shop.id, ImportedProduct.shopify_product_id.isnot(None)
    ).all()
    count = 0
    for prod in products:
        if not prod.skus:
            continue
        try:
            store_aliexpress_sku_ids(shop, prod.shopify_product_id, prod.skus)
            count += 1
        except Exception as e:
            print(f"Error for {prod.aliexpress_id}: {e}")
    return {"backfilled": count}


@api.post("/admin/backfill-product-skus")
def backfill_product_skus(background_tasks: BackgroundTasks, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_any_full(db, shop)
    shop_id = shop.id

    def run():
        inner_db = SessionLocal()
        inner_db.info["shop_id"] = shop_id
        products = inner_db.query(ImportedProduct).filter(
            ImportedProduct.shop_id == shop_id, ImportedProduct.shopify_product_id.isnot(None)
        ).all()
        for prod in products:
            if not has_full_access(inner_db, shop, prod.aliexpress_id):
                continue
            try:
                raw = get_product(prod.aliexpress_id, inner_db)
                prod.skus = raw.get("skus")
                prod.sku_count = raw.get("sku_count")
                inner_db.commit()
            except Exception as e:
                print(f"[BackfillSkus] Failed {prod.aliexpress_id}: {e}")
        inner_db.close()

    background_tasks.add_task(run)
    return {"message": "SKU backfill started – check terminal for progress"}


# ─────────────────────────────────────────────
# SMART LOOKUP
# ─────────────────────────────────────────────
@api.get("/dashboard/products/lookup")
def lookup_product(q: str = Query(..., description="AliExpress ID (old or new), Shopify ID, or title keyword"),
                    db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    try:
        q = q.strip()
        if not q:
            raise HTTPException(400, "Search query q is required")

        results = []
        seen_imported_ids = set()
        seen_mapping_ids = set()

        def add_imported(p, reason: str):
            if p.id in seen_imported_ids:
                return
            seen_imported_ids.add(p.id)
            results.append({
                "id": p.id, "source": "imported", "aliexpress_id": p.aliexpress_id,
                "replacement_aliexpress_id": p.replacement_aliexpress_id,
                "title": p.custom_title or p.original_title, "main_image": p.main_image,
                "price": p.custom_price or p.original_price,
                **_ali_sale_price_display({"original_price": p.original_price, "currency": p.currency, "skus": p.skus}),
                "shopify_product_id": p.shopify_product_id, "shopify_status": p.shopify_status,
                "is_dead_listing": p.is_dead_listing, "imported_at": p.imported_at.isoformat() if p.imported_at else None,
                "match_reason": reason, "aliexpress_url": f"https://www.aliexpress.com/item/{p.aliexpress_id}.html",
                "shopify_url": f"https://{shop.shop_domain}/admin/products/{p.shopify_product_id}" if p.shopify_product_id else None,
                "avg_rating": p.custom_rating or p.avg_rating, "rating": p.custom_rating or p.avg_rating,
                "sku_count": p.sku_count, "track_price": p.track_price, "price_mode": p.price_mode,
                "price_increase": p.price_increase, "price_percentage": p.price_percentage, "store_name": p.store_name,
                "custom_title": p.custom_title, "custom_description": p.custom_description,
                "custom_rating": p.custom_rating, "original_title": p.original_title, "skus": p.skus,
            })

        def add_mapping(m, reason: str):
            if m.id in seen_mapping_ids:
                return
            seen_mapping_ids.add(m.id)
            results.append({
                "id": m.id, "source": "mapping", "aliexpress_id": m.aliexpress_id, "replacement_aliexpress_id": m.replacement_aliexpress_id,
                "title": getattr(m, "custom_title", None) or m.shopify_product_title, "main_image": None,
                "track_price": m.track_price, "price_mode": m.price_mode, "price_increase": m.price_increase, "price_percentage": m.price_percentage,
                "custom_title": m.custom_title, "custom_description": m.custom_description,
                "custom_rating": m.custom_rating, "shopify_product_title": m.shopify_product_title,
                "price": None, "currency": None, "shopify_product_id": m.shopify_product_id, "shopify_status": None,
                "is_dead_listing": m.is_dead_listing, "imported_at": m.created_at.isoformat() if m.created_at else None,
                "match_reason": reason, "aliexpress_url": f"https://www.aliexpress.com/item/{m.aliexpress_id}.html",
                "shopify_url": f"https://{shop.shop_domain}/admin/products/{m.shopify_product_id}" if m.shopify_product_id else None,
            })

        base_ip = db.query(ImportedProduct).filter(ImportedProduct.shop_id == shop.id)
        base_pm = db.query(ProductMapping).filter(ProductMapping.shop_id == shop.id)

        for p in base_ip.filter(ImportedProduct.aliexpress_id == q).all():
            add_imported(p, "Exact AliExpress ID match (current ID)")
        for p in base_ip.filter(ImportedProduct.replacement_aliexpress_id == q).all():
            add_imported(p, f"Old AliExpress ID match — current ID is now {p.aliexpress_id}")
        for p in base_ip.filter(ImportedProduct.shopify_product_id == q).all():
            add_imported(p, "Shopify product ID match")
        for p in base_ip.filter(ImportedProduct.aliexpress_id.ilike(f"%{q}%")).all():
            add_imported(p, "Partial AliExpress ID match (current ID)")
        for p in base_ip.filter(ImportedProduct.replacement_aliexpress_id.ilike(f"%{q}%")).all():
            add_imported(p, f"Partial old AliExpress ID match — current ID is {p.aliexpress_id}")

        term = f"%{q}%"
        for p in base_ip.filter((ImportedProduct.original_title.ilike(term)) | (ImportedProduct.custom_title.ilike(term))).all():
            add_imported(p, "Title keyword match")

        for m in base_pm.filter(ProductMapping.aliexpress_id == q).all():
            add_mapping(m, "Exact AliExpress ID match (mapping)")
        for m in base_pm.filter(ProductMapping.shopify_product_id == q).all():
            add_mapping(m, "Shopify product ID match (mapping)")
        for m in base_pm.filter(ProductMapping.aliexpress_id.ilike(f"%{q}%")).all():
            add_mapping(m, "Partial AliExpress ID match (mapping)")

        for p in base_ip.filter(ImportedProduct.shopify_product_id.ilike(term)).all():
            add_imported(p, "Partial Shopify product ID match")
        for m in base_pm.filter(ProductMapping.shopify_product_id.ilike(term)).all():
            add_mapping(m, "Partial Shopify product ID match (mapping)")
        for m in base_pm.filter(ProductMapping.replacement_aliexpress_id.ilike(term)).all():
            add_mapping(m, f"Old AliExpress ID match (mapping) - current ID is {m.aliexpress_id}")

        mapping_title_filter = ProductMapping.shopify_product_title.ilike(term)
        if hasattr(ProductMapping, "custom_title"):
            mapping_title_filter = mapping_title_filter | ProductMapping.custom_title.ilike(term)
        for m in base_pm.filter(mapping_title_filter).all():
            add_mapping(m, "Title keyword match (mapping)")

        if not results:
            return {"found": False, "count": 0, "results": [],
                    "message": f"No product found matching '{q}'. Try Settings → Dead Listing Scanner, or search by title."}

        return {"found": True, "count": len(results), "results": results, "message": f"Found {len(results)} product(s) matching '{q}'"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"[Lookup][ERROR] q={q!r}: {e}")
        traceback.print_exc()
        raise HTTPException(500, f"Lookup failed: {e}")


# ─────────────────────────────────────────────
# MANUAL BULK SYNC / DEBUG / SUBSCRIPTION
# ─────────────────────────────────────────────
@api.post("/dashboard/sync-now")
def manual_sync(background_tasks: BackgroundTasks, db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    require_any_full(db, shop)
    products = db.query(ImportedProduct).filter(ImportedProduct.shop_id == shop.id, ImportedProduct.track_price == True).all()
    def run_sync():
        sub_db = SessionLocal()
        sub_db.info["shop_id"] = shop.id
        for p in products:
            sync_product_price(shop, p.id, sub_db)
        sub_db.close()
    background_tasks.add_task(run_sync)
    return {"message": f"Price sync started for {len(products)} products"}


@api.get("/debug/scheduler-status")
def scheduler_status(shop: Shop = Depends(get_current_shop)):
    scheduler = globals().get("scheduler")
    if scheduler is None:
        return {"running": False, "message": "Scheduler not started"}
    jobs = scheduler.get_jobs()
    return {"running": True, "jobs": [{"id": j.id, "next_run_time": str(j.next_run_time), "trigger": str(j.trigger)} for j in jobs]}


@api.post("/debug/run-sync-now")
def debug_run_sync_now(background_tasks: BackgroundTasks, db: Session = Depends(get_db),
                       shop: Shop = Depends(get_current_shop)):
    return manual_sync(background_tasks, db, shop)


# @api.get("/subscription/status")
# def subscription_status(shop: Shop = Depends(get_current_shop)):
#     return {
#         "status": shop.subscription_status, "plan": shop.plan_name,
#         "trial_ends_at": shop.trial_ends_at.isoformat() if shop.trial_ends_at else None,
#         "is_active": shop.subscription_status == "active",
#     }


# main.py

@api.get("/subscription/status")
def subscription_status(shop: Shop = Depends(get_current_shop), db: Session = Depends(get_db)):
    return subscription_summary(db, account(db, shop.user_id))


@api.get("/debug/product/{shopify_product_id}")
def debug_product_prices(shopify_product_id: str, shop: Shop = Depends(get_current_shop)):
    res = _shopify_request("GET", f"{_base(shop.shop_domain)}/products/{shopify_product_id}.json",
                           params={"fields": "id,title,variants"}, headers=_h(shop.access_token))
    if res.status_code != 200:
        raise HTTPException(502, res.text)
    product = res.json().get("product", {})
    return {"shopify_id": product.get("id"), "title": product.get("title"),
            "variants": [{"id": v.get("id"), "option1": v.get("option1"), "price": v.get("price")}
                         for v in product.get("variants", [])]}


@api.get("/debug/aliexpress-raw/{aliexpress_id}")
def debug_aliexpress_raw(aliexpress_id: str, db: Session = Depends(get_db),
                         shop: Shop = Depends(get_current_shop)):
    # get_product uses this request's user-scoped token and preserves the full normalized payload.
    reserve_product(db, shop, aliexpress_id)
    return get_product(aliexpress_id, db)


@api.post("/mappings/{mapping_id}/update-price")
def update_mapping_price(mapping_id: int, payload: dict, db: Session = Depends(get_db),
                         shop: Shop = Depends(get_current_shop)):
    require_entity(db, shop, "mapping", mapping_id)
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id,
        ProductMapping.shop_id == shop.id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    try:
        new_price = float(payload.get("price"))
    except (TypeError, ValueError):
        raise HTTPException(400, "A valid price is required")
    if new_price < 0:
        raise HTTPException(400, "Price cannot be negative")
    if not update_shopify_product_price(shop, mapping.shopify_product_id, new_price):
        raise HTTPException(502, "Failed to update Shopify product price")
    mapping.price_mode = "manual"
    mapping.price_increase = 0.0
    mapping.price_percentage = 0.0
    db.commit()
    return {"message": f"Price updated to {new_price} (manual mode)",
            "product_id": mapping.shopify_product_id, "price_mode": "manual"}


@api.post("/mappings/sync-all")
def sync_all_mapped_products(background_tasks: BackgroundTasks, db: Session = Depends(get_db),
                             shop: Shop = Depends(get_current_shop)):
    require_any_full(db, shop)
    ids = [row.id for row in db.query(ProductMapping).filter(
        ProductMapping.shop_id == shop.id, ProductMapping.track_price == True).all()]
    if not ids:
        return {"message": "No mapped products to sync"}

    def run(selected_shop_id: int, mapping_ids: list[int]):
        sub_db = SessionLocal()
        try:
            selected_shop = sub_db.query(Shop).filter(Shop.id == selected_shop_id, Shop.is_active == True).first()
            if not selected_shop:
                return
            sub_db.info["shop_id"] = selected_shop.id
            sub_db.info["user_id"] = selected_shop.user_id
            for row in sub_db.query(ProductMapping).filter(ProductMapping.id.in_(mapping_ids),
                    ProductMapping.shop_id == selected_shop.id).all():
                try:
                    require_access(sub_db, selected_shop, row.aliexpress_id)
                    raw = get_product(row.aliexpress_id, sub_db)
                    skus = raw.get("skus") or []
                    if skus:
                        update_shopify_product_prices_with_skus(selected_shop, row.shopify_product_id,
                            _apply_product_price_increase(skus, row.price_increase, row.price_percentage))
                        update_shopify_product_inventory_with_skus(selected_shop, row.shopify_product_id, skus)
                except Exception as exc:
                    print(f"[SyncMapped][{selected_shop.shop_domain}] {row.aliexpress_id}: {exc}")
        finally:
            sub_db.close()

    background_tasks.add_task(run, shop.id, ids)
    return {"message": f"Started price and inventory sync for {len(ids)} mapped products"}

# ─────────────────────────────────────────────
# ROUTER REGISTRATION — MUST BE LAST
# ─────────────────────────────────────────────



@api.post("/pricing/{entity_type}/{entity_id}/markup")
def set_product_markup(entity_type: str, entity_id: int, payload: dict,
                       db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    if entity_type not in {"imported", "mapping"}:
        raise HTTPException(400, "Invalid product type")
    entity = require_entity(db, shop, entity_type, entity_id)
    kind = payload.get("kind", "fixed")
    try:
        amount = float(payload["amount"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Enter a valid markup amount")
    if kind not in {"fixed", "percentage"} or not math.isfinite(amount):
        raise HTTPException(400, "Use a finite fixed or percentage adjustment")
    if kind == "percentage" and amount < -100:
        raise HTTPException(400, "Percentage cannot be less than -100%")
    if not entity.shopify_product_id:
        raise HTTPException(400, "No Shopify product linked")
    raw = get_product(entity.aliexpress_id, db)
    fixed, percentage = (amount, 0) if kind == "fixed" else (0, amount)
    skus = _apply_product_price_increase(raw.get("skus", []), fixed, percentage)
    errors = []
    result = update_shopify_product_prices_with_skus(
        shop, entity.shopify_product_id, skus, error_details=errors, reset_manual_increases=True)
    if result == "failed":
        raise HTTPException(502, "; ".join(errors) or "Could not apply markup to Shopify")
    entity.price_increase, entity.price_percentage = fixed, percentage
    entity.price_mode = "percentage" if kind == "percentage" else "increase"
    if entity_type == "imported": entity.custom_price = None
    db.commit()
    return {"message": f"{kind.title()} adjustment {amount:+g}{'%' if kind == 'percentage' else ' USD'} saved and applied to unlocked variants", "price_mode": entity.price_mode}


@api.get("/product-mapping/{entity_type}/{entity_id}/variants")
def variant_mapping_options(entity_type: str, entity_id: int, db: Session = Depends(get_db),
                            shop: Shop = Depends(get_current_shop)):
    if entity_type not in {"imported", "mapping"}: raise HTTPException(400, "Invalid product type")
    entity = require_entity(db, shop, entity_type, entity_id, "mapping")
    state = get_variant_sync_state(shop, entity.shopify_product_id)
    raw = get_product(entity.aliexpress_id, db)
    return {"variants": [{"id": vid, "label": str(variant.get("title") or variant.get("sku") or vid),
                          "supplier_sku_id": variant.get("ae_sku_id")} for vid, variant in state.items()],
            "supplier_variants": [{"id": str(sku["sku_id"]), "label": sku.get("label") or sku.get("sku_attr") or str(sku["sku_id"])}
                                  for sku in raw.get("skus", []) if sku.get("sku_id") is not None]}


@api.post("/product-mapping/{entity_type}/{entity_id}/variants")
def save_variant_mapping(entity_type: str, entity_id: int, payload: dict,
                         db: Session = Depends(get_db), shop: Shop = Depends(get_current_shop)):
    if entity_type not in {"imported", "mapping"}: raise HTTPException(400, "Invalid product type")
    entity = require_entity(db, shop, entity_type, entity_id, "mapping")
    options = variant_mapping_options(entity_type, entity_id, db, shop)
    owned = {str(v["id"]) for v in options["variants"]}
    supplier = {v["id"] for v in options["supplier_variants"]}
    updates, seen_variants, seen_suppliers = [], set(), set()
    entries = payload.get("variants")
    if not isinstance(entries, list) or not entries: raise HTTPException(400, "Choose variant mappings")
    for entry in entries:
        if not isinstance(entry, dict): raise HTTPException(400, "Invalid variant mapping")
        vid, sku = str(entry.get("variant_id", "")), str(entry.get("supplier_sku_id", ""))
        if vid not in owned or sku not in supplier: raise HTTPException(400, "Choose variants belonging to this product and supplier")
        if vid in seen_variants or sku in seen_suppliers: raise HTTPException(400, "Each variant must have a unique supplier mapping")
        seen_variants.add(vid); seen_suppliers.add(sku)
        updates.append({"variant_id": int(vid), "ae_sku_id": sku})
    for offset in range(0,len(updates),100):
        batch=updates[offset:offset+100]
        result=bulk_update_variant_prices(shop,entity.shopify_product_id,batch)
        if not result.get("success") or result.get("errors") or result.get("updated") != len(batch):
            raise HTTPException(502, "Shopify could not save all variant mappings. Refresh and retry.")
    return {"message": "Supplier variant mappings saved. Prices and inventory were not changed."}
