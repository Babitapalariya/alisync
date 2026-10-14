"""Google authentication checks with mocked Google responses and SQLite."""
import ast
import hashlib
import hmac
import secrets
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse, urlencode
from unittest.mock import Mock
import requests
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from alisync_app.database import Base, get_db
from alisync_app.models import User


class GoogleAuthTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        Base.metadata.create_all(self.engine);self.db=Session(self.engine)
        self.settings=SimpleNamespace(GOOGLE_CLIENT_ID='client',GOOGLE_CLIENT_SECRET='secret',
            GOOGLE_REDIRECT_URI='http://testserver/app/auth/google/callback',FRONTEND_URL='http://frontend',APP_SECRET='x'*40)
        self.http=Mock();self.http.RequestException=requests.RequestException
        self.http.post.return_value.json.return_value={'access_token':'google-token'}
        self.http.get.return_value.json.return_value={'sub':'google-user','email':'USER@example.test','email_verified':True,'name':'Example'}
        router=APIRouter(prefix='/app')
        ns=dict(router=router,settings=self.settings,time=time,secrets=secrets,hmac=hmac,hashlib=hashlib,
            urlencode=urlencode,RedirectResponse=RedirectResponse,Request=Request,Depends=Depends,Session=Session,
            get_db=get_db,requests=self.http,User=User,hash_password=lambda value:'hashed-random-password',
            create_token=lambda id:f'session-{id}',IntegrityError=IntegrityError)
        source=Path(__file__).parent/'alisync_app'/'routes.py'
        names={'google_status','_google_return','google_login','google_callback'}
        fns=[n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name in names]
        exec(compile(ast.Module(body=fns,type_ignores=[]),str(source),'exec'),ns)
        app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:self.db
        self.client=TestClient(app)

    def tearDown(self):self.client.close();self.db.close();self.engine.dispose()

    def start(self):
        response=self.client.get('/app/auth/google',follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertIn('HttpOnly',response.headers['set-cookie'])
        return parse_qs(urlparse(response.headers['location']).query)['state'][0]

    def callback(self,state,**extra):
        response=self.client.get('/app/auth/google/callback',params={'state':state,'code':'code',**extra},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['cache-control'],'no-store')
        return parse_qs(urlparse(response.headers['location']).fragment)

    def test_signup_creates_verified_account_and_uses_fragment(self):
        result=self.callback(self.start())
        self.assertIn('auth_token',result)
        user=self.db.query(User).one();self.assertEqual(user.email,'user@example.test')
        self.assertTrue(user.email_verified);self.assertIsNone(user.selected_plan)

    def test_existing_account_is_reused(self):
        self.db.add(User(email='user@example.test',password_hash='existing',is_active=True));self.db.commit()
        self.assertIn('auth_token',self.callback(self.start()))
        self.assertEqual(self.db.query(User).count(),1)
        self.assertEqual(self.db.query(User).one().password_hash,'existing')

    def test_inactive_account_cannot_login(self):
        self.db.add(User(email='user@example.test',password_hash='existing',is_active=False));self.db.commit()
        self.assertIn('auth_error',self.callback(self.start()))

    def test_missing_or_forged_state_never_contacts_google(self):
        self.start()
        self.assertIn('auth_error',self.callback('forged'))
        self.http.post.assert_not_called()

    def test_other_browser_cannot_use_state(self):
        state=self.start();self.client.cookies.clear()
        self.assertIn('auth_error',self.callback(state));self.http.post.assert_not_called()

    def test_expired_state_is_rejected(self):
        payload=f'{int(time.time())-601}.nonce'
        signature=hmac.new(self.settings.APP_SECRET.encode(),payload.encode(),hashlib.sha256).hexdigest()
        state=f'{payload}.{signature}'
        self.client.cookies.set('alisync_google_state',state,path='/app/auth/google')
        self.assertIn('auth_error',self.callback(state));self.http.post.assert_not_called()

    def test_cancellation_returns_to_login(self):
        self.assertIn('auth_error',self.callback(self.start(),error='access_denied'))
        self.http.post.assert_not_called()

    def test_unverified_email_cannot_create_account(self):
        self.http.get.return_value.json.return_value['email_verified']=False
        self.assertIn('auth_error',self.callback(self.start()))
        self.assertEqual(self.db.query(User).count(),0)

    def test_google_error_is_handled_without_exposing_response(self):
        self.http.post.side_effect=requests.RequestException('private provider details')
        result=self.callback(self.start())
        self.assertIn('auth_error',result)
        self.assertNotIn('private',str(result))

    def test_missing_credentials_returns_to_login(self):
        self.settings.GOOGLE_CLIENT_ID=''
        response=self.client.get('/app/auth/google',follow_redirects=False)
        self.assertIn('auth_error',parse_qs(urlparse(response.headers['location']).fragment))


if __name__=='__main__':unittest.main()
