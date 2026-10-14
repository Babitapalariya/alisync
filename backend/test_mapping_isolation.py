"""Exercise mapping endpoints without loading production startup or its database."""
import ast
from pathlib import Path
import unittest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from alisync_app.database import Base, get_db
from alisync_app.models import Mapping, Shop, User
from alisync_app.security import current_user
from alisync_app.shop_context import get_current_shop


def load_endpoints(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)


class MappingIsolationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.add_all([User(id=1, email="one@example.test", password_hash="test"),
                         User(id=2, email="two@example.test", password_hash="test")])
        self.db.add_all([Shop(id=11, user_id=1, shop_domain="same.myshopify.com", access_token="test"),
                         Shop(id=12, user_id=1, shop_domain="other.myshopify.com", access_token="test"),
                         Shop(id=21, user_id=2, shop_domain="same.myshopify.com", access_token="test")])
        self.db.flush()
        for mapping_id, user_id, shop_id in [(101,1,11),(102,1,12),(201,2,21)]:
            self.db.add(Mapping(id=mapping_id,user_id=user_id,shop_id=shop_id,
                aliexpress_id="same-ali-id",shopify_product_id="same-shopify-id",shopify_product_title="Test"))
        self.db.commit()
        router, api = APIRouter(prefix="/app"), APIRouter(prefix="/app")
        namespace = dict(router=router,api=api,Depends=Depends,Query=Query,HTTPException=HTTPException,
            Session=Session,Shop=Shop,Mapping=Mapping,ProductMapping=Mapping,get_db=get_db,get_current_shop=get_current_shop)
        root = Path(__file__).parent / "alisync_app"
        load_endpoints(root/"routes.py", {"mappings","delete_mapping"}, namespace)
        load_endpoints(root/"operations.py", {"list_mappings"}, namespace)
        app=FastAPI();app.include_router(api);app.include_router(router)
        app.dependency_overrides[get_db]=lambda:self.db
        def user(request: Request):
            return self.db.get(User,int(request.headers.get("X-Test-User","1")))
        app.dependency_overrides[current_user]=user
        self.client=TestClient(app)

    def tearDown(self):
        self.client.close();self.db.close();self.engine.dispose()

    def headers(self,user=1,shop=11):
        return {"X-Test-User":str(user),"X-Shop-ID":str(shop)}

    def test_both_list_endpoints_return_only_selected_shop(self):
        for user,shop,expected in [(1,11,101),(1,12,102),(2,21,201)]:
            for url in ["/app/mappings","/app/mappings/list"]:
                response=self.client.get(url,headers=self.headers(user,shop))
                self.assertEqual(response.status_code,200,response.text)
                data=response.json();rows=data["mappings"] if isinstance(data,dict) else data
                self.assertEqual([row["id"] for row in rows],[expected])

    def test_delete_preserves_identical_ids_in_other_shops_and_users(self):
        response=self.client.delete("/app/mappings/101",headers=self.headers())
        self.assertEqual(response.status_code,200)
        self.assertIsNone(self.db.get(Mapping,101))
        self.assertIsNotNone(self.db.get(Mapping,102))
        self.assertIsNotNone(self.db.get(Mapping,201))

    def test_delete_other_shop_on_same_account_is_rejected(self):
        response=self.client.delete("/app/mappings/102",headers=self.headers())
        self.assertEqual(response.status_code,404)
        self.assertIsNotNone(self.db.get(Mapping,102))

    def test_delete_other_user_mapping_is_rejected(self):
        response=self.client.delete("/app/mappings/201",headers=self.headers())
        self.assertEqual(response.status_code,404)
        self.assertIsNotNone(self.db.get(Mapping,201))

    def test_forged_shop_header_is_rejected(self):
        for method,path in [("get","/app/mappings/list"),("get","/app/mappings"),("delete","/app/mappings/201")]:
            response=getattr(self.client,method)(path,headers=self.headers(1,21))
            self.assertEqual(response.status_code,404)
        self.assertIsNotNone(self.db.get(Mapping,201))

    def test_multiple_shops_require_explicit_selection(self):
        for method,path in [("get","/app/mappings"),("get","/app/mappings/list"),("delete","/app/mappings/101")]:
            response=getattr(self.client,method)(path,headers={"X-Test-User":"1"})
            self.assertEqual(response.status_code,400,response.text)

    def test_single_owned_shop_is_safe_default(self):
        response=self.client.get("/app/mappings",headers={"X-Test-User":"2"})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([row["id"] for row in response.json()],[201])


if __name__ == "__main__":
    unittest.main()
