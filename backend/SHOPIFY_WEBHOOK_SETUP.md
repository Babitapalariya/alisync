# Shopify compliance webhook deployment

Deploy the updated backend and restart it. No new database tables are required.
Set SHOPIFY_CLIENT_SECRET and SHOPIFY_CLIENT_ID in the live backend environment
to the credentials of the public app being reviewed (not an older store app).
Do not expose the secret in frontend code or Shopify URL fields.

The same handler accepts all three topics at either URL:

- https://alisync.retradviews.com/app/webhooks/shopify/privacy
- https://alisync.retradviews.com/api/webhooks/shopify/privacy

Use the /app/ URL if your reverse proxy already forwards /app/ to FastAPI.
Ensure the selected path forwards POST requests and the original body/headers
to the backend, not the frontend HTML server. A browser GET is not a webhook test.

Merge the following into the public app's existing shopify.app.toml. This is a
configuration fragment, not a complete app configuration. No Shopify CLI app
configuration was present in this repository when this handler was added.

```toml
[webhooks]
api_version = "2026-10"

[[webhooks.subscriptions]]
compliance_topics = ["customers/data_request", "customers/redact", "shop/redact"]
uri = "https://alisync.retradviews.com/app/webhooks/shopify/privacy"
```

Do not duplicate an existing [webhooks] section. Release this configuration for
the correct public app using Shopify CLI (`shopify app deploy`). If the review
configuration exposes separate compliance URL fields, use the same endpoint for
all three. The X-Shopify-Topic header selects the action.

Behavior:
- Missing/incorrect HMAC returns 401 before JSON parsing or data changes.
- Missing server secret returns 503, rather than accepting unsigned requests.
- Customer requests acknowledge that this product/supplier app stores no Shopify
  customer/order records. Payload customer email/phone are not persisted or logged.
  If customer/order storage is added later, implement export delivery to the
  merchant and customer erasure before enabling it.
- shop/redact removes the matching store's local products, mappings, queue,
  notifications, sync logs, plan reservations and stored Shopify credentials.
  It preserves other stores and the independent AliSync user/AliExpress account.
  It does not delete products in Shopify. Duplicate deliveries are safe when
  the store has not been re-created. Shopify normally sends this topic 48 hours
  after uninstall; do not send signed redaction tests against real store data.

Run `python -m unittest test_shopify_webhooks` from backend. After deploying and
registering subscriptions, rerun Shopify's automated review checks. Local tests
do not confirm proxy routing, live credentials, or Shopify registration.

Reference: https://shopify.dev/docs/apps/build/compliance/privacy-law-compliance
