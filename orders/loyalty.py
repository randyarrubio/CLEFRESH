"""Loyalty rewards: earn points on completed orders, spend them for ₱ off at booking.

Rules (settings.LOYALTY_*):
  * Earn floor(total paid / LOYALTY_PESOS_PER_POINT) points once an order is delivered AND paid.
  * Redeem at booking only when the price is final (not weigh-at-shop): 1 point = ₱1, capped at
    LOYALTY_MAX_REDEEM_PERCENT of the order and never below LOYALTY_MIN_CHARGE left to pay.
  * Cancelled/declined orders give spent points back; refunding a delivered order takes the earned
    points away again (never below a zero balance).
Every change is a LoyaltyEntry, unique per (order, kind), so each step is idempotent.
"""
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum

from notifications.models import Notification
from .models import LoyaltyEntry, Order


def balance(user):
    return max(LoyaltyEntry.objects.filter(user=user).aggregate(s=Sum('points'))['s'] or 0, 0)


def points_for(amount):
    return int(Decimal(amount) // settings.LOYALTY_PESOS_PER_POINT)


def max_redeemable(user_balance, total):
    """Whole points that may be spent on an order of `total` pesos."""
    total = Decimal(total)
    cap_percent = total * settings.LOYALTY_MAX_REDEEM_PERCENT / 100
    cap_min_charge = total - settings.LOYALTY_MIN_CHARGE
    return max(int(min(Decimal(user_balance), cap_percent, cap_min_charge)), 0)


def _entry(user, order, kind, points):
    try:
        with transaction.atomic():
            LoyaltyEntry.objects.create(user=user, order=order, kind=kind, points=points)
        return True
    except IntegrityError:
        return False          # already recorded for this order


def redeem(user, order, points):
    """Record points spent on a just-created order (caller holds the user row lock)."""
    if points > 0:
        _entry(user, order, 'redeem', -points)


def award(order_id):
    """Give points for a delivered + paid order. Safe to call repeatedly from any code path."""
    order = Order.objects.select_related('customer').filter(pk=order_id).first()
    if (not order or order.customer_id is None or order.status != 'delivered'
            or order.payment_status != 'paid' or order.customer.role != 'customer'):
        return 0
    points = points_for(order.total_amount)
    if points <= 0 or not _entry(order.customer, order, 'earn', points):
        return 0
    Notification.objects.create(
        user=order.customer, link='/orders/',
        message=f'You earned {points} loyalty point{"s" if points != 1 else ""} on order #{order.id}! '
                f'Use them for a discount on your next booking.',
    )
    return points


def restore(order):
    """Give back points spent on an order that was cancelled or declined."""
    if order.points_redeemed and order.customer_id:
        _entry(order.customer, order, 'restore', order.points_redeemed)


def revoke(order):
    """Take back points earned on an order that was refunded."""
    earned = LoyaltyEntry.objects.filter(order=order, kind='earn').values_list('points', flat=True).first()
    if earned and order.customer_id:
        _entry(order.customer, order, 'revoke', -min(earned, balance(order.customer)))
