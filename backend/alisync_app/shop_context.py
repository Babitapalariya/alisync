"""Resolve an owned active shop for all operational endpoints."""

from fastapi import Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
import requests

from .database import get_db
from .models import Shop, User
from .security import current_user
from .security import decrypt_secret
from .shopify_auth import shopify_token_error


def ensure_shopify_access_token(shop: Shop, db: Session) -> Shop:
    """Refresh expiring client-credentials tokens before Shopify API calls."""
    if shop.shopify_auth_method == "token_exchange":
        from .shopify_exchange import refresh_offline_token
        return refresh_offline_token(shop, db)
    if not shop.shopify_client_id or not shop.shopify_client_secret_encrypted:
        return shop

    expires_at = shop.access_token_expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > datetime.now(timezone.utc):
            return shop

    try:
        response = requests.post(
            f"https://{shop.shop_domain}/admin/oauth/access_token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": shop.shopify_client_id,
                "client_secret": decrypt_secret(shop.shopify_client_secret_encrypted),
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        detail = (shopify_token_error(exc.response, shop.shop_domain)
                  if exc.response is not None else "Could not reach Shopify while refreshing the access token.")
        raise HTTPException(502, detail) from exc

    token = data.get("access_token")
    if not token:
        raise HTTPException(502, "Shopify token refresh returned no access token")
    expires_in = int(data.get("expires_in") or 86399)
    shop.access_token = token
    shop.access_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, expires_in - 300))
    granted_scopes = data.get("scope")
    if granted_scopes:
        shop.scopes = granted_scopes
        shop.scope = granted_scopes
    db.commit()
    db.refresh(shop)
    return shop


def get_current_shop(
    shop_id: int | None = Query(default=None),
    x_shop_id: int | None = Header(default=None, alias="X-Shop-ID"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Shop:
    selected_id = x_shop_id if x_shop_id is not None else shop_id
    query = db.query(Shop).filter(Shop.user_id == user.id, Shop.is_active == True)
    if selected_id is not None:
        query = query.filter(Shop.id == selected_id)
    shops = query.order_by(Shop.id.asc()).limit(2).all()
    if not shops:
        raise HTTPException(404, "No active Shopify shop found")
    if selected_id is None and len(shops) > 1:
        raise HTTPException(400, "Select a shop using X-Shop-ID or the shop_id query parameter")
    shop = shops[0]
    if not shop.access_token:
        raise HTTPException(409, "The selected Shopify shop is not connected")
    shop = ensure_shopify_access_token(shop, db)
    db.info["user_id"] = user.id
    db.info["shop_id"] = shop.id
    return shop
