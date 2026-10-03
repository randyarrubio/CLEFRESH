import logging
import os
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum, Count, Avg, Max, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.html import format_html
from accounts.decorators import role_required
from accounts.models import RiderProfile, User
from shops.models import Shop
from orders.models import Order, OrderService, PhotoProof
from payments.models import PayMongoTransaction, Refund
from notifications.models import Notification
from utils.pagination import paginate

logger = logging.getLogger(__name__)


def _collect_user_files(user):
    """Return every FileField value owned by this user's data graph.

    Captured before cascade-delete so we can purge the files from disk afterwards.
    Only files genuinely owned by the user are included (not rider-attached
    photos on someone else's order — those survive the cascade).
    """
    files = []
    if user.profile_picture:
        files.append(user.profile_picture)
    rider = getattr(user, 'rider_profile', None)
    if rider is not None:
        for fname in ('government_id_front', 'government_id_back',
                      'drivers_license_front', 'drivers_license_back',
                      'vehicle_cr', 'vehicle_or', 'selfie_with_id'):
            f = getattr(rider, fname, None)
            if f:
                files.append(f)
    for shop in Shop.objects.filter(owner=user):
        files.extend(_collect_shop_files(shop))
    for proof in PhotoProof.objects.filter(order__customer=user):
        if proof.photo:
            files.append(proof.photo)
    return files


def _collect_shop_files(shop):
    """Logo, banner, owner ID documents and order photo proofs that a shop delete orphans."""
    files = [f for f in (shop.logo, shop.banner, shop.owner_government_id, shop.owner_selfie_with_id) if f]
    files += [p.photo for p in PhotoProof.objects.filter(order__shop=shop) if p.photo]
    return files


@role_required('admin')
def admin_dashboard_view(request):
    total_users = User.objects.exclude(role='admin').count()
    total_shops = Shop.objects.filter(status='approved').count()
    pending_shops = Shop.objects.filter(status='pending').count()
    pending_riders = RiderProfile.objects.filter(is_submitted=True, approval_status='pending').count()
    total_orders = Order.objects.count()
    total_revenue = Order.objects.filter(payment_status='paid').aggregate(s=Sum('total_amount'))['s'] or 0
    recent_orders = Order.objects.select_related('customer', 'shop').order_by('-created_at')[:10]
    return render(request, 'admin_panel/dashboard.html', {
        'total_users': total_users,
        'total_shops': total_shops,
        'pending_shops': pending_shops,
        'pending_riders': pending_riders,
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'recent_orders': recent_orders,
    })


_VALID_SHOP_STATUSES = {'pending', 'approved', 'rejected', 'suspended'}
_VALID_USER_ROLES = {'customer', 'shop_owner', 'rider', 'admin'}


@role_required('admin')
def admin_shops_view(request):
    status_filter = request.GET.get('status', '')
    if status_filter not in _VALID_SHOP_STATUSES:
        status_filter = ''
    shops = Shop.objects.select_related('owner').order_by('-created_at')
    if status_filter:
        shops = shops.filter(status=status_filter)
    return render(request, 'admin_panel/shops.html', {'shops': shops, 'status_filter': status_filter})


@role_required('admin')
def admin_approve_shop_view(request, shop_id):
    if request.method != 'POST':
        return redirect('admin_shops')
    shop = get_object_or_404(Shop, id=shop_id)
    action = request.POST.get('action')
    from utils.email_notifications import send_email_notification
    allowed_from = {
        'approve': ('pending', 'rejected'),
        'reject': ('pending',),
        'suspend': ('approved',),
        'unsuspend': ('suspended',),
    }
    if action in allowed_from and shop.status not in allowed_from[action]:
        verb = {'approve': 'approved', 'reject': 'rejected', 'suspend': 'suspended', 'unsuspend': 'reinstated'}[action]
        messages.error(request, f'Shop "{shop.name}" is {shop.get_status_display().lower()}, so it can\'t be {verb}.')
        return redirect('admin_shops')
    if action == 'approve':
        shop.status = 'approved'
        shop.rejection_reason = ''
        shop.save()
        Notification.objects.create(user=shop.owner, message=f'Your shop "{shop.name}" has been approved!', link='/shop-dashboard/')
        send_email_notification('shop_approved', shop.owner)
        messages.success(request, f'Shop "{shop.name}" approved.')
    elif action == 'reject':
        reason = request.POST.get('reason', '')[:1000]
        shop.status = 'rejected'
        shop.rejection_reason = reason
        shop.save()
        Notification.objects.create(user=shop.owner, message=f'Your shop "{shop.name}" was rejected.', link='/shop-dashboard/')
        send_email_notification('shop_rejected', shop.owner, extra={'reason': reason})
        messages.warning(request, f'Shop "{shop.name}" rejected.')
    elif action == 'suspend':
        shop.status = 'suspended'
        shop.save()
        Notification.objects.create(user=shop.owner, message=f'Your shop "{shop.name}" has been suspended. Contact support for details.', link='/shop-dashboard/')
        messages.warning(request, f'Shop "{shop.name}" suspended.')
    elif action == 'unsuspend':
        shop.status = 'approved'
        shop.save()
        Notification.objects.create(user=shop.owner, message=f'Your shop "{shop.name}" has been reinstated and is now active.', link='/shop-dashboard/')
        messages.success(request, f'Shop "{shop.name}" reinstated.')
    elif action == 'delete':
        shop_name = shop.name
        files_to_purge = _collect_shop_files(shop)
        with transaction.atomic():
            shop.delete()
        for f in files_to_purge:
            try:
                f.delete(save=False)
            except Exception:
                logger.exception('Failed to delete file for deleted shop %s: %s', shop_name, getattr(f, 'name', '?'))
        messages.success(request, f'Shop "{shop_name}" permanently deleted.')
        return redirect('admin_shops')
    return redirect('admin_shops')


@role_required('admin')
def admin_users_view(request):
    role_filter = request.GET.get('role', '')
    if role_filter not in _VALID_USER_ROLES:
        role_filter = ''
    users = User.objects.all().order_by('-date_joined')
    if role_filter:
        users = users.filter(role=role_filter)
    page_obj, page_qs = paginate(request, users)
    return render(request, 'admin_panel/users.html', {'users': page_obj, 'page_obj': page_obj, 'page_qs': page_qs,
                                                      'role_filter': role_filter})


@role_required('admin')
def admin_toggle_user_view(request, user_id):
    if request.method != 'POST':
        return redirect('admin_users')
    user = get_object_or_404(User, id=user_id)
    if user == request.user or user.is_superuser:
        messages.error(request, 'You cannot deactivate your own account or a superuser.')
        return redirect('admin_users')
    user.is_active = not user.is_active
    user.save()
    status = 'activated' if user.is_active else 'deactivated'
    messages.success(request, f'User {user.email} {status}.')
    return redirect('admin_users')


@role_required('admin')
def admin_delete_user_view(request, user_id):
    if request.method != 'POST':
        return redirect('admin_users')
    user = get_object_or_404(User, id=user_id)
    if user.is_superuser:
        messages.error(request, 'Superusers cannot be deleted from this page.')
        return redirect('admin_users')
    if user == request.user:
        messages.error(request, 'You cannot delete your own account.')
        return redirect('admin_users')
    email = user.email

    files_to_purge = _collect_user_files(user)
    with transaction.atomic():
        user.delete()
    for f in files_to_purge:
        try:
            f.delete(save=False)
        except Exception:
            logger.exception('Failed to delete file for purged user %s: %s', email, getattr(f, 'name', '?'))

    messages.success(request, f'User {email} and all associated data permanently deleted.')
    return redirect('admin_users')


_RIDER_APPROVAL_FILTERS = {'pending', 'approved', 'rejected'}


@role_required('admin')
def admin_riders_view(request):
    status_filter = request.GET.get('status', 'pending')
    if status_filter not in _RIDER_APPROVAL_FILTERS:
        status_filter = 'pending'
    profiles = (
        RiderProfile.objects.select_related('user')
        .filter(is_submitted=True, approval_status=status_filter)
        .order_by('submitted_at')
    )
    field_labels = {f.name: f.verbose_name.title() for f in RiderProfile._meta.fields}
    applications = []
    for profile in profiles:
        docs = [
            (field_labels[f], os.path.basename(getattr(profile, f).name))
            for f in RiderProfile.DOCUMENT_FIELDS
            if getattr(profile, f)
        ]
        applications.append({'profile': profile, 'docs': docs})
    counts = {
        row['approval_status']: row['n']
        for row in RiderProfile.objects.filter(is_submitted=True)
        .values('approval_status').annotate(n=Count('id'))
    }
    return render(request, 'admin_panel/riders.html', {
        'applications': applications,
        'status_filter': status_filter,
        'counts': counts,
    })


@role_required('admin')
def admin_rider_action_view(request, profile_id):
    if request.method != 'POST':
        return redirect('admin_riders')
    profile = get_object_or_404(RiderProfile.objects.select_related('user'), id=profile_id, is_submitted=True)
    action = request.POST.get('action')
    rider = profile.user
    if action == 'approve':
        profile.approval_status = 'approved'
        profile.rejection_reason = ''
        message = 'Your rider application has been approved. You can now accept delivery jobs.'
        messages.success(request, f'Rider {rider.email} approved.')
    elif action == 'reject':
        reason = request.POST.get('reason', '').strip()[:500]
        profile.approval_status = 'rejected'
        profile.rejection_reason = reason
        message = 'Your rider application was not approved.' + (f' Reason: {reason}' if reason else '')
        messages.warning(request, f'Rider {rider.email} rejected.')
    else:
        messages.error(request, 'Invalid action.')
        return redirect('admin_riders')
    profile.reviewed_at = timezone.now()
    with transaction.atomic():
        profile.save(update_fields=['approval_status', 'rejection_reason', 'reviewed_at'])
        Notification.objects.create(user=rider, message=message, link='/rider/')
        if action == 'reject':
            # Release jobs not yet picked up so the shop can reassign them; in-progress
            # deliveries (laundry already with the rider) stay assigned.
            released = list(Order.objects.filter(rider=rider, status='accepted').select_related('shop__owner'))
            for order in released:
                order.rider = None
                order.save(update_fields=['rider', 'updated_at'])
                Notification.objects.create(
                    user=order.shop.owner,
                    message=f'The rider for Order #{order.id} is no longer available. Please assign another rider.',
                    link=f'/shop-dashboard/orders/{order.id}/',
                )
            if released:
                messages.info(request, f'{len(released)} unstarted job(s) released for reassignment.')
    return_status = request.POST.get('return_status', 'pending')
    if return_status not in _RIDER_APPROVAL_FILTERS:
        return_status = 'pending'
    return redirect(f"{reverse('admin_riders')}?status={return_status}")


@role_required('admin')
def admin_orders_view(request):
    valid_order_statuses = {s for s, _ in Order.STATUS_CHOICES}
    status_filter = request.GET.get('status', '')
    if status_filter not in valid_order_statuses:
        status_filter = ''
    orders = Order.objects.select_related('customer', 'shop', 'rider').order_by('-created_at')
    if status_filter:
        orders = orders.filter(status=status_filter)
    page_obj, page_qs = paginate(request, orders)
    return render(request, 'admin_panel/orders.html', {
        'orders': page_obj, 'page_obj': page_obj, 'page_qs': page_qs,
        'status_filter': status_filter,
        'status_choices': Order.STATUS_CHOICES,
    })


@role_required('admin')
def admin_transactions_view(request):
    transactions = PayMongoTransaction.objects.select_related('order__customer', 'order__shop').order_by('-created_at')
    page_obj, page_qs = paginate(request, transactions)
    return render(request, 'admin_panel/transactions.html', {'transactions': page_obj, 'page_obj': page_obj,
                                                             'page_qs': page_qs})


@role_required('admin')
def admin_analytics_view(request):
    try:
        days = int(request.GET.get('days', 30))
        if days not in (7, 30, 90):
            days = 30
    except (ValueError, TypeError):
        days = 30

    now = timezone.now()
    period_start = now - timedelta(days=days)
    prev_start = period_start - timedelta(days=days)

    cur_qs = Order.objects.filter(created_at__gte=period_start)
    prev_qs = Order.objects.filter(created_at__gte=prev_start, created_at__lt=period_start)
    paid_cur = cur_qs.filter(payment_status='paid')
    paid_prev = prev_qs.filter(payment_status='paid')

    cur_totals = paid_cur.aggregate(s=Sum('total_amount'), a=Avg('total_amount'))     # one query per period
    prev_totals = paid_prev.aggregate(s=Sum('total_amount'), a=Avg('total_amount'))
    total_revenue = cur_totals['s'] or 0
    prev_revenue = prev_totals['s'] or 0
    avg_value = cur_totals['a'] or 0
    prev_avg_value = prev_totals['a'] or 0

    total_customers = cur_qs.values('customer').distinct().count()
    returning = cur_qs.values('customer').annotate(cnt=Count('id')).filter(cnt__gte=2).count()
    retention_rate = round(returning / total_customers * 100) if total_customers else 0
    prev_cust_total = prev_qs.values('customer').distinct().count()
    prev_returning = prev_qs.values('customer').annotate(cnt=Count('id')).filter(cnt__gte=2).count()
    prev_retention = round(prev_returning / prev_cust_total * 100) if prev_cust_total else 0

    active_shops = Shop.objects.filter(status='approved').count()
    total_shops = Shop.objects.count()

    def _pct(cur, prev):
        if not prev:
            return None
        return round((float(cur) - float(prev)) / float(prev) * 100, 1)

    # Revenue over time — fill every day in range with 0 for missing days
    rev_map = {
        r['day'].isoformat(): float(r['revenue'])
        for r in paid_cur.annotate(day=TruncDate('created_at'))
                         .values('day').annotate(revenue=Sum('total_amount')).order_by('day')
    }
    prev_rev_map = {
        r['day'].isoformat(): float(r['revenue'])
        for r in paid_prev.annotate(day=TruncDate('created_at'))
                          .values('day').annotate(revenue=Sum('total_amount')).order_by('day')
    }
    chart_labels, chart_values, prev_chart_values = [], [], []
    for i in range(days):
        cur_day = timezone.localtime(period_start + timedelta(days=i + 1)).date()
        prev_day = timezone.localtime(prev_start + timedelta(days=i + 1)).date()
        chart_labels.append(cur_day.strftime('%b %d'))
        chart_values.append(rev_map.get(cur_day.isoformat(), 0))
        prev_chart_values.append(prev_rev_map.get(prev_day.isoformat(), 0))

    # Service type breakdown for donut chart
    svc_qs = (OrderService.objects.filter(order__in=cur_qs)
              .values('service__name').annotate(cnt=Count('id')).order_by('-cnt'))
    service_names = [s['service__name'].replace('_', ' ').title() for s in svc_qs]
    service_values = [s['cnt'] for s in svc_qs]

    # Top 5 shops by revenue
    top_shops = list(
        paid_cur.values('shop__id', 'shop__name')
                .annotate(revenue=Sum('total_amount'))
                .order_by('-revenue')[:5]
    )
    max_shop_rev = float(top_shops[0]['revenue']) if top_shops else 1

    # Top 5 customers by spend in period
    top_customers = list(
        cur_qs.values('customer__id', 'customer__first_name', 'customer__last_name', 'customer__email')
              .annotate(order_count=Count('id'), total_spend=Sum('total_amount'), last_order=Max('created_at'))
              .order_by('-total_spend')[:5]
    )
    top_cust_ids = [c['customer__id'] for c in top_customers]
    all_order_counts = {
        r['customer']: r['cnt']
        for r in Order.objects.filter(customer__in=top_cust_ids).values('customer').annotate(cnt=Count('id'))
    }
    for c in top_customers:
        all_time = all_order_counts.get(c['customer__id'], 0)
        c['tier'] = 'platinum' if all_time >= 15 else 'gold' if all_time >= 10 else 'silver' if all_time >= 5 else 'bronze'
        fn = c['customer__first_name'] or ''
        ln = c['customer__last_name'] or ''
        c['initials'] = (fn[:1] + ln[:1]).upper() or c['customer__email'][:1].upper()

    # Smart insight
    insight = None
    if top_shops:
        # format_html escapes the owner-controlled shop name (template renders insight with |safe).
        insight = format_html(
            'Based on the last {} days, <strong>{}</strong> is your top-performing shop with ₱{} in revenue.',
            days, top_shops[0]['shop__name'], f'{float(top_shops[0]["revenue"]):,.2f}',
        )
    elif total_revenue:
        insight = f'Platform revenue for the last {days} days totals ₱{float(total_revenue):,.2f}.'

    return render(request, 'admin_panel/analytics.html', {
        'days': days,
        'total_revenue': f'{float(total_revenue):,.2f}',
        'prev_revenue': f'{float(prev_revenue):,.2f}',
        'revenue_change': _pct(total_revenue, prev_revenue),
        'avg_value': f'{float(avg_value):,.2f}',
        'prev_avg_value': f'{float(prev_avg_value):,.2f}',
        'avg_change': _pct(avg_value, prev_avg_value),
        'retention_rate': retention_rate,
        'retention_change': retention_rate - prev_retention,
        'active_shops': active_shops,
        'total_shops': total_shops,
        'total_orders': cur_qs.count(),
        'chart_labels': chart_labels,
        'chart_values': chart_values,
        'prev_chart_values': prev_chart_values,
        'service_names': service_names,
        'service_values': service_values,
        'top_shops': top_shops,
        'max_shop_rev': max_shop_rev,
        'top_customers': top_customers,
        'insight': insight,
    })


@role_required('admin')
def admin_broadcast_view(request):
    if request.method == 'POST':
        message = request.POST.get('message', '').strip()[:500]
        role = request.POST.get('role', '')
        if not message:
            messages.error(request, 'Broadcast message cannot be empty.')
            return redirect('admin_dashboard')
        if role not in _VALID_USER_ROLES:
            role = ''
        users = User.objects.filter(role=role) if role else User.objects.all()
        notifications = [Notification(user=u, message=message) for u in users]
        Notification.objects.bulk_create(notifications)
        messages.success(request, f'Broadcast sent to {len(notifications)} users.')
        return redirect('admin_dashboard')
    return redirect('admin_dashboard')
