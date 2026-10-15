import base64
import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from alisync_app.database import Base, get_db
from alisync_app.models import User, Shop, Product, Mapping, PendingImport, SyncLog, Notification, PlanProduct
from alisync_app.shopify_webhooks import router


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.add(User(id=1, email='merchant@test.invalid', password_hash='test'))
        self.db.add_all([
            Shop(id=1, user_id=1, shop_domain='first.myshopify.com', shopify_client_id='public'),
            Shop(id=2, user_id=1, shop_domain='other.myshopify.com', shopify_client_id='public'),
        ])
        for shop_id in (1, 2):
            self.db.add_all([
                Product(user_id=1, shop_id=shop_id, aliexpress_id='123'),
                Mapping(user_id=1, shop_id=shop_id, aliexpress_id='456', shopify_product_id='789'),
                PendingImport(user_id=1, shop_id=shop_id, aliexpress_id='123'),
                PlanProduct(user_id=1, shop_id=shop_id, aliexpress_id='123'),
                SyncLog(user_id=1, shop_id=shop_id, entity_type='product', action='sync', status='ok'),
                Notification(user_id=1, shop_id=shop_id, event_type='test', category='sync', priority='information', title='test', message='test'),
            ])
        self.db.commit()
        self.settings = SimpleNamespace(SHOPIFY_CLIENT_SECRET='test-secret', SHOPIFY_CLIENT_ID='public')
        self.enterContext(patch('alisync_app.shopify_webhooks.get_settings', return_value=self.settings))
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close(); self.db.close(); self.engine.dispose()

    def send(self, topic='shop/redact', body=None, signature=None, path='/app/webhooks/shopify/privacy', **headers):
        body = body if body is not None else json.dumps({'shop_domain': 'first.myshopify.com', 'shop_id': 123}).encode()
        signature = signature if signature is not None else base64.b64encode(hmac.new(b'test-secret', body, hashlib.sha256).digest()).decode()
        return self.client.post(path, content=body, headers={'Content-Type': 'application/json', 'X-Shopify-Topic': topic, 'X-Shopify-Hmac-Sha256': signature, **headers})

    def test_invalid_hmac_precedes_json_parsing_and_deletion(self):
        for signature in ('', 'invalid'):
            self.assertEqual(self.send(body=b'not json', signature=signature).status_code, 401)
        self.assertEqual(self.db.query(Shop).count(), 2)

    def test_valid_raw_body_and_customer_topics(self):
        body = b'{ "shop_domain" : "first.myshopify.com", "customer": {"email":"buyer@test.invalid"} }'
        for topic in ('customers/data_request', 'customers/redact'):
            response = self.send(topic, body, path='/api/webhooks/shopify/privacy')
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['customer_data_stored'])
        self.assertEqual(self.db.query(Product).count(), 2)

    def test_tampered_body_rejected(self):
        self.assertEqual(self.send(signature=base64.b64encode(hmac.new(b'test-secret', b'{}', hashlib.sha256).digest()).decode()).status_code, 401)

    def test_redaction_is_scoped_and_repeatable(self):
        for _ in range(2):
            self.assertEqual(self.send().status_code, 200)
        for model in (Product, Mapping, PendingImport, PlanProduct, SyncLog, Notification):
            self.assertEqual(self.db.query(model).filter_by(shop_id=1).count(), 0)
            self.assertEqual(self.db.query(model).filter_by(shop_id=2).count(), 1)
        self.assertIsNone(self.db.get(Shop, 1))
        self.assertIsNotNone(self.db.get(Shop, 2))
        self.assertIsNotNone(self.db.get(User, 1))

    def test_other_app_credentials_not_erased(self):
        self.db.get(Shop, 1).shopify_client_id='different-app'; self.db.commit()
        self.assertEqual(self.send().status_code, 200)
        self.assertIsNotNone(self.db.get(Shop, 1))

    def test_invalid_payload_topic_and_domain(self):
        for body in (b'[]', b'{}', b'not json'):
            self.assertEqual(self.send(body=body).status_code, 400)
        self.assertEqual(self.send(topic='orders/create').status_code, 400)
        self.assertEqual(self.send(**{'X-Shopify-Shop-Domain':'other.myshopify.com'}).status_code, 400)

    def test_missing_secret_fails_closed(self):
        self.settings.SHOPIFY_CLIENT_SECRET=''
        self.assertEqual(self.send().status_code, 503)
        self.assertEqual(self.db.query(Shop).count(), 2)


if __name__ == '__main__':
    unittest.main()
