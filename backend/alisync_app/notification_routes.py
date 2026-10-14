from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from .database import get_db
from .models import Mapping, Notification, NotificationPreference, Product, Shop, User
from .notifications import create_notification, get_preferences
from .security import current_user

router = APIRouter(prefix="/app/notifications", tags=["Notifications"])


def selected_shop_id(db: Session, user: User, x_shop_id: int | None) -> int | None:
    """Resolve an owned selected shop without triggering an external token refresh."""
    query = db.query(Shop.id).filter(Shop.user_id == user.id, Shop.is_active == True)
    if x_shop_id is not None:
        shop_id = query.filter(Shop.id == x_shop_id).scalar()
        if shop_id is None:
            raise HTTPException(404, "Selected Shopify shop not found")
        return shop_id
    rows = [row[0] for row in query.order_by(Shop.id.asc()).limit(2).all()]
    return rows[0] if len(rows) == 1 else None


def reconcile_supplier_notifications(db: Session, user: User, shop_id: int | None) -> None:
    """Backfill current supplier warnings that predate the notification system."""
    if shop_id is None:
        return
    states = {"sync_warning", "verification_in_progress", "supplier_unavailable", "confirmed_dead"}
    entities = [
        ("product", row) for row in db.query(Product).filter(
            Product.shop_id == shop_id, Product.supplier_status.in_(states)).all()
    ] + [
        ("mapping", row) for row in db.query(Mapping).filter(
            Mapping.shop_id == shop_id, Mapping.supplier_status.in_(states)).all()
    ]
    for entity_type, entity in entities:
        status = entity.supplier_status
        confirmed = status == "confirmed_dead"
        dedupe_key = f"supplier:{entity_type}:{entity.id}:{status}"
        if db.query(Notification.id).filter(
            Notification.user_id == user.id,
            Notification.shop_id == shop_id,
            Notification.dedupe_key == dedupe_key,
        ).first():
            continue
        product_name = (getattr(entity, "custom_title", None) or getattr(entity, "original_title", None)
                        or getattr(entity, "shopify_product_title", None) or f"AliExpress {entity.aliexpress_id}")
        create_notification(
            db, user_id=user.id, shop_id=shop_id, entity_type=entity_type, entity_id=str(entity.id),
            event_type="dead_listing" if confirmed else "sync_failed",
            category="dead_listings" if confirmed else "sync",
            priority="critical" if confirmed else "warning",
            title="Dead supplier listing confirmed" if confirmed else "Supplier sync needs verification",
            message=(f"{product_name}: supplier status is {status.replace('_', ' ')} after "
                     f"{entity.verification_attempts or 0}/3 checks."),
            product_name=product_name, product_image=getattr(entity, "main_image", None),
            new_value={"supplier_status": status},
            automatic_action=("Shopify inventory protection was applied after confirmation."
                              if confirmed and entity.inventory_protected
                              else "No Shopify product or inventory change was made."),
            merchant_action_required=confirmed, action_label="Review", action_url="dead-listings",
            supplier_url=f"https://www.aliexpress.com/item/{entity.aliexpress_id}.html",
            shopify_url=(f"https://admin.shopify.com/products/{entity.shopify_product_id}"
                         if entity.shopify_product_id else None),
            dedupe_key=dedupe_key,
        )
    db.commit()


def serialize(row: Notification, db: Session) -> dict:
    data = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    # Enrich older rows that were created before product links were persisted.
    if row.entity_type in {"product", "mapping"} and row.entity_id:
        try:
            model = Product if row.entity_type == "product" else Mapping
            entity = db.query(model).filter(model.id == int(row.entity_id)).first()
        except (TypeError, ValueError):
            entity = None
        if entity:
            data["aliexpress_id"] = entity.aliexpress_id
            data["shopify_product_id"] = entity.shopify_product_id
            data["editable_entity"] = {
                "id": entity.id, "entity_type": row.entity_type,
                "aliexpress_id": entity.aliexpress_id,
                "shopify_product_id": entity.shopify_product_id,
                "title": (getattr(entity, "custom_title", None) or getattr(entity, "original_title", None)
                          or getattr(entity, "shopify_product_title", None) or getattr(entity, "title", None)),
                "original_title": getattr(entity, "original_title", None),
                "shopify_product_title": getattr(entity, "shopify_product_title", None),
                "custom_title": getattr(entity, "custom_title", None),
                "custom_description": getattr(entity, "custom_description", None),
                "custom_rating": getattr(entity, "custom_rating", None),
                "avg_rating": getattr(entity, "avg_rating", None),
                "original_price": getattr(entity, "original_price", None),
                "currency": getattr(entity, "currency", None),
                "main_image": getattr(entity, "main_image", None),
                "skus": getattr(entity, "skus", None) or [],
                "track_price": entity.track_price,
                "price_mode": entity.price_mode,
                "price_increase": entity.price_increase,
                "price_percentage": entity.price_percentage,
                "supplier_status": getattr(entity, "supplier_status", None),
                "verification_attempts": getattr(entity, "verification_attempts", None),
                "inventory_protected": getattr(entity, "inventory_protected", None),
                "total_stock": getattr(entity, "total_stock", None),
                "last_failure_reason": getattr(entity, "last_failure_reason", None),
            }
            if not data.get("supplier_url") and entity.aliexpress_id:
                data["supplier_url"] = f"https://www.aliexpress.com/item/{entity.aliexpress_id}.html"
            if not data.get("shopify_url") and entity.shopify_product_id:
                data["shopify_url"] = f"https://admin.shopify.com/products/{entity.shopify_product_id}"
    return data


@router.get("")
def list_notifications(category: str | None = None, priority: str | None = None,
                       unread_only: bool = False, page: int = Query(1, ge=1),
                       page_size: int = Query(25, ge=1, le=100),
                       x_shop_id: int | None = Header(default=None, alias="X-Shop-ID"),
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    shop_id = selected_shop_id(db, user, x_shop_id)
    reconcile_supplier_notifications(db, user, shop_id)
    query = db.query(Notification).filter(Notification.user_id == user.id)
    if shop_id is not None:
        query = query.filter(Notification.shop_id == shop_id)
    if category and category != "all":
        query = query.filter(Notification.category == category)
    if priority:
        query = query.filter(Notification.priority == priority)
    if unread_only:
        query = query.filter(Notification.is_read == False)
    total = query.count()
    rows = query.order_by(Notification.created_at.desc(), Notification.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    unread_query = db.query(Notification).filter(Notification.user_id == user.id, Notification.is_read == False)
    critical_query = unread_query.filter(Notification.priority == "critical")
    if shop_id is not None:
        unread_query = unread_query.filter(Notification.shop_id == shop_id)
        critical_query = critical_query.filter(Notification.shop_id == shop_id)
    unread = unread_query.count()
    critical_unread = critical_query.count()
    return {"items": [serialize(row, db) for row in rows], "total": total, "unread": unread,
            "critical_unread": critical_unread, "page": page, "page_size": page_size}


@router.get("/summary")
def notification_summary(x_shop_id: int | None = Header(default=None, alias="X-Shop-ID"),
                         db: Session = Depends(get_db), user: User = Depends(current_user)):
    shop_id = selected_shop_id(db, user, x_shop_id)
    reconcile_supplier_notifications(db, user, shop_id)
    unread_query = db.query(Notification).filter(Notification.user_id == user.id, Notification.is_read == False)
    critical_query = unread_query.filter(Notification.priority == "critical")
    if shop_id is not None:
        unread_query = unread_query.filter(Notification.shop_id == shop_id)
        critical_query = critical_query.filter(Notification.shop_id == shop_id)
    unread = unread_query.count()
    critical = critical_query.count()
    return {"unread": unread, "critical_unread": critical}


@router.post("/{notification_id}/read")
def mark_read(notification_id: int, x_shop_id: int | None = Header(default=None, alias="X-Shop-ID"),
              db: Session = Depends(get_db), user: User = Depends(current_user)):
    shop_id = selected_shop_id(db, user, x_shop_id)
    row = db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == user.id).first()
    if not row or (shop_id is not None and row.shop_id != shop_id):
        raise HTTPException(404, "Notification not found")
    row.is_read = True
    row.read_at = datetime.now(timezone.utc)
    db.commit()
    return {"message": "Notification marked as read"}


@router.delete("/{notification_id}")
def delete_notification(notification_id: int, x_shop_id: int | None = Header(default=None, alias="X-Shop-ID"),
                        db: Session = Depends(get_db), user: User = Depends(current_user)):
    shop_id = selected_shop_id(db, user, x_shop_id)
    row = db.query(Notification).filter(
        Notification.id == notification_id, Notification.user_id == user.id,
    ).first()
    if not row or (shop_id is not None and row.shop_id != shop_id):
        raise HTTPException(404, "Notification not found")
    db.delete(row)
    db.commit()
    return {"message": "Notification deleted"}


@router.post("/read-all")
def mark_all_read(x_shop_id: int | None = Header(default=None, alias="X-Shop-ID"),
                  db: Session = Depends(get_db), user: User = Depends(current_user)):
    shop_id = selected_shop_id(db, user, x_shop_id)
    now = datetime.now(timezone.utc)
    query = db.query(Notification).filter(Notification.user_id == user.id, Notification.is_read == False)
    if shop_id is not None:
        query = query.filter(Notification.shop_id == shop_id)
    rows = query.all()
    for row in rows:
        row.is_read = True; row.read_at = now
    db.commit()
    return {"message": f"Marked {len(rows)} notification(s) as read", "updated": len(rows)}


@router.get("/preferences")
def preferences(db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = get_preferences(db, user.id); db.commit(); db.refresh(row)
    return {column.name: getattr(row, column.name) for column in row.__table__.columns
            if column.name not in {"id", "user_id", "updated_at"}}


@router.put("/preferences")
def update_preferences(payload: dict, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = get_preferences(db, user.id)
    editable = {column.name for column in NotificationPreference.__table__.columns} - {"id", "user_id", "updated_at"}
    for key, value in payload.items():
        if key in editable:
            setattr(row, key, value)
    row.low_stock_threshold = max(0, int(row.low_stock_threshold))
    row.large_price_change_percent = max(0.0, float(row.large_price_change_percent))
    row.large_inventory_drop_percent = min(100.0, max(0.0, float(row.large_inventory_drop_percent)))
    row.minimum_profit = max(0.0, float(row.minimum_profit))
    db.commit()
    return {"message": "Notification preferences saved"}
