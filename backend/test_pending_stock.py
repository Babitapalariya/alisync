import ast
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session
from alisync_app.database import Base
from alisync_app.models import Shop, PendingImport, ImportedProduct, User
from alisync_app.subscriptions import has_full_access


class PendingStockTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine)
        self.db=Session(self.engine)
        self.db.add(User(id=1,email="pro@test.invalid",password_hash="test",selected_plan="pro"))
        self.db.add(Shop(id=1,user_id=1,shop_domain='test.myshopify.com',access_token='test'))
        self.pending=PendingImport(id=1,user_id=1,shop_id=1,aliexpress_id='ali',status='partial')
        self.product=ImportedProduct(id=1,user_id=1,shop_id=1,aliexpress_id='ali',shopify_product_id='123',auto_inventory_sync=True)
        self.db.add_all([self.pending,self.product]);self.db.commit()
        self.raw={'skus':[{'sku_id':'a','stock':10}]}
        self.update=Mock(return_value=True);self.importer=Mock(return_value=(self.product,True))
        self.ns=dict(has_full_access=has_full_access,__package__='alisync_app',SessionLocal=lambda:self.db,PendingImport=PendingImport,
            ImportedProduct=ImportedProduct,Shop=Shop,sqlfunc=func,HTTPException=HTTPException,
            get_product=lambda *args:self.raw,update_shopify_product_inventory_with_skus=self.update,
            import_aliexpress_product_to_shopify=self.importer)
        source=Path(__file__).parent/'alisync_app'/'operations.py'
        nodes=[n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name in {'classify_skus','process_pending_imports','upsert_pending','list_pending_imports','retry_pending_import'}]
        for node in nodes:
            node.decorator_list=[]
            node.args.defaults=[ast.Constant(None) for _ in node.args.defaults]
        self.ns.update(Session=Session, require_access=Mock())
        ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[]))
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),self.ns)
        self.enterContext(patch('alisync_app.shop_context.ensure_shopify_access_token',side_effect=lambda shop,db:shop))

    def tearDown(self):self.db.close();self.engine.dispose()
    def run_job(self):
        self.ns['process_pending_imports']()
        return self.db.get(PendingImport,1)

    def test_partial_is_rechecked_and_completes_on_returned_stock(self):
        row=self.run_job();self.assertEqual(row.status,'imported');self.assertEqual(row.retry_count,1)
        self.update.assert_called_once()

    def test_partial_keeps_retrying(self):
        self.raw['skus'].append({'sku_id':'b','stock':0})
        self.assertEqual(self.run_job().status,'partial')
        self.assertEqual(self.run_job().retry_count,2)
        self.assertEqual(self.update.call_count,2)

    def test_update_failure_does_not_complete_queue(self):
        self.update.return_value=False
        row=self.run_job();self.assertEqual(row.status,'partial');self.assertIn('failed',row.error_message)

    def test_returned_stock_automatically_imports_unlinked_product(self):
        self.product.shopify_product_id=None;self.pending.status='pending';self.db.commit()
        row=self.run_job()
        self.assertEqual(row.status,'imported')
        self.importer.assert_called_once()
        self.update.assert_not_called()

    def test_partial_stock_imports_and_stays_queued(self):
        self.product.shopify_product_id=None;self.pending.status='pending';self.db.commit()
        self.raw['skus'].append({'sku_id':'b','stock':0})
        row=self.run_job()
        self.assertEqual(row.status,'partial')
        self.importer.assert_called_once()

    def test_all_out_of_stock_is_not_imported(self):
        self.db.delete(self.product);self.pending.status='pending';self.db.commit()
        self.raw['skus'][0]['stock']=0
        self.assertEqual(self.run_job().status,'pending');self.importer.assert_not_called()

    def test_unknown_stock_does_not_complete_queue(self):
        self.raw['skus'][0]['stock']=None
        self.assertEqual(self.run_job().status,'partial');self.update.assert_not_called()

    def test_disabled_inventory_sync_is_respected(self):
        self.product.auto_inventory_sync=False;self.db.commit()
        self.assertEqual(self.run_job().status,'partial');self.update.assert_not_called()

    def test_import_conflict_is_not_marked_complete(self):
        self.db.delete(self.product);self.pending.status='pending';self.db.commit()
        self.importer.side_effect=HTTPException(409,'Conflicting mapping')
        row=self.run_job();self.assertEqual(row.status,'pending');self.assertIn('Conflicting',row.error_message)

    def test_scan_queues_mixed_stock_as_partial(self):
        self.raw['skus'].append({'sku_id':'b','stock':0})
        shop=self.db.get(Shop,1)
        row=self.ns['upsert_pending'](shop,'ali',self.raw,self.ns['classify_skus'](self.raw),'pending',self.db)
        self.assertEqual(row.status,'partial')

    def test_saved_stock_drives_display_instead_of_stale_queue_status(self):
        for stocks, expected in [([0,10],'partial'),([0,0],'pending'),([0,None],'unknown'),([None,None],'unknown'),([], 'unknown'),([10,20],'in_stock')]:
            with self.subTest(stocks=stocks):
                raw={'skus':[{'sku_id':str(i),'stock':stock} for i,stock in enumerate(stocks)]}
                info=self.ns['classify_skus'](raw)
                self.pending.status='pending';self.pending.product_data=raw
                self.pending.in_stock_skus=info['in_stock'];self.pending.out_of_stock_skus=info['out_of_stock']
                self.db.commit()
                result=self.ns['list_pending_imports'](self.db,self.db.get(Shop,1))['pendings'][0]
                self.assertEqual(result['status'],expected)

    def test_mixed_stock_refresh_corrects_status_even_with_sync_disabled(self):
        self.pending.status='pending';self.product.auto_inventory_sync=False;self.db.commit()
        self.raw['skus'].append({'sku_id':'b','stock':0})
        self.assertEqual(self.run_job().status,'partial');self.update.assert_not_called()

    def test_retry_saves_fresh_snapshot_and_mixed_status(self):
        self.pending.status='pending';self.db.commit()
        self.raw['skus'].append({'sku_id':'b','stock':0})
        self.ns['retry_pending_import']('ali',self.db,self.db.get(Shop,1))
        self.assertEqual(self.pending.status,'partial')
        self.assertEqual(self.pending.product_data,self.raw)

    def test_retry_all_oos_corrects_previous_partial(self):
        self.raw['skus'][0]['stock']=0
        self.ns['retry_pending_import']('ali',self.db,self.db.get(Shop,1))
        self.assertEqual(self.pending.status,'pending')

    def test_retry_missing_stock_does_not_complete_or_import(self):
        self.db.delete(self.product);self.db.commit();self.raw={'skus':[]}
        self.ns['retry_pending_import']('ali',self.db,self.db.get(Shop,1))
        self.assertNotEqual(self.pending.status,'imported')
        self.importer.assert_not_called();self.update.assert_not_called()

if __name__=='__main__':unittest.main()
