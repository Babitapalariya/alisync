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
entire multi-store AliSync account. The signed Shopify domain must already be
linked to that AliSync account. Link existing stores through the standalone
dashboard if necessary; no new Shopify store is created by this implementation.

Production routing must send /api/auth/shopify-session to FastAPI, preserving
Authorization. Existing /app API routes must continue to reach FastAPI.
No customer/store data or Shopify domain is changed by session verification.

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
