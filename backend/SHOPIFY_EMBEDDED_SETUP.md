# Embedded Shopify authentication

Deploy both frontend/index.html and the backend changes. Install backend
requirements (including PyJWT) and restart the backend. The frontend's public
shopify-api-key must match SHOPIFY_CLIENT_ID; SHOPIFY_CLIENT_SECRET stays on
the backend. The public client ID is not a secret.

The frontend loads App Bridge from Shopify's CDN. For embedded launches it
requests a fresh ID token, verifies it through /api/auth/shopify-session, and
attaches fresh tokens to subsequent API calls as X-Shopify-Session-Token.
The proxy must forward that header. Existing AliSync account authentication
remains required: a Shopify staff session must not grant access to the owner's
entire multi-store AliSync account. After AliSync login, the frontend calls
POST /api/auth/shopify-connect with both the AliSync bearer token and a fresh
X-Shopify-Session-Token. The backend validates both and derives the store
domain only from the signed Shopify token, then creates or updates that user's
local store link. It never creates a Shopify store or changes its domain.

The backend exchanges the ID token for an expiring offline access token using
the server's public-app credentials. It stores the access token using the
existing server-only token field and encrypts the refresh token with APP_SECRET.
Keep APP_SECRET stable. Never paste the public app secret into the merchant UI.
The embedded Connect Shopify button now uses token exchange automatically;
the credentials modal remains for standalone same-organization integrations.
Connections belonging to another app are preserved and return a conflict.

On startup, the existing additive schema migration adds nullable columns
shopify_auth_method, shopify_refresh_token_encrypted, and
shopify_refresh_token_expires_at to app_shops. Deploy the updated models and
restart the backend before loading the new frontend; the database user must
have permission to add columns. Do not run create_tables.py to reset data.
Refresh runs under a store-row lock; it rotates and stores both tokens. Active
embedded sessions can acquire a new pair after expiry using token exchange.

Production routing must send /api/auth/shopify-session and
/api/auth/shopify-connect to FastAPI, preserving Authorization and
X-Shopify-Session-Token. Existing /app API routes must continue to reach FastAPI.
No customer/store data or Shopify domain is changed by session verification.

Validation: run `python -m unittest test_shopify_exchange test_shopify_session
test_shopify_webhooks` from backend. In the installed app, sign in and inspect
Network: POST shopify-connect should return 200 with connected=true and the
current test store's domain. No client-secret form should appear. Shopify
should show connected; AliExpress authorization and plan setup remain separate.
Do not share Authorization headers, ID tokens, or server secrets in screenshots.

Open the installed app inside Shopify Admin to exercise the integration.
Do not claim the review checks passed from unit tests: Shopify's dashboard
requires observed embedded activity and says its checks run every two hours
using a dev store. An existing live-store session may help verify behavior,
but is not a guaranteed replacement for Shopify's stated review process.
If no dev store can be used and the checks remain pending, contact Shopify
Partner Support for their supported alternative.

The AI self-review checkbox is separate. Complete the Shopify App Store
self-review and resolve findings before marking it done. This change is not
a completed App Store self-review or a production deployment.
