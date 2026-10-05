from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from .models import User
from .sso_verification import start_pending_sso_verification


class AccountAdapter(DefaultAccountAdapter):
    """After SSO login, send users without a role to the role-selection page."""

    def get_login_redirect_url(self, request):
        user = getattr(request, 'user', None)
        if user and user.is_authenticated and not user.role:
            return reverse('sso_complete')
        return super().get_login_redirect_url(request)

    def is_open_for_signup(self, request):
        # Email/password sign-up goes through /auth/register/ (password rules, shop-owner
        # email code). allauth's own /accounts/signup/ must not bypass that.
        return False


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Require CLEFRESH email-code verification before creating Google users."""

    def is_open_for_signup(self, request, sociallogin):
        return True     # Google sign-up stays open (the account adapter closes local sign-up)

    def pre_social_login(self, request, sociallogin):
        is_signup = sociallogin.state.get('process') == 'signup'
        if sociallogin.is_existing:
            if is_signup:
                messages.error(request, 'That Google account is already registered. Please sign in instead.')
                raise ImmediateHttpResponse(redirect('login'))
            return
        if sociallogin.account.provider != 'google':
            return

        email = (getattr(sociallogin.user, 'email', '') or '').strip().lower()
        if not email:
            messages.error(request, 'Google did not provide an email address. Please register with email and password.')
            raise ImmediateHttpResponse(redirect('register'))

        if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
            messages.error(request, 'An account with this email already exists. Please sign in first, then connect Google from your account.')
            raise ImmediateHttpResponse(redirect('login'))

        request.session['pending_google_sso'] = sociallogin.serialize()
        start_pending_sso_verification(request, email, sociallogin.user.get_full_name())
        raise ImmediateHttpResponse(redirect('sso_verify_email'))
    
    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        import logging
        logging.getLogger(__name__).warning(
            'Google login failed: provider=%s error=%s exception=%r',
            getattr(provider, 'id', provider), error, exception,
        )
        return super().on_authentication_error(
            request, provider, error=error, exception=exception, extra_context=extra_context,
        )