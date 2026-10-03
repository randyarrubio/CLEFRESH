import json
import logging

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from accounts.decorators import role_required
from accounts.models import is_approved_rider
from orders.models import Order, OrderStatusLog, PhotoProof
from notifications.models import Notification
from .models import RiderLocation
from utils.email_notifications import send_email_notification
from utils.validators import validate_image_upload

logger = logging.getLogger(__name__)


STATUS_PROOF_REQUIREMENTS = {
    'picked_up': ('pickup', 'pickup proof'),
    'out_for_delivery': ('dropoff', 'shop drop-off proof'),
    'delivered': ('delivery', 'delivery proof'),
}


def _proof_type_for_current_step(order):
    if order.status == 'accepted':
        return 'pickup'
    # Drop-off proof stays uploadable while the shop processes the order, otherwise
    # a shop advancing past picked_up first would block out_for_delivery forever.
    if order.status in ('picked_up', 'washing', 'drying', 'folding', 'ready'):
        return 'dropoff'
    if order.status == 'out_for_delivery':
        return 'delivery'
    return None


@role_required('rider')
def rider_dashboard_view(request):
    orders = Order.objects.filter(
        rider=request.user,
        status__in=['accepted', 'picked_up', 'out_for_delivery', 'ready']
    ).select_related('customer', 'shop').order_by('pickup_datetime')
    rider_profile = getattr(request.user, 'rider_profile', None)
    # Job board: only orders the shop has already accepted, only for approved riders.
    if is_approved_rider(request.user):
        available_orders = Order.objects.filter(
            rider__isnull=True,
            status='accepted',
            shop__status='approved',
        ).select_related('shop').order_by('pickup_datetime', 'created_at')
    else:
        available_orders = Order.objects.none()
    today = timezone.localdate()
    completed_today = Order.objects.filter(
        rider=request.user,
        status='delivered',
        updated_at__date=today,
    )
    recent_jobs = Order.objects.filter(rider=request.user, status='delivered').select_related('shop').order_by('-updated_at')[:3]
    today_earnings = completed_today.aggregate(total=Sum('total_amount'))['total'] or 0
    return render(request, 'rider/dashboard.html', {
        'orders': orders,
        'available_orders': available_orders,
        'active_count': orders.count(),
        'available_count': available_orders.count(),
        'completed_today_count': completed_today.count(),
        'today_earnings': today_earnings,
        'recent_jobs': recent_jobs,
        'has_active_delivery': any(o.status == 'out_for_delivery' for o in orders),
        'rider_profile': rider_profile,
        'is_approved_rider': is_approved_rider(request.user),
    })


@role_required('rider')
@require_POST
def rider_accept_job_view(request, order_id):
    """Claim a delivery job. Only the shop accepts/declines the order itself;
    the rider just takes an already-accepted, unassigned job."""
    if not is_approved_rider(request.user):
        messages.error(request, 'Your rider account must be approved by an admin before you can accept jobs.')
        return redirect('rider_dashboard')
    with transaction.atomic():
        # Conditional claim: select_for_update is a no-op on SQLite, so two riders could both
        # read rider=NULL. Only the update that actually flips it wins.
        claimed = Order.objects.filter(
            id=order_id, rider__isnull=True, status='accepted', shop__status='approved',
        ).update(rider=request.user, updated_at=timezone.now())
        if not claimed:
            messages.error(request, 'This job is no longer available.')
            return redirect('rider_dashboard')
        order = Order.objects.select_related('customer', 'shop__owner').get(id=order_id)
        Notification.objects.create(
            user=order.customer,
            message=f'A rider has been assigned to your Order #{order.id}.',
            link=f'/orders/{order.id}/',
        )
        Notification.objects.create(
            user=order.shop.owner,
            message=f'{request.user.get_full_name() or request.user.email} will handle pickup and delivery for Order #{order.id}.',
            link=f'/shop-dashboard/orders/{order.id}/',
        )
    send_email_notification('rider_task_assigned', request.user, order=order)
    send_email_notification('rider_assigned', order.customer, order=order)
    messages.success(request, f'Order #{order.id} accepted.')
    return redirect('rider_task_detail', order_id=order.id)


@role_required('rider')
def rider_task_detail_view(request, order_id):
    order = get_object_or_404(Order, id=order_id, rider=request.user)
    logs = order.status_logs.all()
    proofs = order.photo_proofs.all()
    proof_types = set(proofs.values_list('proof_type', flat=True))
    return render(request, 'rider/task_detail.html', {
        'order': order,
        'logs': logs,
        'proofs': proofs,
        'has_pickup_proof': 'pickup' in proof_types,
        'has_dropoff_proof': 'dropoff' in proof_types,
        'has_delivery_proof': 'delivery' in proof_types,
        'current_proof_type': _proof_type_for_current_step(order),
        'processing_statuses': ('washing', 'drying', 'folding'),
        'delivery_block': order.delivery_block_reason() if order.status == 'ready' else None,
    })


@role_required('rider')
def rider_update_status_view(request, order_id):
    if request.method != 'POST':
        return redirect('rider_task_detail', order_id=order_id)
    order = get_object_or_404(Order.objects.select_related('customer', 'shop__owner'), id=order_id, rider=request.user)
    new_status = request.POST.get('status')
    allowed = ['picked_up', 'out_for_delivery', 'delivered']
    if new_status not in allowed:
        messages.error(request, 'Invalid status.')
        return redirect('rider_task_detail', order_id=order_id)

    valid_prior_status = {
        'picked_up': 'accepted',
        'out_for_delivery': 'ready',
        'delivered': 'out_for_delivery',
    }
    if order.status != valid_prior_status[new_status]:
        messages.error(request, f'Cannot mark as {new_status.replace("_", " ")} from current status.')
        return redirect('rider_task_detail', order_id=order_id)
    if new_status == 'out_for_delivery' and order.delivery_block_reason():
        messages.error(request, order.delivery_block_reason())
        return redirect('rider_task_detail', order_id=order_id)

    required_proof_type, required_proof_label = STATUS_PROOF_REQUIREMENTS[new_status]
    if not order.photo_proofs.filter(proof_type=required_proof_type).exists():
        messages.error(request, f'Upload a {required_proof_label} before confirming this action.')
        return redirect('rider_task_detail', order_id=order_id)
    cash_due = new_status == 'delivered' and order.payment_method == 'cod' and order.payment_status != 'paid'
    if cash_due and request.POST.get('cod_collected') != 'on':
        messages.error(request, f'Confirm you collected ₱{order.total_amount:,.2f} in cash before completing the delivery.')
        return redirect('rider_task_detail', order_id=order_id)
    order.status = new_status
    fields = {'status': new_status, 'updated_at': timezone.now()}
    if cash_due:
        order.payment_status = 'paid'
        fields['payment_status'] = 'paid'
    # Conditional update: a double submit, or the shop moving the order at the same time, can't apply twice.
    if not Order.objects.filter(pk=order.pk, status=valid_prior_status[new_status]).update(**fields):
        messages.error(request, 'This order was just updated. Please refresh and try again.')
        return redirect('rider_task_detail', order_id=order_id)
    OrderStatusLog.objects.create(order=order, status=new_status)
    _rider_customer_messages = {
        'picked_up': f'Your laundry for order #{order.id} has been picked up by your rider.',
        'out_for_delivery': f'Your order #{order.id} is out for delivery — your rider is on the way!',
        'delivered': f'Your laundry has been delivered! Order #{order.id} is complete.',
    }
    _rider_shop_messages = {
        'picked_up': f'Rider picked up order #{order.id} from the customer.',
        'out_for_delivery': f'Order #{order.id} is now out for delivery.',
        'delivered': f'Order #{order.id} has been delivered successfully.',
    }
    Notification.objects.create(
        user=order.customer,
        message=_rider_customer_messages.get(new_status, f'Your order #{order.id} status: {order.get_status_display()}.'),
        link=f'/orders/{order.id}/'
    )
    Notification.objects.create(
        user=order.shop.owner,
        message=_rider_shop_messages.get(new_status, f'Order #{order.id} status: {order.get_status_display()}.'),
        link=f'/shop-dashboard/orders/{order.id}/'
    )
    if new_status == 'delivered':
        send_email_notification('order_delivered', order.customer, order=order)
    messages.success(request, 'Status updated.')
    return redirect('rider_task_detail', order_id=order_id)


@role_required('rider')
def upload_proof_view(request, order_id):
    order = get_object_or_404(Order, id=order_id, rider=request.user)
    if request.method == 'POST':
        expected_type = _proof_type_for_current_step(order)
        proof_type = request.POST.get('proof_type') or expected_type
        if not expected_type or proof_type != expected_type:
            messages.error(request, 'This order is not ready for that photo proof upload.')
            return redirect('rider_task_detail', order_id=order_id)
        if order.photo_proofs.filter(proof_type=proof_type).exists():
            messages.error(request, f'A {proof_type} proof photo has already been uploaded for this order.')
            return redirect('rider_task_detail', order_id=order_id)
        photo = request.FILES.get('photo')
        if not photo:
            messages.error(request, 'Please select a photo.')
            return redirect('rider_task_detail', order_id=order_id)
        try:
            validate_image_upload(photo)
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
            return redirect('rider_task_detail', order_id=order_id)
        order_for_notify = get_object_or_404(
            Order.objects.select_related('customer', 'shop__owner'), id=order_id, rider=request.user
        )
        PhotoProof.objects.create(
            order=order_for_notify,
            photo=photo,
            proof_type=proof_type,
            uploaded_by=request.user,
        )

        # Auto-advance status on pickup/delivery; notify-only on dropoff
        _auto_advance = {
            'pickup':   ('accepted',         'picked_up'),
            'delivery': ('out_for_delivery', 'delivered'),
        }
        if proof_type in _auto_advance:
            required_prior, new_status = _auto_advance[proof_type]
            cash_due = (new_status == 'delivered' and order_for_notify.payment_method == 'cod'
                        and order_for_notify.payment_status != 'paid')
            if cash_due and request.POST.get('cod_collected') != 'on':
                messages.info(request, 'Photo uploaded. Confirm the cash payment below to complete the delivery.')
                return redirect('rider_task_detail', order_id=order_id)
            if order_for_notify.status == required_prior:
                order_for_notify.status = new_status
                fields = {'status': new_status, 'updated_at': timezone.now()}
                if cash_due:
                    order_for_notify.payment_status = 'paid'
                    fields['payment_status'] = 'paid'
                if not Order.objects.filter(pk=order_for_notify.pk, status=required_prior).update(**fields):
                    messages.success(request, 'Photo proof uploaded.')   # someone else advanced it already
                    return redirect('rider_task_detail', order_id=order_id)
                OrderStatusLog.objects.create(order=order_for_notify, status=new_status)
                _customer_msgs = {
                    'picked_up': f'Your laundry for order #{order_for_notify.id} has been picked up by your rider.',
                    'delivered': f'Your laundry has been delivered! Order #{order_for_notify.id} is complete.',
                }
                _shop_msgs = {
                    'picked_up': f'Rider dropped off order #{order_for_notify.id} at your shop.',
                    'delivered': f'Order #{order_for_notify.id} has been delivered successfully.',
                }
                Notification.objects.create(
                    user=order_for_notify.customer,
                    message=_customer_msgs[new_status],
                    link=f'/orders/{order_for_notify.id}/'
                )
                Notification.objects.create(
                    user=order_for_notify.shop.owner,
                    message=_shop_msgs[new_status],
                    link=f'/shop-dashboard/orders/{order_for_notify.id}/'
                )
                if new_status == 'delivered':
                    send_email_notification('order_delivered', order_for_notify.customer, order=order_for_notify)
                messages.success(request, f'Photo uploaded and order marked as {new_status.replace("_", " ")}.')
                return redirect('rider_task_detail', order_id=order_id)

        elif proof_type == 'dropoff' and order_for_notify.status == 'picked_up':
            Notification.objects.create(
                user=order_for_notify.customer,
                message=f'Your laundry for order #{order_for_notify.id} has been dropped off at the shop and is now being processed.',
                link=f'/orders/{order_for_notify.id}/'
            )
            Notification.objects.create(
                user=order_for_notify.shop.owner,
                message=f'Rider has dropped off order #{order_for_notify.id} at your shop. Ready to process.',
                link=f'/shop-dashboard/orders/{order_for_notify.id}/'
            )

        messages.success(request, 'Photo proof uploaded.')
        return redirect('rider_task_detail', order_id=order_id)
    # Upload forms live on the task detail page (there is no standalone template).
    return redirect('rider_task_detail', order_id=order_id)


@login_required
@require_POST
def update_rider_location_view(request, rider_id):
    if request.user.role != 'rider' and not request.user.is_superuser:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.user.id != rider_id and not request.user.is_superuser:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    try:
        data = json.loads(request.body)
        lat = data.get('latitude')
        lng = data.get('longitude')
        lat = float(lat)
        lng = float(lng)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return JsonResponse({'error': 'Invalid coordinates'}, status=400)
        RiderLocation.objects.update_or_create(
            rider=request.user,
            defaults={'latitude': lat, 'longitude': lng}
        )
        return JsonResponse({'status': 'ok'})
    except (json.JSONDecodeError, ValueError, TypeError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    except Exception:
        logger.exception("Rider location update failed for rider %s", request.user.id)
        return JsonResponse({'error': 'Server error.'}, status=500)


def geocode_proxy_view(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)
    rate_key = f'geocode_{request.user.id}'
    cache.add(rate_key, 0, 3600)          # atomic counter: concurrent requests can't share a slot
    try:
        calls = cache.incr(rate_key)
    except ValueError:                    # expired between add and incr
        cache.set(rate_key, 1, 3600)
        calls = 1
    if calls > 30:
        return JsonResponse({'error': 'Rate limit exceeded. Try again later.'}, status=429)
    from utils.geocode import geocode_address
    address = request.GET.get('address', '').strip()
    if not address or len(address) > 300:
        return JsonResponse({'error': 'Invalid address'}, status=400)
    lat, lng = geocode_address(address)
    return JsonResponse({'lat': lat, 'lng': lng})
