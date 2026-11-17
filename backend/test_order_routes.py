import unittest
from fastapi import FastAPI, APIRouter, Depends, HTTPException
from fastapi.testclient import TestClient
from alisync_app.order_routes import include_order_routes
class OrderRouteTests(unittest.TestCase):
 def test_alias_preserves_parameters_method_and_auth(self):
  def authorize(): raise HTTPException(401,'Authentication required')
  router=APIRouter(prefix='/app/orders',dependencies=[Depends(authorize)])
  @router.get('/{order_id}')
  def detail(order_id:int):return {'id':order_id}
  @router.post('/sync')
  def sync():return {'synced':0}
  app=FastAPI();include_order_routes(app,router);client=TestClient(app)
  for prefix in ['/app/orders','/api/orders']:
   self.assertEqual(client.get(prefix+'/42').status_code,401)
   self.assertEqual(client.post(prefix+'/sync').status_code,401)
  app.dependency_overrides[authorize]=lambda:None
  for prefix in ['/app/orders','/api/orders']:
   self.assertEqual(client.get(prefix+'/42').json(),{'id':42})
   self.assertEqual(client.post(prefix+'/sync').json(),{'synced':0})
