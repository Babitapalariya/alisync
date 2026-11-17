import unittest
from alisync_app.aliexpress import _parse_product
class PromotionDisplayTests(unittest.TestCase):
 def test_api_offer_is_not_discounted_twice_or_called_welcome_deal(self):
  raw={"aliexpress_ds_product_get_response":{"result":{
   "ae_item_base_info_dto":{"currency_code":"CNY"},
   "ae_item_sku_info_dtos":{"ae_item_sku_info_d_t_o":[
    {"sku_id":"1","sku_price":"100","offer_sale_price":"70","currency_code":"USD"},
    {"sku_id":"2","sku_price":"120","offer_sale_price":"80","currency_code":"USD"}]}}}}
  p=_parse_product(raw,'US')
  self.assertEqual(p['sale_price'],'70')
  self.assertEqual(p['final_display_price'],'70')
  self.assertIsNone(p['promotional_price'])
  self.assertIsNone(p['promotion_type'])
  self.assertEqual(p['promotion_eligibility'],'unknown')
  self.assertFalse(p['pricing_context']['checkout_price_verified'])
  self.assertEqual([s['final_display_price'] for s in p['skus']],['70','80'])
 def test_regular_price_and_missing_price(self):
  for regular,expected in [('25','25'),(None,None)]:
   p=_parse_product({'aliexpress_ds_product_get_response':{'result':{'ae_item_base_info_dto':{},'ae_item_sku_info_dtos':{'ae_item_sku_info_d_t_o':[{'sku_id':'1','sku_price':regular,'currency_code':'USD'}]}}}})
   self.assertEqual(p['final_display_price'],expected)
   self.assertIsNone(p['promotional_price'])
