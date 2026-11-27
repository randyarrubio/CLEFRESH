"""Kiosk / POS: walk-in orders taken at the shop counter.

The laundry is weighed on the spot, so the price is final when the ticket is printed. The order
then follows the normal processing steps (wash & fold → ready) and is claimed at the counter,
where any unpaid balance is collected. Walk-ins carry no platform commission (CLEFRESH didn't
bring the customer); linking a customer's CLEFRESH email lets them earn loyalty points.
"""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from orders.models import Order, OrderService, OrderStatusLog
from .access import shop_staff_required
from .models import Promotion, Service

User = get_user_model()
MAX_KG = Decimal('200')
MAX_PIECES = 1000
POS_PAYMENTS = [
    ('counter_paid', 'Cash — paid now'),
    ('gcash_paid', 'GCash at counter — paid now'),
    ('counter_unpaid', 'Pay when claiming'),
]


def _pos_context(shop, form=None):
    today = timezone.localdate()
    walk_ins = Order.objects.filter(shop=shop, is_walk_in=True)
    return {
        'shop': shop,
        'services': shop.services.exclude(name__in=Service.LEGACY_SERVICE_NAMES),
        'pos_payments': POS_PAYMENTS,
        'form': form or {},
        'ready_orders': walk_ins.filter(status='ready').order_by('updated_at'),
        'in_progress': walk_ins.filter(status__in=['picked_up', 'wash_fold']).order_by('created_at'),
        'today_count': walk_ins.filter(created_at__date=today).count(),
    }


def _parse_lines(request, services):
    lines, subtotal = [], Decimal('0.00')
    for svc in services:
        weight, qty = Decimal('0'), 0
        if svc.is_weighed:
            raw = request.POST.get(f'weight_{svc.id}', '').strip()
            if raw:
                try:
                    weight = Decimal(raw)
                    if not weight.is_finite() or weight < 0 or weight > MAX_KG:
                        raise InvalidOperation
                    weight = weight.quantize(Decimal('0.01'))
                except (InvalidOperation, ValueError):
                    raise ValueError(f'{svc.get_name_display()}: enter a weight between 0 and {MAX_KG} kg.')
        if svc.price_per_piece:
            raw = request.POST.get(f'qty_{svc.id}', '').strip() or '0'
            if not raw.isdigit() or int(raw) > MAX_PIECES:
                raise ValueError(f'{svc.get_name_display()}: enter a valid piece count.')
            qty = int(raw)
        line_total = Decimal('0.00')
        if svc.is_weighed and weight:
            line_total += svc.weight_charge(weight)          # per load (rounded up) or per kg
        if svc.price_per_piece and qty:
            line_total += svc.price_per_piece * qty
        if line_total > 0:
            lines.append((svc, weight or None, qty, line_total))
            subtotal += line_total
    if not lines:
        raise ValueError('Add at least one service with a weight or piece count.')
    if subtotal > Decimal('50000.00'):
        raise ValueError('Order total exceeds the maximum allowed amount (₱50,000).')
    return lines, subtotal


@shop_staff_required
def pos_view(request):
    shop = request.staff_shop
    if request.method != 'POST':
        return render(request, 'shop_dashboard/pos.html', _pos_context(shop))

    form = {k: v for k, v in request.POST.items() if k != 'csrfmiddlewaretoken'}
    name = request.POST.get('walk_in_name', '').strip()[:120]
    phone = request.POST.get('walk_in_phone', '').strip()[:30]
    email = request.POST.get('customer_email', '').strip()
    payment = request.POST.get('payment', '')
    notes = request.POST.get('notes', '').strip()[:500]
    services = list(shop.services.exclude(name__in=Service.LEGACY_SERVICE_NAMES))
    error = None
    customer = None
    if not name:
        error = "Enter the customer's name for the claim ticket."
    elif payment not in dict(POS_PAYMENTS):
        error = 'Choose how the customer is paying.'
    elif email:
        customer = User.objects.filter(email__iexact=email, role='customer', is_active=True).first()
        if not customer:
            error = 'No CLEFRESH customer account uses that email. Leave it blank for a walk-in without an account.'
    if not error:
        try:
            lines, subtotal = _parse_lines(request, services)
        except ValueError as exc:
            error = str(exc)
    if error:
        messages.error(request, error)
        return render(request, 'shop_dashboard/pos.html', _pos_context(shop, form))

    promotion, discount = Promotion.best_for(shop, subtotal)
    total = subtotal - discount
    method, status = {'counter_paid': ('counter', 'paid'), 'gcash_paid': ('gcash', 'paid'),
                      'counter_unpaid': ('counter', 'unpaid')}[payment]
    now = timezone.now()
    staff = request.user.get_full_name() or request.user.email
    with transaction.atomic():
        order = Order.objects.create(
            customer=customer, shop=shop, is_walk_in=True, walk_in_name=name, walk_in_phone=phone,
            pickup_address=f'Walk-in at {shop.name}', delivery_address='Claim at the counter',
            pickup_datetime=now, status='picked_up', payment_method=method, payment_status=status,
            subtotal_amount=subtotal, discount_amount=discount, total_amount=total,
            promotion=promotion, promotion_title=promotion.title if promotion else '',
            price_confirmed_at=now, price_confirmed_by=request.user,
            commission_rate=Decimal('0'), notes=notes,
        )
        OrderService.objects.bulk_create([
            OrderService(order=order, service=svc, weight_kg=weight, quantity=qty, subtotal=line_total,
                         loads=svc.loads_for(weight) if weight else None)
            for svc, weight, qty, line_total in lines
        ])
        OrderStatusLog.objects.create(order=order, status='picked_up',
                                      note=f'Walk-in received at the counter by {staff}.'
                                           + (f' Paid ₱{total:,.2f} ({order.get_payment_method_display()}).'
                                              if status == 'paid' else ''))
    messages.success(request, f'Walk-in order #{order.id} created for {name} — ₱{total:,.2f}.')
    return redirect('pos_receipt', order_id=order.id)


@shop_staff_required
def pos_receipt_view(request, order_id):
    order = get_object_or_404(Order, id=order_id, shop=request.staff_shop, is_walk_in=True)
    return render(request, 'shop_dashboard/pos_receipt.html', {
        'shop': request.staff_shop, 'order': order,
        'lines': order.order_services.select_related('service'),
    })


@shop_staff_required
def pos_claim_view(request, order_id):
    """Customer collects a ready walk-in order; any unpaid balance is collected now."""
    if request.method != 'POST':
        return redirect('shop_pos')
    order = get_object_or_404(Order, id=order_id, shop=request.staff_shop, is_walk_in=True)
    if order.status != 'ready':
        messages.error(request, f'Order #{order.id} is not ready to claim yet.')
        return redirect('shop_pos')
    fields = {'status': 'delivered', 'updated_at': timezone.now()}
    note = 'Claimed at the counter.'
    if order.payment_status != 'paid':
        if request.POST.get('cash_collected') != 'on':
            messages.error(request, f'Confirm you collected ₱{order.total_amount:,.2f} before handing over order #{order.id}.')
            return redirect('shop_pos')
        fields['payment_status'] = 'paid'
        note += f' Cash payment of ₱{order.total_amount:,.2f} collected.'
    staff = request.user.get_full_name() or request.user.email
    with transaction.atomic():
        if not Order.objects.filter(pk=order.pk, status='ready').update(**fields):
            messages.error(request, 'This order was just updated. Please refresh and try again.')
            return redirect('shop_pos')
        OrderStatusLog.objects.create(order=order, status='delivered', note=f'{note} ({staff})')
    from orders import loyalty
    loyalty.award(order.id)
    messages.success(request, f'Order #{order.id} claimed by {order.customer_label}.')
    return redirect('shop_pos')
