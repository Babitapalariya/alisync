import unittest
from alisync_app.pricing import sku_pricing, product_pricing
class PricingTests(unittest.TestCase):
 def test_discount(self):
  self.assertEqual(sku_pricing({'price':'100','sale_price':'75','discount':'25%'})['final_price'],'75')
 def test_fallback(self):
  for offer in (None,'','NaN','Infinity','-1','120'):
   self.assertEqual(sku_pricing({'price':'100','sale_price':offer})['final_price'],'100')
 def test_free(self):
  self.assertEqual(sku_pricing({'price':'10','sale_price':'0'})['final_price'],'0')
 def test_pair(self):
  p=product_pricing([{'price':'100','sale_price':'75','currency':'USD'},{'price':'200','sale_price':'70','currency':'USD'}],{'currency_code':'CNY'})
  self.assertEqual(p,{'original_price':'200','sale_price':'70','currency':'USD','discount':'65.00%'})
