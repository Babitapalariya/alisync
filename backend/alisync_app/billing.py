"""Shopify is the source of truth for paid account access."""
from urllib.parse import quote

import requests
from fastapi import HTTPException

from .config import get_settings
from .models import Shop
from .shop_context import ensure_shopify_access_token
from .subscriptions import PLANS


def billing_error(errors, fallback):
    messages = [str(error.get("message", "")).strip()[:500]
                for error in (errors or []) if isinstance(error, dict)]
    detail = "; ".join(message for message in messages if message)
    return f"Shopify billing: {detail}" if detail else fallback


def graphql(db, shop, query, variables=None):
    try:
        ensure_shopify_access_token(shop, db)
    except HTTPException as exc:
        raise HTTPException(exc.status_code,
            f"Billing verification for {shop.shop_domain}: {exc.detail}") from exc
    settings = get_settings()
    try:
        response = requests.post(
            f"https://{shop.shop_domain}/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": shop.access_token},
            json={"query": query, "variables": variables or {}}, timeout=30)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(502, "Could not verify billing with Shopify. Please retry.") from exc
    if body.get("errors") or not body.get("data"):
        raise HTTPException(502, billing_error(body.get("errors"), "Shopify could not process billing. Please retry."))
    return body["data"]


def reconcile(db, user):
    shops = db.query(Shop).filter_by(user_id=user.id).all()
    # Tokens issued to another app cannot verify this app's subscription.
    shops = [shop for shop in shops if not shop.shopify_client_id
             or shop.shopify_client_id == get_settings().SHOPIFY_CLIENT_ID]
    for shop in shops:
        if not shop.is_active or not shop.access_token:
            shop.plan_name = "free"
            shop.subscription_status = "none"
            shop.subscription_id = None
            continue
        data = graphql(db, shop, """{ currentAppInstallation { activeSubscriptions {
            id name status test lineItems { plan { pricingDetails {
              ... on AppRecurringPricing { interval price { amount currencyCode } }
            } } }
        } } }""")
        installation = data.get("currentAppInstallation")
        if installation is None:
            raise HTTPException(502, "Shopify installation could not be verified")
        shop.plan_name = "free"
        shop.subscription_status = "none"
        shop.subscription_id = None
        for subscription in installation["activeSubscriptions"]:
            for key in ("pro", "growth"):
                lines = subscription.get("lineItems", [])
                pricing = lines[0]["plan"]["pricingDetails"] if len(lines) == 1 else {}
                price = pricing.get("price", {})
                if (subscription["status"] == "ACTIVE" and subscription["name"] == f"AliSync {PLANS[key]['name']}"
                        and (not subscription.get("test") or get_settings().SHOPIFY_BILLING_TEST)
                        and pricing.get("interval") == "EVERY_30_DAYS"
                        and price.get("currencyCode") == "USD"
                        and float(price.get("amount", -1)) == PLANS[key]["price"]):
                    shop.plan_name = key
                    shop.subscription_id = subscription["id"]
                    shop.subscription_status = "active"
    user.selected_plan = max((s.plan_name or "free" for s in shops),
                             key=lambda p: PLANS.get(p, PLANS["free"])["price"], default="free")
    user.subscription_status = "active" if user.selected_plan != "free" else "none"
    db.commit()


def select_plan(db, user, shop, plan):
    if shop.shopify_client_id and shop.shopify_client_id != get_settings().SHOPIFY_CLIENT_ID:
        raise HTTPException(409, "Reconnect this store using the AliSync Partner app before changing plans")
    reconcile(db, user)
    if plan == "free":
        # Plans cover the account: cancel every billed store before downgrading.
        for billed in db.query(Shop).filter_by(user_id=user.id, is_active=True).all():
            if billed.shopify_client_id and billed.shopify_client_id != get_settings().SHOPIFY_CLIENT_ID:
                continue
            if billed.subscription_id:
                result = graphql(db, billed, """mutation($id: ID!) {
                    appSubscriptionCancel(id: $id) { userErrors { message } }
                }""", {"id": billed.subscription_id})["appSubscriptionCancel"]
                if result["userErrors"]:
                    raise HTTPException(409, billing_error(result["userErrors"], "Shopify could not cancel the subscription. Please retry."))
        reconcile(db, user)
        return {"plan": user.selected_plan, "message": "Subscription updated", "payment_required": False}
    if not get_settings().BILLING_ENABLED:
        raise HTTPException(503, "Paid plans are temporarily unavailable")
    if user.selected_plan == plan:
        return {"plan": plan, "message": "Plan already active", "payment_required": False}
    other = next((candidate for candidate in db.query(Shop).filter(
        Shop.user_id == user.id, Shop.id != shop.id,
        Shop.subscription_status == "active").all()
        if not candidate.shopify_client_id or candidate.shopify_client_id == get_settings().SHOPIFY_CLIENT_ID), None)
    if other:
        raise HTTPException(409, "Change your plan from the store where your subscription was approved")
    settings = get_settings()
    if not settings.SHOPIFY_CLIENT_ID:
        raise HTTPException(503, "Shopify billing is not configured")
    return_url = f"https://{shop.shop_domain}/admin/apps/{quote(settings.SHOPIFY_CLIENT_ID, safe='')}"
    result = graphql(db, shop, """mutation($name: String!, $url: URL!, $test: Boolean!,
        $items: [AppSubscriptionLineItemInput!]!) {
        appSubscriptionCreate(name: $name, returnUrl: $url, test: $test,
          replacementBehavior: APPLY_IMMEDIATELY, lineItems: $items) {
            confirmationUrl userErrors { message }
        }
    }""", {"name": f"AliSync {PLANS[plan]['name']}", "url": return_url,
        "test": settings.SHOPIFY_BILLING_TEST,
        "items": [{"plan": {"appRecurringPricingDetails": {
            "price": {"amount": PLANS[plan]["price"], "currencyCode": "USD"},
            "interval": "EVERY_30_DAYS"}}}]})["appSubscriptionCreate"]
    if result["userErrors"] or not result.get("confirmationUrl"):
        raise HTTPException(409, billing_error(result["userErrors"], "Shopify returned no charge approval URL. Please retry."))
    return {"plan": user.selected_plan, "confirmation_url": result["confirmationUrl"],
            "payment_required": True, "message": "Approve your plan in Shopify"}
