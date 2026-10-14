"""Persistent, deduplicated in-app and optional email notifications."""

import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from sqlalchemy.orm import Session

from .config import get_settings
from .models import Notification, NotificationPreference, User

settings = get_settings()

CATEGORY_PREF = {
    "inventory": "inventory_notifications",
    "pricing": "pricing_notifications",
    "dead_listings": "dead_listing_notifications",
    "sync": "sync_notifications",
}


def get_preferences(db: Session, user_id: int) -> NotificationPreference:
    row = db.query(NotificationPreference).filter(NotificationPreference.user_id == user_id).first()
    if not row:
        row = NotificationPreference(user_id=user_id)
        db.add(row)
        db.flush()
    return row


def _send_email(user: User, notification: Notification) -> None:
    if not settings.SMTP_HOST or not settings.SMTP_FROM_EMAIL:
        notification.email_status = "not_configured"
        return
    message = EmailMessage()
    message["Subject"] = f"[{notification.priority.title()}] {notification.title}"
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = user.email
    message.set_content(
        f"{notification.message}\n\n"
        f"What AliSync did: {notification.automatic_action or 'No automatic action.'}\n"
        f"Action required: {'Yes' if notification.merchant_action_required else 'No'}\n"
        f"Open AliSync to review this notification."
    )
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as smtp:
            if settings.SMTP_USE_TLS:
                smtp.starttls()
            if settings.SMTP_USERNAME:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            smtp.send_message(message)
        notification.email_status = "sent"
        notification.email_error = None
    except Exception as exc:
        notification.email_status = "failed"
        notification.email_error = str(exc)[:2000]


def create_notification(db: Session, *, user_id: int, event_type: str, category: str,
                        priority: str, title: str, message: str, shop_id: int | None = None,
                        entity_type: str | None = None, entity_id: str | None = None,
                        product_name: str | None = None, product_image: str | None = None,
                        variant_name: str | None = None, old_value=None, new_value=None,
                        automatic_action: str | None = None, merchant_action_required: bool = False,
                        action_label: str | None = None, action_url: str | None = None,
                        supplier_url: str | None = None, shopify_url: str | None = None,
                        dedupe_key: str | None = None) -> Notification | None:
    prefs = get_preferences(db, user_id)
    enabled_field = CATEGORY_PREF.get(category)
    if not prefs.in_app_enabled or (enabled_field and not getattr(prefs, enabled_field)):
        return None

    # Do not repeat an unchanged alert every sync cycle. Escalations use a new priority/key.
    if dedupe_key:
        existing = db.query(Notification).filter(
            Notification.user_id == user_id, Notification.dedupe_key == dedupe_key,
            Notification.priority == priority, Notification.is_read == False,
        ).order_by(Notification.id.desc()).first()
        if existing:
            existing.occurrence_count += 1
            existing.updated_at = datetime.now(timezone.utc)
            existing.message = message
            existing.new_value = new_value
            db.flush()
            return existing

    row = Notification(
        user_id=user_id, shop_id=shop_id, entity_type=entity_type, entity_id=entity_id,
        event_type=event_type, category=category, priority=priority, title=title,
        message=message, product_name=product_name, product_image=product_image,
        variant_name=variant_name, old_value=old_value, new_value=new_value,
        automatic_action=automatic_action, merchant_action_required=merchant_action_required,
        action_label=action_label, action_url=action_url, supplier_url=supplier_url,
        shopify_url=shopify_url, dedupe_key=dedupe_key,
    )
    db.add(row)
    db.flush()
    if prefs.email_enabled and (not prefs.email_critical_only or priority == "critical"):
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            _send_email(user, row)
    return row

