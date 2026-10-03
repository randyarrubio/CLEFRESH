from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta, date, time


class Command(BaseCommand):
    help = 'Seed the database with sample data for development/demo'

    def handle(self, *args, **options):
        from accounts.models import RiderProfile, User
        from shops.models import Shop, Service, DeliverySlot
        from orders.models import Order, OrderService, OrderStatusLog
        from notifications.models import Notification

        self.stdout.write('Seeding data...')

        # Admin
        admin, _ = User.objects.get_or_create(
            email='admin@clefresh.com',
            defaults={'username': 'admin@clefresh.com', 'first_name': 'Admin', 'last_name': 'CLEFRESH',
                      'role': 'admin', 'is_staff': True, 'is_superuser': True}
        )
        if not admin.check_password('admin1234'):
            admin.set_password('admin1234')
            admin.save()
        self.stdout.write(f'  Admin: admin@clefresh.com / admin1234')

        # Shop owners
        owner1, _ = User.objects.get_or_create(
            email='owner1@clefresh.com',
            defaults={'username': 'owner1@clefresh.com', 'first_name': 'Maria', 'last_name': 'Santos', 'role': 'shop_owner'}
        )
        if not owner1.check_password('owner1234'):
            owner1.set_password('owner1234')
            owner1.save()

        owner2, _ = User.objects.get_or_create(
            email='owner2@clefresh.com',
            defaults={'username': 'owner2@clefresh.com', 'first_name': 'Jose', 'last_name': 'Reyes', 'role': 'shop_owner'}
        )
        if not owner2.check_password('owner1234'):
            owner2.set_password('owner1234')
            owner2.save()

        # Riders
        rider1, _ = User.objects.get_or_create(
            email='rider1@clefresh.com',
            defaults={'username': 'rider1@clefresh.com', 'first_name': 'Juan', 'last_name': 'dela Cruz', 'role': 'rider', 'contact_number': '09171234567'}
        )
        if not rider1.check_password('rider1234'):
            rider1.set_password('rider1234')
            rider1.save()

        rider2, _ = User.objects.get_or_create(
            email='rider2@clefresh.com',
            defaults={'username': 'rider2@clefresh.com', 'first_name': 'Ana', 'last_name': 'Gomez', 'role': 'rider', 'contact_number': '09189876543'}
        )
        if not rider2.check_password('rider1234'):
            rider2.set_password('rider1234')
            rider2.save()

        # Customers
        customer1, _ = User.objects.get_or_create(
            email='customer1@clefresh.com',
            defaults={'username': 'customer1@clefresh.com', 'first_name': 'Liza', 'last_name': 'Marcos', 'role': 'customer',
                      'address': 'Cantilan, Surigao del Sur', 'contact_number': '09201112222'}
        )
        if not customer1.check_password('customer1234'):
            customer1.set_password('customer1234')
            customer1.save()

        customer2, _ = User.objects.get_or_create(
            email='customer2@clefresh.com',
            defaults={'username': 'customer2@clefresh.com', 'first_name': 'Carlo', 'last_name': 'Bautista', 'role': 'customer',
                      'address': 'Cantilan, Surigao del Sur', 'contact_number': '09203334444'}
        )
        if not customer2.check_password('customer1234'):
            customer2.set_password('customer1234')
            customer2.save()

        customer3, _ = User.objects.get_or_create(
            email='sso.customer@gmail.com',
            defaults={'username': 'sso.customer@gmail.com', 'first_name': 'Google', 'last_name': 'User',
                      'role': 'customer', 'sso_provider': 'google'}
        )

        self.stdout.write(f'  Users created: 2 owners, 2 riders, 3 customers')

        # Seed accounts skip the email-code flow; unverified shop owners are
        # otherwise bounced to /auth/verify-email/ by @role_required.
        User.objects.filter(
            pk__in=[u.pk for u in (admin, owner1, owner2, rider1, rider2, customer1, customer2, customer3)]
        ).update(email_verified=True)
        # Seeded riders are pre-approved so the demo job board works out of the box.
        for rider in (rider1, rider2):
            RiderProfile.objects.update_or_create(
                user=rider,
                defaults={'is_submitted': True, 'submitted_at': timezone.now(), 'approval_status': 'approved',
                          'reviewed_at': timezone.now(), 'vehicle_type': 'motorcycle'},
            )

        # Shops
        shop1, _ = Shop.objects.get_or_create(
            owner=owner1,
            name='Fresh & Clean Laundry',
            defaults={
                'description': 'Professional laundry services with fast turnaround. Serving Cantilan since 2020.',
                'address': 'Purok 1, Cantilan, Surigao del Sur',
                'contact': '09171234567',
                'latitude': 9.3401,
                'longitude': 125.9840,
                'status': 'approved',
                'max_orders_per_day': 20,
                'operating_hours_start': time(8, 0),
                'operating_hours_end': time(18, 0),
            }
        )

        shop2, _ = Shop.objects.get_or_create(
            owner=owner2,
            name='Sparkling Suds Laundromat',
            defaults={
                'description': 'Premium dry cleaning and laundry. Pickup and delivery available.',
                'address': 'Purok 3, Cantilan, Surigao del Sur',
                'contact': '09189876543',
                'latitude': 9.3350,
                'longitude': 125.9800,
                'status': 'approved',
                'max_orders_per_day': 15,
                'operating_hours_start': time(7, 0),
                'operating_hours_end': time(19, 0),
            }
        )

        # Services
        for shop, services in [
            (shop1, [('wash', 60, None), ('dry', 50, None), ('fold', 30, None), ('iron', None, 15)]),
            (shop2, [('wash', 70, None), ('dry', 60, None), ('fold', 35, None), ('dry_clean', None, 120)]),
        ]:
            for name, ppkg, ppiece in services:
                Service.objects.get_or_create(
                    shop=shop, name=name,
                    defaults={'price_per_kg': ppkg, 'price_per_piece': ppiece}
                )

        # Delivery slots
        today = timezone.now().date()
        for shop in [shop1, shop2]:
            for day_offset in range(1, 8):
                slot_date = today + timedelta(days=day_offset)
                for time_start, time_end in [(time(9, 0), time(11, 0)), (time(14, 0), time(16, 0))]:
                    DeliverySlot.objects.get_or_create(
                        shop=shop, date=slot_date, time_start=time_start,
                        defaults={'time_end': time_end, 'max_capacity': 5, 'current_bookings': 0}
                    )

        self.stdout.write('  Shops, services, and delivery slots created')

        # Sample orders
        wash_svc = shop1.services.filter(name='wash').first()
        dry_svc = shop1.services.filter(name='dry').first()

        if wash_svc and not Order.objects.filter(customer=customer1, shop=shop1, status='delivered').exists():
            order_delivered = Order.objects.create(
                customer=customer1,
                shop=shop1,
                rider=rider1,
                pickup_address='123 Mahogany St, Cantilan',
                delivery_address='123 Mahogany St, Cantilan',
                pickup_lat=9.3410,
                pickup_lng=125.9850,
                delivery_lat=9.3410,
                delivery_lng=125.9850,
                pickup_datetime=timezone.now() - timedelta(days=3),
                payment_method='gcash',
                payment_status='paid',
                total_amount=270,
                status='delivered',
                paymongo_payment_intent_id='pi_test_sample_delivered',
            )
            OrderService.objects.create(order=order_delivered, service=wash_svc, quantity=1, weight_kg=3, subtotal=180)
            if dry_svc:
                OrderService.objects.create(order=order_delivered, service=dry_svc, quantity=1, weight_kg=3, subtotal=90)
            for status in ['pending', 'accepted', 'picked_up', 'washing', 'drying', 'folding', 'ready', 'out_for_delivery', 'delivered']:
                OrderStatusLog.objects.create(order=order_delivered, status=status)

        if wash_svc and not Order.objects.filter(customer=customer2, shop=shop1, status='pending').exists():
            order_pending = Order.objects.create(
                customer=customer2,
                shop=shop1,
                pickup_address='456 Pine Ave, Cantilan',
                delivery_address='456 Pine Ave, Cantilan',
                pickup_lat=9.3380,
                pickup_lng=125.9820,
                delivery_lat=9.3380,
                delivery_lng=125.9820,
                pickup_datetime=timezone.now() + timedelta(hours=2),
                payment_method='cod',
                payment_status='unpaid',
                total_amount=180,
                status='pending',
            )
            OrderService.objects.create(order=order_pending, service=wash_svc, quantity=1, weight_kg=3, subtotal=180)
            OrderStatusLog.objects.create(order=order_pending, status='pending', note='Order placed by customer.')

        # Notifications
        Notification.objects.get_or_create(
            user=customer1,
            message='Welcome to CLEFRESH! Browse shops to place your first order.',
            defaults={'link': '/shops/'}
        )
        Notification.objects.get_or_create(
            user=owner1,
            message='Your shop "Fresh & Clean Laundry" has been approved!',
            defaults={'link': '/shop-dashboard/', 'is_read': True}
        )

        self.stdout.write('  Sample orders and notifications created')
        self.stdout.write(self.style.SUCCESS('\nSeed data complete! Login credentials:'))
        self.stdout.write('  admin@clefresh.com       / admin1234     (Admin)')
        self.stdout.write('  owner1@clefresh.com      / owner1234     (Shop Owner - Fresh & Clean)')
        self.stdout.write('  owner2@clefresh.com      / owner1234     (Shop Owner - Sparkling Suds)')
        self.stdout.write('  rider1@clefresh.com      / rider1234     (Rider)')
        self.stdout.write('  rider2@clefresh.com      / rider1234     (Rider)')
        self.stdout.write('  customer1@clefresh.com   / customer1234  (Customer)')
        self.stdout.write('  customer2@clefresh.com   / customer1234  (Customer)')
