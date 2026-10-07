import logging
import threading
import requests
from django.conf import settings
from django.core.mail import send_mail
from django.db import connection

logger = logging.getLogger(__name__)

_SENSITIVE_EVENTS = {'email_verification', 'password_reset'}


def _smtp_configured():
    # SMTP host, or an HTTPS API backend (anymail) for hosts that block SMTP ports (Railway).
    return bool(getattr(settings, 'EMAIL_HOST', '')) or 'anymail' in settings.EMAIL_BACKEND


def _deliver_smtp(recipient_email, subject, body):
    send_mail(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [recipient_email],
        fail_silently=False,
    )


def _deliver_formspree(payload, recipient_email, subject, body_preview, order, event_type, in_thread=True):
    """Deliver via SMTP (or Formspree fallback) and write EmailLog.

    When run in a background thread, closes the thread's own DB connection afterwards.
    """
    status_code = None
    try:
        if _smtp_configured():
            _deliver_smtp(recipient_email, subject, payload['message'])
            status_code = 202
        elif event_type in _SENSITIVE_EVENTS:
            # Formspree is a contact-form relay, not a transactional mail service. Never
            # send reset links or verification codes to a third-party form inbox.
            logger.error("Email event %s for %s not sent: configure SMTP to deliver transactional email.",
                         event_type, recipient_email)
        elif settings.FORMSPREE_ENDPOINT:
            response = requests.post(
                settings.FORMSPREE_ENDPOINT,
                data=payload,
                headers={'Accept': 'application/json'},
                timeout=10
            )
            status_code = response.status_code
            logger.warning(
                "Email event %s submitted to Formspree for %s. Formspree does not send transactional email to arbitrary recipients.",
                event_type,
                recipient_email,
            )
        else:
            logger.warning("Email event %s for %s was not sent: no SMTP or Formspree endpoint configured.", event_type, recipient_email)
    except Exception:
        logger.exception("Failed to send email notification: event=%s recipient=%s", event_type, recipient_email)
    try:
        from orders.models import EmailLog
        EmailLog.objects.create(
            recipient_email=recipient_email,
            subject=subject,
            body_preview=body_preview,
            formspree_response_status=status_code,
            order=order,
            triggered_by=event_type,
        )
    except Exception:
        logger.exception("Failed to write EmailLog: event=%s recipient=%s", event_type, recipient_email)
    finally:
        if in_thread:
            connection.close()


def send_email_notification(event_type, user, order=None, extra=None, force=False):
    if not getattr(user, 'email', None):
        return
    if not force and not getattr(user, 'email_notifications_enabled', True):
        return
    if extra is None:
        extra = {}

    order_id = getattr(order, 'id', '') if order else ''
    shop_name = order.shop.name if order and order.shop_id else ''
    total = order.total_amount if order else ''
    # Weigh-at-shop orders have no final total until the shop weighs the laundry.
    if order and getattr(order, 'price_pending', False):
        total_line = 'Total: to be confirmed after the shop weighs your laundry'
    else:
        total_line = f'Total: ₱{total}'
    pickup = order.pickup_datetime if order else ''
    pickup_addr = order.pickup_address if order else ''
    delivery_addr = order.delivery_address if order else ''
    payment_method = order.get_payment_method_display() if order else ''
    full_name = user.get_full_name() or user.email
    discount_line = ''
    if order and getattr(order, 'discount_amount', 0):
        discount_line = f"Promo: {order.promotion_title or 'Discount'} (-₱{order.discount_amount})\n"

    templates = {
        'welcome': {
            'subject': 'Welcome to CLEFRESH!',
            'body': f"Hi {full_name},\n\nYour CLEFRESH account has been created successfully. Start booking laundry services today!\n\nThe CLEFRESH Team",
        },
        'order_placed': {
            'subject': f'Order #{order_id} Placed - CLEFRESH',
            'body': f"Hi {full_name},\n\nYour laundry order #{order_id} has been placed successfully.\nShop: {shop_name}\n{discount_line}{total_line}\nPickup: {pickup}\n\nWe'll notify you when the shop confirms your order.\n\nThe CLEFRESH Team",
        },
        'payment_reminder': {
            'subject': f'Payment Reminder for Order #{order_id} - CLEFRESH',
            'body': f"Hi {full_name},\n\nYour order #{order_id} at {shop_name} is waiting for payment: ₱{total}.\nPlease pay by {extra.get('due', '')} so your laundry can be delivered: {settings.SITE_URL}/orders/{order_id}/\n\nIf it isn't paid by then, the order switches to cash on delivery and you can pay the rider instead.\n\nThe CLEFRESH Team",
        },
        'payment_switched_cod': {
            'subject': f'Order #{order_id} Is Now Cash on Delivery - CLEFRESH',
            'body': f"Hi {full_name},\n\nOrder #{order_id} wasn't paid online in time, so it has been switched to cash on delivery.\nPlease have ₱{total} ready for the rider when your laundry arrives.\n\nThe CLEFRESH Team",
        },
        'price_confirmed': {
            'subject': f'Final Price for Order #{order_id} - CLEFRESH',
            'body': f"Hi {full_name},\n\n{shop_name} weighed your laundry for order #{order_id}: {extra.get('weight', '')} kg.\n{discount_line}Final total: ₱{total}\n\n{'Please pay online so your laundry can be delivered: ' + settings.SITE_URL + '/orders/' + str(order_id) + '/' if extra.get('pay_now') else 'You can pay online now (' + settings.SITE_URL + '/orders/' + str(order_id) + '/), or pay the rider in cash on delivery.'}\n\nThe CLEFRESH Team",
        },
        'order_accepted': {
            'subject': f'Order #{order_id} Accepted - CLEFRESH',
            'body': f"Hi {full_name},\n\nGreat news! {shop_name} has accepted your order #{order_id}.\nA rider will be assigned to pick up your laundry soon.\n\nTrack your order: {settings.SITE_URL}/orders/{order_id}/\n\nThe CLEFRESH Team",
        },
        'order_declined': {
            'subject': f'Order #{order_id} Declined - CLEFRESH',
            'body': f"Hi {full_name},\n\nUnfortunately, {shop_name} has declined your order #{order_id}.\nReason: {extra.get('reason', 'Not specified')}\n\nYou may place a new order with another shop.\n\nThe CLEFRESH Team",
        },
        'payment_confirmed': {
            'subject': f'Payment Confirmed for Order #{order_id} - CLEFRESH',
            'body': f"Hi {full_name},\n\nYour payment of ₱{total} for Order #{order_id} has been confirmed.\nPayment method: {payment_method}\n\nThank you!\n\nThe CLEFRESH Team",
        },
        'rider_assigned': {
            'subject': f'Rider Assigned for Order #{order_id} - CLEFRESH',
            'body': f"Hi {full_name},\n\nA rider has been assigned to your order #{order_id}.\nExpect pickup at: {pickup}\n\nTrack your order live: {settings.SITE_URL}/orders/{order_id}/\n\nThe CLEFRESH Team",
        },
        'order_picked_up': {
            'subject': f'Laundry Picked Up - Order #{order_id}',
            'body': f"Hi {full_name},\n\nYour laundry for Order #{order_id} has been picked up and is on its way to {shop_name} for processing.\n\nThe CLEFRESH Team",
        },
        'order_delivered': {
            'subject': f'Order #{order_id} Delivered - CLEFRESH',
            'body': f"Hi {full_name},\n\nYour freshly cleaned laundry for Order #{order_id} has been delivered!\n\nPlease leave a review: {settings.SITE_URL}/orders/{order_id}/review/\n\nThe CLEFRESH Team",
        },
        'refund_processed': {
            'subject': f'Refund Processed for Order #{order_id} - CLEFRESH',
            'body': f"Hi {full_name},\n\nA refund of ₱{extra.get('amount', '')} has been processed for Order #{order_id}.\nPlease allow 3-5 business days for the amount to reflect.\n\nThe CLEFRESH Team",
        },
        'shop_approved': {
            'subject': 'Your Shop Has Been Approved - CLEFRESH',
            'body': f"Hi {full_name},\n\nCongratulations! Your laundry shop has been approved on CLEFRESH.\nYou can now log in and start accepting orders.\n\nThe CLEFRESH Team",
        },
        'shop_rejected': {
            'subject': 'Shop Registration Update - CLEFRESH',
            'body': f"Hi {full_name},\n\nWe were unable to approve your shop registration at this time.\nReason: {extra.get('reason', 'Not specified')}\n\nPlease contact support for assistance.\n\nThe CLEFRESH Team",
        },
        'new_order_for_shop': {
            'subject': f'New Order #{order_id} Received - CLEFRESH',
            'body': f"Hi {full_name},\n\nYou have a new laundry order #{order_id}.\nCustomer: {order.customer.get_full_name() if order else ''}\nServices: {extra.get('services_summary', '')}\nPickup: {pickup}\n\nLog in to accept: {settings.SITE_URL}/shop-dashboard/orders/\n\nThe CLEFRESH Team",
        },
        'rider_task_assigned': {
            'subject': f'New Delivery Task - Order #{order_id}',
            'body': f"Hi {full_name},\n\nYou have been assigned a new task for Order #{order_id}.\nPickup address: {pickup_addr}\n\nLog in to view: {settings.SITE_URL}/rider/\n\nThe CLEFRESH Team",
        },
        'password_reset': {
            'subject': 'Reset your CLEFRESH password',
            'body': f"Hi {full_name},\n\nWe received a request to reset your password.\n\nClick the link below to set a new password (expires in 3 days):\n{extra.get('reset_url', '')}\n\nIf you did not request this, you can safely ignore this email.\n\nThe CLEFRESH Team",
        },
        'email_verification': {
            'subject': 'Your CLEFRESH verification code',
            'body': f"Hi {full_name},\n\nYour CLEFRESH email verification code is:\n\n    {extra.get('code', '')}\n\nThis code expires in 10 minutes. Enter it on the verification page to continue your registration.\n\nIf you did not request this, you can ignore this email.\n\nThe CLEFRESH Team",
        },
    }

    tmpl = templates.get(event_type)
    if not tmpl:
        return

    payload = {
        '_replyto': user.email,
        'email': user.email,
        '_subject': tmpl['subject'],
        'message': tmpl['body'],
        '_template': 'box',
    }

    # Never persist one-time codes or reset tokens in EmailLog.
    if event_type in _SENSITIVE_EVENTS:
        body_preview = '[redacted: contains a one-time code or reset link]'
    else:
        body_preview = tmpl['body'][:200]

    args = (payload, user.email, tmpl['subject'], body_preview, order, event_type)
    # Codes and reset links are sent inline: the user is waiting for them, and hosts like
    # PythonAnywhere (uWSGI without threads) never run background threads.
    if event_type in _SENSITIVE_EVENTS or not getattr(settings, 'EMAIL_ASYNC', True):
        _deliver_formspree(*args, in_thread=False)
    else:
        threading.Thread(target=_deliver_formspree, args=args, daemon=True).start()
