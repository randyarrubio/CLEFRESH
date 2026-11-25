"""Fill in map coordinates that are missing (orders booked or shops saved while geocoding failed).

    python manage.py geocode_missing            # active orders + shops without coordinates
    python manage.py geocode_missing --all      # include delivered/cancelled orders too
    python manage.py geocode_missing --dry-run

Uses utils.geocode (Google if a working key is set, otherwise OpenStreetMap at 1 request/second),
so a large backlog takes a while; results are cached, so repeated addresses are instant.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from orders.models import Order
from shops.models import Shop
from utils.geocode import geocode_address


class Command(BaseCommand):
    help = 'Geocode orders and shops that have no map coordinates yet.'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='Include finished (delivered/cancelled/declined) orders.')
        parser.add_argument('--dry-run', action='store_true', help='Show what would be updated without saving.')

    def handle(self, *args, all=False, dry_run=False, **options):
        shops = Shop.objects.filter(Q(latitude__isnull=True) | Q(longitude__isnull=True)).exclude(address='')
        shop_done = 0
        for shop in shops:
            lat, lng = geocode_address(shop.address)
            if lat is None:
                self.stdout.write(f'  shop #{shop.id} {shop.name}: not found')
                continue
            shop_done += 1
            self.stdout.write(f'  shop #{shop.id} {shop.name}: {lat:.5f}, {lng:.5f}')
            if not dry_run:
                Shop.objects.filter(pk=shop.pk).update(latitude=lat, longitude=lng)

        orders = Order.objects.filter(is_walk_in=False).filter(
            Q(pickup_lat__isnull=True) | Q(delivery_lat__isnull=True))
        if not all:
            orders = orders.exclude(status__in=['delivered', 'cancelled', 'declined'])
        order_done = 0
        for order in orders.order_by('-created_at'):
            p = geocode_address(order.pickup_address)
            d = p if order.delivery_address == order.pickup_address else geocode_address(order.delivery_address)
            if p[0] is None and d[0] is None:
                self.stdout.write(f'  order #{order.id}: addresses not found')
                continue
            order_done += 1
            self.stdout.write(f'  order #{order.id}: pickup {p}, delivery {d}')
            if not dry_run:
                Order.objects.filter(pk=order.pk).update(pickup_lat=p[0], pickup_lng=p[1],
                                                         delivery_lat=d[0], delivery_lng=d[1])
        prefix = '[dry run] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(f'{prefix}{shop_done} shop(s) and {order_done} order(s) geocoded.'))
