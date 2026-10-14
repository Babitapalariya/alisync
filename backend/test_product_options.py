import ast
import copy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from alisync_app import shopify


def resolver():
    tree = ast.parse(Path(__file__).with_name('alisync_app').joinpath('aliexpress.py').read_text(encoding='utf-8'))
    ns = {}
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_resolve_sku_info']
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'aliexpress.py', 'exec'), ns)
    return ns['_resolve_sku_info']


class OptionImportTests(unittest.TestCase):
    def setUp(self):
        self.skus = []
        for i, (color, size) in enumerate([('AB0102', 'M'), ('AB0102', 'L'), ('Black', 'M')], 1):
            props = [dict(sku_property_id=14, sku_property_name='Color', property_value_definition_name=color),
                     dict(sku_property_id=5, sku_property_name='Size', property_value_definition_name=size)]
            info = resolver()({'ae_sku_property_dtos': {'ae_sku_property_d_t_o': props}})
            self.skus.append(dict(info, sku_id=str(i), sale_price=str(50+i), stock=i))

    def test_color_size_preserve_real_combinations_prices_and_sku_links(self):
        product = shopify.normalize_aliexpress_product({'skus': self.skus})
        self.assertEqual([o['name'] for o in product['options']], ['Color', 'Size'])
        self.assertEqual([(v['option1'], v['option2']) for v in product['variants']],
                         [('AB0102', 'M'), ('AB0102', 'L'), ('Black', 'M')])
        self.assertEqual([v['price'] for v in product['variants']], ['51', '52', '53'])
        self.assertEqual([v['sku'] for v in product['variants']], ['1', '2', '3'])
        self.assertEqual([v['inventory_quantity'] for v in product['variants']], [1, 2, 3])

    def test_reordered_properties_use_property_identity(self):
        self.skus[1]['options'].reverse()
        _, rows = shopify.build_supplier_options(self.skus)
        self.assertEqual(rows[1], ['AB0102', 'L'])

    def test_incomplete_duplicate_and_excess_options_rejected(self):
        for kind in ('missing', 'duplicate', 'excess'):
            skus = copy.deepcopy(self.skus)
            if kind == 'missing': skus[1]['options'].pop()
            if kind == 'duplicate': skus[1]['options'] = copy.deepcopy(skus[0]['options'])
            if kind == 'excess':
                for s in skus:
                    s['options'] += [dict(id='a', name='A', value='a'), dict(id='b', name='B', value='b')]
            with self.subTest(kind=kind), self.assertRaises(HTTPException):
                shopify.build_supplier_options(skus)

    def test_legacy_labels_remain_readable(self):
        options, rows = shopify.build_supplier_options([{'label': 'Red / Large'}])
        self.assertEqual(options[0]['name'], 'Variant')
        self.assertEqual(rows, [['Red / Large']])

    def test_warehouse_collapse_keeps_color_and_size_options(self):
        tree = ast.parse(Path(__file__).with_name('alisync_app').joinpath('aliexpress.py').read_text(encoding='utf-8'))
        ns = {'COUNTRY_CODE_TO_NAME': {'US': 'United States'},
              'KNOWN_SHIP_LOCATIONS': {'united states', 'china mainland'}}
        selected = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name in ('_sku_stock_int', '_filter_skus_by_ship_location')]
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'aliexpress.py', 'exec'), ns)
        skus = []
        for country in ('United States', 'China Mainland'):
            s = copy.deepcopy(self.skus[0])
            s['label'] += ' / ' + country
            s['options'].append(dict(id='200007763', name='Ships From', value=country))
            skus.append(s)
        result = ns['_filter_skus_by_ship_location'](skus, 'US')
        self.assertEqual(len(result), 1)
        self.assertEqual([o['name'] for o in result[0]['options']], ['Color', 'Size'])
        self.assertEqual(len(skus[0]['options']), 3)



if __name__ == "__main__":
    unittest.main()
