"""Shop attendants: owner-managed accounts, time-in/time-out, and earnings from worked hours."""
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from accounts.decorators import role_required
from .access import staff_shop
from .models import AttendanceRecord, ShopAttendant

User = get_user_model()
PERIODS = (7, 15, 30)       # payroll-style windows: weekly, semi-monthly, monthly


def _owner_shop(request):
    return request.user.shops.filter(status='approved').first()


def _period(request):
    try:
        days = int(request.GET.get('days', 15))
    except (TypeError, ValueError):
        days = 15
    days = days if days in PERIODS else 15
    start = timezone.make_aware(datetime.combine(timezone.localdate() - timedelta(days=days - 1), time.min))
    return days, start


def _totals(records):
    hours = sum((r.hours for r in records), Decimal('0'))
    earnings = sum((r.earnings for r in records), Decimal('0'))
    return {'hours': hours.quantize(Decimal('0.01')), 'earnings': earnings.quantize(Decimal('0.01')),
            'shifts': len(records), 'late': sum(1 for r in records if r.is_late)}


def _parse_rate(raw):
    try:
        rate = Decimal((raw or '0').strip())
        if not rate.is_finite() or rate < 0 or rate > 10000:
            raise InvalidOperation
        return rate.quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        raise ValidationError('Hourly rate must be a number from 0 to 10,000.')


def _parse_time(raw):
    if not raw:
        return None
    try:
        h, m = raw.split(':')[:2]
        return time(int(h), int(m))
    except (ValueError, TypeError):
        raise ValidationError('Enter shift times as HH:MM.')


# ── Owner: manage attendants ─────────────────────────────────────

@role_required('shop_owner')
def staff_view(request):
    shop = _owner_shop(request)
    if not shop:
        return redirect('shop_dashboard')
    days, start = _period(request)
    attendants = list(shop.attendants.select_related('user'))
    records = list(
        AttendanceRecord.objects.filter(attendant__shop=shop, time_in__gte=start)
        .select_related('attendant__user')
    )
    rows = []
    for a in attendants:
        mine = [r for r in records if r.attendant_id == a.id]
        rows.append({'attendant': a, 'totals': _totals(mine), 'on_shift': a.open_record})
    return render(request, 'shop_dashboard/staff.html', {
        'shop': shop, 'rows': rows, 'records': records[:50], 'days': days, 'periods': PERIODS,
        'totals': _totals(records), 'weekdays': ShopAttendant.WEEKDAYS,
    })


@role_required('shop_owner')
def create_attendant_view(request):
    if request.method != 'POST':
        return redirect('shop_staff')
    shop = _owner_shop(request)
    if not shop:
        return redirect('shop_dashboard')
    email = request.POST.get('email', '').strip().lower()
    first, last = request.POST.get('first_name', '').strip()[:150], request.POST.get('last_name', '').strip()[:150]
    password = request.POST.get('password', '')
    try:
        validate_email(email)
        if not first:
            raise ValidationError('First name is required.')
        if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
            raise ValidationError('An account with that email already exists.')
        validate_password(password)
        rate = _parse_rate(request.POST.get('hourly_rate'))
        shift_start, shift_end = _parse_time(request.POST.get('shift_start')), _parse_time(request.POST.get('shift_end'))
        if shift_start and shift_end and shift_end <= shift_start:
            raise ValidationError('Shift end must be after shift start.')
        days = [d for d in request.POST.getlist('work_days') if d in dict(ShopAttendant.WEEKDAYS)]
        with transaction.atomic():
            user = User.objects.create_user(username=email, email=email, password=password, first_name=first,
                                            last_name=last, role='attendant', email_verified=True)
            ShopAttendant.objects.create(shop=shop, user=user, hourly_rate=rate, shift_start=shift_start,
                                         shift_end=shift_end, work_days=days)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return redirect('shop_staff')
    except IntegrityError:
        messages.error(request, 'Could not create the attendant account. Try a different email.')
        return redirect('shop_staff')
    messages.success(request, f'Attendant account created for {email}. Share the password with them privately.')
    return redirect('shop_staff')


@role_required('shop_owner')
def update_attendant_view(request, attendant_id):
    if request.method != 'POST':
        return redirect('shop_staff')
    shop = _owner_shop(request)
    attendant = get_object_or_404(ShopAttendant.objects.select_related('user'), id=attendant_id, shop=shop)
    action = request.POST.get('action')
    if action == 'toggle':
        attendant.is_active = not attendant.is_active
        attendant.user.is_active = attendant.is_active      # an inactive attendant can't log in at all
        with transaction.atomic():
            attendant.save(update_fields=['is_active'])
            attendant.user.save(update_fields=['is_active'])
            if not attendant.is_active:
                AttendanceRecord.objects.filter(attendant=attendant, time_out__isnull=True).update(
                    time_out=timezone.now(), note='Closed when the account was deactivated')
        messages.success(request, f'{attendant.user.email} {"reactivated" if attendant.is_active else "deactivated"}.')
    elif action == 'update':
        try:
            attendant.hourly_rate = _parse_rate(request.POST.get('hourly_rate'))
            attendant.shift_start = _parse_time(request.POST.get('shift_start'))
            attendant.shift_end = _parse_time(request.POST.get('shift_end'))
            if attendant.shift_start and attendant.shift_end and attendant.shift_end <= attendant.shift_start:
                raise ValidationError('Shift end must be after shift start.')
            attendant.work_days = [d for d in request.POST.getlist('work_days') if d in dict(ShopAttendant.WEEKDAYS)]
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
            return redirect('shop_staff')
        attendant.save(update_fields=['hourly_rate', 'shift_start', 'shift_end', 'work_days'])
        messages.success(request, f'Schedule and rate updated for {attendant.user.email}. New shifts use the new rate.')
    else:
        messages.error(request, 'Invalid action.')
    return redirect('shop_staff')


# ── Attendant: counter dashboard + time-in/time-out ──────────────

def attendant_dashboard_view(request):
    """Called from shops.views.dashboard_view for role 'attendant'."""
    shop = staff_shop(request.user)
    profile = getattr(request.user, 'attendant_profile', None)
    if not shop or not profile:
        messages.error(request, 'Your attendant account is inactive. Please contact the shop owner.')
        return redirect('/')
    from orders.models import Order
    days, start = _period(request)
    records = list(profile.attendance.filter(time_in__gte=start))
    return render(request, 'shop_dashboard/attendant_dashboard.html', {
        'shop': shop, 'profile': profile, 'open_record': profile.open_record,
        'records': records, 'totals': _totals(records), 'days': days, 'periods': PERIODS,
        'pending_orders': Order.objects.filter(shop=shop, status='pending').count(),
        'active_orders': Order.objects.filter(shop=shop, status__in=['accepted', 'picked_up', 'wash_fold',
                                                                     'ready']).count(),
        'ready_walk_ins': Order.objects.filter(shop=shop, is_walk_in=True, status='ready').count(),
    })


@role_required('attendant')
def clock_view(request):
    if request.method != 'POST':
        return redirect('shop_dashboard')
    profile = getattr(request.user, 'attendant_profile', None)
    if not profile or not staff_shop(request.user):
        messages.error(request, 'Your attendant account is inactive.')
        return redirect('/')
    action = request.POST.get('action')
    now = timezone.now()
    if action == 'in':
        try:
            with transaction.atomic():
                AttendanceRecord.objects.create(attendant=profile, time_in=now, hourly_rate=profile.hourly_rate)
        except IntegrityError:
            messages.info(request, 'You are already timed in.')
            return redirect('shop_dashboard')
        messages.success(request, f'Timed in at {timezone.localtime(now):%I:%M %p}.')
    elif action == 'out':
        if not AttendanceRecord.objects.filter(attendant=profile, time_out__isnull=True).update(time_out=now):
            messages.info(request, 'You are not timed in.')
            return redirect('shop_dashboard')
        messages.success(request, f'Timed out at {timezone.localtime(now):%I:%M %p}. Good work today!')
    else:
        messages.error(request, 'Invalid action.')
    return redirect('shop_dashboard')
