import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alisync_app.database import Base
from alisync_app.models import User, Shop
from alisync_app.security import create_token, current_user
from alisync_app.shopify_session import router, verify_shopify_session


class ShopifySessionTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(SHOPIFY_CLIENT_ID="public-app",
            SHOPIFY_CLIENT_SECRET="s" * 40, APP_SECRET="a" * 40)
        for module in ("shopify_session", "security"):
            self.enterContext(patch(f"alisync_app.{module}.get_settings", return_value=self.settings))
        self.claims = dict(aud="public-app", iss="https://test.myshopify.com/admin",
            dest="https://test.myshopify.com", sub="123", iat=int(time.time()),
            nbf=int(time.time()) - 1, exp=int(time.time()) + 60)

    def token(self, **changes):
        return jwt.encode(self.claims | changes, self.settings.SHOPIFY_CLIENT_SECRET, algorithm="HS256")

    def test_valid_session_endpoint_aliases(self):
        app = FastAPI(); app.include_router(router)
        with TestClient(app) as client:
            for prefix in ("api", "app"):
                response = client.get(f"/{prefix}/auth/shopify-session",
                    headers={"Authorization": "Bearer " + self.token()})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["shop_domain"], "test.myshopify.com")
                self.assertEqual(client.get(f"/{prefix}/auth/shopify-session").status_code, 401)

    def test_rejects_wrong_app_expiry_issuer_and_destination(self):
        for changes in ({"aud": "another-app"}, {"exp": 1}, {"nbf": int(time.time()) + 600},
                        {"iss": "https://other.myshopify.com/admin"},
                        {"dest": "https://test.myshopify.com.evil.example"}, {"sub": ""}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException) as error:
                verify_shopify_session(self.token(**changes))
            self.assertEqual(error.exception.status_code, 401)

    def test_rejects_signature_algorithm_and_missing_claims(self):
        tokens = [jwt.encode(self.claims, "wrong" * 8, algorithm="HS256"),
                  jwt.encode(self.claims, self.settings.SHOPIFY_CLIENT_SECRET, algorithm="HS384"),
                  jwt.encode({"sub": "123"}, self.settings.SHOPIFY_CLIENT_SECRET, algorithm="HS256"), "broken"]
        for token in tokens:
            with self.assertRaises(HTTPException) as error:
                verify_shopify_session(token)
            self.assertEqual(error.exception.status_code, 401)

    def test_account_binding_and_standalone_compatibility(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as db:
                user = User(email="owner@example.test", password_hash="unused")
                db.add(user); db.flush()
                db.add(Shop(user_id=user.id, shop_domain="test.myshopify.com")); db.commit()
                credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=create_token(user.id))
                self.assertEqual(current_user(credentials, db, None).id, user.id)
                self.assertEqual(current_user(credentials, db, self.token()).id, user.id)
                with self.assertRaises(HTTPException) as error:
                    current_user(credentials, db, self.token(dest="https://other.myshopify.com",
                        iss="https://other.myshopify.com/admin"))
                self.assertEqual(error.exception.status_code, 403)
                with self.assertRaises(HTTPException):
                    current_user(credentials, db, "invalid")
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
