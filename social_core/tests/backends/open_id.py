# -*- coding: utf-8 -*-
from calendar import timegm

import os
import sys
import json
import datetime

from jwkest.jwk import RSAKey, KEYS
from jwkest.jws import JWS
from jwkest.jwt import b64encode_item

import requests

from openid import oidutil


PY3 = sys.version_info[0] == 3

if PY3:
    from html.parser import HTMLParser
    HTMLParser  # placate pyflakes
else:
    from HTMLParser import HTMLParser

from httpretty import HTTPretty

sys.path.insert(0, '..')

from ...utils import parse_qs, module_member
from ...backends.utils import load_backends
from ...exceptions import AuthTokenError
from .base import BaseBackendTest
from ..models import TestStorage, User, TestUserSocialAuth, \
    TestNonce, TestAssociation
from ..strategy import TestStrategy

# Patch to remove the too-verbose output until a new version is released
oidutil.log = lambda *args, **kwargs: None


class FormHTMLParser(HTMLParser):
    form = {}
    inputs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.form.update(attrs)
        elif tag == 'input' and 'name' in attrs:
            self.inputs[attrs['name']] = attrs['value']


class OpenIdTest(BaseBackendTest):
    backend_path = None
    backend = None
    access_token_body = None
    user_data_body = None
    user_data_url = ''
    expected_username = ''
    settings = None
    partial_login_settings = None
    raw_complete_url = '/complete/{0}/'

    def setUp(self):
        HTTPretty.enable()
        Backend = module_member(self.backend_path)
        self.strategy = TestStrategy(TestStorage)
        self.complete_url = self.raw_complete_url.format(Backend.name)
        self.backend = Backend(self.strategy, redirect_uri=self.complete_url)
        self.strategy.set_settings({
            'SOCIAL_AUTH_AUTHENTICATION_BACKENDS': (
                self.backend_path,
                'social_core.tests.backends.test_broken.BrokenBackendAuth'
            )
        })
        # Force backends loading to trash PSA cache
        load_backends(
            self.strategy.get_setting('SOCIAL_AUTH_AUTHENTICATION_BACKENDS'),
            force_load=True
        )

    def tearDown(self):
        self.strategy = None
        User.reset_cache()
        TestUserSocialAuth.reset_cache()
        TestNonce.reset_cache()
        TestAssociation.reset_cache()
        HTTPretty.disable()

    def get_form_data(self, html):
        parser = FormHTMLParser()
        parser.feed(html)
        return parser.form, parser.inputs

    def openid_url(self):
        return self.backend.openid_url()

    def post_start(self):
        pass

    def do_start(self):
        HTTPretty.register_uri(HTTPretty.GET,
                               self.openid_url(),
                               status=200,
                               body=self.discovery_body,
                               content_type='application/xrds+xml')
        start = self.backend.start()
        self.post_start()
        form, inputs = self.get_form_data(start)
        HTTPretty.register_uri(HTTPretty.POST,
                               form.get('action'),
                               status=200,
                               body=self.server_response)
        response = requests.post(form.get('action'), data=inputs)
        self.strategy.set_request_data(parse_qs(response.content),
                                       self.backend)
        HTTPretty.register_uri(HTTPretty.POST,
                               form.get('action'),
                               status=200,
                               body='is_valid:true\n')
        return self.backend.complete()


class OpenIdConnectTestMixin(object):
    """
    Mixin to test OpenID Connect consumers. Inheriting classes should also
    inherit OAuth2Test.
    """
    client_key = 'a-key'
    client_secret = 'a-secret-key'
    issuer = None  # id_token issuer
    openid_config_body = None
    key = None

    def setUp(self):
        super(OpenIdConnectTestMixin, self).setUp()
        here = os.path.dirname(__file__)
        self.key = RSAKey(kid='testkey').load(os.path.join(here, '../testkey.pem'))
        HTTPretty.register_uri(HTTPretty.GET,
                               self.backend.OIDC_ENDPOINT + '/.well-known/openid-configuration',
                               status=200,
                               body=self.openid_config_body
                               )
        oidc_config = json.loads(self.openid_config_body)

        def jwks(_request, _uri, headers):
            ks = KEYS()
            ks.add(self.key.serialize())
            return 200, headers, ks.dump_jwks()

        HTTPretty.register_uri(HTTPretty.GET,
                               oidc_config.get('jwks_uri'),
                               status=200,
                               body=jwks)

    def extra_settings(self):
        settings = super(OpenIdConnectTestMixin, self).extra_settings()
        settings.update({
            'SOCIAL_AUTH_{0}_KEY'.format(self.name): self.client_key,
            'SOCIAL_AUTH_{0}_SECRET'.format(self.name): self.client_secret,
            'SOCIAL_AUTH_{0}_ID_TOKEN_DECRYPTION_KEY'.format(self.name):
                self.client_secret
        })
        return settings

    def access_token_body(self, request, _url, headers):
        """
        Get the nonce from the request parameters, add it to the id_token, and
        return the complete response.
        """
        nonce = self.backend.data['nonce'].encode('utf-8')
        body = self.prepare_access_token_body(nonce=nonce)
        return 200, headers, body

    def get_id_token(self, client_key=None, expiration_datetime=None,
                     issue_datetime=None, nonce=None, issuer=None):
        """
        Return the id_token to be added to the access token body.
        """

        id_token = {
            'iss': issuer,
            'nonce': nonce,
            'aud': client_key,
            'azp': client_key,
            'exp': expiration_datetime,
            'iat': issue_datetime,
            'sub': '1234'
        }

        return id_token

    def prepare_access_token_body(self, client_key=None, tamper_message=False,
                                  expiration_datetime=None,
                                  issue_datetime=None, nonce=None,
                                  issuer=None):
        """
        Prepares a provider access token response. Arguments:

        client_id       -- (str) OAuth ID for the client that requested
                                 authentication.
        expiration_time -- (datetime) Date and time after which the response
                                      should be considered invalid.
        """

        body = {'access_token': 'foobar', 'token_type': 'bearer'}
        client_key = client_key or self.client_key
        now = datetime.datetime.utcnow()
        expiration_datetime = expiration_datetime or \
                              (now + datetime.timedelta(seconds=30))
        issue_datetime = issue_datetime or now
        nonce = nonce or 'a-nonce'
        issuer = issuer or self.issuer
        id_token = self.get_id_token(
            client_key, timegm(expiration_datetime.utctimetuple()),
            timegm(issue_datetime.utctimetuple()), nonce, issuer)

        body['id_token'] = JWS(id_token, jwk=self.key, alg='RS256').sign_compact()
        if tamper_message:
            header, msg, sig = body['id_token'].split('.')
            id_token['sub'] = '1235'
            msg = b64encode_item(id_token).decode('utf-8')
            body['id_token'] = '.'.join([header, msg, sig])

        return json.dumps(body)

    def authtoken_raised(self, expected_message, **access_token_kwargs):
        self.access_token_body = self.prepare_access_token_body(
            **access_token_kwargs
        )
        with self.assertRaisesRegexp(AuthTokenError, expected_message):
            self.do_login()

    def test_invalid_signature(self):
        self.authtoken_raised(
            'Token error: Signature verification failed',
            tamper_message=True
        )

    def test_expired_signature(self):
        expiration_datetime = datetime.datetime.utcnow() - \
                              datetime.timedelta(seconds=30)
        self.authtoken_raised('Token error: Signature has expired',
                              expiration_datetime=expiration_datetime)

    def test_invalid_issuer(self):
        self.authtoken_raised('Token error: Invalid issuer',
                              issuer='someone-else')

    def test_invalid_audience(self):
        self.authtoken_raised('Token error: Invalid audience',
                              client_key='someone-else')

    def test_invalid_issue_time(self):
        expiration_datetime = datetime.datetime.utcnow() - \
                              datetime.timedelta(hours=1)
        self.authtoken_raised('Token error: Incorrect id_token: iat',
                              issue_datetime=expiration_datetime)

    def test_invalid_nonce(self):
        self.authtoken_raised(
            'Token error: Incorrect id_token: nonce',
            nonce='something-wrong'
        )
