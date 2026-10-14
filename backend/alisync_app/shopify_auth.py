"""User-safe messages for Shopify token endpoint failures."""

import html
import re


def shopify_token_error(response, shop_domain: str) -> str:
    """Translate Shopify JSON/HTML OAuth failures without exposing whole response pages."""
    body = (response.text or "").strip()
    lowered = body.lower()

    if "app_not_installed" in lowered or "application is not installed" in lowered:
        return (
            f"The Shopify app is not installed on {shop_domain}. Install this app on that store "
            "from Shopify Dev Dashboard, then enter the Client ID and Client Secret for that same app."
        )
    if "shop_not_permitted" in lowered or "client credentials cannot be performed" in lowered:
        return (
            "Shopify does not permit client-credentials access for this store. The app and store must "
            "belong to the same Shopify organization; otherwise connect through Shopify OAuth."
        )
    if "invalid_client" in lowered or "application_cannot_be_found" in lowered:
        return (
            "Shopify rejected the Client ID or Client Secret. Confirm that both values come from the "
            f"app installed on {shop_domain}."
        )

    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
        if title:
            return f"Shopify rejected the connection (HTTP {response.status_code}): {title[:180]}"

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    detail = payload.get("error_description") or payload.get("error") or payload.get("message")
    if detail:
        return f"Shopify rejected the connection (HTTP {response.status_code}): {str(detail)[:300]}"
    return (
        f"Shopify rejected the connection (HTTP {response.status_code}). Verify the store domain, "
        "installed app, Client ID, and Client Secret."
    )


def shopify_api_error(response, action: str = "complete this action") -> str:
    """Translate common Admin API permission failures into merchant-facing guidance."""
    body = (response.text or "").strip()
    lowered = body.lower()
    if "merchant approval" in lowered and "write_products" in lowered:
        return (
            "Shopify has not approved the write_products permission for this app. In Shopify Dev "
            "Dashboard, add write_products to the app version, release the version, install or update "
            "the app on this store, and then reconnect the store in AliSync."
        )
    return f"Shopify could not {action} (HTTP {response.status_code}): {body[:500]}"
