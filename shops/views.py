import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import Http404, JsonResponse, QueryDict
from django.urls import reverse
from django.utils import timezone
from django.db import IntegrityError, transaction
from django.db.models import Count, Min, RestrictedError
from django.db.models import F, Q, Sum
from .models import Shop, Service, DeliverySlot
from accounts.decorators import role_required
from accounts.forms import RegisterForm

logger = logging.getLogger(__name__)


# Server-side order status transitions for shop owners. Keep in sync with the
# rider flow in maps/views.py.
VALID_TRANSITIONS = {
    'pending': {'accepted', 'declined'},
    'accepted': {'picked_up', 'declined'},
    # Processing steps are forward-only but skippable (e.g. an iron-only order goes straight to ready).
    'picked_up': {'washing', 'drying', 'folding', 'ready'},
    'washing': {'drying', 'folding', 'ready'},
    'drying': {'folding', 'ready'},
    'folding': {'ready'},
    'ready': {'out_for_delivery'},
    'out_for_delivery': {'delivered'},
}


SHOP_META_PREFIXES = {
    'tagline': 'Tagline: ',
    'minimum_load': 'Minimum load: ',
    'turnaround': 'Turnaround: ',
    'payments': 'Payments: ',
    'unclaimed_policy': 'Policy: ',
    'customer_notes': 'Customer notes: ',
}


def _shop_edit_context(shop):
    meta = {
        'tagline': shop.tagline or '',
        'minimum_load': shop.minimum_load_kg or '',
        'turnaround': shop.turnaround or '',
        'payments': shop.payments or [],
        'unclaimed_policy': shop.unclaimed_policy or '',
        'customer_notes': shop.customer_notes or '',
    }
    description_lines = []
    for line in (shop.description or '').splitlines():
        matched = False
        for key, prefix in SHOP_META_PREFIXES.items():
            if line.startswith(prefix):
                value = line[len(prefix):].strip()
                if meta.get(key):
                    matched = True
                    break
                if key == 'minimum_load':
                    value = value.removesuffix(' kg').strip()
                elif key == 'payments':
                    value = [item.strip() for item in value.split(',') if item.strip()]
                meta[key] = value
                matched = True
                break
        if not matched:
            description_lines.append(line)

    services = {service.name: service for service in shop.services.all()}
    return {
        'shop': shop,
        'shop_description': '\n'.join(description_lines).strip(),
        'shop_meta': meta,
        'selected_services': set(services.keys()),
        'service_prices': {
            'wash': services.get('wash').price_per_kg if services.get('wash') else '',
            'dry': services.get('dry').price_per_kg if services.get('dry') else '',
            'fold': services.get('fold').price_per_kg if services.get('fold') else '',
            'iron': services.get('iron').price_per_piece if services.get('iron') else '',
            'dry_clean': services.get('dry_clean').price_per_piece if services.get('dry_clean') else '',
        },
    }


def _save_shop_from_owner_form(request, shop, set_pending=False):
    from utils.geocode import geocode_address

    address = request.POST.get('address', '').strip()
    if not address:
        address = ', '.join(filter(None, [
            request.POST.get('street_address', '').strip(),
            request.POST.get('barangay', '').strip(),
            request.POST.get('city', '').strip(),
        ]))
    old_address = shop.address
    lat, lng = geocode_address(address) if address and address != old_address else (shop.latitude, shop.longitude)

    minimum_load = None
    if request.POST.get('minimum_load', '').strip():
        try:
            minimum_load = Decimal(request.POST.get('minimum_load', '').strip())
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError('Minimum load must be a valid number.')
        if minimum_load < 0:
            raise ValidationError('Minimum load cannot be negative.')

    payments = request.POST.getlist('payments')
    description_parts = [
        request.POST.get('description', '').strip(),
    ]

    shop.name = request.POST.get('name', '').strip()
    shop.description = '\n'.join(part for part in description_parts if part)
    shop.tagline = request.POST.get('tagline', '').strip()
    shop.address = address
    shop.contact = request.POST.get('contact', '').strip()
    shop.latitude = lat
    shop.longitude = lng
    shop.operating_hours_start = request.POST.get('hours_start') or '08:00'
    shop.operating_hours_end = request.POST.get('hours_end') or '18:00'
    shop.minimum_load_kg = minimum_load
    shop.turnaround = request.POST.get('turnaround', '').strip()
    shop.payments = payments
    shop.unclaimed_policy = request.POST.get('unclaimed_policy', '').strip()
    shop.customer_notes = request.POST.get('customer_notes', '').strip()
    if set_pending:
        shop.status = 'pending'
    if 'logo' in request.FILES:
        shop.logo = request.FILES['logo']
    if 'banner' in request.FILES:
        shop.banner = request.FILES['banner']
    if 'government_id' in request.FILES:
        shop.owner_government_id = request.FILES['government_id']
    if 'selfie_id' in request.FILES:
        shop.owner_selfie_with_id = request.FILES['selfie_id']
    shop.full_clean()
    shop.save()

    service_prices = {
        'wash': {'price_per_kg': request.POST.get('price_wash_kg') or None, 'price_per_piece': None},
        'dry': {'price_per_kg': request.POST.get('price_dry_kg') or None, 'price_per_piece': None},
        'fold': {'price_per_kg': request.POST.get('price_fold_kg') or None, 'price_per_piece': None},
        'iron': {'price_per_kg': None, 'price_per_piece': request.POST.get('price_iron_piece') or None},
        'dry_clean': {'price_per_kg': None, 'price_per_piece': request.POST.get('price_dry_clean_piece') or None},
    }
    selected_services = set(request.POST.getlist('services'))
    try:
        shop.services.exclude(name__in=selected_services).delete()
    except RestrictedError:
        raise ValidationError('A service you unchecked has existing orders and cannot be removed.')
    for service_name in selected_services:
        if service_name not in service_prices:
            continue
        existing_services = list(shop.services.filter(name=service_name).order_by('id'))
        service = existing_services[0] if existing_services else Service(shop=shop, name=service_name)
        service.price_per_kg = service_prices[service_name]['price_per_kg']
        service.price_per_piece = service_prices[service_name]['price_per_piece']
        service.full_clean()
        service.save()
        if len(existing_services) > 1:
            Service.objects.filter(id__in=[svc.id for svc in existing_services[1:]]).delete()


def _live_promos_prefetch():
    """Prefetch each shop's live promos into shop.live_promos_list (one query for all cards)."""
    from django.db.models import Prefetch
    from .models import Promotion
    return Prefetch('promotions', queryset=Promotion.objects.live().order_by('end_date'), to_attr='live_promos_list')


def home_view(request):
    from django.db.models import Avg
    # Annotated once here so shop cards don't run a rating/count query each.
    shops = (
        Shop.objects.filter(status='approved')
        .annotate(avg_rating=Avg('reviews__rating'), review_count=Count('reviews', distinct=True))
        .prefetch_related('services', _live_promos_prefetch())
        .order_by('-created_at')
    )
    register_form = RegisterForm()
    context = {'shops': shops, 'register_form': register_form}

    if not request.user.is_authenticated:
        # Landing-page stats: real platform figures only (no marketing placeholders).
        from orders.models import Order
        from orders.views import BOOKABLE_PAYMENT_METHODS
        context['landing_stats'] = {
            'shops': shops.count(),
            'service_types': len(Service.SERVICE_CHOICES),
            'payment_methods': len(BOOKABLE_PAYMENT_METHODS),
            'deliveries': Order.objects.filter(status='delivered').count(),
        }
        # The landing page shows 6 shops; fetch just those once (the template both slices and
        # truth-tests `shops`, which otherwise loaded every shop + prefetches twice).
        context['shops'] = list(shops[:6])

    if request.user.is_authenticated and request.user.role == 'rider':
        return redirect('rider_dashboard')

    if request.user.is_authenticated and request.user.role == 'shop_owner':
        return redirect('shop_dashboard')

    if request.user.is_authenticated and (request.user.role == 'admin' or request.user.is_superuser):
        return redirect('admin_dashboard')

    if request.user.is_authenticated and request.user.role == 'customer':
        customer_shops = shops
        q = request.GET.get('q', '').strip()
        service_filter = request.GET.get('service', '').strip()
        sort_filter = request.GET.get('sort', 'nearest').strip()

        if q:
            customer_shops = customer_shops.filter(
                Q(name__icontains=q) | Q(description__icontains=q) | Q(address__icontains=q)
            )
        if service_filter:
            customer_shops = customer_shops.filter(services__name=service_filter)
        if sort_filter == 'price':
            # Annotate instead of ordering across the services join, which duplicated shop cards.
            customer_shops = customer_shops.distinct().annotate(
                min_price=Min('services__price_per_kg')
            ).order_by(F('min_price').asc(nulls_last=True), 'name')
        elif sort_filter == 'rated':
            customer_shops = customer_shops.distinct().order_by(F('avg_rating').desc(nulls_last=True), 'name')
        else:
            customer_shops = customer_shops.distinct()

        context.update({
            'is_customer_home': True,
            'customer_shops': customer_shops,
            'customer_query': q,
            'customer_service_filter': service_filter,
            'customer_sort_filter': sort_filter,
            'customer_location': request.user.address or 'Your area',
        })

    elif request.user.is_authenticated and request.user.role == 'shop_owner':
        from orders.models import Order

        owner_shops = request.user.shops.prefetch_related('services', 'delivery_slots').order_by('-created_at')
        owner_shop = owner_shops.filter(status='approved').first() or owner_shops.first()
        today = timezone.localdate()
        owner_orders = Order.objects.none()
        next_slots = DeliverySlot.objects.none()
        pending_orders = active_orders = total_orders = 0
        today_revenue = 0

        if owner_shop:
            owner_orders = Order.objects.filter(shop=owner_shop).select_related('customer', 'rider')
            pending_orders = owner_orders.filter(status='pending').count()
            active_orders = owner_orders.filter(
                status__in=['accepted', 'picked_up', 'washing', 'drying', 'folding', 'ready', 'out_for_delivery']
            ).count()
            total_orders = owner_orders.count()
            today_revenue = (
                owner_orders.filter(created_at__date=today, payment_status='paid').aggregate(total=Sum('total_amount'))['total']
                or 0
            )
            next_slots = owner_shop.delivery_slots.filter(date__gte=today).order_by('date', 'time_start')[:4]

        context.update({
            'is_shop_owner_home': True,
            'owner_shop': owner_shop,
            'owner_orders': owner_orders.order_by('-created_at')[:6],
            'owner_pending_orders': pending_orders,
            'owner_active_orders': active_orders,
            'owner_total_orders': total_orders,
            'owner_today_revenue': today_revenue,
            'owner_next_slots': next_slots,
        })

    return render(request, 'home.html', context)


INFO_PAGES = {
    'user-guide': {
        'title': 'User Guide',
        'eyebrow': 'Getting Started',
        'intro': 'A complete walkthrough of CLEFRESH for customers, shop owners, riders, and administrators. Use this as your reference for every feature in the platform.',
        'sections': [
            {
                'heading': 'Creating an account',
                'body': [
                    'Click Register in the top navigation and choose a role: customer, shop owner, or rider. Shop owners confirm their email with a 6-digit code (valid for 10 minutes); customers and riders can start right away.',
                    'You can also sign in with your Google account using "Continue with Google". First-time Google sign-ins go through a short completion step to choose your role and add contact details.',
                    'Forgot your password? Use the "Forgot password" link on the login screen. A reset link is emailed to you and stays valid for 3 days — check your spam folder if it does not arrive within a few minutes.',
                    'Update your name, contact number, address, and profile picture from your Profile page. You can also turn off email notifications there (password resets and verification codes are always sent).',
                ],
            },
            {
                'heading': 'Customers — finding a shop',
                'body': [
                    'The home page lists approved shops. Search by name, description, or address, filter by service type (wash, dry, fold, iron, dry clean) or shops that are open now, and sort by newest, top rated, lowest price, or name. Shops are also pinned on the map.',
                    'Click any shop card to open its page. From there you can view services, pricing (per kilogram or per piece), available delivery slots, customer reviews, active promos, and the shop\'s How It Works and About pages.',
                ],
            },
            {
                'heading': 'Customers — placing an order',
                'body': [
                    'On the shop page, select the services you want. You do not need to weigh your laundry: per-kilo services (wash, dry, fold) are weighed by the shop. For per-piece services such as ironing, enter the number of pieces. Optional add-ons such as express or hypoallergenic handling can be added. Choose a delivery slot from the available date and time windows — full slots cannot be selected.',
                    'Enter your pickup and delivery addresses and pick a payment method: cash on delivery (default), GCash, GrabPay, or Maya. Orders are limited to ₱50,000.',
                    'A rider collects your laundry and drops it at the shop. The shop then weighs it and confirms the final price, applying the best available promo. You are notified with the weight and total, and can view a photo of the scale if the shop added one.',
                    'Online payments are made after weighing, through PayMongo, and must be completed before your laundry is delivered. You will get payment reminders, and you can switch to cash on delivery at any time before delivery. If payment is still not made after 72 hours, the order switches to cash on delivery automatically. If you chose cash on delivery, you can still pay online after weighing, up until the rider heads out for delivery.',
                ],
            },
            {
                'heading': 'Customers — tracking and managing orders',
                'body': [
                    'My Orders shows every order with its current status and payment state. The flow is pending → accepted → picked up → washing → drying → folding → ready → out for delivery → delivered; the shop may skip steps that do not apply to your services. Each change is logged with a timestamp in the order history.',
                    'When the order is out for delivery, the rider\'s live location appears on a map and refreshes every few seconds. Location is shared only during this status, for privacy.',
                    'You can cancel an order while it is still pending. Once the shop has accepted it, cancellation is no longer available from the customer side — contact the shop if you need changes. If a paid order is cancelled or declined by the shop, the payment is refunded automatically.',
                    'After an order is delivered, you can leave one rating (1–5 stars) and a written review for that order. Reviews appear on the shop\'s public page and cannot be edited after submitting.',
                ],
            },
            {
                'heading': 'Shop owners — registering a shop',
                'body': [
                    'After verifying your email, submit your shop details along with a government ID and a selfie holding that ID. These documents are private — only you and administrators can view them.',
                    'Your shop starts in "pending" status and becomes visible to customers only after an administrator approves it. If it is rejected, you can correct the details and resubmit.',
                    'Once approved, edit the shop profile to keep the address, contact details, operating hours, and description current — edits do not require re-approval. The address is geocoded automatically so the shop appears correctly on the map.',
                    'A suspended shop is hidden from customers and its dashboard is locked until an administrator reinstates it.',
                ],
            },
            {
                'heading': 'Shop owners — services, slots, and promos',
                'body': [
                    'Use the Services page to add or update what you offer. Each service has a name, type (wash, dry, fold, iron, dry clean), pricing model (per kilogram or per piece), and price in pesos.',
                    'Use the Delivery Slots page to publish pickup windows. Set the date, start and end time, and maximum capacity. As customers book, the booking count is tracked and full slots can no longer be selected.',
                    'Use the Promos page to create discounts, switch them on or off, or delete them. The best active promo is applied automatically to qualifying orders.',
                ],
            },
            {
                'heading': 'Shop owners — handling orders',
                'body': [
                    'The Orders page lists every order placed at your shop. Filter by status to focus on what needs attention, and click an order to open its details.',
                    'From order details you can accept or decline a pending order, move it through the laundry workflow, and assign a rider (approved riders can also claim accepted orders themselves). Status changes are enforced — you can only move forward to a valid next step.',
                    'When the laundry arrives at your shop, weigh it and enter the kilos in the "Weigh laundry & confirm price" card, optionally with a photo of the scale. This sets the final price and notifies the customer to pay. You can correct it until the customer pays. An order cannot go out for delivery until it is weighed and, for online payment, paid.',
                    'The customer is notified automatically at every status change.',
                ],
            },
            {
                'heading': 'Shop owners — analytics and payments',
                'body': [
                    'The Analytics page shows revenue, order counts, and trends over the last 7, 30, or 90 days compared with the previous period. Revenue counts paid orders only.',
                    'The Payments page lists PayMongo transactions for your shop\'s orders and lets you issue a refund when needed. Amounts are shown in pesos.',
                ],
            },
            {
                'heading': 'Riders — getting started',
                'body': [
                    'After registering as a rider, complete onboarding by uploading your government ID (front and back), driver\'s license (front and back), vehicle CR/OR, and a selfie holding your ID.',
                    'An administrator reviews your documents before you can take jobs. Until then your dashboard shows a verification notice. Uploading new documents later sends your account back for review.',
                    'Once approved, the Rider Dashboard lists open jobs you can claim, plus the tasks assigned to you with pickup and delivery addresses, customer contact details, and shop information.',
                ],
            },
            {
                'heading': 'Riders — completing a task',
                'body': [
                    'Pickup: collect the laundry from the customer and upload a pickup photo. This marks the order as picked up.',
                    'Drop-off: bring the laundry to the shop and upload a drop-off photo. The shop and customer are notified that the laundry has arrived.',
                    'Delivery: once the order is ready (weighed and, for online payment, paid), set it to out for delivery. Your location is shared with the customer while you are on the way. Upload a delivery photo at handover to mark the order delivered; for cash on delivery, this also records the payment as received.',
                ],
            },
            {
                'heading': 'Notifications',
                'body': [
                    'The bell icon in the top navigation shows in-app notifications for events relevant to your role — new orders, status changes, rider assignments, payments and refunds, shop approvals, and admin announcements.',
                    'Important updates are also sent by email unless you have turned email notifications off in your profile. Open the bell to mark notifications as read or jump to the related order.',
                ],
            },
            {
                'heading': 'Administrators',
                'body': [
                    'The admin panel covers shop approvals, rider verification, user management, order oversight, transaction history, business analytics, and broadcast announcements.',
                    'Shops can be approved, rejected, suspended (hidden from customers and locked from the owner dashboard, but reinstatable), or permanently deleted. Deletion also removes the shop\'s orders, so use it carefully.',
                    'Riders are approved or rejected (with a reason) after their documents are reviewed. Rejecting a rider releases any jobs they have not yet picked up. User accounts can be deleted, and inactive accounts can be reactivated.',
                    'Broadcast announcements are sent from the Broadcast nav link and reach every user as an in-app notification.',
                ],
            },
            {
                'heading': 'Need more help?',
                'body': [
                    'Visit the Help Center for quick answers grouped by role.',
                    'Review the Privacy Policy, Terms of Service, and Refund and Cancellation pages for the full policy details.',
                ],
            },
        ],
    },
    'help': {
        'title': 'Help Center',
        'eyebrow': 'Support',
        'intro': 'Start with the fastest path for common customer, shop, and rider issues.',
        'sections': [
            {
                'heading': 'For customers',
                'body': [
                    'Use My Orders to review order status, payment state, pickup details, and rider updates.',
                    'If an order is still pending, you can cancel it from the order details page.',
                    'For item-specific concerns, check the shop information shown on the shop or order page.',
                ],
            },
            {
                'heading': 'For shop owners',
                'body': [
                    'Use the Shop Dashboard to manage orders, update statuses, assign riders, and monitor payments.',
                    'Keep delivery slots and service pricing current so customers see accurate availability.',
                ],
            },
            {
                'heading': 'For riders',
                'body': [
                    'Use the Rider Dashboard to view assigned tasks, upload proof photos, and update delivery progress.',
                    'Check pickup and delivery locations carefully before changing the final status of an order.',
                ],
            },
        ],
    },
    'privacy': {
        'title': 'Privacy Policy',
        'eyebrow': 'Legal',
        'intro': 'This page explains the kinds of account, order, and location-related information used to operate CLEFRESH.',
        'sections': [
            {
                'heading': 'Information we use',
                'body': [
                    'Account details such as name, email address, role, and optional contact information.',
                    'Order details such as pickup address, delivery address, selected services, schedule, and payment status.',
                    'Operational data such as shop details, rider assignment updates, and notification history.',
                ],
            },
            {
                'heading': 'Why we use it',
                'body': [
                    'To create and manage accounts, process bookings, coordinate pickups and deliveries, and provide status updates.',
                    'To support payments, refunds, fraud prevention, and platform administration.',
                ],
            },
            {
                'heading': 'Data handling',
                'body': [
                    'Only collect information that is needed to deliver the service and operate the platform.',
                    'Review your profile details regularly and avoid placing sensitive information in public-facing fields or notes.',
                ],
            },
        ],
    },
    'terms': {
        'title': 'Terms of Service',
        'eyebrow': 'Legal',
        'intro': 'These terms describe the basic responsibilities of customers, shop owners, riders, and administrators when using CLEFRESH.',
        'sections': [
            {
                'heading': 'Platform use',
                'body': [
                    'Users should provide accurate account, address, and order information.',
                    'Shops are responsible for maintaining correct pricing, service availability, and order handling.',
                    'Riders and customers should use status updates honestly and avoid misuse of the platform.',
                ],
            },
            {
                'heading': 'Orders and payments',
                'body': [
                    'An order is only valid once it has been submitted through the platform and accepted within the system flow.',
                    'Online payments and cash-on-delivery are subject to the payment method selected at checkout and the platform configuration.',
                ],
            },
            {
                'heading': 'Operational limits',
                'body': [
                    'Service availability depends on shop approval status, slot capacity, operating hours, and rider availability.',
                    'The platform may restrict access to users who abuse workflows, payment flows, or account permissions.',
                ],
            },
        ],
    },
    'refunds': {
        'title': 'Refund and Cancellation',
        'eyebrow': 'Policy',
        'intro': 'Orders and payments move through different statuses, so cancellations and refunds depend on when the request is made.',
        'sections': [
            {
                'heading': 'Order cancellation',
                'body': [
                    'Customers can cancel an order while it is still in pending status.',
                    'Once an order has moved past the initial pending state, cancellation may no longer be available through the customer flow.',
                ],
            },
            {
                'heading': 'Refund handling',
                'body': [
                    'Refunds apply to eligible paid orders and are initiated through the payment flow used by the platform.',
                    'Processing time may vary depending on the payment provider and transaction state.',
                ],
            },
            {
                'heading': 'Before requesting changes',
                'body': [
                    'Review the order timeline, payment status, and shop details first.',
                    'If the issue is operational rather than payment-related, start with the order details page and the relevant dashboard workflow.',
                ],
            },
        ],
    },
}


SHOP_LIST_SORTS = {
    'newest': ('Newest', ('-created_at',)),
    'rated': ('Top rated', (F('avg_rating').desc(nulls_last=True), 'name')),
    'price': ('Lowest price', (F('min_price_kg').asc(nulls_last=True), 'name')),
    'name': ('Name (A–Z)', ('name',)),
}


def shop_list_view(request):
    """Public "all shops" page. Customers browse from their home page instead."""
    if request.user.is_authenticated and request.user.role == 'customer':
        return redirect(reverse('home') + '#shops')
    from urllib.parse import urlencode
    from django.core.paginator import Paginator
    from django.db.models import Avg

    q = request.GET.get('q', '').strip()[:100]
    service_filter = request.GET.get('service', '')
    if service_filter not in dict(Service.SERVICE_CHOICES):
        service_filter = ''
    sort = request.GET.get('sort', 'newest')
    if sort not in SHOP_LIST_SORTS:
        sort = 'newest'
    open_now = request.GET.get('open') == '1'

    shops = Shop.objects.filter(status='approved')
    if q:
        shops = shops.filter(Q(name__icontains=q) | Q(description__icontains=q) | Q(address__icontains=q))
    if service_filter:
        shops = shops.filter(services__name=service_filter)
    # Annotate per-card figures so the template doesn't query per shop.
    shops = (
        shops.distinct()
        .annotate(
            avg_rating=Avg('reviews__rating'),  # join repeats each review equally; mean unaffected
            review_count=Count('reviews', distinct=True),
            min_price_kg=Min('services__price_per_kg'),
            min_price_piece=Min('services__price_per_piece'),
        )
        .prefetch_related('services', _live_promos_prefetch())
        .order_by(*SHOP_LIST_SORTS[sort][1])
    )
    shops = list(shops)
    if open_now:
        shops = [s for s in shops if s.is_open]   # is_open is computed from today's hours

    shop_markers = [
        {'lat': s.latitude, 'lng': s.longitude, 'name': s.name, 'url': reverse('shop_detail', args=[s.id])}
        for s in shops if s.latitude and s.longitude
    ]
    page = Paginator(shops, 12).get_page(request.GET.get('page'))

    def qs(**overrides):
        """Current filters as a query string, with overrides (None drops a key)."""
        params = {'q': q, 'service': service_filter, 'sort': sort if sort != 'newest' else '', 'open': '1' if open_now else ''}
        params.update(overrides)
        return urlencode({k: v for k, v in params.items() if v})

    return render(request, 'shops/list.html', {
        'shops': page.object_list,
        'page_obj': page,
        'result_count': len(shops),
        'query': q,
        'service_filter': service_filter,
        'sort': sort,
        'open_now': open_now,
        'has_filters': bool(q or service_filter or open_now),
        'sort_choices': [(key, label) for key, (label, _order) in SHOP_LIST_SORTS.items()],
        'service_chips': [('', 'All services', qs(service=''))] + [
            (value, label, qs(service=value)) for value, label in Service.SERVICE_CHOICES
        ],
        'page_qs': qs(),                 # filters to carry into pagination links
        'shop_markers': shop_markers,
    })


def info_page_view(request, slug):
    page = INFO_PAGES.get(slug)
    if not page:
        raise Http404('Page not found.')
    return render(request, 'info_page.html', {'page': page, 'page_slug': slug})


def shop_site_view(request, shop_id, page='home'):
    valid_pages = {'home', 'services', 'how_it_works', 'about'}
    if page not in valid_pages:
        raise Http404('Page not found.')
    shop = get_object_or_404(Shop, id=shop_id, status='approved')
    services = list(shop.services.all())          # the template tests and loops it; query once
    today = timezone.localdate()
    slots = shop.delivery_slots.filter(date__gte=today).order_by('date', 'time_start')
    reviews = shop.reviews.select_related('customer').order_by('-created_at')[:20]
    total_reviews = shop.reviews.count()
    shop._reviews_total = total_reviews           # reused by shop.reviews_total in the template
    dist = {i: 0 for i in range(1, 6)}
    for row in shop.reviews.values('rating').annotate(n=Count('id')):
        dist[row['rating']] = row['n']
    context = _shop_edit_context(shop)
    context.update({
        'shop': shop,
        'is_shop_site': True,
        'page': page,
        'services': services,
        'slots': slots,
        'reviews': reviews,
        'total_reviews': total_reviews,
        'rating_dist': dist,
        'rating_dist_list': [(i, dist[i]) for i in range(5, 0, -1)],
        'live_promos': shop.promotions.live(),
    })
    return render(request, 'shops/site.html', context)


def shop_detail_view(request, shop_id):
    return shop_site_view(request, shop_id, 'home')


# ─── Shop Dashboard Views ───────────────────────────────────────────────────

@role_required('shop_owner')
def dashboard_view(request):
    shops = request.user.shops.filter(status='approved')
    if not shops.exists():
        pending = request.user.shops.filter(status='pending')
        return render(request, 'shop_dashboard/no_shop.html', {
            'pending': pending,
            'existing_shop': request.user.shops.first(),
        })
    shop = shops.first()
    today = timezone.localdate()
    from orders.models import Order
    pending_orders = Order.objects.filter(shop=shop, status='pending').count()
    active_orders = Order.objects.filter(shop=shop, status__in=['accepted', 'picked_up', 'washing', 'drying', 'folding', 'ready', 'out_for_delivery']).count()
    todays_orders = Order.objects.filter(shop=shop, created_at__date=today)
    today_revenue = todays_orders.filter(payment_status='paid').aggregate(total=Sum('total_amount'))['total'] or 0
    recent_orders = Order.objects.filter(shop=shop).select_related('customer').order_by('-created_at')[:10]
    return render(request, 'shop_dashboard/dashboard.html', {
        'shop': shop,
        'pending_orders': pending_orders,
        'active_orders': active_orders,
        'today_revenue': today_revenue,
        'recent_orders': recent_orders,
    })


@role_required('shop_owner')
def dashboard_orders_view(request):
    shops = request.user.shops.filter(status='approved')
    if not shops.exists():
        return redirect('shop_dashboard')
    shop = shops.first()
    from orders.models import Order
    status_filter = request.GET.get('status', '')
    orders = Order.objects.filter(shop=shop).select_related('customer', 'rider', 'delivery_slot')
    if status_filter:
        orders = orders.filter(status=status_filter)
    from utils.pagination import paginate
    page_obj, page_qs = paginate(request, orders.order_by('-created_at'))
    return render(request, 'shop_dashboard/orders.html', {
        'shop': shop,
        'orders': page_obj, 'page_obj': page_obj, 'page_qs': page_qs,
        'status_filter': status_filter,
        'status_choices': Order.STATUS_CHOICES,
    })


@role_required('shop_owner')
def dashboard_order_detail_view(request, order_id):
    shops = request.user.shops.filter(status='approved')
    if not shops.exists():
        return redirect('shop_dashboard')
    shop = shops.first()
    from orders.models import Order, OrderStatusLog
    order = get_object_or_404(Order, id=order_id, shop=shop)
    logs = order.status_logs.all()
    from accounts.models import approved_riders
    riders = approved_riders().order_by('first_name', 'email')
    allowed = VALID_TRANSITIONS.get(order.status, set())
    next_statuses = [(value, label) for value, label in Order.STATUS_CHOICES if value in allowed]
    return render(request, 'shop_dashboard/order_detail.html', {
        'shop': shop,
        'order': order,
        'logs': logs,
        'riders': riders,
        'next_statuses': next_statuses,
        # Weigh-at-shop: the form is shown once the laundry is at the shop and until it's paid.
        'can_weigh': order.weigh_at_shop and order.payment_status != 'paid' and order.status in WEIGH_ALLOWED_STATUSES,
        'order_lines': order.order_services.select_related('service'),
        'delivery_block': order.delivery_block_reason() if 'out_for_delivery' in allowed else None,
    })


@role_required('shop_owner')
def update_order_status_view(request, order_id):
    if request.method != 'POST':
        return redirect('shop_dashboard_orders')
    if not request.user.shops.filter(status='approved').exists():
        return redirect('shop_dashboard')
    from orders.models import Order, OrderStatusLog
    from notifications.models import Notification
    from utils.email_notifications import send_email_notification

    order = get_object_or_404(Order, id=order_id, shop__owner=request.user, shop__status='approved')
    new_status = request.POST.get('status')
    note = request.POST.get('note', '')
    decline_reason = request.POST.get('decline_reason', '')

    valid_statuses = [s[0] for s in Order.STATUS_CHOICES]
    if new_status not in valid_statuses:
        messages.error(request, 'Invalid status.')
        return redirect('shop_dashboard_order_detail', order_id=order_id)

    terminal_statuses = {'delivered', 'cancelled', 'declined'}
    if order.status in terminal_statuses:
        messages.error(request, f'Cannot change a {order.get_status_display()} order.')
        return redirect('shop_dashboard_order_detail', order_id=order_id)

    valid_transitions = VALID_TRANSITIONS
    allowed_next = valid_transitions.get(order.status, set())
    if new_status not in allowed_next:
        from_label = order.get_status_display()
        to_label = dict(Order.STATUS_CHOICES).get(new_status, new_status)
        messages.error(request, f'Cannot move order from {from_label} to {to_label}.')
        return redirect('shop_dashboard_order_detail', order_id=order_id)
    if new_status == 'out_for_delivery' and order.delivery_block_reason():
        messages.error(request, order.delivery_block_reason())
        return redirect('shop_dashboard_order_detail', order_id=order_id)
    if new_status == 'declined' and order.payment_status != 'paid' and order.online_payment_in_progress():
        messages.error(request, 'The customer is completing a payment right now. Try again in a few minutes.')
        return redirect('shop_dashboard_order_detail', order_id=order_id)

    old_status = order.status
    order.status = new_status
    fields = {'status': new_status, 'updated_at': timezone.now()}
    if new_status == 'declined' and decline_reason:
        order.decline_reason = decline_reason[:1000]
        fields['decline_reason'] = order.decline_reason
    with transaction.atomic():
        # Conditional update: a double submit (or a concurrent rider update) can't apply twice.
        if not Order.objects.filter(pk=order.pk, status=old_status).update(**fields):
            messages.error(request, 'This order was just updated. Please review it and try again.')
            return redirect('shop_dashboard_order_detail', order_id=order_id)
        OrderStatusLog.objects.create(order=order, status=new_status, note=note[:500])
        # Declined orders free their delivery-slot seat (mirrors cancel_order_view).
        if new_status == 'declined' and order.delivery_slot_id:
            DeliverySlot.objects.filter(id=order.delivery_slot_id, current_bookings__gt=0).update(
                current_bookings=F('current_bookings') - 1
            )
    _customer_messages = {
        'accepted': f'Your order #{order.id} has been accepted by {order.shop.name}.',
        'declined': f'Your order #{order.id} was declined by {order.shop.name}.',
        'picked_up': f'Your laundry for order #{order.id} has been picked up.',
        'washing': f'Your laundry (order #{order.id}) is now being washed.',
        'drying': f'Your laundry (order #{order.id}) is now being dried.',
        'folding': f'Your laundry (order #{order.id}) is being folded and prepared.',
        'ready': f'Your laundry is ready! Order #{order.id} will be out for delivery soon.',
        'out_for_delivery': f'Your order #{order.id} is out for delivery — your rider is on the way!',
        'delivered': f'Your laundry has been delivered! Order #{order.id} is complete.',
    }
    Notification.objects.create(
        user=order.customer,
        message=_customer_messages.get(new_status, f'Your order #{order.id} status: {order.get_status_display()}.'),
        link=f'/orders/{order.id}/'
    )

    status_email_map = {
        'accepted': ('order_accepted', order.customer),
        'declined': ('order_declined', order.customer),
        'picked_up': ('order_picked_up', order.customer),
        'delivered': ('order_delivered', order.customer),
    }
    if new_status in status_email_map:
        event, recipient = status_email_map[new_status]
        extra = {'reason': decline_reason} if new_status == 'declined' else {}
        send_email_notification(event, recipient, order=order, extra=extra)
    if new_status == 'declined':
        order.refresh_from_db(fields=['payment_status'])
        if order.payment_status == 'paid':
            from payments.views import refund_if_paid
            refund_if_paid(order, 'the order was declined after the customer paid.', initiated_by=request.user)
            messages.info(request, 'The customer had already paid online, so a refund has been started.')

    messages.success(request, f'Order status updated to {order.get_status_display()}.')
    return redirect('shop_dashboard_order_detail', order_id=order_id)


WEIGH_ALLOWED_STATUSES = ('picked_up', 'washing', 'drying', 'folding', 'ready')   # laundry is at the shop
MAX_LINE_WEIGHT_KG = Decimal('200')


@role_required('shop_owner')
def confirm_order_weight_view(request, order_id):
    """Shop enters the real weight (and piece counts) for a weigh-at-shop order.

    Recomputes line subtotals, applies the best live promo, and confirms the price so the
    customer can pay. Re-saving is allowed for corrections until the customer has paid.
    """
    if request.method != 'POST':
        return redirect('shop_dashboard_order_detail', order_id=order_id)
    from orders.models import Order, PhotoProof
    from notifications.models import Notification
    from payments.models import PayMongoTransaction
    from utils.email_notifications import send_email_notification
    from utils.validators import validate_image_upload
    from .models import Promotion

    shop = _owner_approved_shop(request)
    order = get_object_or_404(Order, id=order_id, shop=shop)
    back = redirect('shop_dashboard_order_detail', order_id=order.id)
    if not order.weigh_at_shop:
        messages.error(request, 'This order was priced at booking; there is nothing to weigh.')
        return back
    if order.payment_status == 'paid':
        messages.error(request, 'This order is already paid, so its price is locked.')
        return back
    if order.status not in WEIGH_ALLOWED_STATUSES:
        messages.error(request, 'Weigh the laundry once it has been dropped off at your shop.')
        return back
    if PayMongoTransaction.in_progress(order).exists():
        messages.error(request, 'The customer is completing a payment right now. Try again in a few minutes.')
        return back

    lines = list(order.order_services.select_related('service'))
    updates, subtotal = [], Decimal('0.00')
    try:
        for line in lines:
            svc = line.service
            weight = line.weight_kg
            qty = line.quantity
            if svc.price_per_kg:
                raw = request.POST.get(f'weight_{line.id}', '').strip()
                try:
                    weight = Decimal(raw)
                    if not weight.is_finite():
                        raise InvalidOperation
                    weight = weight.quantize(Decimal('0.01'))
                except (InvalidOperation, ValueError):
                    raise ValidationError(f'Enter the weight for {svc.get_name_display()}.')
                if weight <= 0 or weight > MAX_LINE_WEIGHT_KG:
                    raise ValidationError(f'{svc.get_name_display()} weight must be between 0.01 and {MAX_LINE_WEIGHT_KG} kg.')
            if svc.price_per_piece and f'qty_{line.id}' in request.POST:
                raw = request.POST.get(f'qty_{line.id}', '').strip() or '0'
                if not raw.isdigit() or int(raw) > 1000:
                    raise ValidationError(f'Enter a valid piece count for {svc.get_name_display()}.')
                qty = int(raw)
            line_total = Decimal('0.00')
            if svc.price_per_piece and qty:
                line_total += svc.price_per_piece * qty
            if svc.price_per_kg and weight:
                line_total += (svc.price_per_kg * weight).quantize(Decimal('0.01'))
            if line_total <= 0:
                raise ValidationError(f'{svc.get_name_display()} needs a weight or piece count.')
            updates.append((line, weight if svc.price_per_kg else line.weight_kg, qty, line_total))
            subtotal += line_total
        if subtotal > Decimal('50000.00'):
            raise ValidationError('Order total exceeds the maximum allowed amount (₱50,000).')
        photo = request.FILES.get('scale_photo')
        if photo:
            validate_image_upload(photo)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return back

    promotion, discount = Promotion.best_for(shop, subtotal)    # promo checked at weighing time
    total = subtotal - discount
    total_kg = sum((w for _l, w, _q, _t in updates if w), Decimal('0'))
    kg_text = f'{total_kg.normalize():f}'          # 4.20 -> "4.2", 10.00 -> "10" (no "1E+1")
    reconfirm = order.price_confirmed_at is not None
    with transaction.atomic():
        # Re-check under the row lock that payment_process also takes before charging.
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.payment_status == 'paid' or PayMongoTransaction.in_progress(locked).exists():
            messages.error(request, 'The customer is completing a payment right now. Try again in a few minutes.')
            return back
        for line, weight, qty, line_total in updates:
            line.weight_kg, line.quantity, line.subtotal = weight, qty, line_total
            line.save(update_fields=['weight_kg', 'quantity', 'subtotal'])
        order.subtotal_amount, order.discount_amount, order.total_amount = subtotal, discount, total
        order.promotion, order.promotion_title = promotion, (promotion.title if promotion else '')
        order.price_confirmed_at, order.price_confirmed_by = timezone.now(), request.user
        order.start_payment_clock()             # (re)start the online-payment deadline from the final price
        order.save(update_fields=['subtotal_amount', 'discount_amount', 'total_amount', 'promotion',
                                  'promotion_title', 'price_confirmed_at', 'price_confirmed_by',
                                  'payment_due_at', 'payment_reminders_sent', 'updated_at'])
        if photo:
            PhotoProof.objects.create(order=order, photo=photo, proof_type='weight', uploaded_by=request.user)
        pay_hint = ''
        if order.awaiting_online_payment and order.payment_due_at:
            due = timezone.localtime(order.payment_due_at)
            pay_hint = f' Please pay by {due:%b %d, %I:%M %p} so your laundry can be delivered.'
        cod_hint = ' You can pay online now, or pay the rider in cash on delivery.' if order.can_pay_online and order.payment_method == 'cod' else ''
        Notification.objects.create(
            user=order.customer,
            message=(f'{"Updated: your" if reconfirm else "Your"} laundry for order #{order.id} weighs {kg_text} kg. '
                     f'Final total: ₱{total:,.2f}.{pay_hint}{cod_hint}'),
            link=f'/orders/{order.id}/',
        )
    send_email_notification('price_confirmed', order.customer, order=order,
                            extra={'weight': f'{kg_text}', 'pay_now': bool(pay_hint)})
    messages.success(request, f'{"Price updated" if reconfirm else "Weight confirmed"}: {kg_text} kg, total ₱{total:,.2f}. '
                              'The customer has been notified.')
    return back


@role_required('shop_owner')
def assign_rider_view(request, order_id):
    if request.method != 'POST':
        return redirect('shop_dashboard_orders')
    shops = request.user.shops.filter(status='approved')
    shop = shops.first() if shops.exists() else None
    if not shop:
        return redirect('shop_dashboard')
    from orders.models import Order
    from notifications.models import Notification
    from utils.email_notifications import send_email_notification
    from accounts.models import approved_riders

    order = get_object_or_404(Order, id=order_id, shop=shop)
    if order.status in ('delivered', 'cancelled', 'declined'):
        messages.error(request, f'Cannot assign a rider to a {order.get_status_display()} order.')
        return redirect('shop_dashboard_order_detail', order_id=order_id)
    if order.rider_id and order.status in ('picked_up', 'out_for_delivery'):
        messages.error(request, 'The current rider has the laundry right now. Reassign after it reaches its destination.')
        return redirect('shop_dashboard_order_detail', order_id=order_id)
    previous_rider = order.rider
    rider_id = request.POST.get('rider_id', '')
    if not rider_id.isdigit():
        messages.error(request, 'Please select a rider.')
        return redirect('shop_dashboard_order_detail', order_id=order_id)
    rider = approved_riders().filter(id=rider_id).first()
    if not rider:
        messages.error(request, 'Only admin-approved riders can be assigned.')
        return redirect('shop_dashboard_order_detail', order_id=order_id)
    order.rider = rider
    order.save(update_fields=['rider', 'updated_at'])
    if previous_rider and previous_rider != rider:
        Notification.objects.create(user=previous_rider, message=f'You are no longer assigned to Order #{order.id}.', link='/rider/')
    Notification.objects.create(user=rider, message=f'You have been assigned to Order #{order.id}.', link=f'/rider/tasks/{order.id}/')
    Notification.objects.create(user=order.customer, message=f'A rider has been assigned to your Order #{order.id}.', link=f'/orders/{order.id}/')
    send_email_notification('rider_assigned', order.customer, order=order)
    send_email_notification('rider_task_assigned', rider, order=order)
    messages.success(request, 'Rider assigned successfully.')
    return redirect('shop_dashboard_order_detail', order_id=order_id)


@role_required('shop_owner')
def dashboard_slots_view(request):
    shops = request.user.shops.filter(status='approved')
    if not shops.exists():
        return redirect('shop_dashboard')
    shop = shops.first()
    today = timezone.localdate()
    slots = shop.delivery_slots.filter(date__gte=today).order_by('date', 'time_start')
    return render(request, 'shop_dashboard/slots.html', {'shop': shop, 'slots': slots})


@role_required('shop_owner')
def create_slot_view(request):
    if request.method != 'POST':
        return redirect('shop_dashboard_slots')
    shops = request.user.shops.filter(status='approved')
    shop = shops.first() if shops.exists() else None
    if not shop:
        return redirect('shop_dashboard')
    slot_date = request.POST.get('date')
    if slot_date and slot_date < str(timezone.localdate()):
        messages.error(request, 'Slot date cannot be in the past.')
        return redirect('shop_dashboard_slots')
    slot = DeliverySlot(
        shop=shop,
        date=slot_date,
        time_start=request.POST.get('time_start'),
        time_end=request.POST.get('time_end'),
        max_capacity=request.POST.get('max_capacity', 5),
    )
    try:
        slot.full_clean()
        slot.save()
    except (ValidationError, IntegrityError) as exc:
        logger.warning("Slot creation failed for shop %s: %s", shop.id, exc)
        messages.error(request, 'Could not create slot. Check the values and try again.')
        return redirect('shop_dashboard_slots')
    messages.success(request, 'Slot created.')
    return redirect('shop_dashboard_slots')


@role_required('shop_owner')
def delete_slot_view(request, slot_id):
    if request.method != 'POST':
        return redirect('shop_dashboard_slots')
    shops = request.user.shops.filter(status='approved')
    shop = shops.first() if shops.exists() else None
    slot = get_object_or_404(DeliverySlot, id=slot_id, shop=shop)
    if slot.order_set.exclude(status__in=['delivered', 'cancelled', 'declined']).exists():
        messages.error(request, 'This slot has active bookings, so it can\'t be deleted.')
        return redirect('shop_dashboard_slots')
    slot.delete()
    messages.success(request, 'Slot deleted.')
    return redirect('shop_dashboard_slots')


@role_required('shop_owner')
def dashboard_services_view(request):
    shops = request.user.shops.filter(status='approved')
    if not shops.exists():
        return redirect('shop_dashboard')
    shop = shops.first()
    services = shop.services.all()
    return render(request, 'shop_dashboard/services.html', {'shop': shop, 'services': services})


@role_required('shop_owner')
def create_service_view(request):
    if request.method != 'POST':
        return redirect('shop_dashboard_services')
    shops = request.user.shops.filter(status='approved')
    shop = shops.first() if shops.exists() else None
    if not shop:
        return redirect('shop_dashboard')
    service = Service(
        shop=shop,
        name=request.POST.get('name'),
        price_per_kg=request.POST.get('price_per_kg') or None,
        price_per_piece=request.POST.get('price_per_piece') or None,
    )
    try:
        service.full_clean()
        service.save()
    except (ValidationError, IntegrityError) as exc:
        logger.warning("Service creation failed for shop %s: %s", shop.id, exc)
        messages.error(request, 'Could not add service. Check the values and try again.')
        return redirect('shop_dashboard_services')
    messages.success(request, 'Service added.')
    return redirect('shop_dashboard_services')


@role_required('shop_owner')
def delete_service_view(request, service_id):
    if request.method != 'POST':
        return redirect('shop_dashboard_services')
    shops = request.user.shops.filter(status='approved')
    shop = shops.first() if shops.exists() else None
    service = get_object_or_404(Service, id=service_id, shop=shop)
    try:
        service.delete()
    except RestrictedError:
        messages.error(request, 'This service has existing orders and cannot be deleted.')
        return redirect('shop_dashboard_services')
    messages.success(request, 'Service deleted.')
    return redirect('shop_dashboard_services')


def _owner_approved_shop(request):
    return request.user.shops.filter(status='approved').first()


@role_required('shop_owner')
def dashboard_promos_view(request):
    """List + create/edit promos. ?edit=<id> pre-fills the form with that promo."""
    from .forms import PromotionForm
    from .models import Promotion

    shop = _owner_approved_shop(request)
    if not shop:
        return redirect('shop_dashboard')

    editing = None
    edit_id = request.POST.get('promo_id') or request.GET.get('edit')
    if edit_id:
        if not str(edit_id).isdigit():
            raise Http404
        editing = get_object_or_404(Promotion, id=edit_id, shop=shop)

    if request.method == 'POST':
        form = PromotionForm(request.POST, instance=editing)
        if form.is_valid():
            promo = form.save(commit=False)
            promo.shop = shop
            promo.save()
            messages.success(request, 'Promo updated.' if editing else 'Promo posted.')
            return redirect('shop_dashboard_promos')
        messages.error(request, 'Please fix the highlighted fields.')
    else:
        today = timezone.localdate()
        form = PromotionForm(instance=editing, initial=None if editing else {
            'start_date': today, 'end_date': today + timedelta(days=7), 'is_active': True,
        })

    return render(request, 'shop_dashboard/promos.html', {
        'shop': shop,
        'form': form,
        'editing': editing,
        'promos': shop.promotions.order_by('-is_active', '-end_date', '-id'),
    })


@role_required('shop_owner')
def toggle_promo_view(request, promo_id):
    from .models import Promotion
    if request.method != 'POST':
        return redirect('shop_dashboard_promos')
    promo = get_object_or_404(Promotion, id=promo_id, shop=_owner_approved_shop(request))
    promo.is_active = not promo.is_active
    promo.save(update_fields=['is_active'])
    messages.success(request, f'Promo {"resumed" if promo.is_active else "paused"}.')
    return redirect('shop_dashboard_promos')


@role_required('shop_owner')
def delete_promo_view(request, promo_id):
    from .models import Promotion
    if request.method != 'POST':
        return redirect('shop_dashboard_promos')
    promo = get_object_or_404(Promotion, id=promo_id, shop=_owner_approved_shop(request))
    promo.delete()
    messages.success(request, 'Promo deleted.')
    return redirect('shop_dashboard_promos')


@role_required('shop_owner')
def dashboard_analytics_view(request):
    shops = request.user.shops.filter(status='approved')
    if not shops.exists():
        return redirect('shop_dashboard')
    shop = shops.first()
    from orders.models import Order
    from django.db.models import Sum, Count
    orders = Order.objects.filter(shop=shop)
    total_revenue = orders.filter(payment_status='paid').aggregate(s=Sum('total_amount'))['s'] or 0
    total_orders = orders.count()
    delivered = orders.filter(status='delivered').count()
    avg_rating = shop.average_rating()
    payment_breakdown = {}
    for pm, label in Order.PAYMENT_METHOD_CHOICES:
        payment_breakdown[label] = orders.filter(payment_method=pm).count()
    return render(request, 'shop_dashboard/analytics.html', {
        'shop': shop,
        'total_revenue': total_revenue,
        'total_orders': total_orders,
        'delivered': delivered,
        'avg_rating': avg_rating,
        'payment_breakdown': payment_breakdown,
    })


@role_required('shop_owner')
def register_shop_view(request):
    existing = request.user.shops.first()
    # Only new or rejected shops may (re)submit; otherwise a suspended owner could
    # flip their own shop back to pending.
    if existing and existing.status in ('approved', 'suspended'):
        if existing.status == 'suspended':
            messages.error(request, 'Your shop is suspended. Please contact support.')
            return redirect('shop_dashboard')
        return redirect('edit_shop')
    if request.method == 'POST':
        shop = existing or Shop(owner=request.user)
        try:
            with transaction.atomic():
                _save_shop_from_owner_form(request, shop, set_pending=True)
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
            return render(request, 'shop_dashboard/register_shop.html', {'shop': shop})
        messages.success(request, 'Shop registration submitted for approval.')
        return redirect('shop_dashboard')
    return render(request, 'shop_dashboard/register_shop.html', {'shop': existing})


@role_required('shop_owner')
def edit_shop_view(request):
    shop = request.user.shops.prefetch_related('services').first()
    if not shop:
        return redirect('register_shop')
    if shop.status == 'suspended':
        messages.error(request, 'Your shop is suspended. Please contact support.')
        return redirect('shop_dashboard')
    if request.method == 'POST':
        try:
            with transaction.atomic():
                _save_shop_from_owner_form(request, shop, set_pending=False)
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
            return render(request, 'shop_dashboard/edit_shop.html', _shop_edit_context(shop))
        messages.success(request, 'Shop details updated.')
        return redirect('shop_dashboard')
    return render(request, 'shop_dashboard/edit_shop.html', _shop_edit_context(shop))


@role_required('shop_owner')
def dashboard_payments_view(request):
    shops = request.user.shops.filter(status='approved')
    if not shops.exists():
        return redirect('shop_dashboard')
    shop = shops.first()
    from orders.models import Order
    orders = Order.objects.filter(shop=shop).select_related('customer', 'rider').order_by('-created_at')
    from utils.pagination import paginate
    page_obj, page_qs = paginate(request, orders)
    return render(request, 'shop_dashboard/payments.html', {'shop': shop, 'orders': page_obj,
                                                            'page_obj': page_obj, 'page_qs': page_qs})


@login_required
def serve_shop_doc_view(request, shop_id, field):
    """Owner verification documents: visible to the shop's owner and admins only."""
    if field not in ('owner_government_id', 'owner_selfie_with_id'):
        raise Http404
    shop = get_object_or_404(Shop, id=shop_id)
    is_admin = request.user.is_superuser or request.user.role == 'admin'
    if not (is_admin or shop.owner_id == request.user.id):
        raise Http404
    doc = getattr(shop, field)
    if not doc:
        raise Http404
    import mimetypes
    from django.http import FileResponse
    try:
        handle = doc.open('rb')
    except FileNotFoundError:
        raise Http404
    content_type, _ = mimetypes.guess_type(doc.name)
    return FileResponse(handle, content_type=content_type or 'application/octet-stream')


# ─── API Views ───────────────────────────────────────────────────────────────

@login_required
def api_slots_view(request, shop_id):
    shop = get_object_or_404(Shop, id=shop_id, status='approved')
    today = timezone.localdate()
    slots = shop.delivery_slots.filter(date__gte=today, current_bookings__lt=F('max_capacity'))
    data = [
        {
            'id': s.id,
            'date': str(s.date),
            'time_start': str(s.time_start),
            'time_end': str(s.time_end),
            'available': s.max_capacity - s.current_bookings,
            'capacity_percent': s.capacity_percent,
        }
        for s in slots
    ]
    return JsonResponse({'slots': data})
