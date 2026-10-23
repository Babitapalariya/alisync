"""Validate Shopify App Bridge ID tokens without granting AliSync account access."""
import re

import jwt
from fastapi import APIRouter, Header, HTTPException

from .config import get_settings

router = APIRouter(tags=["Shopify embedded session"])


def verify_shopify_session(token: str) -> dict:
    settings = get_settings()
    if not settings.SHOPIFY_CLIENT_ID or not settings.SHOPIFY_CLIENT_SECRET:
        raise HTTPException(503, "Shopify session verification is not configured")
    try:
        claims = jwt.decode(
            token, settings.SHOPIFY_CLIENT_SECRET, algorithms=["HS256"],
            audience=settings.SHOPIFY_CLIENT_ID,
            options={"require": ["exp", "nbf", "iat", "iss", "dest", "aud", "sub"]},
        )
        dest = claims["dest"]
        if not isinstance(dest, str) or not re.fullmatch(r"https://[a-z0-9][a-z0-9-]*\.myshopify\.com", dest):
            raise ValueError("Invalid destination")
        if claims["iss"] != dest + "/admin" or not claims["sub"]:
            raise ValueError("Invalid issuer or subject")
        return {"shop_domain": dest.removeprefix("https://"), "shopify_user_id": claims["sub"]}
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(401, "Invalid or expired Shopify session") from exc


@router.get("/app/auth/shopify-session")
@router.get("/api/auth/shopify-session")
def shopify_session(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Shopify session token required")
    return verify_shopify_session(authorization[7:])
