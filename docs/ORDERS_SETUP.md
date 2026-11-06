# Orders setup and acceptance checks

## Included

The Orders workspace is scoped to the authenticated Shopify store. It includes signed order webhooks, a durable webhook queue, scheduled/manual Shopify synchronization, search and filters, per-item supplier quotes, editable encrypted shipping addresses, exact variant mapping, confirmed supplier purchases, existing-order recovery, verified payment/tracking refresh, explicit partial Shopify fulfillment, activity history, selected-order refresh, and an estimated-profit CSV export.

Supplier payment, Shopify customer payment, fulfillment and delivery are separate states. Quotes expire after ten minutes. Prices, quantities, mappings and recipient details are rechecked before purchase. Changes require fresh confirmation. Uncertain purchase or fulfillment responses block another submission. Different currencies are never combined into a fabricated profit estimate.

## Deployment

1. Back up the database, then deploy the backend and the updated `frontend/index.html`. Orders CSS/JS are bundled into the HTML so production SPA fallback routes cannot return HTML in place of those assets. After editing `orders.js` or `orders.css`, run `node frontend/build_orders.cjs` and deploy the regenerated HTML. Keep the source CSS/JS and build script in the repository. Install `backend/requirements.txt`. New order tables are created by the existing startup metadata creation; existing product tables are unchanged.
2. Keep `APP_SECRET` stable: stored shipping addresses and temporary privacy exports use the existing encryption key. Rotating that key without a migration makes existing encrypted records unreadable.
3. Deploy the updated `shopify.app.toml` through the existing Shopify app release process. It registers order created/updated/cancelled/paid webhooks and requests merchant-managed fulfillment order read/write scopes. Approve updated permissions in the store. Order/customer fields also require Shopify protected customer data access appropriate to this app. Initial synchronization covers the usual last 60 days; older history requires Shopify approval for `read_all_orders` and a separate historical import.
4. Set these in the server runtime environment (normally `backend/.env`), then restart the backend:

```dotenv
ORDER_SYNC_ENABLED=true
ALIEXPRESS_ORDER_CREATE_ENABLED=false
ORDER_AUTO_FULFILL=false
```

`.env.example` is documentation, not loaded runtime configuration. Do not put credentials in it. The repository's current ignore rules exclude `.env.*`; the settings above are therefore also recorded here for deployment.

5. Open Orders and use Sync Shopify orders. The scheduler processes webhooks every minute and runs incremental order/supplier synchronization every ten minutes. Shopify imports resume using saved cursors; failures back off. Run only the intended scheduler instance for your deployment.

## AliExpress activation: live verification still required

The adapter uses documented DS methods: `aliexpress.logistics.buyer.freight.calculate`, `aliexpress.trade.buy.placeorder`, and `aliexpress.ds.trade.order.get`. Alibaba's legacy DS documentation may not match every currently approved app. Automated tests mock these APIs; they do not establish that this app has current order API permission or a working live checkout.

Keep supplier creation disabled until an authorized test with this app verifies freight response shape, exact SKU/warehouse, stock, recipient address, shipping method, creation response and order ID. Then enable creation only for a controlled merchant-approved test, verify its unpaid order in the same AliExpress account, pay manually if approved, and verify the API payment/tracking transitions. Never automatically retry a timed-out creation. Inspect AliExpress and use Link existing supplier order when its product, variant, quantity and recipient match. There is no automatic card payment or browser checkout automation.

A direct payment button is shown only when AliExpress returns a validated HTTPS AliExpress payment URL. The documented creation response supplies order IDs, not a guaranteed cashier URL. Otherwise the UI opens AliExpress Orders and displays the saved ID so the merchant can choose Pay now. A guaranteed direct payment-page deep link is **not verified** and is not fabricated.

Validate Shopify fulfillment on a test order with multiple items and, if relevant, split locations. Only the purchased line's explicit remaining quantities should be fulfilled. The default requires merchant confirmation and offers a customer-notification checkbox. `ORDER_AUTO_FULFILL=true` opts into automatic verified shipment fulfillment with notifications. An uncertain fulfillment must be reviewed in Shopify; the app intentionally does not blindly resend it or claim it succeeded.

## Privacy

Order shipping addresses and temporary customer-data request exports are encrypted. Authenticated order responses are marked private/no-store. Webhook jobs store order identifiers rather than customer payloads. Customer redaction clears saved addresses and names and records a tombstone to prevent reimport; shop redaction removes associated order records. Merchant-only data request exports are accessible from Orders and purged after 30 days when scheduled order synchronization is enabled. Keep synchronization enabled for automatic export cleanup. Do not put customer data in operational logs.

## Tests and limits

Run the root backend unit test modules, excluding `backend/iop/test_get.py` (a vendor live-network demo). `backend/test_orders.py` uses mocked external APIs and covers tenant isolation, mapping, confirmations, repricing, duplicate prevention, uncertain results, address changes, signed webhooks, privacy and exact fulfillment quantities. `frontend/test_orders.cjs` checks rendering and recovery/payment controls without network calls.

Live API acceptance, Shopify app release, permission approval and server deployment are separate from these local checks. The profit export covers at most 5,000 items and is an estimate, excluding taxes, payment fees and FX conversion. Bulk supplier purchases, automatic refunds/cancellations and currency conversion are not implemented; selected-order status refresh is supported.

API references:
- https://shopify.dev/docs/api/admin-graphql/2026-07/mutations/fulfillmentCreate
- https://developer.alibaba.com/docs/doc.htm?articleId=35446&docType=2&treeId=762
- https://developer.alibaba.com/docs/api.htm?apiId=39371
- https://developer.alibaba.com/docs/api.htm?apiId=60455
