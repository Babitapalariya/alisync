"""Orders regression tests: in-memory data and mocked platforms; never place orders."""
import copy
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from alisync_app.database import Base, get_db
from alisync_app.models import User, Shop, Mapping, StoreOrder, StoreOrderLine, OrderActivity, OrderFulfillment, OrderRedactedCustomer
from alisync_app import orders
from alisync_app import order_supplier as supplier


def shopify_order(order_id='100', quantity=2):
    return {'id':'gid://shopify/Order/'+order_id,'name':'#1052','createdAt':'2026-11-01T10:00:00Z','updatedAt':'2026-11-01T10:00:00Z',
        'cancelledAt':None,'displayFinancialStatus':'PAID','displayFulfillmentStatus':'UNFULFILLED',
        'customer':{'id':'gid://shopify/Customer/1','displayName':'Test buyer'},
        'shippingAddress':{'name':'Test buyer','address1':'123 Test Street','city':'Test City','province':'CA','zip':'90001','countryCodeV2':'US','phone':'+12025550123'},
        'currentTotalPriceSet':{'shopMoney':{'amount':'59.98','currencyCode':'USD'}},
        'lines':[{'id':'gid://shopify/LineItem/1001','product':{'id':'gid://shopify/Product/10'},'variant':{'id':'gid://shopify/ProductVariant/11'},
            'title':'Test tire','variantTitle':'Black','quantity':quantity,'currentQuantity':quantity,
            'discountedTotalSet':{'shopMoney':{'amount':'59.98','currencyCode':'USD'}}}]}


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        Base.metadata.create_all(self.engine);self.db=Session(self.engine)
        self.db.add(User(id=1,email='order@test.invalid',password_hash='test'))
        self.shop=Shop(id=1,user_id=1,shop_domain='one.myshopify.com',access_token='fake')
        self.other=Shop(id=2,user_id=1,shop_domain='two.myshopify.com',access_token='fake')
        self.db.add_all([self.shop,self.other]);self.db.commit();self.db.info.update(user_id=1,shop_id=1)
        self.enterContext(patch.object(orders,'encrypt_secret',side_effect=lambda s:'encrypted:'+s))
        self.enterContext(patch.object(orders,'decrypt_secret',side_effect=lambda s:s.removeprefix('encrypted:')))
        self.settings=SimpleNamespace(ALIEXPRESS_ORDER_CREATE_ENABLED=True,ORDER_SYNC_ENABLED=False,ORDER_AUTO_FULFILL=False)
        self.enterContext(patch.object(orders,'get_settings',return_value=self.settings))
        self.data=shopify_order();self.order=orders.ingest(self.db,self.shop,self.data)
        self.line=self.db.query(StoreOrderLine).one()
        self.db.add(Mapping(user_id=1,shop_id=1,aliexpress_id='123456',shopify_product_id='10'));self.db.commit()
        self.enterContext(patch.object(orders,'fetch_order',side_effect=lambda *a:copy.deepcopy(self.data)))
        self.enterContext(patch.object(orders,'get_variant_sync_state',return_value={11:{'ae_sku_id':'sku-1'}}))
        self.quote={'product_id':'123456','sku_id':'sku-1','sku_attr':'14:193','title':'Supplier tire','variant':'Black','image':None,
            'unit_price':'14.0000','quantity':2,'currency':'USD','methods':[{'code':'STANDARD','cost':'5.0000','delivery':'8-15 days'}],
            'ship_from':'CN','product_url':'https://www.aliexpress.com/item/123456.html'}
        self.quote_mock=self.enterContext(patch.object(supplier,'quote',side_effect=lambda *a:copy.deepcopy(self.quote)))
        self.create_mock=self.enterContext(patch.object(supplier,'create',return_value={'order_id':'999999','payment_url':None}))
        self.address=orders.address_of(self.order);self.address.update(phone_country='+1',phone='2025550123')
        self.order.address_override_encrypted=orders.encrypt_secret(json.dumps(self.address));self.db.commit()
        self.verified={'status':'shipped','raw_status':'WAIT_BUYER_ACCEPT_GOODS','paid':True,
            'tracking':[{'number':'TRACK123','carrier':'Test carrier'}], 'children':[{'product_id':'123456','product_count':2}],'payment_url':None}
        self.status_mock=self.enterContext(patch.object(supplier,'status',return_value=self.verified))

    def tearDown(self):
        self.db.close();self.engine.dispose()

    def prepare(self):
        return orders.prepare_line(self.line.id,{'ship_from':'CN'},self.db,self.shop)

    def purchase(self, token=None):
        return orders.purchase_line(self.line.id,{'confirmed':True,'quote_token':token or self.line.quote_token},self.db,self.shop)

    def test_ingestion_is_idempotent_and_store_scoped(self):
        orders.ingest(self.db,self.shop,self.data)
        self.assertEqual(self.db.query(StoreOrder).count(),1)
        self.assertEqual(self.db.query(StoreOrderLine).count(),1)
        self.assertEqual(self.db.query(OrderActivity).count(),1)
        orders.ingest(self.db,self.other,self.data)
        self.assertEqual(self.db.query(StoreOrder).count(),2)
        with self.assertRaises(HTTPException):orders.owned_order(self.db,self.other,self.order.id)
        self.assertTrue(self.order.address_encrypted.startswith('encrypted:'))

    def test_exact_mapping_and_profit(self):
        result=self.prepare();self.assertEqual(result['quote']['profit'],'26.9800')
        self.assertEqual(result['quote']['quantity'],2)
        self.assertEqual(result['quote']['shipping']['code'],'STANDARD')
        self.assertEqual(self.quote_mock.call_args.args[2],'sku-1')

    def test_quote_does_not_guess_unmapped_variant(self):
        with patch.object(orders,'get_variant_sync_state',return_value={}):
            with self.assertRaises(HTTPException):self.prepare()
        self.assertEqual(self.line.status,'needs_mapping');self.create_mock.assert_not_called()

    def test_purchase_requires_confirmation_and_is_idempotent(self):
        self.prepare()
        with self.assertRaises(HTTPException):orders.purchase_line(self.line.id,{},self.db,self.shop)
        first=self.purchase();second=self.purchase()
        self.assertEqual(first['supplier_order_id'],second['supplier_order_id'])
        self.assertEqual(self.create_mock.call_count,1)
        self.assertFalse(self.line.payment_confirmed)
        self.assertEqual(self.create_mock.call_args.args[2]['address1'],'123 Test Street')

    def test_price_change_requires_fresh_confirmation(self):
        self.prepare();old=self.line.quote_token;self.quote['unit_price']='18.0000'
        with self.assertRaises(HTTPException) as error:self.purchase(old)
        self.assertEqual(error.exception.status_code,409)
        self.create_mock.assert_not_called();self.assertNotEqual(self.line.quote_token,old)
        self.assertTrue(self.line.quote['price_increased'])
        self.purchase();self.assertEqual(self.create_mock.call_count,1)

    def test_uncertain_purchase_cannot_be_repeated(self):
        self.prepare();token=self.line.quote_token;self.create_mock.side_effect=TimeoutError()
        with self.assertRaises(HTTPException):self.purchase(token)
        self.assertEqual(self.line.status,'uncertain')
        with self.assertRaises(HTTPException):self.purchase(token)
        with self.assertRaises(HTTPException):self.prepare()
        self.assertEqual(self.create_mock.call_count,1)
        result=orders.attach_supplier_order(self.line.id,{'supplier_order_id':'999999','confirmed':True},self.db,self.shop)
        self.assertEqual(result['supplier_order_id'],'999999')

    def test_quote_cannot_overwrite_inflight_purchase(self):
        self.prepare();q=self.line.quote;self.line.status='creating';self.db.commit()
        with self.assertRaises(HTTPException):orders.save_quote(self.db,self.order,self.line,q)
        self.db.refresh(self.line);self.assertEqual(self.line.status,'creating')

    def test_cancelled_order_blocks_purchase(self):
        self.prepare();self.data['cancelledAt']='2026-11-02T00:00:00Z'
        with self.assertRaises(HTTPException):self.purchase()
        self.create_mock.assert_not_called()

    def test_address_edit_invalidates_quote_and_preserves_shopify_address(self):
        self.prepare();original=self.order.address_encrypted
        orders.edit_address(self.order.id,self.address|{'city':'Corrected City'},self.db,self.shop)
        self.assertIsNone(self.line.quote_token);self.assertEqual(self.order.address_encrypted,original)
        self.assertEqual(orders.address_of(self.order)['city'],'Corrected City')

    def test_currency_mismatch_has_no_invented_profit(self):
        self.quote['currency']='EUR';self.prepare();self.assertIsNone(self.line.quote['profit'])

    def test_checkout_disabled_makes_no_purchase(self):
        self.settings.ALIEXPRESS_ORDER_CREATE_ENABLED=False
        with self.assertRaises(HTTPException):self.purchase()
        self.create_mock.assert_not_called()

    def test_status_refresh_never_marks_shopify_fulfilled(self):
        self.prepare();self.purchase()
        with patch.object(orders,'_graphql') as api:
            orders.refresh_supplier(self.db,self.shop,self.line,self.order)
            api.assert_not_called()
        self.assertTrue(self.line.payment_confirmed);self.assertEqual(self.line.status,'shipped')
        self.assertIsNone(self.line.fulfillment_id)

    def test_fulfillment_sends_only_the_exact_line_quantity(self):
        self.prepare();self.purchase()
        def graphql(shop,query,variables):
            if 'fulfillmentOrders' in query:
                return {'order':{'fulfillmentOrders':{'nodes':[{'id':'gid://shopify/FulfillmentOrder/8','status':'OPEN',
                    'lineItems':{'nodes':[{'id':'gid://shopify/FulfillmentOrderLineItem/9','remainingQuantity':2,'lineItem':{'id':self.line.shopify_line_id}},
                    {'id':'gid://shopify/FulfillmentOrderLineItem/10','remainingQuantity':3,'lineItem':{'id':'other-line'}}],
                    'pageInfo':{'hasNextPage':False}}}], 'pageInfo':{'hasNextPage':False}}}}
            return {'fulfillmentCreate':{'fulfillment':{'id':'gid://shopify/Fulfillment/22'},'userErrors':[]}}
        with patch.object(orders,'_graphql',side_effect=graphql) as api:
            orders.fulfill_line(self.db,self.shop,self.line,self.order)
            payload=api.call_args.args[2]['fulfillment']
            self.assertEqual(payload['lineItemsByFulfillmentOrder'][0]['fulfillmentOrderLineItems'],[{'id':'gid://shopify/FulfillmentOrderLineItem/9','quantity':2}])
            orders.fulfill_line(self.db,self.shop,self.line,self.order)
            self.assertEqual(api.call_count,2)
        self.assertEqual(self.line.fulfillment_state,'complete')

    def test_uncertain_fulfillment_is_never_resubmitted(self):
        self.prepare();self.purchase()
        self.db.add(OrderFulfillment(shop_id=self.shop.id,order_id=self.order.id,line_id=self.line.id,
            fulfillment_order_id='gid://shopify/FulfillmentOrder/8',fulfillment_line_id='gid://shopify/FulfillmentOrderLineItem/9',quantity=2))
        self.db.commit()
        with patch.object(orders,'_graphql',side_effect=TimeoutError('response lost')) as api:
            with self.assertRaises(HTTPException):orders.fulfill_line(self.db,self.shop,self.line,self.order)
            self.assertEqual(self.line.fulfillment_state,'uncertain')
            with self.assertRaises(HTTPException):orders.fulfill_line(self.db,self.shop,self.line,self.order)
            self.assertEqual(api.call_count,1)

    def test_sync_reports_missing_customer_scope_and_preserves_it_during_cooldown(self):
        with patch.object(orders, 'ensure_shopify_access_token'), patch.object(orders, '_graphql', side_effect=HTTPException(502, "Access denied for customer field. Required access: read_customers")) as api:
            with self.assertRaises(HTTPException) as first:
                orders.sync_shop(self.db, self.shop)
            self.assertIn('read_customers', first.exception.detail)
            with self.assertRaises(HTTPException) as second:
                orders.sync_shop(self.db, self.shop)
            self.assertEqual(second.exception.status_code, 429)
            self.assertIn('read_customers', second.exception.detail)
            self.assertEqual(api.call_count, 1)

    def test_listing_link_available_before_quote_without_supplier_api(self):
        detail = orders.serialized_order(self.db, self.order, True)
        self.assertEqual(detail['lines'][0]['supplier_product_url'], 'https://www.aliexpress.com/item/123456.html')
        self.assertIsNone(self.line.supplier_product_id)
        self.quote_mock.assert_not_called()

    def test_listing_link_does_not_use_other_store_or_conflicting_mapping(self):
        self.db.query(Mapping).delete()
        self.db.add(Mapping(user_id=1,shop_id=2,aliexpress_id='777',shopify_product_id='10'))
        self.db.commit()
        self.assertIsNone(orders.serialized_order(self.db,self.order,True)['lines'][0]['supplier_product_url'])
        self.db.add_all([Mapping(user_id=1,shop_id=1,aliexpress_id='888',shopify_product_id='10'),
                         Mapping(user_id=1,shop_id=1,aliexpress_id='999',shopify_product_id='10')])
        self.db.commit()
        self.assertIsNone(orders.serialized_order(self.db,self.order,True)['lines'][0]['supplier_product_url'])

    def test_saved_supplier_listing_is_preserved_for_existing_purchase(self):
        self.line.supplier_product_id='111'
        self.db.commit()
        self.assertEqual(orders.serialized_order(self.db,self.order,True)['lines'][0]['supplier_product_url'],
                         'https://www.aliexpress.com/item/111.html')

    def test_api_cross_store_isolation(self):
        app=FastAPI();app.include_router(orders.router)
        app.dependency_overrides[get_db]=lambda:self.db
        app.dependency_overrides[orders.get_current_shop]=lambda:self.other
        with TestClient(app) as client:
            self.assertEqual(client.get(f'/app/orders/{self.order.id}').status_code,404)
            self.assertEqual(client.post(f'/app/orders/lines/{self.line.id}/quote',json={'ship_from':'CN'}).status_code,404)
            self.assertEqual(client.get('/app/orders').json()['total'],0)

    def test_signed_order_webhook_is_deduplicated_without_storing_address(self):
        import base64, hashlib, hmac
        from alisync_app.shopify_webhooks import router
        from alisync_app.models import OrderWebhookTask
        app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:self.db
        body=json.dumps({'id':100,'shipping_address':{'address1':'sensitive'}}).encode()
        headers={'X-Shopify-Topic':'orders/create','X-Shopify-Shop-Domain':self.shop.shop_domain,
            'X-Shopify-Hmac-Sha256':base64.b64encode(hmac.new(b'test-secret',body,hashlib.sha256).digest()).decode()}
        with patch('alisync_app.shopify_webhooks.get_settings',return_value=SimpleNamespace(SHOPIFY_CLIENT_SECRET='test-secret',SHOPIFY_CLIENT_ID='app')):
            with TestClient(app) as client:
                for _ in range(2):self.assertEqual(client.post('/api/webhooks/shopify/orders',content=body,headers=headers).status_code,200)
                self.assertEqual(self.db.query(OrderWebhookTask).count(),1)
                task=self.db.query(OrderWebhookTask).one();self.assertEqual(task.order_gid,'gid://shopify/Order/100')
                headers['X-Shopify-Hmac-Sha256']='invalid'
                self.assertEqual(client.post('/api/webhooks/shopify/orders',content=body,headers=headers).status_code,401)

    def test_customer_redaction_is_scoped_and_blocks_purchase(self):
        import base64, hashlib, hmac
        from alisync_app.shopify_webhooks import router
        other=orders.ingest(self.db,self.other,self.data)
        app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:self.db
        body=json.dumps({'shop_domain':self.shop.shop_domain,'customer':{'id':1}}).encode()
        headers={'X-Shopify-Topic':'customers/redact','X-Shopify-Hmac-Sha256':base64.b64encode(hmac.new(b'test-secret',body,hashlib.sha256).digest()).decode()}
        with patch('alisync_app.shopify_webhooks.get_settings',return_value=SimpleNamespace(SHOPIFY_CLIENT_SECRET='test-secret',SHOPIFY_CLIENT_ID='app')):
            with TestClient(app) as client:self.assertEqual(client.post('/api/webhooks/shopify/privacy',content=body,headers=headers).status_code,200)
        self.db.refresh(self.order);self.db.refresh(other)
        self.assertTrue(self.order.privacy_redacted);self.assertIsNone(self.order.address_encrypted)
        self.assertIsNone(self.order.customer_name);self.assertIsNotNone(other.address_encrypted)
        orders.ingest(self.db,self.shop,self.data);self.assertIsNone(self.order.address_encrypted)
        with self.assertRaises(HTTPException):self.prepare()

    def test_redacted_customer_is_not_reimported(self):
        self.db.add(OrderRedactedCustomer(shop_id=1,customer_id='gid://shopify/Customer/1'));self.db.commit()
        other_data=shopify_order('101');orders.ingest(self.db,self.shop,other_data)
        row=self.db.query(StoreOrder).filter_by(shopify_order_id=other_data['id']).one()
        self.assertTrue(row.privacy_redacted);self.assertIsNone(row.address_encrypted)


class SupplierAdapterTests(unittest.TestCase):
    def test_payment_links_are_only_https_aliexpress(self):
        self.assertIsNone(supplier.safe_payment_url('https://aliexpress.com.evil.test/pay'))
        self.assertIsNone(supplier.safe_payment_url('javascript:alert(1)'))
        self.assertIsNone(supplier.safe_payment_url('https://user:pass@aliexpress.com/pay'))
        self.assertEqual(supplier.safe_payment_url('https://www.aliexpress.com/pay'),'https://www.aliexpress.com/pay')

    def test_finish_is_not_delivery_and_unknown_status_is_rejected(self):
        with patch.object(supplier,'call',return_value={'order_status':'FINISH'}):
            result=supplier.status(None,'1');self.assertNotEqual(result['status'],'delivered');self.assertFalse(result['paid'])
        with patch.object(supplier,'call',return_value={'order_status':'NEW_UNKNOWN_STATUS'}):
            with self.assertRaises(HTTPException):supplier.status(None,'1')

    def test_supplier_create_payload_uses_exact_variant_and_address(self):
        prepared={'product_id':'123','sku_attr':'14:193','quantity':2,'shipping':{'code':'STANDARD'}}
        address={'name':'Buyer','address1':'123 Test','city':'Test','country':'US','phone':'123456789','phone_country':'+1'}
        with patch.object(supplier,'call',return_value={'is_success':True,'order_list':{'number':[999999]}}) as api:
            result=supplier.create(None,prepared,address,'test')
            payload=json.loads(api.call_args.args[2]['param_place_order_request4_open_api_d_t_o'])
            self.assertEqual(payload['product_items'][0]['sku_attr'],'14:193')
            self.assertEqual(payload['logistics_address']['address'],'123 Test')
            self.assertEqual(result['order_id'],'999999');self.assertIsNone(result['payment_url'])


if __name__=='__main__':unittest.main()
