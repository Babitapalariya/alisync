"""Subscription boundaries use SQLite only; no Shopify, billing or production database."""
import ast
import copy
import math
import unittest
from pathlib import Path
from types import SimpleNamespace
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from alisync_app.models import User, Shop, Product, Mapping, PlanProduct
from alisync_app import subscriptions as policy


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        for model in (User, Shop, Product, Mapping, PlanProduct):
            model.__table__.create(self.engine)
        self.db = Session(self.engine)
        self.db.add_all([User(id=i, email=f"{i}@test.invalid", password_hash="test", email_verified=True, selected_plan="free") for i in (1, 2)])
        self.db.add_all([Shop(id=i, user_id=1 if i < 3 else 2, shop_domain=f"s{i}.myshopify.com") for i in (1, 2, 3)])
        self.db.commit()
        self.shop = self.db.get(Shop, 1)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def select(self, plan):
        user = self.db.get(User, 1); user.selected_plan = plan; self.db.commit()

    def add(self, number, mapping=False, shop=None):
        shop = shop or self.shop
        policy.reserve_product(self.db, shop, str(number), mapping=mapping)
        model = Mapping if mapping else Product
        row = model(user_id=shop.user_id, shop_id=shop.id, aliexpress_id=str(number), shopify_product_id=str(number+1000))
        self.db.add(row); self.db.commit()
        return row

    def access_endpoint(self):
        source = Path(__file__).parent / "alisync_app/routes.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "set_full_product_access")
        ns = dict(policy.__dict__, __name__="alisync_app.routes", __package__="alisync_app", router=APIRouter(), Depends=Depends, Session=Session, current_user=lambda:None, get_db=lambda:None)
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),"exec"),ns)
        return ns[node.name]

    def test_free_quota_includes_all_stores_and_repeat_is_idempotent(self):
        for i in range(25): policy.reserve_product(self.db,self.shop,str(i))
        policy.reserve_product(self.db,self.shop,"0")
        with self.assertRaises(HTTPException) as error: policy.reserve_product(self.db,self.db.get(Shop,2),"26")
        self.assertEqual(error.exception.status_code,403)
        self.assertEqual(error.exception.detail,policy.FREE_LIMIT)
        self.db.rollback()
        policy.reserve_product(self.db,self.db.get(Shop,3),"26")

    def test_growth_user_chooses_five_and_can_release(self):
        self.select("growth")
        rows=[self.add(i) for i in range(6)]
        endpoint=self.access_endpoint();user=self.db.get(User,1)
        self.assertFalse(policy.has_full_access(self.db,self.shop,"0"))
        for row in rows[:5]: endpoint("imported",row.id,{"enabled":True},self.db,user)
        with self.assertRaises(HTTPException) as error: endpoint("imported",rows[5].id,{"enabled":True},self.db,user)
        self.assertEqual(error.exception.detail,policy.FULL_LIMIT);self.db.rollback()
        endpoint("imported",rows[0].id,{"enabled":False},self.db,user)
        endpoint("imported",rows[5].id,{"enabled":True},self.db,user)
        self.assertTrue(policy.has_full_access(self.db,self.shop,"5"))
        self.assertFalse(policy.has_full_access(self.db,self.shop,"0"))
        with self.assertRaises(HTTPException): endpoint("imported",rows[0].id,{"enabled":True},self.db,self.db.get(User,2))

    def test_free_mapping_and_first_five_access(self):
        for i in range(25): self.add(i,mapping=True)
        for i in range(25):
            self.assertEqual(policy.has_full_access(self.db,self.shop,str(i)),i<5)
        with self.assertRaises(HTTPException): policy.reserve_product(self.db,self.shop,"26",mapping=True)
        self.db.rollback();self.select("growth")
        self.add(26,mapping=True)
        policy.require_access(self.db,self.shop,"20","relink")
        with self.assertRaises(HTTPException): policy.require_access(self.db,self.shop,"20")
        with self.assertRaises(HTTPException): policy.require_any_full(self.db,self.shop)

    def test_upgrades_downgrades_preserve_catalogue_and_free_selection(self):
        row=self.add(1)
        self.select("pro")
        for i in range(2,30):self.add(i)
        self.assertTrue(policy.has_full_access(self.db,self.shop,"29"))
        self.select("free")
        self.assertTrue(policy.has_full_access(self.db,self.shop,"1"))
        self.assertFalse(policy.has_full_access(self.db,self.shop,"29"))
        self.assertEqual(self.db.query(Product).count(),29)
        with self.assertRaises(HTTPException): policy.reserve_product(self.db,self.shop,"30")

    def test_relink_preserves_selected_slot(self):
        self.add(1,mapping=True)
        policy.move_access(self.db,self.shop,"1","2");self.db.commit()
        self.assertTrue(policy.has_full_access(self.db,self.shop,"2"))
        self.assertFalse(policy.has_full_access(self.db,self.shop,"1"))

    def test_percentage_math_and_supplier_baseline(self):
        source=Path(__file__).parent / "alisync_app/operations.py"
        tree=ast.parse(source.read_text(encoding="utf-8"));node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="_apply_product_price_increase")
        ns={"copy":copy};exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),"exec"),ns)
        apply=ns[node.name];skus=[{"price":"100"}]
        for fixed,percent,expected in [(50,0,150),(-10,0,90),(0,20,120),(0,-10,90)]:
            result=apply(skus,fixed,percent)[0]
            self.assertEqual(float(result["price"]),expected);self.assertEqual(result["_supplier_price"],"100")
        self.assertEqual(skus,[{"price":"100"}])


if __name__ == "__main__": unittest.main()
