"""Money summaries shared by the shop Payments page and the admin Finance page.

All amounts are in ₱ (Order.total_amount is stored in pesos; PayMongo refunds are centavos and are
converted here). "Gross" counts paid orders only; refunded orders drop out because their
payment_status becomes 'refunded', and the refund itself shows up as outgoing money.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

MONEY = DecimalField(max_digits=14, decimal_places=2)
ZERO = Value(Decimal('0.00'), output_field=MONEY)
PERIODS = (7, 30, 90)

COMMISSION = ExpressionWrapper(
    F('total_amount') * Coalesce(F('commission_rate'), Value(Decimal('0'), output_field=MONEY))
    / Value(Decimal('100'), output_field=MONEY),
    output_field=MONEY,
)


def _money(value):
    return Decimal(value or 0).quantize(Decimal('0.01'))


def current_commission_rate():
    from payments.models import PlatformSettings
    return PlatformSettings.current().commission_percent


def period_from_request(request, default=30):
    try:
        days = int(request.GET.get('days', default))
    except (TypeError, ValueError):
        days = default
    if days not in PERIODS:
        days = default
    return days, timezone.now() - timedelta(days=days)


def summarize(orders):
    """Totals for an Order queryset (already filtered to a shop and/or period)."""
    from orders.models import Order
    from payments.models import Refund

    paid = orders.filter(payment_status='paid')
    agg = paid.aggregate(
        gross=Coalesce(Sum('total_amount'), ZERO, output_field=MONEY),
        commission=Coalesce(Sum(COMMISSION), ZERO, output_field=MONEY),
        cash=Coalesce(Sum('total_amount', filter=Q(payment_method__in=Order.CASH_METHODS)), ZERO, output_field=MONEY),
        count=Count('id'),
    )
    outstanding = (
        orders.exclude(payment_status__in=['paid', 'refunded'])
        .exclude(status__in=['cancelled', 'declined'])
        .aggregate(total=Coalesce(Sum('total_amount'), ZERO, output_field=MONEY), n=Count('id'))
    )
    refunded_centavos = (
        Refund.objects.filter(order__in=orders, status='succeeded').aggregate(s=Sum('amount'))['s'] or 0
    )
    gross, commission, cash = _money(agg['gross']), _money(agg['commission']), _money(agg['cash'])
    return {
        'gross': gross,
        'commission': commission,
        'net': gross - commission,
        'cash': cash,
        'online': gross - cash,
        'paid_count': agg['count'],
        'outstanding': _money(outstanding['total']),
        'outstanding_count': outstanding['n'],
        'refunds': _money(Decimal(refunded_centavos) / 100),
    }


def daily_series(orders, days):
    """Per-day gross and net (after commission) for paid orders, with empty days filled in."""
    rows = (
        orders.filter(payment_status='paid')
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(gross=Sum('total_amount'), commission=Sum(COMMISSION))
        .order_by('day')
    )
    by_day = {r['day']: r for r in rows}
    today = timezone.localdate()
    labels, gross, net = [], [], []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        row = by_day.get(day)
        g = _money(row['gross']) if row else Decimal('0')
        c = _money(row['commission']) if row else Decimal('0')
        labels.append(day.strftime('%b %d'))
        gross.append(float(g))
        net.append(float(g - c))
    return {'labels': labels, 'gross': gross, 'net': net}


def per_shop(orders):
    """Paid totals per shop, biggest first (admin Finance page)."""
    rows = (
        orders.filter(payment_status='paid')
        .values('shop_id', 'shop__name')
        .annotate(count=Count('id'), gross=Sum('total_amount'), commission=Sum(COMMISSION))
        .order_by('-gross')
    )
    return [
        {'shop_id': r['shop_id'], 'name': r['shop__name'], 'count': r['count'],
         'gross': _money(r['gross']), 'commission': _money(r['commission']),
         'net': _money(r['gross']) - _money(r['commission'])}
        for r in rows
    ]
