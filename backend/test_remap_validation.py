"""Remap collisions are rejected before supplier calls or database changes."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from fastapi import Depends, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from alisync_app.models import Base, ImportedProduct, ProductMapping


class RemapValidationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.shop = SimpleNamespace(id=11)
        self.db.add_all([
            ImportedProduct(id=1, user_id=1, shop_id=11, aliexpress_id='100'),
            ProductMapping(id=1, user_id=1, shop_id=11, aliexpress_id='200', shopify_product_id='500'),
            ImportedProduct(id=2, user_id=1, shop_id=11, aliexpress_id='300'),
            ProductMapping(id=2, user_id=1, shop_id=11, aliexpress_id='400', shopify_product_id='600'),
            ImportedProduct(id=3, user_id=2, shop_id=22, aliexpress_id='900'),
        ])
        self.db.commit()
        self.fetch = Mock()
        self.ns = dict(ImportedProduct=ImportedProduct, ProductMapping=ProductMapping,
            HTTPException=HTTPException, Depends=Depends, Session=Session, Shop=object,
            get_db=lambda: None, get_current_shop=lambda: None,
            require_entity=Mock(), get_product=self.fetch)
        tree = ast.parse(Path(__file__).with_name('alisync_app').joinpath('operations.py').read_text(encoding='utf-8'))
        names = {'_validate_remap_id_available', 'remap_listing', 'remap_mapping_listing'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        for n in nodes:
            n.decorator_list = []
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'operations.py', 'exec'), self.ns)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_both_endpoints_reject_duplicates_in_either_table(self):
        for endpoint in ('remap_listing', 'remap_mapping_listing'):
            for duplicate in ('300', '400'):
                with self.subTest(endpoint=endpoint, duplicate=duplicate), self.assertRaises(HTTPException) as error:
                    self.ns[endpoint](1, {'new_aliexpress_id': ' ' + duplicate + ' '}, self.db, self.shop)
                self.assertEqual(error.exception.status_code, 409)
                self.assertIn('already used', error.exception.detail)
        self.fetch.assert_not_called()
        self.assertEqual(self.db.get(ImportedProduct, 1).aliexpress_id, '100')
        self.assertEqual(self.db.get(ProductMapping, 1).aliexpress_id, '200')

    def test_current_id_is_rejected(self):
        for endpoint, current in (('remap_listing', '100'), ('remap_mapping_listing', '200')):
            with self.assertRaises(HTTPException) as error:
                self.ns[endpoint](1, {'new_aliexpress_id': current}, self.db, self.shop)
            self.assertEqual(error.exception.status_code, 400)
        self.fetch.assert_not_called()

    def test_unused_and_other_shop_ids_are_allowed(self):
        for new_id in ('800', '900'):
            self.ns['_validate_remap_id_available'](self.db, self.shop, new_id)

    def test_replacement_failure_does_not_change_supplier(self):
        for endpoint, model in (('remap_listing', ImportedProduct), ('remap_mapping_listing', ProductMapping)):
            record = self.db.get(model, 1)
            record.shopify_product_id = '500'
            self.db.commit()
            old_id = record.aliexpress_id
            self.fetch.return_value = {'skus': [{'sku_id': 'new'}]}
            replace = Mock(side_effect=HTTPException(502, 'Shopify rejected replacement'))
            self.ns.update(is_listing_dead=lambda raw: False, move_access=Mock(),
                _apply_product_price_increase=lambda skus, *args: skus,
                replace_supplier_variants=replace)
            with self.assertRaises(HTTPException):
                self.ns[endpoint](1, {'new_aliexpress_id': '800'}, self.db, self.shop)
            replace.assert_called_once()
            self.assertEqual(self.db.get(model, 1).aliexpress_id, old_id)

    def test_both_routes_preserve_content_and_replace_before_save(self):
        for endpoint, model in (('remap_listing', ImportedProduct), ('remap_mapping_listing', ProductMapping)):
            record = self.db.get(model, 1)
            record.shopify_product_id = '500'
            record.title = 'My title'
            record.custom_title = 'Custom title'
            record.custom_description = 'My description'
            self.db.commit()
            old_id = record.aliexpress_id
            new_id = '801' if model is ImportedProduct else '802'
            self.fetch.return_value = {'title': 'Supplier title', 'skus': [{'sku_id': 'new'}], 'total_stock': 0}
            def replace(shop, product_id, supplier, supplier_id):
                self.assertEqual(record.aliexpress_id, old_id)
                self.assertEqual(supplier_id, new_id)
                return {'shopify_variants_replaced': True, 'warnings': []}
            self.ns.update(is_listing_dead=lambda raw: False, move_access=Mock(),
                _apply_product_price_increase=lambda skus, *args: skus,
                replace_supplier_variants=Mock(side_effect=replace), _safe_int=lambda value: value,
                SyncLog=lambda **kwargs: __import__('alisync_app.models', fromlist=['SyncLog']).SyncLog(**kwargs),
                create_notification=Mock())
            self.shop.user_id = 1
            result = self.ns[endpoint](1, {'new_aliexpress_id': new_id}, self.db, self.shop)
            self.assertTrue(result['shopify_variants_replaced'])
            self.assertEqual(record.aliexpress_id, new_id)
            self.assertEqual(record.title, 'My title')
            self.assertEqual(record.custom_title, 'Custom title')
            self.assertEqual(record.custom_description, 'My description')
            if model is ImportedProduct:
                self.assertEqual(record.skus, [{'sku_id': 'new'}])
                self.assertEqual(record.total_stock, 0)


if __name__ == '__main__':
    unittest.main()
