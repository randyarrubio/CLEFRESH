"""Fill in map coordinates that are missing (orders booked or shops saved while geocoding failed).

    python manage.py geocode_missing            # active orders + shops without coordinates
    python manage.py geocode_missing --all      # include delivered/cancelled orders too
    python manage.py geocode_missing --dry-run
    python manage.py geocode_missing --recheck  # also clear order pins over 50 km from their shop, then retry

Uses utils.geocode (Google if a working key is set, otherwise OpenStreetMap at 1 request/second),
so a large backlog takes a while; results are cached, so repeated addresses are instant. Order
pins more than MAX_KM_FROM_SHOP from the shop are rejected (a vague address matched elsewhere).
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from orders.models import Order
from shops.models import Shop
from utils.geocode import MAX_KM_FROM_SHOP, distance_km, geocode_address, geocode_near


class Command(BaseCommand):
    help = 'Geocode orders and shops that have no map coordinates yet.'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='Include finished (delivered/cancelled/declined) orders.')
        parser.add_argument('--dry-run', action='store_true', help='Show what would be updated without saving.')
        parser.add_argument('--recheck', action='store_true',
                            help=f'Clear order pins more than {MAX_KM_FROM_SHOP} km from the shop, then retry them.')

    def handle(self, *args, all=False, dry_run=False, recheck=False, **options):
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

        cleared = set()
        if recheck:
            for order in Order.objects.select_related('shop').exclude(pickup_lat__isnull=True):
                shop = order.shop
                if shop.latitude is None or shop.longitude is None:
                    continue
                points = [(order.pickup_lat, order.pickup_lng), (order.delivery_lat, order.delivery_lng)]
                if any(lat is not None and lng is not None
                       and distance_km(lat, lng, shop.latitude, shop.longitude) > MAX_KM_FROM_SHOP
                       for lat, lng in points):
                    cleared.add(order.pk)
                    self.stdout.write(f'  order #{order.id}: pin over {MAX_KM_FROM_SHOP} km from the shop, cleared')
                    if not dry_run:
                        Order.objects.filter(pk=order.pk).update(pickup_lat=None, pickup_lng=None,
                                                                 delivery_lat=None, delivery_lng=None)
            self.stdout.write(f'{len(cleared)} implausible order pin(s) cleared.')

        orders = Order.objects.filter(is_walk_in=False).filter(
            Q(pickup_lat__isnull=True) | Q(delivery_lat__isnull=True) | Q(pk__in=cleared))
        if not all:
            orders = orders.exclude(status__in=['delivered', 'cancelled', 'declined'])
        order_done = 0
        for order in orders.select_related('shop').order_by('-created_at'):
            ref = (order.shop.latitude, order.shop.longitude)
            p = geocode_near(order.pickup_address, *ref)
            d = p if order.delivery_address == order.pickup_address else geocode_near(order.delivery_address, *ref)
            if p[0] is None and d[0] is None:
                self.stdout.write(f'  order #{order.id}: no address found within {MAX_KM_FROM_SHOP} km of the shop')
                continue
            order_done += 1
            self.stdout.write(f'  order #{order.id}: pickup {p}, delivery {d}')
            if not dry_run:
                Order.objects.filter(pk=order.pk).update(pickup_lat=p[0], pickup_lng=p[1],
                                                         delivery_lat=d[0], delivery_lng=d[1])
        prefix = '[dry run] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(f'{prefix}{shop_done} shop(s) and {order_done} order(s) geocoded.'))
