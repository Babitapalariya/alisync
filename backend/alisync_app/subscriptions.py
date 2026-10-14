"""Server-owned account entitlements; billing is intentionally disabled by default."""
from fastapi import HTTPException
from .models import User, Product, Mapping, PlanProduct
from .config import get_settings

PLANS = {
    "free": {"name": "Free", "price": 0, "max_imported_products": 25, "full_feature_products": 5,
             "description": "Start free with 25 products and unlock full Pro features on your first 5 products."},
    "growth": {"name": "Growth", "price": 14, "max_imported_products": None, "full_feature_products": 5,
               "description": "Build without limits: source, import and map unlimited products, with full Pro features on 5 products of your choice."},
    "pro": {"name": "Pro", "price": 19, "max_imported_products": None, "full_feature_products": None,
            "description": "Everything included: unlimited sourcing, mapping, pricing and inventory automation."},
}
FREE_LIMIT = "You have reached the 25-product Free Plan limit. Upgrade to import unlimited products."
FULL_LIMIT = "Upgrade your plan to use full automation on more products."


def account(db, user_id, lock=False):
    query = db.query(User).filter(User.id == user_id).populate_existing()
    user = (query.with_for_update() if lock else query).first()
    if user is None:
        raise HTTPException(401, "Account unavailable")
    return user


def plan_name(user):
    return user.selected_plan if user.selected_plan in PLANS else "free"


def hydrate(db, user_id):
    # Existing catalogues stay intact; upgrades never delete or silently select products.
    user = db.query(User).filter_by(id=user_id).first()
    if user.subscription_catalog_initialized:
        return
    known = {(r.shop_id, r.aliexpress_id) for r in db.query(PlanProduct).filter_by(user_id=user_id).with_for_update().all()}
    for model in (Product, Mapping):
        for entity in db.query(model).filter_by(user_id=user_id).order_by(model.id).with_for_update().all():
            key = (entity.shop_id, entity.aliexpress_id)
            if key not in known:
                db.add(PlanProduct(user_id=user_id, shop_id=key[0], aliexpress_id=key[1], full_access=False))
                known.add(key)
    user.subscription_catalog_initialized = True
    db.flush()


def reserve_product(db, shop, aliexpress_id, mapping=False):
    user = account(db, shop.user_id, lock=True)
    hydrate(db, user.id)
    name = plan_name(user)
    row = db.query(PlanProduct).filter_by(shop_id=shop.id, aliexpress_id=str(aliexpress_id)).with_for_update().first()
    if row is None:
        if name == "free" and len(db.query(PlanProduct).filter_by(user_id=user.id).with_for_update().all()) >= 25:
            raise HTTPException(403, FREE_LIMIT)
        row = PlanProduct(user_id=user.id, shop_id=shop.id, aliexpress_id=str(aliexpress_id), full_access=False)
        db.add(row)
    db.commit()  # Reserve under the account row lock, before any external creation.
    return row


def has_full_access(db, shop, aliexpress_id):
    name = plan_name(account(db, shop.user_id))
    if name == "pro":
        return True
    rows = db.query(PlanProduct).filter_by(user_id=shop.user_id)
    if name == "free":
        return any(row.shop_id == shop.id and row.aliexpress_id == str(aliexpress_id)
                   for row in rows.order_by(PlanProduct.id).limit(5).all())
    return rows.filter_by(shop_id=shop.id, aliexpress_id=str(aliexpress_id), full_access=True).first() is not None


def require_access(db, shop, aliexpress_id, feature="automation"):
    name = plan_name(account(db, shop.user_id))
    if name == "pro" or (feature == "mapping" and name in {"free", "growth"}) or (feature == "relink" and name == "growth") or has_full_access(db, shop, aliexpress_id):
        return
    message = ("Choose this product for one of your 5 full-access slots in Plans, or upgrade to Pro. " + FULL_LIMIT
               if name == "growth" else "Free includes full Pro features on your first 5 products. Upgrade to Growth to choose 5 products, or Pro for all products.")
    raise HTTPException(403, message)


def require_entity(db, shop, entity_type, entity_id, feature="automation"):
    model = Mapping if entity_type == "mapping" else Product
    entity = db.query(model).filter_by(id=entity_id, shop_id=shop.id, user_id=shop.user_id).first()
    if entity is None:
        raise HTTPException(404, "Product not found")
    require_access(db, shop, entity.aliexpress_id, feature)
    return entity


def move_access(db, shop, old_id, new_id):
    account(db, shop.user_id, lock=True)
    old = db.query(PlanProduct).filter_by(shop_id=shop.id, aliexpress_id=str(old_id)).first()
    target = db.query(PlanProduct).filter_by(shop_id=shop.id, aliexpress_id=str(new_id)).first()
    if old and target and old.id != target.id:
        target.full_access = target.full_access or old.full_access
        db.delete(old)
    elif old:
        old.aliexpress_id = str(new_id)
    db.flush()


def subscription_summary(db, user):
    user = account(db, user.id, lock=True)
    hydrate(db, user.id)
    rows = db.query(PlanProduct).filter_by(user_id=user.id).order_by(PlanProduct.id).all()
    name = plan_name(user)
    products = []
    selected = {(r.shop_id, r.aliexpress_id): r.full_access for r in rows}
    free_keys = {(r.shop_id, r.aliexpress_id) for r in rows[:5]}
    for model, kind in ((Product, "imported"), (Mapping, "mapping")):
        for entity in db.query(model).filter_by(user_id=user.id).all():
            full = name == "pro" or (name == "free" and (entity.shop_id, entity.aliexpress_id) in free_keys) or (name == "growth" and selected.get((entity.shop_id, entity.aliexpress_id), False))
            products.append({"id": entity.id, "source": kind, "shop_id": entity.shop_id,
                "aliexpress_id": entity.aliexpress_id, "title": entity.custom_title or
                getattr(entity, "original_title", None) or getattr(entity, "shopify_product_title", None) or entity.title,
                "full_access": full, "selected": selected.get((entity.shop_id, entity.aliexpress_id), False)})
    db.commit()
    return {"plan": name, "status": user.subscription_status, "billing_enabled": get_settings().BILLING_ENABLED,
            "payment_required": False, "is_active": True, "plans": PLANS,
            "limits": PLANS[name], "usage": {"sourced_products": len(rows),
            "full_feature_products": len(rows) if name == "pro" else min(5, len(rows)) if name == "free" else sum(bool(r.full_access) for r in rows)}, "products": products}


def require_any_full(db, shop):
    if plan_name(account(db, shop.user_id)) == "pro":
        return
    rows = db.query(PlanProduct).filter_by(user_id=shop.user_id, shop_id=shop.id).all()
    if any(has_full_access(db, shop, row.aliexpress_id) for row in rows):
        return
    raise HTTPException(403, "Full automation requires a first-five Free product, a selected Growth product, or Pro.")


def require_bulk(db, shop, entity_type, ids):
    require_any_full(db, shop)
    for entity_id in ids:
        require_entity(db, shop, entity_type, entity_id)


def release_deleted_slot(db, user_id, shop_id, aliexpress_id):
    account(db, user_id, lock=True)
    db.flush()
    if any(db.query(model).filter_by(shop_id=shop_id, aliexpress_id=aliexpress_id).first() for model in (Product, Mapping)):
        return
    row = db.query(PlanProduct).filter_by(user_id=user_id, shop_id=shop_id, aliexpress_id=aliexpress_id).first()
    if row: row.full_access = False
