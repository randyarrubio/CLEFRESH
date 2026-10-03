import hashlib
import mimetypes
import os
import re
import secrets
import time

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password as django_validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email as django_validate_email
from django.db import IntegrityError
from django.http import Http404, FileResponse, JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_encode, urlsafe_base64_decode
from allauth.socialaccount.adapter import get_adapter as get_socialaccount_adapter
from allauth.socialaccount.models import SocialLogin
from .forms import RegisterForm, LoginForm, ProfileForm, validate_password_strength
from .models import User, RiderProfile
from .sso_verification import (
    PENDING_SSO_CODE_TTL,
    PENDING_SSO_EMAIL_SESSION_KEY,
    PENDING_SSO_MAX_ATTEMPTS,
    PENDING_SSO_SESSION_KEY,
    PENDING_SSO_TOKEN_SESSION_KEY,
    clear_pending_sso_verification,
    pending_sso_attempts_key,
    pending_sso_code_key,
    pending_sso_resend_cooldown_remaining,
    send_pending_sso_verification_code,
)
from utils.email_notifications import send_email_notification
from utils.validators import validate_document_upload, validate_image_upload

_SAFE_REGISTER_FIELDS = {'email', 'role', 'first_name', 'last_name', 'contact_number'}

_CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_LOGIN_MAX_ATTEMPTS = 10          # per IP, and per account from one IP
_LOGIN_ACCOUNT_MAX_ATTEMPTS = 50  # per account from all IPs (distributed guessing)
_LOGIN_COOLDOWN = 900  # 15 minutes


def client_ip(request):
    """The visitor's IP. Behind a reverse proxy every request comes from the proxy, so the
    proxy must set a header (e.g. nginx `proxy_set_header X-Real-IP $remote_addr;`) and
    CLIENT_IP_HEADER must name it. Never trust a header the client itself can set."""
    header = getattr(settings, 'CLIENT_IP_HEADER', '')
    if header:
        value = request.META.get(header, '').split(',')[0].strip()
        if value:
            return value
    return request.META.get('REMOTE_ADDR', '')

_VERIFY_CODE_TTL = 600  # 10 minutes
_VERIFY_MAX_ATTEMPTS = 5
_VERIFY_RESEND_COOLDOWN = 60  # 1 minute


def _sanitize_str(value, max_length):
    """Strip null bytes and ASCII control characters; enforce max length."""
    if not value:
        return ''
    return _CONTROL_CHARS_RE.sub('', value)[:max_length]


def _validate_login_inputs(email, password):
    """
    Return a list of error strings, empty if all inputs are valid.
    Validates email format, password length, and character safety.
    """
    errors = []
    if not email:
        errors.append('Email address is required.')
    else:
        try:
            django_validate_email(email)
        except ValidationError:
            errors.append('Enter a valid email address.')
    if not password:
        errors.append('Password is required.')
    elif len(password) < 8:
        errors.append('Password must be at least 8 characters.')
    elif len(password) > 128:
        errors.append('Password is too long.')
    return errors


def _issue_email_verification_code(user):
    """Generate a 6-digit code, cache it, and email it. Returns the code."""
    code = f'{secrets.randbelow(1_000_000):06d}'
    cache.set(f'email_verify_code_{user.id}', code, _VERIFY_CODE_TTL)
    cache.delete(f'email_verify_attempts_{user.id}')
    cache.set(
        f'email_verify_resend_{user.id}',
        time.time() + _VERIFY_RESEND_COOLDOWN,
        _VERIFY_RESEND_COOLDOWN,
    )
    send_email_notification('email_verification', user, extra={'code': code}, force=True)
    return code


def _debug_verification_code(cache_key):
    if not settings.DEBUG:
        return ''
    return cache.get(cache_key, '')


def _parse_optional(raw, field, label):
    """Clean an optional POST value with a Django form field; '' -> None."""
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return field.clean(raw)
    except ValidationError:
        raise ValidationError(f'Enter a valid {label}.')


def _resend_cooldown_remaining(user):
    next_allowed = cache.get(f'email_verify_resend_{user.id}', 0)
    return max(int(next_allowed - time.time()), 0)


def register_view(request):
    if request.user.is_authenticated:
        return redirect('/')
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            try:
                user = form.save()
            except IntegrityError:
                form.add_error('email', 'An account with this email already exists.')
                return render(request, 'accounts/register.html', {'form': form})
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            send_email_notification('welcome', user)
            if user.role == 'shop_owner':
                _issue_email_verification_code(user)
                messages.success(request, 'Account created. Check your email for a 6-digit verification code.')
                return redirect('verify_email')
            user.email_verified = True
            user.save(update_fields=['email_verified'])
            messages.success(request, 'Account created successfully!')
            if user.role == 'rider':
                return redirect('rider_onboarding')
            return redirect('/')
    else:
        form = RegisterForm()
    return render(request, 'accounts/register.html', {'form': form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('/')

    def _redirect_or_render(template):
        if request.GET.get('next') == '/':
            return redirect('/#login')
        return render(request, template)

    if request.method == 'POST':
        ip = client_ip(request)
        ip_key = f'login_attempts_{ip}'
        ip_attempts = cache.get(ip_key, 0)

        # Per-IP block
        if ip_attempts >= _LOGIN_MAX_ATTEMPTS:
            messages.error(request, 'Too many login attempts. Please wait 15 minutes.')
            return _redirect_or_render('accounts/login.html')

        # Sanitize raw input
        email = _sanitize_str(request.POST.get('username', '').strip().lower(), 254)
        password = _sanitize_str(request.POST.get('password', ''), 128)

        # Validate format / length
        errors = _validate_login_inputs(email, password)
        if errors:
            for msg in errors:
                messages.error(request, msg)
            return _redirect_or_render('accounts/login.html')

        # Per-account blocks (keyed on a hash to avoid storing plaintext emails in cache).
        # The tight limit is per account *and* IP, so a stranger failing someone's password
        # can't lock the real owner out; the loose per-account limit stops spread-out guessing.
        email_hash = hashlib.sha256(email.encode()).hexdigest()[:20]
        email_key = f'login_attempts_email_{email_hash}_{ip}'
        account_key = f'login_attempts_account_{email_hash}'
        email_attempts = cache.get(email_key, 0)
        account_attempts = cache.get(account_key, 0)
        if email_attempts >= _LOGIN_MAX_ATTEMPTS or account_attempts >= _LOGIN_ACCOUNT_MAX_ATTEMPTS:
            messages.error(request, 'Too many failed attempts for this account. Please wait 15 minutes.')
            return _redirect_or_render('accounts/login.html')

        user = authenticate(request, username=email, password=password)
        if user:
            cache.delete(ip_key)
            cache.delete(email_key)
            cache.delete(account_key)
            login(request, user)
            if not request.POST.get('remember_me'):
                request.session.set_expiry(0)
            next_url = request.GET.get('next', '/')
            if not url_has_allowed_host_and_scheme(
                url=next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                next_url = '/'
            return redirect(next_url)

        # Failed attempt — increment the counters
        cache.set(ip_key, ip_attempts + 1, _LOGIN_COOLDOWN)
        cache.set(email_key, email_attempts + 1, _LOGIN_COOLDOWN)
        cache.set(account_key, account_attempts + 1, _LOGIN_COOLDOWN)
        messages.error(request, 'Invalid email or password.')
        return _redirect_or_render('accounts/login.html')

    return render(request, 'accounts/login.html')


def logout_view(request):
    if request.method != 'POST':
        return redirect('/')
    logout(request)
    return redirect('/')


@login_required
def verify_email_view(request):
    user = request.user
    if user.email_verified:
        return redirect('/')
    if user.role != 'shop_owner':
        # Only shop owners go through email verification; everyone else is auto-verified.
        user.email_verified = True
        user.save(update_fields=['email_verified'])
        return redirect('/')

    code_key = f'email_verify_code_{user.id}'
    attempts_key = f'email_verify_attempts_{user.id}'

    context = {
        'masked_email': _mask_email(user.email),
        'cooldown_left': _resend_cooldown_remaining(user),
        'debug_code': _debug_verification_code(code_key),
    }

    if request.method == 'POST':
        submitted = _sanitize_str(request.POST.get('code', '').strip(), 6)
        attempts = cache.get(attempts_key, 0)

        if attempts >= _VERIFY_MAX_ATTEMPTS:
            messages.error(request, 'Too many incorrect attempts. Please request a new code.')
            return render(request, 'accounts/verify_email.html', context)

        stored = cache.get(code_key)
        if not stored:
            messages.error(request, 'Your verification code expired. Please request a new one.')
            return render(request, 'accounts/verify_email.html', context)

        if not submitted or len(submitted) != 6 or not submitted.isdigit():
            cache.set(attempts_key, attempts + 1, _VERIFY_CODE_TTL)
            messages.error(request, 'Enter the 6-digit code from your email.')
            return render(request, 'accounts/verify_email.html', context)

        if not secrets.compare_digest(submitted, stored):
            cache.set(attempts_key, attempts + 1, _VERIFY_CODE_TTL)
            remaining = _VERIFY_MAX_ATTEMPTS - (attempts + 1)
            msg = 'Incorrect code.'
            if remaining > 0:
                msg += f' {remaining} attempt(s) remaining.'
            else:
                msg += ' Please request a new code.'
            messages.error(request, msg)
            return render(request, 'accounts/verify_email.html', context)

        user.email_verified = True
        user.save(update_fields=['email_verified'])
        cache.delete(code_key)
        cache.delete(attempts_key)
        cache.delete(f'email_verify_resend_{user.id}')
        messages.success(request, 'Email verified! You can now set up your shop.')
        return redirect('register_shop')

    return render(request, 'accounts/verify_email.html', context)


@login_required
def resend_verify_code_view(request):
    if request.method != 'POST':
        return redirect('verify_email')
    user = request.user
    if user.email_verified:
        return redirect('/')
    if user.role != 'shop_owner':
        return redirect('/')

    if _resend_cooldown_remaining(user) > 0:
        messages.error(request, 'Please wait a moment before requesting another code.')
        return redirect('verify_email')

    _issue_email_verification_code(user)
    messages.success(request, 'A new verification code has been sent to your email.')
    return redirect('verify_email')


def sso_verify_email_view(request):
    pending = request.session.get(PENDING_SSO_SESSION_KEY)
    token = request.session.get(PENDING_SSO_TOKEN_SESSION_KEY)
    email = request.session.get(PENDING_SSO_EMAIL_SESSION_KEY)
    if not pending or not token or not email:
        messages.error(request, 'Your Google registration session expired. Please try again.')
        return redirect('register')

    context = {
        'masked_email': _mask_email(email),
        'cooldown_left': pending_sso_resend_cooldown_remaining(request),
        'resend_url': reverse('sso_verify_email_resend'),
        'cancel_url': reverse('register'),
        'cancel_text': 'Choose a different Google account',
        'debug_code': _debug_verification_code(pending_sso_code_key(token)),
    }

    if request.method == 'POST':
        submitted = _sanitize_str(request.POST.get('code', '').strip(), 6)
        attempts_key = pending_sso_attempts_key(token)
        attempts = cache.get(attempts_key, 0)

        if attempts >= PENDING_SSO_MAX_ATTEMPTS:
            messages.error(request, 'Too many incorrect attempts. Please request a new code.')
            return render(request, 'accounts/verify_email.html', context)

        stored = cache.get(pending_sso_code_key(token))
        if not stored:
            messages.error(request, 'Your verification code expired. Please request a new one.')
            return render(request, 'accounts/verify_email.html', context)

        if not submitted or len(submitted) != 6 or not submitted.isdigit():
            cache.set(attempts_key, attempts + 1, PENDING_SSO_CODE_TTL)
            messages.error(request, 'Enter the 6-digit code from your email.')
            return render(request, 'accounts/verify_email.html', context)

        if not secrets.compare_digest(submitted, stored):
            cache.set(attempts_key, attempts + 1, PENDING_SSO_CODE_TTL)
            remaining = PENDING_SSO_MAX_ATTEMPTS - (attempts + 1)
            msg = 'Incorrect code.'
            if remaining > 0:
                msg += f' {remaining} attempt(s) remaining.'
            else:
                msg += ' Please request a new code.'
            messages.error(request, msg)
            return render(request, 'accounts/verify_email.html', context)

        if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
            clear_pending_sso_verification(request)
            messages.error(request, 'An account with this email already exists. Please sign in.')
            return redirect('login')

        sociallogin = SocialLogin.deserialize(pending)
        sociallogin.user.email = email
        sociallogin.user.username = email
        sociallogin.user.email_verified = True
        sociallogin.user.sso_provider = sociallogin.account.provider
        user = get_socialaccount_adapter(request).save_user(request, sociallogin, form=None)
        clear_pending_sso_verification(request)
        login(request, user, backend='allauth.account.auth_backends.AuthenticationBackend')
        messages.success(request, 'Email verified. Complete your profile to finish registration.')
        return redirect('sso_complete')

    return render(request, 'accounts/verify_email.html', context)


def sso_resend_verify_code_view(request):
    if request.method != 'POST':
        return redirect('sso_verify_email')
    pending = request.session.get(PENDING_SSO_SESSION_KEY)
    email = request.session.get(PENDING_SSO_EMAIL_SESSION_KEY)
    if not pending or not email:
        messages.error(request, 'Your Google registration session expired. Please try again.')
        return redirect('register')
    if pending_sso_resend_cooldown_remaining(request) > 0:
        messages.error(request, 'Please wait a moment before requesting another code.')
        return redirect('sso_verify_email')

    sociallogin = SocialLogin.deserialize(pending)
    send_pending_sso_verification_code(request, email, sociallogin.user.get_full_name())
    messages.success(request, 'A new verification code has been sent to your email.')
    return redirect('sso_verify_email')


def _mask_email(email):
    if not email or '@' not in email:
        return email or ''
    local, _, domain = email.partition('@')
    if len(local) <= 2:
        masked = local[0] + '*'
    else:
        masked = local[0] + '*' * (len(local) - 2) + local[-1]
    return f'{masked}@{domain}'


def check_email_view(request):
    # Tight limit: a person registering checks one or two addresses; this stops anyone
    # using the endpoint to test lists of emails. The register form still validates on submit.
    bucket = f'check_email_{client_ip(request)}'
    used = cache.get(bucket, 0)
    if used >= 10:
        return JsonResponse({'exists': False, 'rate_limited': True}, status=429)
    cache.set(bucket, used + 1, 600)

    email = _sanitize_str(request.GET.get('email', '').strip().lower(), 254)
    if not email:
        return JsonResponse({'exists': False})
    try:
        django_validate_email(email)
    except ValidationError:
        return JsonResponse({'exists': False, 'invalid': True})
    exists = (
        User.objects.filter(email__iexact=email).exists()
        or User.objects.filter(username__iexact=email).exists()
    )
    return JsonResponse({'exists': exists})


def forgot_password_view(request):
    if request.user.is_authenticated:
        return redirect('/')
    if request.method == 'POST':
        email = _sanitize_str(request.POST.get('email', '').strip().lower(), 254)
        # 5 requests / 15 min per IP; response is identical either way (no enumeration).
        rate_key = f"forgot_pw_{client_ip(request)}"
        sent = cache.get(rate_key, 0)
        if sent < 5:
            cache.set(rate_key, sent + 1, _LOGIN_COOLDOWN)
            user = User.objects.filter(email__iexact=email, is_active=True).order_by('id').first() if email else None
            if user:
                uid = urlsafe_base64_encode(force_bytes(user.pk))
                token = default_token_generator.make_token(user)
                reset_url = settings.SITE_URL.rstrip('/') + reverse(
                    'password_reset_confirm', kwargs={'uidb64': uid, 'token': token})
                send_email_notification('password_reset', user, extra={'reset_url': reset_url}, force=True)
        return redirect('password_reset_done')
    return render(request, 'accounts/forgot_password.html')


def password_reset_done_view(request):
    return render(request, 'accounts/password_reset_done.html')


def reset_password_confirm_view(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    valid = user is not None and default_token_generator.check_token(user, token)
    if valid and request.method == 'POST':
        password = request.POST.get('password', '')
        confirm = request.POST.get('confirm_password', '')
        try:
            validate_password_strength(password)
            django_validate_password(password, user)
            strength_error = None
        except ValidationError as exc:
            strength_error = '; '.join(exc.messages)
        if strength_error:
            messages.error(request, strength_error)
        elif password != confirm:
            messages.error(request, 'Passwords do not match.')
        else:
            user.set_password(password)
            user.save()
            messages.success(request, 'Password reset successfully. Please sign in.')
            return redirect('login')
    return render(request, 'accounts/reset_password_confirm.html', {'valid': valid})


@login_required
def sso_complete_view(request):
    if request.user.role:
        return redirect('/')
    if request.method == 'POST':
        role = request.POST.get('role')
        if role not in ['customer', 'shop_owner', 'rider']:
            messages.error(request, 'Please select a valid role.')
            return redirect('/accounts/social/signup/')
        request.user.role = role
        social_account = request.user.socialaccount_set.first()
        if not social_account:
            # Only Google-authenticated users may complete sign-up here.
            messages.error(request, 'Please register with your email and password instead.')
            return redirect('register')
        if social_account:
            request.user.sso_provider = social_account.provider
        # SSO providers have already verified the email — skip the OTP flow.
        request.user.email_verified = True
        request.user.save()
        send_email_notification('welcome', request.user)
        messages.success(request, 'Profile complete! Welcome to CLEFRESH.')
        if request.user.role == 'shop_owner':
            return redirect('register_shop')
        if request.user.role == 'rider':
            return redirect('rider_onboarding')
        return redirect('/')
    return render(request, 'accounts/sso_complete.html')


_RIDER_DOC_LABELS = {
    'government_id_front': 'Government ID (front)',
    'government_id_back': 'Government ID (back)',
    'drivers_license_front': "Driver's license (front)",
    'drivers_license_back': "Driver's license (back)",
    'vehicle_cr': 'Certificate of Registration (CR)',
    'vehicle_or': 'Official Receipt (OR)',
    'selfie_with_id': 'Selfie with ID',
}


def _missing_rider_documents(profile, vehicle_type, files):
    """Documents required for review that are neither on file nor in this upload."""
    required = ['government_id_front', 'government_id_back', 'selfie_with_id']
    if vehicle_type in ('motorcycle', 'car'):
        required += ['drivers_license_front', 'drivers_license_back', 'vehicle_cr', 'vehicle_or']
    return [_RIDER_DOC_LABELS[f] for f in required if not getattr(profile, f) and f not in files]


def _split_rider_address(address):
    """Undo the 'home, city, province' join done on save so re-saving doesn't duplicate parts."""
    parts = [p.strip() for p in (address or '').split(',') if p.strip()]
    if len(parts) >= 3:
        return ', '.join(parts[:-2]), parts[-2], parts[-1]
    return ', '.join(parts), '', ''


def _rider_onboarding_context(user, profile):
    home, city, province = _split_rider_address(user.address)
    submitted = profile.is_submitted
    return {
        'profile': profile,
        'addr_home': home,
        'addr_city': city,
        'addr_province': province,
        # Defaults for first-time applicants; saved values once they've submitted.
        'selected_vehicle': profile.vehicle_type or 'motorcycle',
        'selected_days': set(profile.available_days.split(', ')) if submitted else {'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'},
        'selected_zones': set(profile.coverage_areas.split(', ')) if submitted else {'Cantilan', 'Madrid'},
        'pref_cod': profile.accepts_cod if submitted else True,
        'pref_multi': profile.allows_multiple_dropoffs if submitted else True,
        'pref_alerts': profile.nearby_job_alerts if submitted else True,
        'docs_on_file': {f for f in RiderProfile.DOCUMENT_FIELDS if getattr(profile, f)},
        'max_vehicle_year': timezone.localdate().year + 1,
        'day_choices': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        'zone_choices': ['Cantilan', 'Madrid', 'Carmen', 'Lanuza', 'Carrascal', 'Tandag'],
        'gender_choices': ['Male', 'Female', 'Prefer not to say'],
        'relationship_choices': ['Parent', 'Spouse / Partner', 'Sibling', 'Friend', 'Other'],
    }


@login_required
def rider_onboarding_view(request):
    if request.user.role != 'rider':
        return redirect('/')

    profile, _ = RiderProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        try:
            if 'profile_picture' in request.FILES:
                validate_image_upload(request.FILES['profile_picture'])
            for image_field in ['selfie_with_id']:
                if image_field in request.FILES:
                    validate_image_upload(request.FILES[image_field])
            for document_field in [
                'government_id_front', 'government_id_back', 'drivers_license_front',
                'drivers_license_back', 'vehicle_cr', 'vehicle_or'
            ]:
                if document_field in request.FILES:
                    validate_document_upload(request.FILES[document_field])
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
            return render(request, 'accounts/rider_onboarding.html', _rider_onboarding_context(request.user, profile))

        missing_docs = _missing_rider_documents(profile, request.POST.get('vehicle_type', ''), request.FILES)
        if missing_docs:
            messages.error(request, 'Please upload: ' + ', '.join(missing_docs) + '.')
            return render(request, 'accounts/rider_onboarding.html', _rider_onboarding_context(request.user, profile))

        # Parse typed fields up front; raw strings previously crashed save() with a 500.
        try:
            date_of_birth = _parse_optional(request.POST.get('date_of_birth'), forms.DateField(), 'date of birth')
            vehicle_year = _parse_optional(
                request.POST.get('vehicle_year'),
                forms.IntegerField(min_value=1950, max_value=timezone.localdate().year + 1),
                'vehicle year',
            )
            shift_start = _parse_optional(request.POST.get('shift_start'), forms.TimeField(), 'shift start')
            shift_end = _parse_optional(request.POST.get('shift_end'), forms.TimeField(), 'shift end')
            max_radius_km = _parse_optional(
                request.POST.get('max_radius_km'), forms.IntegerField(min_value=1, max_value=100), 'max radius'
            ) or 5
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
            return render(request, 'accounts/rider_onboarding.html', _rider_onboarding_context(request.user, profile))

        request.user.first_name = request.POST.get('first_name', request.user.first_name)
        request.user.last_name = request.POST.get('last_name', request.user.last_name)
        request.user.contact_number = request.POST.get('contact_number', request.user.contact_number)
        request.user.address = ', '.join(filter(None, [
            request.POST.get('home_address', ''),
            request.POST.get('city', ''),
            request.POST.get('province', ''),
        ]))
        if 'profile_picture' in request.FILES:
            request.user.profile_picture = request.FILES['profile_picture']
        request.user.save()

        profile.date_of_birth = date_of_birth
        profile.gender = request.POST.get('gender', '')[:30]
        profile.emergency_contact_name = request.POST.get('emergency_contact_name', '')[:120]
        profile.emergency_contact_relationship = request.POST.get('emergency_contact_relationship', '')[:60]
        profile.emergency_contact_number = request.POST.get('emergency_contact_number', '')[:30]
        vehicle_type = request.POST.get('vehicle_type', '')
        profile.vehicle_type = vehicle_type if vehicle_type in dict(RiderProfile.VEHICLE_CHOICES) else ''
        profile.vehicle_make_model = request.POST.get('vehicle_make_model', '')[:120]
        profile.vehicle_year = vehicle_year
        profile.vehicle_color = request.POST.get('vehicle_color', '')[:60]
        profile.plate_number = request.POST.get('plate_number', '').upper()[:40]
        profile.available_days = ', '.join(request.POST.getlist('available_days'))[:120]
        profile.shift_start = shift_start
        profile.shift_end = shift_end
        profile.coverage_areas = ', '.join(request.POST.getlist('coverage_areas'))
        profile.max_radius_km = max_radius_km
        profile.accepts_cod = request.POST.get('accepts_cod') == 'on'
        profile.allows_multiple_dropoffs = request.POST.get('allows_multiple_dropoffs') == 'on'
        profile.nearby_job_alerts = request.POST.get('nearby_job_alerts') == 'on'
        new_documents = False
        for field in RiderProfile.DOCUMENT_FIELDS:
            if field in request.FILES:
                setattr(profile, field, request.FILES[field])
                new_documents = True
        profile.is_submitted = True
        profile.submitted_at = timezone.now()
        # Rejected riders resubmit for review; approved riders who swap documents
        # must be re-verified. Approved riders editing only availability stay approved.
        if profile.approval_status == 'rejected' or (profile.is_approved and new_documents):
            profile.approval_status = 'pending'
            profile.reviewed_at = None
        profile.save()
        if profile.is_approved:
            messages.success(request, 'Rider profile updated.')
        else:
            messages.success(request, 'Rider application submitted. You can accept jobs once an admin approves it.')
        return redirect('rider_dashboard')

    return render(request, 'accounts/rider_onboarding.html', _rider_onboarding_context(request.user, profile))


@login_required
def profile_view(request):
    if request.method == 'POST':
        form = ProfileForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated.')
            return redirect('profile')
    else:
        form = ProfileForm(instance=request.user)
    return render(request, 'accounts/profile.html', {'form': form})


_RIDER_DOC_FIELDS = RiderProfile.DOCUMENT_FIELDS


@login_required
def serve_rider_doc_view(request, filename):
    if os.sep in filename or '/' in filename or '..' in filename:
        raise Http404

    file_path = os.path.join(settings.MEDIA_ROOT, 'rider_docs', filename)
    if not os.path.isfile(file_path):
        raise Http404

    if not (request.user.is_superuser or request.user.role == 'admin'):
        profile = getattr(request.user, 'rider_profile', None)
        if not profile:
            raise Http404
        owned = any(
            getattr(profile, f) and os.path.basename(getattr(profile, f).name) == filename
            for f in _RIDER_DOC_FIELDS
        )
        if not owned:
            raise Http404

    content_type, _ = mimetypes.guess_type(file_path)
    return FileResponse(open(file_path, 'rb'), content_type=content_type or 'application/octet-stream')
