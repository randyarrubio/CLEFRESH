from django import template


register = template.Library()


@register.filter
def split(value, separator):
    if value is None:
        return []
    return str(value).split(separator)


@register.filter
def humanize_status(value):
    return str(value).replace('_', ' ').title()


@register.filter
def centavos_to_php(value):
    """PayMongo amounts are stored in centavos; convert to pesos for display."""
    from decimal import Decimal
    try:
        return Decimal(int(value)) / 100
    except (TypeError, ValueError):
        return value


@register.filter
def order_total(order):
    """'₱1,234.00', or 'To be weighed' while a weigh-at-shop order has no confirmed price."""
    if getattr(order, 'price_pending', False):
        return 'To be weighed'
    try:
        return f'₱{order.total_amount:,.2f}'
    except (TypeError, ValueError):
        return '—'
