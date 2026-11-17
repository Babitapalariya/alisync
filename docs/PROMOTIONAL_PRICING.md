# Promotional pricing investigation (2026-11-14)

Product: 3256806802963585. Destination US; requested USD; connected AliSync user 10 / store 9.
Fresh DS response: black SKU 12000038964067382 sku_price=1739.55, offer_sale_price=817.59, offer_bulk_sale_price=817.59, currency_code=USD, price_include_tax=false. Other returned SKUs: 852.39 sale price. No additional promotion, coupon or eligibility pricing fields found. Base currency CNY must not label the USD SKU prices.

The screenshot shows a Welcome Deal of 649.79. That is not present in this API response. Browsing the product URL redirected to login/cookie synchronization; equivalent authenticated account/region/SKU comparison could not be performed. The connected API token does not prove the browser account or new-customer eligibility.

Official alternatives investigated:
- https://developer.alibaba.com/docs/api.htm?apiId=48595 : affiliate.productdetail.get exposes product-level app_sale_price / target_app_sale_price and promo_code_info. These are not guaranteed exact-SKU buyer checkout prices. A live request using current app credentials returned InsufficientPermission. Not installed as a repeated failing call on each product request.
- https://developer.alibaba.com/docs/api.htm?apiId=48596 : affiliate.featuredpromo.get lists campaign names.
- https://developer.alibaba.com/docs/api.htm?apiId=48597 : affiliate.featuredpromo.products.get exposes campaign products and conditional coupon metadata. Coupon conditions must not be converted to an assumed item price or labeled public/new-user eligible without confirmation.
- https://developer.alibaba.com/docs/api.htm?apiId=60452 : current DS product source.

Additive response fields on product and SKU: promotional_price=null, promotion_type=null, promotion_eligibility=unknown, promotion_status=not_exposed_by_current_source. final_display_price is the available API price, with final_display_price_source=api_sale_price_fallback. Null means unknown, not no promotion. Monetary strings preserve existing API conventions. No extra discount is calculated and no order-creation code changed.

Next prerequisite: approved access to the official affiliate endpoints (and tracking ID where required), then inspect actual product responses and SKU, destination, currency and eligibility coverage before enabling enrichment. Do not replace sale_price or purchase pricing with an unverified affiliate minimum price. Exact account-specific Welcome Deal retrieval remains unresolved.
