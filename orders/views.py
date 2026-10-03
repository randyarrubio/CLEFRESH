from decimal import Decimal, InvalidOperation

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone
from .models import Order, OrderService, OrderStatusLog, PhotoProof
from shops.models import DeliverySlot, Promotion, Service, Shop
from notifications.models import Notification
from utils.geocode import geocode_address
from utils.email_notifications import send_email_notification


# Methods payments.process_payment_view can actually charge, plus COD.
# 'card' / 'online_banking' stay in the model choices for existing rows only.
BOOKABLE_PAYMENT_METHODS = [
    (value, label) for value, label in Order.PAYMENT_METHOD_CHOICES
    if value in ('cod', 'gcash', 'grab_pay', 'maya')
]


# Add-on toggles on the booking page -> text appended to Order.notes.
BOOKING_ADDONS = {
    'express': 'Express service requested',
    'hypoallergenic': 'Use hypoallergenic detergent',
}


def _place_order_context(shop, services, slots, request=None, error_step=None):
    form_data = None
    if request is not None and request.method == 'POST':
        # Echo the submission back so the booking form can be restored after an error.
        form_data = {
            key: request.POST.getlist(key) if key in ('addons', 'svc_selected') else request.POST.get(key, '')
            for key in request.POST
            if key != 'csrfmiddlewaretoken'
        }
    live_promos = list(shop.promotions.live())    # used twice below; one query
    return {
        'shop': shop,
        'services': services,
        'slots': slots,
        'form_data': form_data,
        'error_step': error_step,
        'booking_addons': BOOKING_ADDONS,
        'has_kg_services': any(s.price_per_kg for s in services),
        'live_promos': live_promos,
        # Mirrors Promotion.discount_for() so the page can estimate; the server recomputes on submit.
        'promo_rules': [
            {'title': p.title, 'type': p.discount_type, 'value': float(p.discount_value),
             'min': float(p.min_order_amount) if p.min_order_amount is not None else None}
            for p in live_promos if p.discount_type in ('percent', 'fixed') and p.discount_value
        ],
        'order_payment_choices': BOOKABLE_PAYMENT_METHODS,
        'is_customer_home': True,
    }


def _parse_non_negative_decimal(value, field_label):
    try:
        parsed = Decimal(str(value or '0'))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'Invalid {field_label}.')
    if not parsed.is_finite():   # NaN/Infinity: comparing them raises InvalidOperation
        raise ValueError(f'Invalid {field_label}.')
    if parsed < 0:
        raise ValueError(f'{field_label} cannot be negative.')
    return parsed


def _parse_non_negative_int(value, field_label):
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        raise ValueError(f'Invalid {field_label}.')
    if parsed < 0:
        raise ValueError(f'{field_label} cannot be negative.')
    return parsed


def _get_order_for_status_api(request, order_id):
    if request.user.role == 'customer':
        return get_object_or_404(Order, id=order_id, customer=request.user)
    if request.user.role == 'shop_owner':
        return get_object_or_404(Order, id=order_id, shop__owner=request.user)
    if request.user.role == 'rider':
        return get_object_or_404(Order, id=order_id, rider=request.user)
    if request.user.role == 'admin' or request.user.is_superuser:
        return get_object_or_404(Order, id=order_id)
    raise Http404


@login_required
def order_list_view(request):
    last_order_shop = None
    if request.user.role == 'customer':
        orders = Order.objects.filter(customer=request.user).select_related('shop', 'rider')
        last_order = orders.first()
        last_order_shop = last_order.shop if last_order else None
    elif request.user.role == 'shop_owner':
        return redirect('shop_dashboard_orders')
    elif request.user.role == 'rider':
        return redirect('rider_dashboard')
    elif request.user.role == 'admin' or request.user.is_superuser:
        orders = Order.objects.all().select_related('shop', 'customer', 'rider')
    else:
        return redirect('/')
    orders = orders.annotate(service_count=Count('order_services'))
    return render(request, 'orders/list.html', {
        'orders': orders,
        'last_order_shop': last_order_shop,
        'is_customer_home': request.user.role == 'customer',
    })


@login_required
def order_detail_view(request, order_id):
    # Lines + their services in one go (the template loops over order.order_services.all).
    qs = Order.objects.select_related('shop', 'rider', 'customer').prefetch_related('order_services__service')
    if request.user.role == 'customer':
        order = get_object_or_404(qs, id=order_id, customer=request.user)
    elif request.user.role in ('shop_owner',):
        order = get_object_or_404(qs, id=order_id, shop__owner=request.user)
    elif request.user.role == 'rider':
        order = get_object_or_404(qs, id=order_id, rider=request.user)
    elif request.user.role == 'admin' or request.user.is_superuser:
        order = get_object_or_404(qs, id=order_id)
    else:
        raise Http404
    logs = order.status_logs.all()
    proofs = order.photo_proofs.all()
    rider_location = None
    # Same privacy rule as the status API: coordinates only while out for delivery.
    if order.rider_id and order.status == 'out_for_delivery':
        rider_location = getattr(order.rider, 'rider_location', None)
    return render(request, 'orders/detail.html', {
        'order': order,
        'logs': logs,
        'proofs': proofs,
        'rider_location': rider_location,
    })


@login_required
def place_order_view(request, shop_id):
    shop = get_object_or_404(Shop, id=shop_id, status='approved')
    if request.user.role != 'customer':
        messages.error(request, 'Only customers can place orders.')
        return redirect('shop_detail', shop_id=shop_id)

    services = shop.services.all()
    today = timezone.localdate()
    slots = shop.delivery_slots.filter(date__gte=today, current_bookings__lt=F('max_capacity'))

    if request.method == 'POST':
        pickup_address = request.POST.get('pickup_address', '')
        delivery_address = request.POST.get('delivery_address', '')
        pickup_datetime_str = request.POST.get('pickup_datetime', '')
        slot_id = request.POST.get('delivery_slot', '')
        payment_method = request.POST.get('payment_method', 'cod')
        notes = request.POST.get('notes', '').strip()
        addon_lines = [BOOKING_ADDONS[a] for a in request.POST.getlist('addons') if a in BOOKING_ADDONS]
        if addon_lines:
            notes = '\n'.join(filter(None, ['Add-ons: ' + '; '.join(addon_lines), notes]))
        notes = notes[:1000]

        if not pickup_address or not delivery_address or not pickup_datetime_str:
            messages.error(request, 'Please fill in all required fields.')
            return render(request, 'orders/place_order.html', _place_order_context(shop, services, slots, request, error_step=2))
        if len(pickup_address) > 500 or len(delivery_address) > 500:
            messages.error(request, 'Address is too long (max 500 characters).')
            return render(request, 'orders/place_order.html', _place_order_context(shop, services, slots, request, error_step=2))

        valid_payment_methods = {value for value, _label in BOOKABLE_PAYMENT_METHODS}
        if payment_method not in valid_payment_methods:
            messages.error(request, 'Invalid payment method.')
            return render(request, 'orders/place_order.html', _place_order_context(shop, services, slots, request, error_step=4))

        # Geocode addresses
        pickup_lat, pickup_lng = geocode_address(pickup_address)
        delivery_lat, delivery_lng = geocode_address(delivery_address)

        # Parse pickup datetime
        from datetime import datetime
        try:
            pickup_dt = datetime.fromisoformat(pickup_datetime_str)
            pickup_dt = timezone.make_aware(pickup_dt)
        except Exception:
            messages.error(request, 'Invalid pickup datetime.')
            return render(request, 'orders/place_order.html', _place_order_context(shop, services, slots, request, error_step=3))
        if pickup_dt <= timezone.now():
            messages.error(request, 'Pickup datetime must be in the future.')
            return render(request, 'orders/place_order.html', _place_order_context(shop, services, slots, request, error_step=3))

        slot = None
        if slot_id:
            if not str(slot_id).isdigit():
                messages.error(request, 'Invalid delivery slot.')
                return render(request, 'orders/place_order.html', _place_order_context(shop, services, slots, request, error_step=3))
            slot = get_object_or_404(DeliverySlot, id=slot_id, shop=shop)

        # Weighing is always done by the shop: per-kg services are booked without a weight, the
        # shop weighs the laundry at drop-off, and only then is the price final (and payable online).
        # Per-piece items are still priced from the customer's count.
        selected_ids = {int(x) for x in request.POST.getlist('svc_selected') if str(x).isdigit()}

        # Calculate total
        total = Decimal('0.00')
        service_data = []
        has_unweighed_line = False
        try:
            for service in services:
                qty_key = f'qty_{service.id}'
                weight_key = f'weight_{service.id}'
                qty = _parse_non_negative_int(request.POST.get(qty_key, 0), f'{service.get_name_display()} quantity')
                weight = _parse_non_negative_decimal(request.POST.get(weight_key, 0), f'{service.get_name_display()} weight')
                # A per-kg service counts as chosen if ticked (or a weight was sent by an old form);
                # any customer-supplied weight is ignored — the shop's scale decides.
                to_be_weighed = bool(service.price_per_kg) and (service.id in selected_ids or weight > 0)
                if to_be_weighed:
                    weight = Decimal('0')
                if qty > 0 or weight > 0 or to_be_weighed:
                    subtotal = Decimal('0.00')
                    if service.price_per_piece and qty > 0:
                        subtotal += service.price_per_piece * qty
                    if service.price_per_kg and weight > 0:
                        subtotal += service.price_per_kg * weight
                    if not to_be_weighed and subtotal <= 0:
                        continue                      # e.g. a weight typed for a per-piece-only service
                    has_unweighed_line = has_unweighed_line or to_be_weighed
                    total += subtotal
                    service_data.append({'service': service, 'qty': qty, 'weight': weight, 'subtotal': subtotal})
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(request, 'orders/place_order.html', _place_order_context(shop, services, slots, request, error_step=1))

        weigh_at_shop = has_unweighed_line            # only piece items picked: nothing to weigh, price is final
        if not service_data or (total <= 0 and not weigh_at_shop):
            # total can be 0 when e.g. a weight is entered for a per-piece-only service.
            messages.error(request, 'Please select at least one service with a valid quantity or weight.')
            return render(request, 'orders/place_order.html', _place_order_context(shop, services, slots, request, error_step=1))
        if total > Decimal('50000.00'):
            messages.error(request, 'Order total exceeds the maximum allowed amount (₱50,000).')
            return render(request, 'orders/place_order.html', _place_order_context(shop, services, slots, request, error_step=1))

        # Best eligible live promo, computed server-side at submit time (the page only shows an estimate).
        # Weigh-at-shop orders get their promo when the shop confirms the weight.
        if weigh_at_shop:
            promotion, discount = None, Decimal('0.00')
        else:
            promotion, discount = Promotion.best_for(shop, total)
        charged_total = total - discount

        with transaction.atomic():
            if slot:
                slot = DeliverySlot.objects.select_for_update().get(id=slot.id, shop=shop)
                if slot.date < today or slot.current_bookings >= slot.max_capacity:
                    messages.error(request, 'Selected delivery slot is no longer available.')
                    return render(request, 'orders/place_order.html', _place_order_context(shop, services, slots, request, error_step=3))
            order = Order.objects.create(
                customer=request.user,
                shop=shop,
                pickup_address=pickup_address,
                delivery_address=delivery_address,
                pickup_lat=pickup_lat,
                pickup_lng=pickup_lng,
                delivery_lat=delivery_lat,
                delivery_lng=delivery_lng,
                pickup_datetime=pickup_dt,
                delivery_slot=slot,
                payment_method=payment_method,
                subtotal_amount=total,
                discount_amount=discount,
                total_amount=charged_total,
                promotion=promotion,
                promotion_title=promotion.title if promotion else '',
                weigh_at_shop=weigh_at_shop,
                notes=notes,
            )
            if not weigh_at_shop and payment_method != 'cod':
                # Known price: the online-payment deadline starts now (weigh orders start it at weighing).
                order.start_payment_clock()
                order.save(update_fields=['payment_due_at', 'payment_reminders_sent'])
            for sd in service_data:
                OrderService.objects.create(
                    order=order,
                    service=sd['service'],
                    quantity=sd['qty'],
                    weight_kg=sd['weight'] if sd['weight'] > 0 else None,
                    subtotal=sd['subtotal'],
                )
            placed_note = 'Order placed by customer.'
            if weigh_at_shop:
                placed_note += ' Shop will weigh the laundry at drop-off to confirm the price.'
            if promotion:
                placed_note += f' Promo "{promotion.title}" applied: -₱{discount:,.2f}.'
            OrderStatusLog.objects.create(order=order, status='pending', note=placed_note)
            if slot:
                DeliverySlot.objects.filter(id=slot.id).update(current_bookings=F('current_bookings') + 1)

        # Notifications
        Notification.objects.create(
            user=request.user,
            message=f'Your order #{order.id} has been placed.',
            link=f'/orders/{order.id}/'
        )
        Notification.objects.create(
            user=shop.owner,
            message=f'New order #{order.id} received from {request.user.get_full_name() or request.user.email}.',
            link=f'/shop-dashboard/orders/{order.id}/'
        )

        services_summary = ', '.join(sd['service'].get_name_display() for sd in service_data)
        send_email_notification('order_placed', request.user, order=order)
        send_email_notification('new_order_for_shop', shop.owner, order=order, extra={'services_summary': services_summary})

        if promotion:
            messages.info(request, f'Promo "{promotion.title}" applied — you save ₱{discount:,.2f}.')
        if weigh_at_shop:
            # No checkout yet: the price is confirmed after weighing, then the customer is notified to pay.
            if payment_method == 'cod':
                messages.success(request, f'Order #{order.id} placed! The shop will weigh your laundry and confirm '
                                          'the final price. Pay the rider on delivery.')
            else:
                messages.success(request, f'Order #{order.id} placed! The shop will weigh your laundry at drop-off. '
                                          "We'll notify you to pay once the final price is confirmed.")
            return redirect('order_detail', order_id=order.id)
        if payment_method == 'cod':
            messages.success(request, f'Order #{order.id} placed successfully! Pay on delivery.')
            return redirect('order_detail', order_id=order.id)
        else:
            return redirect('payment_checkout', order_id=order.id)

    return render(request, 'orders/place_order.html', {
        **_place_order_context(shop, services, slots),
    })


@login_required
def cancel_order_view(request, order_id):
    if request.method != 'POST':
        return redirect('order_detail', order_id=order_id)
    order = get_object_or_404(Order, id=order_id, customer=request.user)
    if order.status not in ('pending',):
        messages.error(request, 'This order cannot be cancelled.')
        return redirect('order_detail', order_id=order_id)
    if order.payment_status != 'paid' and order.online_payment_in_progress():
        messages.error(request, 'An online payment is in progress for this order. Please wait for it to finish.')
        return redirect('order_detail', order_id=order_id)
    with transaction.atomic():
        # Conditional update: never overwrites a concurrent shop accept or payment confirmation.
        if not Order.objects.filter(pk=order.pk, status='pending').update(status='cancelled', updated_at=timezone.now()):
            messages.error(request, 'This order cannot be cancelled.')
            return redirect('order_detail', order_id=order_id)
        order.refresh_from_db()
        OrderStatusLog.objects.create(order=order, status='cancelled', note='Cancelled by customer.')
        if order.delivery_slot_id:
            DeliverySlot.objects.filter(id=order.delivery_slot_id, current_bookings__gt=0).update(current_bookings=F('current_bookings') - 1)
    if order.payment_status == 'paid':
        from payments.views import refund_if_paid
        refund_if_paid(order, 'the customer cancelled a paid order.')
        messages.success(request, 'Order cancelled. Your online payment will be refunded.')
    else:
        messages.success(request, 'Order cancelled.')
    return redirect('order_list')


@login_required
def switch_to_cod_view(request, order_id):
    """Customer gives up on paying online and pays the rider instead (only before delivery)."""
    if request.method != 'POST':
        return redirect('order_detail', order_id=order_id)
    from payments.models import PayMongoTransaction
    order = get_object_or_404(Order, id=order_id, customer=request.user)
    if order.payment_method == 'cod':
        messages.info(request, 'This order is already set to cash on delivery.')
    elif order.payment_status == 'paid':
        messages.info(request, 'This order is already paid.')
    elif order.status in ('out_for_delivery', 'delivered', 'cancelled', 'declined'):
        messages.error(request, 'The payment method can no longer be changed for this order.')
    elif PayMongoTransaction.in_progress(order).exists():
        messages.error(request, 'An online payment is in progress for this order. Please wait for it to finish.')
    else:
        order.payment_method = 'cod'
        order.payment_status = 'unpaid'
        order.payment_due_at = None            # no online deadline for cash on delivery
        order.save(update_fields=['payment_method', 'payment_status', 'payment_due_at', 'updated_at'])
        Notification.objects.create(
            user=order.shop.owner,
            message=f'Order #{order.id} will be paid in cash on delivery.',
            link=f'/shop-dashboard/orders/{order.id}/',
        )
        messages.success(request, 'Switched to cash on delivery. Please pay the rider when your laundry arrives.')
    return redirect('order_detail', order_id=order_id)


@login_required
def confirm_cash_received_view(request, order_id):
    """Rider or shop marks a delivered cash-on-delivery order as paid (e.g. the box wasn't ticked at delivery)."""
    if request.method != 'POST':
        return redirect('order_detail', order_id=order_id)
    order = get_object_or_404(Order.objects.select_related('shop__owner', 'customer'), id=order_id)
    is_rider = order.rider_id == request.user.id
    is_owner = order.shop.owner_id == request.user.id
    back = redirect('rider_task_detail' if is_rider else 'shop_dashboard_order_detail', order_id=order_id)
    if not (is_rider or is_owner):
        raise Http404
    if order.payment_method != 'cod' or order.status != 'delivered':
        messages.error(request, 'Cash can only be confirmed for delivered cash-on-delivery orders.')
        return back
    if not Order.objects.filter(pk=order.pk, payment_status__in=['unpaid', 'failed']).update(
            payment_status='paid', updated_at=timezone.now()):
        messages.info(request, 'This order is already marked as paid.')
        return back
    who = 'rider' if is_rider else 'shop'
    OrderStatusLog.objects.create(order=order, status='delivered',
                                  note=f'Cash payment of ₱{order.total_amount:,.2f} confirmed by the {who}.')
    notify = order.shop.owner if is_rider else order.rider
    if notify:
        Notification.objects.create(user=notify, message=f'Cash payment confirmed for order #{order.id}.',
                                    link=f'/shop-dashboard/orders/{order.id}/' if is_rider else f'/rider/tasks/{order.id}/')
    messages.success(request, f'Order #{order.id} marked as paid (₱{order.total_amount:,.2f} cash).')
    return back


# AJAX status poll
@login_required
def api_order_status_view(request, order_id):
    order = _get_order_for_status_api(request, order_id)
    data = {
        'status': order.status,
        'status_display': order.get_status_display(),
        'payment_status': order.payment_status,
    }
    if order.rider_id and order.status == 'out_for_delivery':
        loc = getattr(order.rider, 'rider_location', None)
        if loc:
            data['rider_lat'] = loc.latitude
            data['rider_lng'] = loc.longitude
            data['rider_updated_at'] = loc.updated_at.isoformat()  # lets the page flag stale positions
    return JsonResponse(data)
