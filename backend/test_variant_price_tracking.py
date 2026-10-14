"""Offline regression checks for manual adjustments and supplier movements."""
import copy
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
from alisync_app import shopify
from alisync_app.price_history import observe_supplier_price


class PriceTrackingTests(unittest.TestCase):
    def setUp(self):
        self.shop = SimpleNamespace(shop_domain="first.myshopify.com", access_token="test")
        self.state = {1: {"id": 1, "price": "12.00", "ae_sku_id": "sku1",
            "label": "Red", "price_increase": Decimal("2.00"), "supplier_history": None,
            "supplier_history_id": None, "increase_metafield_id": None,
            "locks": {"price": False, "inventory": False, "image": False}}}
        self.writes = []
        def save(shop, product_id, rows):
            self.assertIs(shop, self.shop)
            self.assertEqual(product_id, "100")
            self.writes.extend(copy.deepcopy(rows))
            for row in rows:
                target = self.state[row["variant_id"]]
                for field in ("price", "supplier_history", "ae_sku_id"):
                    if field in row:
                        target[field] = row[field]
                if "price_increase" in row:
                    target["price_increase"] = Decimal(row["price_increase"])
            return {"success": True, "updated": len(rows), "errors": []}
        self.save = save
        self.reader = self.enterContext(patch.object(shopify, "get_variant_sync_state", return_value=self.state))
        self.writer = self.enterContext(patch.object(shopify, "bulk_update_variant_prices", side_effect=save))

    def sync(self, price, **extra):
        return shopify.update_shopify_product_prices_with_skus(self.shop, "100",
            [{"sku_id": "sku1", "label": "Red", "sale_price": price, **extra}])

    def test_supplier_increase_decrease_and_unchanged(self):
        self.sync("10")
        self.assertIsNone(self.state[1]["supplier_history"]["change"])
        self.sync("13")
        self.assertEqual(self.state[1]["price"], "15.00")
        self.assertEqual(self.state[1]["supplier_history"]["change"], "3.00")
        self.sync("8")
        self.assertEqual(self.state[1]["price"], "10.00")
        self.assertEqual(self.state[1]["supplier_history"]["change"], "-5.00")
        self.sync("8")
        self.assertEqual(self.state[1]["supplier_history"]["change"], "0.00")
        self.assertEqual(self.state[1]["price_increase"], Decimal("2"))

    def test_product_markup_excluded_from_supplier_change(self):
        self.sync("15", _supplier_price="10")
        self.sync("17", _supplier_price="12")
        self.assertEqual(self.state[1]["price"], "19.00")
        self.assertEqual(self.state[1]["supplier_history"]["change"], "2.00")

    def test_locked_price_still_tracks_supplier(self):
        self.state[1]["locks"]["price"] = True
        self.sync("10")
        self.sync("14")
        self.assertEqual(self.state[1]["price"], "12.00")
        self.assertEqual(self.state[1]["supplier_history"]["change"], "4.00")
        self.assertTrue(all("price" not in row for row in self.writes))

    def test_manual_edit_persists_through_next_sync(self):
        shopify.save_variant_price_edits(self.shop, "100", [{"variant_id": 1, "price": "17", "price_increase": "7"}])
        self.sync("11")
        self.assertEqual(self.state[1]["price"], "18.00")
        self.assertEqual(self.state[1]["price_increase"], Decimal("7"))

    def test_locked_absolute_edit_preserves_increase(self):
        self.state[1]["locks"]["price"] = True
        shopify.save_variant_price_edits(self.shop, "100", [{"variant_id": 1, "price": "20"}])
        self.assertEqual(self.state[1]["price_increase"], Decimal("2"))

    def test_edit_replaces_five_with_three_and_sync_keeps_three(self):
        for amount in (5, 3):
            shopify.save_variant_price_edits(self.shop, "100", [{
                "variant_id": 1, "price": str(10 + amount), "price_increase": str(amount)}])
        self.sync("10")
        self.assertEqual(self.state[1]["price"], "13.00")
        self.assertEqual(self.state[1]["price_increase"], Decimal("3"))

    def test_product_adjustment_replaces_existing_variant_increase(self):
        self.state[1]["price_increase"] = Decimal("5")
        self.state[1]["price"] = "15.00"
        result = shopify.update_shopify_product_prices_with_skus(self.shop, "100",
            [{"sku_id": "sku1", "label": "Red", "sale_price": "13", "_supplier_price": "10"}],
            reset_manual_increases=True)
        self.assertEqual(result, "updated")
        self.assertEqual(self.state[1]["price"], "13.00")
        self.assertEqual(self.state[1]["price_increase"], Decimal("0"))

    def test_unknown_variant_rejected_before_any_write(self):
        with self.assertRaises(HTTPException):
            shopify.save_variant_price_edits(self.shop, "100", [{"variant_id": 999, "price": "20"}])
        self.writer.assert_not_called()

    def test_nonfinite_edit_rejected_before_any_write(self):
        with self.assertRaises(HTTPException):
            shopify.save_variant_price_edits(self.shop, "100", [{"variant_id": 1, "price": "20", "price_increase": "NaN"}])
        self.writer.assert_not_called()

    def test_supplier_reordering_does_not_change_matches(self):
        variants = {1: {"label": "Red", "ae_sku_id": "one"}, 2: {"label": "Blue", "ae_sku_id": "two"}}
        skus = [{"label": "Blue", "sku_id": "two"}, {"label": "Red", "sku_id": "one"}]
        matches = shopify._match_supplier_variants(variants, skus)
        self.assertEqual(matches[1]["sku_id"], "one")
        self.assertEqual(matches[2]["sku_id"], "two")

    def test_single_variant_custom_title_has_unambiguous_supplier_match(self):
        self.state[1]["label"] = "My custom variant"
        self.state[1]["ae_sku_id"] = None
        errors = []
        result = shopify.update_shopify_product_prices_with_skus(self.shop, "100",
            [{"sku_id": "sku1", "label": "Supplier name", "sale_price": "10"}],
            reset_manual_increases=True, error_details=errors)
        self.assertEqual(result, "updated")
        self.assertEqual(self.state[1]["ae_sku_id"], "sku1")
        self.assertEqual(errors, [])

    def test_ambiguous_match_never_uses_array_position(self):
        self.assertEqual(shopify._match_supplier_variants({1: {"label": "Red"}},
            [{"label": "Red", "sku_id": "one"}, {"label": "Red", "sku_id": "two"}]), {})

    def test_failed_price_read_cannot_overwrite_rules(self):
        self.reader.side_effect = RuntimeError("offline")
        self.assertEqual(self.sync("11"), "failed")
        self.writer.assert_not_called()

    def test_explicit_sync_clears_variant_markup_and_restores_supplier_price(self):
        result = shopify.update_shopify_product_prices_with_skus(self.shop, "100",
            [{"sku_id": "sku1", "label": "Red", "sale_price": "10"}], reset_manual_increases=True)
        self.assertEqual(result, "updated")
        self.assertEqual(self.state[1]["price"], "10.00")
        self.assertEqual(self.state[1]["price_increase"], Decimal("0"))
        self.sync("11")
        self.assertEqual(self.state[1]["price"], "11.00")

    def test_explicit_sync_respects_price_lock(self):
        self.state[1]["locks"]["price"] = True
        shopify.update_shopify_product_prices_with_skus(self.shop, "100",
            [{"sku_id": "sku1", "label": "Red", "sale_price": "10"}], reset_manual_increases=True)
        self.assertEqual(self.state[1]["price"], "12.00")
        self.assertEqual(self.state[1]["price_increase"], Decimal("2"))

    def test_explicit_sync_resets_only_unlocked_variant_in_mixed_product(self):
        self.state[1]["locks"]["price"] = True
        self.state[2] = {**copy.deepcopy(self.state[1]), "id": 2, "ae_sku_id": "sku2", "label": "Blue",
                         "locks": {"price": False, "inventory": False, "image": False}}
        shopify.update_shopify_product_prices_with_skus(self.shop, "100",
            [{"sku_id": "sku1", "label": "Red", "sale_price": "10"},
             {"sku_id": "sku2", "label": "Blue", "sale_price": "8"}], reset_manual_increases=True)
        self.assertEqual(self.state[1]["price"], "12.00")
        self.assertEqual(self.state[1]["price_increase"], Decimal("2"))
        self.assertEqual(self.state[2]["price"], "8.00")
        self.assertEqual(self.state[2]["price_increase"], Decimal("0"))
        for row in self.writes:
            if row["variant_id"] == 1:
                self.assertNotIn("price", row)
                self.assertNotIn("price_increase", row)

    def test_locked_variant_keeps_product_wide_increase_after_reset(self):
        self.state[1]["locks"]["price"] = True
        self.state[1]["price"] = "17.00"
        skus = [{"sku_id": "sku1", "label": "Red", "sale_price": "10"}]
        shopify.update_shopify_product_prices_with_skus(self.shop, "100", skus,
            reset_manual_increases=True, product_price_increase=5)
        self.assertEqual(self.state[1]["price"], "17.00")
        self.assertEqual(self.state[1]["price_increase"], Decimal("7"))
        shopify.update_shopify_product_prices_with_skus(self.shop, "100", skus, reset_manual_increases=True)
        self.assertEqual(self.state[1]["price_increase"], Decimal("7"))

    def test_explicit_sync_does_not_partially_reset_unmatched_variants(self):
        self.state[2] = {**copy.deepcopy(self.state[1]), "id": 2, "label": "Blue", "ae_sku_id": "sku2"}
        result = shopify.update_shopify_product_prices_with_skus(self.shop, "100",
            [{"sku_id": "sku1", "label": "Red", "sale_price": "10"}], reset_manual_increases=True)
        self.assertEqual(result, "failed")
        self.writer.assert_not_called()

    def test_new_link_starts_new_baseline(self):
        self.state[1]["supplier_history"] = observe_supplier_price(None, "100")
        self.state[1]["ae_sku_id"] = "old-link"
        self.sync("10")
        self.assertIsNone(self.state[1]["supplier_history"]["change"])
        self.assertEqual(self.state[1]["ae_sku_id"], "sku1")



class ShopifyPayloadTests(unittest.TestCase):
    def test_mutation_saves_price_adjustment_and_history_together(self):
        shop = SimpleNamespace(shop_domain="second.myshopify.com", access_token="test")
        history = observe_supplier_price(None, "10")
        def graphql(actual_shop, query, variables):
            self.assertIs(actual_shop, shop)
            row = variables["variants"][0]
            self.assertEqual(variables["productId"], "gid://shopify/Product/100")
            self.assertEqual(row["price"], "13.00")
            fields = {f["key"]: f for f in row["metafields"]}
            self.assertEqual(fields["price_increase"]["value"], "3.00")
            self.assertEqual(fields["supplier_price_history"]["type"], "json")
            return {"productVariantsBulkUpdate": {"productVariants": [{"id": row["id"],
                "price": "13.00", "priceIncrease": {"value": "3.00"}}], "userErrors": []}}
        with patch.object(shopify, "_graphql", side_effect=graphql):
            result = shopify.bulk_update_variant_prices(shop, "100", [{"variant_id": 1,
                "price": "13.00", "price_increase": "3.00", "supplier_history": history}])
        self.assertTrue(result["success"])

    def test_state_reader_paginates_using_selected_shop(self):
        shop = SimpleNamespace(shop_domain="second.myshopify.com", access_token="test")
        def graphql(actual_shop, query, variables):
            self.assertIs(actual_shop, shop)
            vid = 1 if variables["after"] is None else 2
            node = {"id": f"gid://shopify/ProductVariant/{vid}", "price": "12.00",
                    "selectedOptions": [{"value": "Red"}], "increase": {"value": "2.00"},
                    "priceLock": {"value": "true"}}
            return {"product": {"variants": {"edges": [{"node": node}],
                "pageInfo": {"hasNextPage": vid == 1, "endCursor": "next"}}}}
        with patch.object(shopify, "_graphql", side_effect=graphql) as request:
            state = shopify.get_variant_sync_state(shop, "100")
        self.assertEqual(set(state), {1, 2})
        self.assertEqual(request.call_count, 2)
        self.assertTrue(state[1]["locks"]["price"])
        self.assertEqual(state[2]["price_increase"], Decimal("2"))

if __name__ == "__main__":
    unittest.main()
