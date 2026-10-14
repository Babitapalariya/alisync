"""Exercise subscription HTTP boundaries without external requests or startup jobs."""
import ast
import copy
import math
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from alisync_app.models import User, Shop, Product, Mapping, PlanProduct
from alisync_app.schemas import PlanSelectionRequest
from alisync_app import subscriptions as policy


class SubscriptionApiTests(unittest.TestCase):
    def setUp(self):
        engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
        for model in (User,Shop,Product,Mapping,PlanProduct):model.__table__.create(engine)
        self.db=Session(engine);self.engine=engine
        self.user=User(id=1,email="test@local.invalid",password_hash="test",email_verified=True,selected_plan="free")
        self.shop=Shop(id=1,user_id=1,shop_domain="test.myshopify.com")
        self.db.add_all([self.user,self.shop]);self.db.commit()
        policy.reserve_product(self.db,self.shop,"123")
        self.db.add(Product(id=1,user_id=1,shop_id=1,aliexpress_id="123",shopify_product_id="1000"));self.db.commit()
        self.supplier=Mock(return_value={"skus":[{"sku_id":"a","price":"100"}]})
        self.sync=Mock(return_value="updated")
        router=APIRouter()
        ns=dict(policy.__dict__,__name__="alisync_app.routes",__package__="alisync_app",router=router,api=router,
            Depends=Depends,Query=Query,Session=Session,get_db=lambda:self.db,current_user=lambda:self.user,
            get_current_shop=lambda:self.shop,Shop=Shop,ImportedProduct=Product,ProductMapping=Mapping,
            PlanSelectionRequest=PlanSelectionRequest,get_settings=lambda:SimpleNamespace(BILLING_ENABLED=False),
            get_product=self.supplier,update_shopify_product_prices_with_skus=self.sync,copy=copy,math=math)
        root=Path(__file__).parent/"alisync_app"
        selections={"routes.py":{"select_subscription","set_full_product_access","subscription_entitlements"},
                    "operations.py":{"set_product_markup","_apply_product_price_increase","sync_product_price","manual_product_price_sync","sync_mapping_inventory","bulk_increase_imported_products"}}
        for file,names in selections.items():
            nodes=[n for n in ast.parse((root/file).read_text(encoding="utf-8")).body if isinstance(n,ast.FunctionDef) and n.name in names]
            exec(compile(ast.Module(body=nodes,type_ignores=[]),str(root/file),"exec"),ns)
        self.ns=ns;app=FastAPI();app.include_router(router);self.client=TestClient(app)

    def tearDown(self):
        self.client.close();self.db.close();self.engine.dispose()

    def test_free_basic_actions_block_before_supplier_calls(self):
        for i in range(6): policy.reserve_product(self.db,self.shop,str(200+i))
        self.db.get(Product,1).aliexpress_id="205";self.db.commit()
        response=self.client.post("/pricing/imported/1/markup",json={"kind":"fixed","amount":5})
        self.assertEqual(response.status_code,403);self.supplier.assert_not_called();self.sync.assert_not_called()
        self.assertEqual(self.client.post("/dashboard/products/1/sync-price").status_code,403)
        self.assertEqual(self.client.post("/dashboard/products/bulk-increase",json={"product_ids":[1],"increase_by":5}).status_code,403)
        self.assertFalse(self.ns["sync_product_price"](self.shop,1,self.db));self.supplier.assert_not_called()

    def test_selected_growth_product_can_apply_percentage(self):
        self.client.post("/subscription/select",json={"plan":"growth"})
        result=self.client.post("/subscription/products/imported/1/access",json={"enabled":True})
        self.assertEqual(result.status_code,200,result.text)
        result=self.client.post("/pricing/imported/1/markup",json={"kind":"percentage","amount":10})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(float(self.sync.call_args.args[2][0]["price"]),110)
        self.assertEqual(self.db.get(Product,1).price_percentage,10)
        self.assertEqual(self.db.get(Product,1).price_mode,"percentage")

    def test_payment_free_plan_switch_and_growth_denial(self):
        response=self.client.post("/subscription/select",json={"plan":"growth"})
        self.assertEqual(response.status_code,200,response.text)
        self.assertFalse(response.json()["payment_required"])
        self.assertEqual(self.client.post("/pricing/imported/1/markup",json={"amount":5}).status_code,403)
        self.assertFalse(self.ns["sync_product_price"](self.shop,1,self.db));self.supplier.assert_not_called()
        self.assertEqual(self.client.post("/subscription/select",json={"plan":"pro"}).status_code,200)
        self.assertEqual(self.client.post("/pricing/imported/1/markup",json={"amount":-10}).status_code,200)
        self.assertEqual(float(self.sync.call_args.args[2][0]["price"]),90)

    def test_invalid_percentage_has_no_external_effect(self):
        self.client.post("/subscription/select",json={"plan":"pro"})
        self.assertEqual(self.client.post("/pricing/imported/1/markup",json={"kind":"percentage","amount":-101}).status_code,400)
        self.supplier.assert_not_called();self.sync.assert_not_called()


if __name__ == "__main__":unittest.main()
