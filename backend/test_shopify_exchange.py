import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch, Mock

import jwt
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from alisync_app.database import Base, get_db
from alisync_app.models import User, Shop
from alisync_app.security import create_token, decrypt_secret
from alisync_app.shopify_session import router
from alisync_app.shop_context import ensure_shopify_access_token


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.db.add_all([User(id=1, email='one@test.invalid', password_hash='unused', email_verified=True),
                         User(id=2, email='two@test.invalid', password_hash='unused', email_verified=True)])
        self.db.add(Shop(id=1, user_id=2, shop_domain='main.myshopify.com', access_token='unchanged'))
        self.db.commit()
        self.settings = SimpleNamespace(SHOPIFY_CLIENT_ID='public', SHOPIFY_CLIENT_SECRET='s'*40, APP_SECRET='a'*40)
        for module in ('security', 'shopify_session', 'shopify_exchange'):
            self.enterContext(patch(f'alisync_app.{module}.get_settings', return_value=self.settings))
        self.post = self.enterContext(patch('alisync_app.shopify_exchange.requests.post'))
        self.post.return_value = Mock(status_code=200)
        self.post.return_value.json.return_value = dict(access_token='access', refresh_token='refresh',
            expires_in=3600, refresh_token_expires_in=86400, scope='write_products')
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = self.enterContext(TestClient(app))

    def headers(self, **claims):
        now = int(time.time())
        payload = dict(aud='public', sub='staff1', iss='https://test.myshopify.com/admin',
            dest='https://test.myshopify.com', iat=now, nbf=now-1, exp=now+60)
        payload.update(claims)
        return {'Authorization': 'Bearer '+create_token(1),
            'X-Shopify-Session-Token': jwt.encode(payload, self.settings.SHOPIFY_CLIENT_SECRET, algorithm='HS256')}

    def connect(self, **claims):
        return self.client.post('/api/auth/shopify-connect', headers=self.headers(**claims),
            json={'shop_domain':'main.myshopify.com', 'client_secret':'ignored'})

    def test_connection_uses_signed_domain_is_idempotent_and_never_returns_tokens(self):
        response = self.connect()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(set(data), {'connected','shop_id','shop_domain'})
        self.assertEqual(data['shop_domain'], 'test.myshopify.com')
        shop = self.db.get(Shop, data['shop_id'])
        self.assertEqual(shop.user_id, 1)
        self.assertEqual(shop.shopify_auth_method, 'token_exchange')
        self.assertEqual(decrypt_secret(shop.shopify_refresh_token_encrypted), 'refresh')
        self.assertIsNone(shop.shopify_client_secret_encrypted)
        self.assertEqual(self.post.call_args.args[0], 'https://test.myshopify.com/admin/oauth/access_token')
        self.assertEqual(self.post.call_args.kwargs['data']['expiring'], '1')
        self.assertEqual(self.connect().status_code, 200)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.db.get(Shop,1).access_token, 'unchanged')

    def test_invalid_session_or_missing_account_never_calls_shopify(self):
        for claims in ({'aud':'other'}, {'exp':1}, {'dest':'https://evil.example'}):
            self.assertEqual(self.connect(**claims).status_code, 401)
        headers = self.headers(); headers.pop('Authorization')
        self.assertEqual(self.client.post('/api/auth/shopify-connect', headers=headers).status_code, 401)
        self.post.assert_not_called()
        self.assertEqual(self.db.query(Shop).count(),1)

    def test_failure_rolls_back_and_does_not_leak_response(self):
        self.post.return_value.status_code = 403
        self.post.return_value.text = 'sensitive Shopify response'
        response = self.connect()
        self.assertEqual(response.status_code,409)
        self.assertNotIn('sensitive',response.text)
        self.assertEqual(self.db.query(Shop).count(),1)

    def test_timeout_and_malformed_response_roll_back(self):
        self.post.side_effect = requests.Timeout('secret')
        self.assertEqual(self.connect().status_code,502)
        self.post.side_effect = None
        self.post.return_value.json.return_value = {'access_token':'partial'}
        self.assertEqual(self.connect().status_code,502)
        self.assertEqual(self.db.query(Shop).count(),1)

    def test_different_app_connection_is_preserved(self):
        self.db.add(Shop(user_id=1, shop_domain='test.myshopify.com', shopify_client_id='other-app', access_token='old'))
        self.db.commit()
        self.assertEqual(self.connect().status_code,409)
        self.post.assert_not_called()
        self.assertEqual(self.db.query(Shop).filter_by(user_id=1).one().access_token,'old')

    def test_expired_offline_token_refreshes_and_rotates(self):
        data=self.connect().json(); shop=self.db.get(Shop,data['shop_id'])
        shop.access_token_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);self.db.commit()
        self.post.return_value.json.return_value.update(access_token='new-access',refresh_token='new-refresh')
        refreshed=ensure_shopify_access_token(shop,self.db)
        self.assertEqual(refreshed.access_token,'new-access')
        self.assertEqual(decrypt_secret(refreshed.shopify_refresh_token_encrypted),'new-refresh')
        self.assertEqual(self.post.call_args.kwargs['data']['grant_type'],'refresh_token')
        self.assertEqual(self.post.call_args.kwargs['data']['refresh_token'],'refresh')
        ensure_shopify_access_token(shop,self.db)
        self.assertEqual(self.post.call_count,2)

    def test_expired_refresh_can_reconnect_using_new_session(self):
        data=self.connect().json(); shop=self.db.get(Shop,data['shop_id'])
        shop.access_token_expires_at=shop.shopify_refresh_token_expires_at=datetime.now(timezone.utc)-timedelta(days=1)
        self.db.commit()
        self.assertEqual(self.connect().status_code,200)
        self.assertEqual(self.post.call_args.kwargs['data']['grant_type'],'urn:ietf:params:oauth:grant-type:token-exchange')


if __name__ == '__main__':
    unittest.main()
