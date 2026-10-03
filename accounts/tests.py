from django.core.cache import cache
from django.contrib.sites.models import Site
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin
from allauth.socialaccount.providers.google.provider import GoogleProvider

from .adapters import SocialAccountAdapter
from .models import User
from .sso_verification import (
    PENDING_SSO_EMAIL_SESSION_KEY,
    PENDING_SSO_SESSION_KEY,
    PENDING_SSO_TOKEN_SESSION_KEY,
    pending_sso_code_key,
)


class GoogleSignupVerificationTests(TestCase):
    def _stash_pending_google_signup(self, email='new@example.com', code='123456'):
        app = SocialApp.objects.filter(provider='google').first()
        if app is None:
            app = SocialApp.objects.create(provider='google', name='Google')
        app.client_id = 'client-id'
        app.secret = 'secret'
        app.save()
        app.sites.add(Site.objects.get_current())
        provider = GoogleProvider(request=None, app=app)
        sociallogin = SocialLogin(
            user=User(
                username=email,
                email=email,
                first_name='New',
                last_name='User',
            ),
            account=SocialAccount(
                provider='google',
                uid='google-uid-123',
                extra_data={'email': email},
            ),
            provider=provider,
        )
        token = 'pending-token'
        session = self.client.session
        session[PENDING_SSO_SESSION_KEY] = sociallogin.serialize()
        session[PENDING_SSO_TOKEN_SESSION_KEY] = token
        session[PENDING_SSO_EMAIL_SESSION_KEY] = email
        session.save()
        cache.set(pending_sso_code_key(token), code, 600)
        return email

    @override_settings(DEBUG=True)
    def test_pending_google_signup_does_not_create_user_before_code(self):
        email = self._stash_pending_google_signup()

        response = self.client.get(reverse('sso_verify_email'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Development code: <strong>123456</strong>', html=True)
        self.assertFalse(User.objects.filter(email=email).exists())
        self.assertFalse(SocialAccount.objects.filter(uid='google-uid-123').exists())

    def test_correct_code_creates_google_user_and_social_account(self):
        email = self._stash_pending_google_signup()

        response = self.client.post(reverse('sso_verify_email'), {'code': '123456'})

        self.assertRedirects(response, reverse('sso_complete'), fetch_redirect_response=False)
        user = User.objects.get(email=email)
        self.assertEqual(user.username, email)
        self.assertEqual(user.sso_provider, 'google')
        self.assertTrue(user.email_verified)
        self.assertEqual(user.role, '')
        self.assertTrue(SocialAccount.objects.filter(user=user, provider='google', uid='google-uid-123').exists())

    def test_signup_process_with_existing_google_account_does_not_login(self):
        user = User.objects.create_user(
            username='owner@example.com',
            email='owner@example.com',
            password='password',
            role='shop_owner',
        )
        account = SocialAccount.objects.create(
            user=user,
            provider='google',
            uid='existing-google-uid',
            extra_data={'email': user.email},
        )
        sociallogin = SocialLogin(user=user, account=account)
        sociallogin.state = {'process': 'signup'}
        request = RequestFactory().get('/accounts/google/login/callback/')
        request.session = self.client.session
        request._messages = FallbackStorage(request)

        with self.assertRaises(ImmediateHttpResponse) as raised:
            SocialAccountAdapter().pre_social_login(request, sociallogin)

        self.assertEqual(raised.exception.response.status_code, 302)
        self.assertEqual(raised.exception.response.url, reverse('login'))
