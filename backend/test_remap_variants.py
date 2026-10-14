import unittest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from alisync_app import remap_variants as remap
from types import SimpleNamespace


class RemapVariantsTests(unittest.TestCase):
    def test_images_follow_sku_ids_after_reordering_and_share_color_image(self):
        skus = [
            {'sku_id': 'red-m', 'options': [{'id': '14', 'value': 'red', 'image': 'https://example.com/red.jpg'}]},
            {'sku_id': 'red-l', 'options': [{'id': '14', 'value': 'red'}]},
        ]
        resolved = remap.resolve_supplier_images(skus)
        variants = [{'id': 22, 'aliexpress_sku_id': 'red-l'}, {'id': 11, 'aliexpress_sku_id': 'red-m'}]
        with patch.object(remap.shopify, '_get_all_variants_for_image_sync', return_value=variants), \
             patch.object(remap.shopify, '_upload_image_to_shopify', return_value=99) as upload, \
             patch.object(remap.shopify, '_shopify_request', return_value=Mock(json=lambda: {'variant': {'image_id': 99}})) as write:
            result = remap.sync_replacement_images(self.shop, '42', resolved)
        self.assertEqual(result['attached'], 2)
        self.assertEqual(result['remaining'], 0)
        upload.assert_called_once()
        self.assertEqual([c.kwargs['json']['variant']['id'] for c in write.call_args_list], [11, 22])

    def test_image_sync_refuses_unmatched_variants(self):
        with patch.object(remap.shopify, '_get_all_variants_for_image_sync', return_value=[{'id': 1, 'aliexpress_sku_id': 'old'}]), \
             patch.object(remap.shopify, '_upload_image_to_shopify') as upload:
            with self.assertRaises(ValueError):
                remap.sync_replacement_images(self.shop, '42', self.raw['skus'])
        upload.assert_not_called()

    def setUp(self):
        self.shop = SimpleNamespace(shop_domain="test.myshopify.com", access_token="test")
        self.raw = {"skus": [{"sku_id": "new1", "label": "black", "sale_price": "12.50", "stock": 0},
                             {"sku_id": "new2", "label": "white", "sale_price": "15", "stock": 8}]}

    def test_replaces_complete_list_without_changing_product_content(self):
        locations = Mock(json=lambda: {"locations": [{"id": 7, "active": True}]})
        response = Mock(json=lambda: {"data": {"productSet": {"product": {"id": "gid://shopify/Product/42"}, "userErrors": []}}})
        with patch.object(remap.shopify, "_shopify_request", side_effect=[locations, response]) as request, \
             patch.object(remap.shopify, "_h", return_value={}), \
             patch.object(remap.shopify, "_graphql", return_value={"metafieldsSet": {"userErrors": []}}), \
             patch.object(remap.shopify, "_invalidate_lock_cache"):
            result = remap.replace_supplier_variants(self.shop, "42", self.raw, "new-product")
        payload = request.call_args.kwargs["json"]["variables"]["input"]
        self.assertEqual(set(payload), {"id", "productOptions", "variants"})
        self.assertEqual(len(payload["variants"]), 2)
        self.assertTrue(all("id" not in variant for variant in payload["variants"]))
        self.assertEqual(payload["variants"][0]["inventoryQuantities"][0]["quantity"], 0)
        self.assertEqual(payload["variants"][1]["metafields"][0]["value"], "new2")
        self.assertEqual(payload["variants"][0]["inventoryItem"]["sku"], "new1")
        self.assertTrue(result["shopify_variants_replaced"])

    def test_invalid_supplier_data_cannot_delete_variants(self):
        for raw in ({"skus": []}, {"skus": [dict(self.raw["skus"][0], stock=None)]},
                    {"skus": [dict(self.raw["skus"][0], sale_price="NaN")]}):
            with patch.object(remap.shopify, "_shopify_request") as request:
                with self.assertRaises(HTTPException):
                    remap.replace_supplier_variants(self.shop, "42", raw, "new")
                request.assert_not_called()



    def test_shopify_errors_and_partial_metafield_failure(self):
        locations = Mock(json=lambda: {"locations": [{"id": 7}]})
        for rejected in (True, False):
            body = {"product": {"id": "gid://shopify/Product/42"},
                    "userErrors": [{"message": "Invalid options"}] if rejected else []}
            response = Mock(json=lambda: {"data": {"productSet": body}})
            with patch.object(remap.shopify, "_shopify_request", side_effect=[locations, response]), \
                 patch.object(remap.shopify, "_h", return_value={}), \
                 patch.object(remap.shopify, "_graphql", side_effect=RuntimeError("Unavailable")) as metadata:
                if rejected:
                    with self.assertRaises(HTTPException):
                        remap.replace_supplier_variants(self.shop, "42", self.raw, "new")
                    metadata.assert_not_called()
                else:
                    result = remap.replace_supplier_variants(self.shop, "42", self.raw, "new")
                    self.assertTrue(result["shopify_variants_replaced"])
                    self.assertFalse(result["shopify_metafield_updated"])
                    self.assertTrue(result["warnings"])

    def test_new_sku_images_are_synced_and_failures_reported(self):
        self.raw["skus"][0]["image"] = "https://example.com/black.jpg"
        locations = Mock(json=lambda: {"locations": [{"id": 7}]})
        response = Mock(json=lambda: {"data": {"productSet": {"product": {"id": "gid://shopify/Product/42"}, "userErrors": []}}})
        for outcome in ({"attached": 2, "remaining": 0}, {"attached": 1, "remaining": 1}, RuntimeError("upload failed")):
            with patch.object(remap.shopify, "_shopify_request", side_effect=[locations, response]), \
                 patch.object(remap.shopify, "_h", return_value={}), \
                 patch.object(remap.shopify, "_graphql", return_value={"metafieldsSet": {"userErrors": []}}), \
                 patch.object(remap, "sync_replacement_images") as images:
                if isinstance(outcome, Exception):
                    images.side_effect = outcome
                else:
                    images.return_value = outcome
                result = remap.replace_supplier_variants(self.shop, "42", self.raw, "new")
            images.assert_called_once_with(self.shop, "42", self.raw["skus"])
            expected = isinstance(outcome, dict) and outcome["remaining"] == 0
            self.assertEqual(result["shopify_images_updated"], expected)
            self.assertEqual(bool(result["warnings"]), not expected)
            self.assertTrue(result["shopify_metafield_updated"])
            self.assertTrue(result["shopify_variants_replaced"])
