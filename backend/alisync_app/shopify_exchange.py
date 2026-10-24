"""Server-side expiring offline token exchange and refresh."""
from datetime import datetime, timedelta, timezone

import requests
from fastapi import HTTPException

from .config import get_settings
from .security import encrypt_secret, decrypt_secret


def is_fresh(expiry):
    if expiry is None:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry > datetime.now(timezone.utc) + timedelta(seconds=60)


def token_request(domain, grant):
    settings = get_settings()
    if not settings.SHOPIFY_CLIENT_ID or not settings.SHOPIFY_CLIENT_SECRET:
        raise HTTPException(503, "Shopify app credentials are not configured on the server")
    try:
        response = requests.post(f"https://{domain}/admin/oauth/access_token", data={
            "client_id": settings.SHOPIFY_CLIENT_ID,
            "client_secret": settings.SHOPIFY_CLIENT_SECRET, **grant,
        }, timeout=30, allow_redirects=False)
    except requests.RequestException:
        raise HTTPException(502, "Could not reach Shopify. Please retry.") from None
    if response.status_code in (400, 401, 403):
        raise HTTPException(409, "Shopify authorization expired or was rejected. Reopen AliSync from Shopify Admin and reconnect.")
    if response.status_code != 200:
        raise HTTPException(502, "Shopify could not complete the connection. Please retry.")
    try:
        data = response.json()
        for field in ("access_token", "refresh_token"):
            if not isinstance(data[field], str) or not data[field]:
                raise ValueError()
        if int(data["expires_in"]) <= 0 or int(data["refresh_token_expires_in"]) <= 0:
            raise ValueError()
        if not isinstance(data.get("scope", ""), str):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise HTTPException(502, "Shopify returned an incomplete token response") from None
    return data


def save_tokens(shop, data):
    now = datetime.now(timezone.utc)
    refresh = encrypt_secret(data["refresh_token"])
    shop.access_token = data["access_token"]
    shop.access_token_expires_at = now + timedelta(seconds=int(data["expires_in"]))
    shop.shopify_refresh_token_encrypted = refresh
    shop.shopify_refresh_token_expires_at = now + timedelta(seconds=int(data["refresh_token_expires_in"]))
    shop.shopify_auth_method = "token_exchange"
    shop.shopify_client_id = get_settings().SHOPIFY_CLIENT_ID
    shop.shopify_client_secret_encrypted = None
    shop.scopes = shop.scope = data.get("scope", "")


def exchange_token(shop, id_token):
    data = token_request(shop.shop_domain, {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": id_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
        "requested_token_type": "urn:shopify:params:oauth:token-type:offline-access-token",
        "expiring": "1",
    })
    save_tokens(shop, data)


def refresh_offline_token(shop, db):
    # Re-read under a row lock so background jobs and browser requests don't
    # rotate the same refresh token concurrently.
    from .models import Shop
    shop = db.query(Shop).filter(Shop.id == shop.id).populate_existing().with_for_update().one()
    try:
        if shop.shopify_client_id != get_settings().SHOPIFY_CLIENT_ID:
            raise HTTPException(409, "This connection belongs to another Shopify app")
        if shop.access_token and is_fresh(shop.access_token_expires_at):
            db.commit()
            return shop
        if not shop.shopify_refresh_token_encrypted or not is_fresh(shop.shopify_refresh_token_expires_at):
            raise HTTPException(409, "Reopen AliSync in Shopify Admin to reconnect this store")
        data = token_request(shop.shop_domain, {"grant_type": "refresh_token",
            "refresh_token": decrypt_secret(shop.shopify_refresh_token_encrypted)})
        save_tokens(shop, data)
        db.commit()
        return shop
    except Exception:
        db.rollback()
        raise
