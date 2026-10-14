# AliSync multi-user application

AliSync is isolated under this directory. It does not modify or mount the
existing single-user backend or frontend.

AliSync uses its own `alisync_app` database. All AliSync-owned tables use the
`app_` prefix.

## URLs

- Frontend: `http://localhost:3002`
- Backend: `http://127.0.0.1:8002`
- API prefix: `http://127.0.0.1:8002/app`
- API docs: `http://127.0.0.1:8002/app/docs`

## Database tables

Only prefixed tables are registered:

- `app_users`
- `app_shops`
- `app_aliexpress_tokens`
- `app_products`
- `app_product_mappings`
- `app_pending_imports`
- `app_sync_logs`
- `app_notifications`
- `app_notification_preferences`

## Supplier safety workflow

Imported products and existing Shopify products linked through mappings use the
same **Dead Listings** queue. A missing supplier response progresses through
`Sync Warning` and `Verification in Progress`; only the third confirmed
invalid-listing signal becomes `Confirmed Dead`. API errors and timeouts become
`Supplier Unavailable` and never zero inventory. Confirmed-dead protection can
set Shopify inventory to zero, but never deletes or recreates the merchant's
Shopify product. Relinking changes only the supplier source and records the
change in sync history while preserving the existing pricing rule.

## Notifications

The in-app Notification Center is enabled by default and keeps read/unread audit
history. Alerts are categorized as pricing, inventory, dead listings, or sync,
with information, warning, and critical priorities. Repeated unchanged events
are deduplicated into an occurrence count. Merchants can configure low-stock,
large-price-change, minimum-profit and auto-restore rules, plus optional email
delivery (all enabled alerts or critical-only). SMTP settings in `.env` are
required before email delivery is attempted.

## Setup

From `alisync/backend`:

1. Copy `.env.example` to `.env` and fill in the database and OAuth settings.
2. Run `python -m pip install -r requirements.txt`.
3. Run `python create_tables.py`.
4. Run `run.cmd`.

From `alisync/frontend`, run `run.cmd`.

The existing single-user application continues using ports 8000 and 3001;
AliSync uses ports 8002 and 3002.


## Google login and signup

Both authentication screens offer **Continue with Google**. Existing accounts
are matched by Google's verified email; new accounts continue to plan selection.

1. In Google Cloud, configure the OAuth consent screen and create an OAuth client
   with application type **Web application**. Add test users while the app is in testing.
2. Register the exact callback URL used by this deployment, for example
   `http://127.0.0.1:8002/app/auth/google/callback` locally.
3. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, and
   `FRONTEND_URL` in `backend/.env`. Use HTTPS URLs for production. Keep the client
   secret on the backend. `APP_SECRET` must contain at least 32 characters.
4. Restart the backend, refresh the frontend, and choose **Continue with Google**.

The OAuth callback validates browser-bound, signed state with a ten-minute expiry.
Provider errors return to the login screen. The frontend consumes the session from
the URL fragment, removes it immediately, and resets the previous shop selection.

Reference: https://developers.google.com/identity/protocols/oauth2/web-server


## Subscription plans (payment collection disabled)

- **Free ($0/month):** 25 distinct sourced/linked products across all account stores. The first 5 sourcing reservations receive full Pro access automatically. Remaining products support basic import and mapping.
- **Growth ($14/month):** unlimited sourcing, imports, existing-product/variant mapping and supplier relinking. Choose up to 5 products for full Pro access in **Plans & Product Access**; release a slot to choose another.
- **Pro ($19/month):** unlimited products with every feature included.

`BILLING_ENABLED=false` remains the default. Plan changes apply without checkout or charges. Setting it to true blocks self-service paid-plan selection until a billing integration exists.

Sourcing reservations are account-wide, persistent and ordered by reservation ID; previews count toward the Free allowance. Failed previews do not reserve quota. Free access is automatic and ignores earlier manual selections; those selections are retained for Growth. Downgrades preserve existing products but enforce the new plan limits. Legacy Starter uses Free rules. Remap buttons remain visible only for dead-listed products.

Restart the backend after updating. The existing additive migration creates the entitlement table and any missing model columns; existing catalogues are registered on their first subscription load.

Offline checks from `backend`:

```powershell
python -m unittest test_subscriptions test_subscription_api test_mapping_prices test_mapping_isolation test_pending_stock test_variant_price_tracking test_google_auth
```

The bundled `iop/test_get.py` is an external SDK sample, not an offline application test.
