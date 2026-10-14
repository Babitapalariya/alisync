"""Offline mapping bulk-increase and explicit-sync regressions."""
import ast
import copy
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from fastapi import Depends, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from alisync_app.database import Base
from alisync_app.models import Mapping, User
from alisync_app.subscriptions import require_bulk, require_access, require_entity


class MappingPriceTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db=Session(self.engine)
        self.db.add(User(id=1,email="pro@test.invalid",password_hash="test",selected_plan="pro"))
        self.db.add_all([Mapping(id=1,user_id=1,shop_id=11,aliexpress_id="ali1",shopify_product_id="100",price_mode="auto"),
            Mapping(id=2,user_id=1,shop_id=11,aliexpress_id="ali2",shopify_product_id="200",price_mode="auto"),
            Mapping(id=3,user_id=2,shop_id=21,aliexpress_id="ali1",shopify_product_id="100",price_mode="auto")])
        self.db.commit()
        self.shop=SimpleNamespace(id=11,user_id=1)
        self.sync=Mock(return_value="updated")
        self.raw={"sale_price":"10", "skus":[{"sku_id":"sku1","sale_price":"10"}]}
        self.ns=dict(require_bulk=require_bulk,require_access=require_access,require_entity=require_entity,Depends=Depends,HTTPException=HTTPException,Session=Session,Shop=object,ProductMapping=Mapping,
            get_db=lambda:None,get_current_shop=lambda:None,math=math,
            get_product=lambda *args:copy.deepcopy(self.raw),update_shopify_product_prices_with_skus=self.sync,
            is_listing_dead=lambda raw:False,_record_supplier_check=lambda *args:None,
            update_shopify_product_inventory_with_skus=lambda *args:True)
        source=Path(__file__).parent/'alisync_app'/'operations.py'
        names={'bulk_increase_mappings','increase_mapping_price','sync_mapped_product_price'}
        functions=[n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name in names]
        for fn in functions:fn.decorator_list=[]
        exec(compile(ast.Module(body=functions,type_ignores=[]),str(source),'exec'),self.ns)

    def tearDown(self):
        self.db.close();self.engine.dispose()

    def bulk(self,amount=5,ids=None):
        return self.ns['bulk_increase_mappings']({'mapping_ids':ids or [1],'increase_by':amount},self.db,self.shop)

    def test_bulk_persists_mode_and_replaces_markup(self):
        self.bulk();self.bulk(3)
        row=self.db.get(Mapping,1)
        self.assertEqual((row.price_mode,row.price_increase),('increase',3))
        self.assertEqual(self.sync.call_args.args[2][0]['sale_price'],'13.0')
        self.assertEqual(self.sync.call_args.args[2][0]['_supplier_price'],'10')

    def test_bulk_cannot_change_another_shop(self):
        with self.assertRaises(HTTPException) as error:
            self.bulk(ids=[1,3])
        self.assertEqual(error.exception.status_code,404)
        self.assertEqual(self.db.get(Mapping,3).price_mode,'auto')
        self.assertEqual(self.db.get(Mapping,3).price_increase,0)
        self.assertEqual(self.sync.call_count,0)

    def test_partial_failure_does_not_save_increase_for_failed_mapping(self):
        def sync(shop,product,skus,**kwargs):
            if product=='200':
                kwargs['error_details'].append('No matching AliExpress SKU')
                return 'failed'
            return 'updated'
        self.sync.side_effect=sync
        result=self.bulk(ids=[1,2])
        self.assertEqual(result['success_count'],1)
        self.assertEqual(result['failed_count'],1)
        self.assertEqual(self.db.get(Mapping,2).price_mode,'auto')
        self.assertEqual(self.db.get(Mapping,2).price_increase,0)
        self.assertIn('No matching',result['results'][1]['error'])

    def test_sync_now_restores_auto_and_passes_unadjusted_supplier_prices(self):
        self.bulk()
        result=self.ns['sync_mapped_product_price']('ali1',self.db,self.shop)
        row=self.db.get(Mapping,1)
        self.assertEqual((row.price_mode,row.price_increase),('auto',0))
        self.assertEqual(result['price_mode'],'auto')
        self.assertTrue(self.sync.call_args.kwargs['reset_manual_increases'])
        self.assertEqual(self.sync.call_args.args[2][0]['sale_price'],'10')

    def test_sync_failure_preserves_mode_and_explains_reason(self):
        self.bulk()
        def fail(*args,**kwargs):
            kwargs['error_details'].append('Variant 123 has no matching supplier SKU')
            return 'failed'
        self.sync.side_effect=fail
        with self.assertRaises(HTTPException) as context:
            self.ns['sync_mapped_product_price']('ali1',self.db,self.shop)
        self.assertIn('Variant 123',context.exception.detail)
        self.assertEqual(self.db.get(Mapping,1).price_mode,'increase')
        self.assertEqual(self.db.get(Mapping,1).price_increase,5)

    def test_variant_manual_mode_resets_even_if_inventory_raises(self):
        row=self.db.get(Mapping,1)
        row.price_mode='variant_manual';row.price_increase=0;self.db.commit()
        self.ns['update_shopify_product_inventory_with_skus']=Mock(side_effect=RuntimeError('inventory unavailable'))
        result=self.ns['sync_mapped_product_price']('ali1',self.db,self.shop)
        self.assertEqual(self.db.get(Mapping,1).price_mode,'auto')
        self.assertTrue(self.sync.call_args.kwargs['reset_manual_increases'])
        self.assertEqual(result['price_mode'],'auto')
        self.assertIn('Locked variant prices and increases preserved',result['message'])
        self.assertFalse(result['inventory_updated'])

    def test_invalid_amount_never_updates_shopify(self):
        with self.assertRaises(HTTPException):self.bulk(float('nan'))
        self.sync.assert_not_called()


if __name__=='__main__':unittest.main()
