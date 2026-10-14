"""AliExpress-token compatibility helpers for the operational API."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import AliExpressCredential, AliExpressToken, Shop
from .security import decrypt_secret


def get_latest_token(db: Session, shop_id: int | None = None) -> AliExpressToken:
    """Return only the current authenticated user's AliExpress token."""
    user_id = db.info.get("user_id")
    if not user_id and db.info.get("shop_id"):
        shop = db.query(Shop).filter(Shop.id == db.info["shop_id"]).first()
        user_id = shop.user_id if shop else None
        if user_id:
            db.info["user_id"] = user_id
    if not user_id:
        raise HTTPException(401, "Authenticated user context is required")
    token = (
        db.query(AliExpressToken)
        .filter(AliExpressToken.user_id == user_id)
        .order_by(AliExpressToken.updated_at.desc())
        .first()
    )
    if not token:
        raise HTTPException(401, "Connect an AliExpress account first")
    return token


def get_aliexpress_credentials(db: Session) -> tuple[str, str, str]:
    user_id = db.info.get("user_id")
    if not user_id and db.info.get("shop_id"):
        shop = db.query(Shop).filter(Shop.id == db.info["shop_id"]).first()
        user_id = shop.user_id if shop else None
    row = db.query(AliExpressCredential).filter(AliExpressCredential.user_id == user_id).first()
    if not row:
        raise HTTPException(401, "Configure AliExpress API credentials first")
    return row.app_key, decrypt_secret(row.app_secret_encrypted), row.redirect_uri
