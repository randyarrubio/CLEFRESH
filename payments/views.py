import hashlib
import hmac
import json
import logging
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from accounts.decorators import role_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt

from notifications.models import Notification
from orders.models import Order
from utils.email_notifications import send_email_notification

from .models import PayMongoTransaction, Refund, WebhookEvent


logger = logging.getLogger(__name__)


def _extract_paymongo_error(response):
    try:
        data = response.json()
    except ValueError:
        return f"HTTP {response.status_code} from PayMongo."

    errors = data.get("errors") or []
    if not errors:
        return ""

    messages_list = []
    for error in errors:
        detail = error.get("detail") or error.get("title") or "Unknown PayMongo error."
        code = error.get("code")
        messages_list.append(f"{detail} ({code})" if code else detail)
    return " ".join(messages_list)


def _fetch_payment_intent_status(pi_id):
    """Return the PayMongo payment-intent status, or None if it can't be read."""
    if not pi_id or not settings.PAYMONGO_SECRET_KEY:
        return None
    try:
        resp = requests.get(
            f"https://api.paymongo.com/v1/payment_intents/{pi_id}",
            auth=(settings.PAYMONGO_SECRET_KEY, ""),
            timeout=10,
        )
        data = resp.json()
        if resp.status_code < 400 and "data" in data:
            return data["data"]["attributes"]["status"]
    except Exception:
        logger.exception("Failed to fetch payment intent %s", pi_id)
    return None


def _notify_refund_needed(order, why):
    """Tell the shop owner (and admins) that money must go back to the customer."""
    from accounts.models import User
    msg = f"Order #{order.id}: {why} Please refund the customer from Payments."
    recipients = [order.shop.owner] + list(User.objects.filter(role="admin", is_active=True))
    Notification.objects.bulk_create([
        Notification(user=u, message=msg, link="/shop-dashboard/payments/" if u == order.shop.owner else "/admin-panel/transactions/")
        for u in recipients
    ])


def refund_order(order, reason="others", initiated_by=None):
    """Refund a paid order's full online payment through PayMongo. Returns (ok, message)."""
    if order.payment_status != "paid":
        return False, "Only paid orders can be refunded."
    if not settings.PAYMONGO_SECRET_KEY:
        return False, "PayMongo is not configured."
    if Refund.objects.filter(order=order, status__in=["pending", "succeeded"]).exists():
        return False, "A refund has already been initiated for this order."
    tx = PayMongoTransaction.objects.filter(order=order).first()
    if not tx:
        return False, "No online payment found for this order. COD orders cannot be refunded online."
    if not tx.payment_id:
        return False, "Missing PayMongo payment ID for this order."
    amount_centavos = min(int(Decimal(order.total_amount) * 100), tx.amount)
    if amount_centavos <= 0:
        return False, "Invalid refund amount."
    try:
        resp = requests.post(
            "https://api.paymongo.com/v1/refunds",
            auth=(settings.PAYMONGO_SECRET_KEY, ""),
            json={"data": {"attributes": {"amount": amount_centavos, "payment_id": tx.payment_id, "reason": reason}}},
            timeout=15,
        )
        data = resp.json()
        if resp.status_code >= 400 or "errors" in data:
            return False, _extract_paymongo_error(resp) or "Refund failed."
        Refund.objects.create(order=order, paymongo_refund_id=data["data"]["id"], amount=amount_centavos,
                              reason=reason, status="pending", initiated_by=initiated_by)
    except Exception:
        logger.exception("Refund request failed for order %s", order.id)
        return False, "Refund processing failed. Please try again."
    return True, "Refund initiated."


def refund_if_paid(order, why, initiated_by=None):
    """Called when a paid order is cancelled/declined: refund automatically, else flag it."""
    if order.payment_status != "paid":
        return
    ok, _ = refund_order(order, initiated_by=initiated_by)
    if not ok:
        _notify_refund_needed(order, why)


def _mark_order_paid(order, pi_id, payment_id=""):
    """Idempotently mark an order paid. Returns True only on the unpaid -> paid transition."""
    tx = PayMongoTransaction.objects.filter(order=order, payment_intent_id=pi_id).first()
    expected = int(Decimal(Order.objects.values_list("total_amount", flat=True).get(pk=order.pk)) * 100)
    if tx and tx.amount < expected:
        # The price went up while the customer was paying (e.g. re-weighed): don't mark the
        # order fully paid for less than its total. The shop settles the difference.
        logger.error("Order %s paid %s centavos but total is %s", order.pk, tx.amount, expected)
        Notification.objects.create(
            user=order.shop.owner, link=f"/shop-dashboard/orders/{order.pk}/",
            message=(f"Order #{order.pk}: the customer paid ₱{Decimal(tx.amount) / 100:,.2f} online, but the total "
                     f"changed to ₱{Decimal(expected) / 100:,.2f} during payment. Please collect the "
                     f"₱{Decimal(expected - tx.amount) / 100:,.2f} difference on delivery."))
    updated = Order.objects.filter(pk=order.pk).exclude(payment_status="paid").update(payment_status="paid")
    order.payment_status = "paid"
    tx_update = {"status": "succeeded"}
    if payment_id:
        tx_update["payment_id"] = payment_id
    PayMongoTransaction.objects.filter(order=order, payment_intent_id=pi_id).update(**tx_update)
    # A cash-on-delivery order paid online: record the e-wallet actually used.
    if tx and tx.payment_method_type:
        if Order.objects.filter(pk=order.pk, payment_method="cod").update(payment_method=tx.payment_method_type):
            order.payment_method = tx.payment_method_type
    if updated:
        status = Order.objects.values_list("status", flat=True).get(pk=order.pk)
        if status in ("cancelled", "declined"):
            # Payment landed after the order was closed: send the money back.
            order.status = status
            transaction.on_commit(lambda o=order: refund_if_paid(o, f"payment arrived after the order was {o.status}."))
    return bool(updated)


@role_required('customer')
def checkout_view(request, order_id):
    order = get_object_or_404(Order, id=order_id, customer=request.user)
    if order.payment_status == "paid":
        messages.info(request, "This order is already paid.")
        return redirect("order_detail", order_id=order_id)
    if order.price_pending:
        # Weigh-at-shop order: nothing to charge until the shop confirms the weight.
        messages.info(request, "You can pay once the shop weighs your laundry and confirms the final price.")
        return redirect("order_detail", order_id=order_id)
    if not order.can_pay_online:
        messages.info(request, "Online payment isn't available for this order right now. "
                               "If your laundry is already out for delivery, please pay the rider.")
        return redirect("order_detail", order_id=order_id)

    payment_methods = [
        ("gcash", "GCash"),
        ("grab_pay", "GrabPay"),
        ("maya", "Maya"),
    ]
    return render(
        request,
        "payments/checkout.html",
        {"order": order, "payment_methods": payment_methods},
    )


@role_required('customer')
def process_payment_view(request, order_id):
    if request.method != "POST":
        return redirect("payment_checkout", order_id=order_id)

    order = get_object_or_404(Order, id=order_id, customer=request.user)
    if order.price_pending:
        # Weigh-at-shop order: nothing to charge until the shop confirms the weight.
        messages.info(request, "You can pay once the shop weighs your laundry and confirms the final price.")
        return redirect("order_detail", order_id=order_id)
    if not order.can_pay_online:
        messages.info(request, "Online payment isn't available for this order right now. "
                               "If your laundry is already out for delivery, please pay the rider.")
        return redirect("order_detail", order_id=order_id)
    if order.status in ("cancelled", "declined"):
        messages.error(request, "This order can no longer be paid.")
        return redirect("order_detail", order_id=order_id)
    if order.payment_status == "paid":
        messages.info(request, "This order is already paid.")
        return redirect("order_detail", order_id=order_id)
    in_progress = PayMongoTransaction.objects.filter(
        order=order, status__in=["awaiting_payment_method", "processing"]
    ).first()
    if in_progress and not in_progress.payment_intent_id:
        if in_progress.updated_at < timezone.now() - timedelta(minutes=2):
            in_progress.status = "failed"            # reservation from a request that died mid-way
            in_progress.save(update_fields=["status", "updated_at"])
            in_progress = None
        else:
            messages.info(request, "A payment is already in progress for this order. Please wait a moment and refresh.")
            return redirect("order_detail", order_id=order_id)
    if in_progress:
        # Re-check the old intent instead of blocking forever: an abandoned or
        # failed e-wallet redirect leaves the intent back at awaiting_payment_method.
        pi_status = _fetch_payment_intent_status(in_progress.payment_intent_id)
        if pi_status == "succeeded":
            _mark_order_paid(order, in_progress.payment_intent_id)
            messages.info(request, "This order is already paid.")
            return redirect("order_detail", order_id=order_id)
        if pi_status in (None, "processing", "awaiting_next_action"):
            messages.info(request, "A payment is already in progress for this order. Please wait a moment and refresh.")
            return redirect("order_detail", order_id=order_id)
        in_progress.status = "failed"
        in_progress.save(update_fields=["status", "updated_at"])
    payment_method_type = request.POST.get("payment_method_type", "gcash")

    method_map = {
        "gcash": ["gcash"],
        "grab_pay": ["grab_pay"],
        "maya": ["paymaya"],
    }
    allowed_methods = method_map.get(payment_method_type)
    if not allowed_methods:
        messages.error(
            request,
            "Unsupported payment method for online processing. Try GCash, GrabPay, or Maya.",
        )
        return redirect("payment_checkout", order_id=order_id)

    if not settings.PAYMONGO_SECRET_KEY:
        messages.error(request, "PayMongo is not configured. Set PAYMONGO_SECRET_KEY in .env.")
        return redirect("payment_checkout", order_id=order_id)

    # Reserve this attempt under a row lock: a double-submitted Pay, or the shop re-weighing
    # mid-payment, sees the in-progress row and backs off instead of racing us.
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if not order.can_pay_online or PayMongoTransaction.in_progress(order).exists():
            messages.info(request, "A payment is already in progress for this order. Please wait a moment and refresh.")
            return redirect("order_detail", order_id=order_id)
        try:
            amount_centavos = int(Decimal(order.total_amount) * 100)
        except (InvalidOperation, TypeError, ValueError):
            amount_centavos = 0
        if amount_centavos <= 0:
            messages.error(request, "Invalid order amount. Please try again.")
            return redirect("order_detail", order_id=order_id)
        PayMongoTransaction.objects.update_or_create(order=order, defaults={
            "payment_intent_id": "", "payment_method_id": "", "payment_id": "",
            "payment_method_type": payment_method_type, "amount": amount_centavos, "status": "processing",
        })
    started = False   # set once PayMongo has taken over; otherwise the reservation is released

    try:
        pi_resp = requests.post(
            "https://api.paymongo.com/v1/payment_intents",
            auth=(settings.PAYMONGO_SECRET_KEY, ""),
            json={
                "data": {
                    "attributes": {
                        "amount": amount_centavos,
                        "currency": "PHP",
                        "payment_method_allowed": allowed_methods,
                        "description": f"CLEFRESH Order #{order.id}",
                        "capture_type": "automatic",
                    }
                }
            },
            timeout=15,
        )
        pi_data = pi_resp.json()
        if pi_resp.status_code >= 400 or "errors" in pi_data:
            error_message = _extract_paymongo_error(pi_resp) or "Payment initialization failed."
            messages.error(request, error_message)
            return redirect("payment_checkout", order_id=order_id)

        pi_id = pi_data["data"]["id"]
        PayMongoTransaction.objects.filter(order=order).update(payment_intent_id=pi_id)
        order.paymongo_payment_intent_id = pi_id
        update_fields = ["paymongo_payment_intent_id", "updated_at"]
        if order.payment_method != "cod":
            # Online order switching e-wallets. A cash-on-delivery order keeps "cod" until the
            # payment succeeds (see _mark_order_paid), so an abandoned attempt never blocks delivery.
            order.payment_method = payment_method_type
            update_fields.append("payment_method")
        order.save(update_fields=update_fields)

        return_url = request.build_absolute_uri(f"/payment/return/?order_id={order.id}")
        billing_name = (
            request.user.get_full_name().strip()
            or request.user.username
            or request.user.email
            or f"Customer {request.user.pk}"
        )

        pm_resp = requests.post(
            "https://api.paymongo.com/v1/payment_methods",
            auth=(settings.PAYMONGO_SECRET_KEY, ""),
            json={
                "data": {
                    "attributes": {
                        "type": allowed_methods[0],
                        "billing": {
                            "name": billing_name,
                            "email": order.customer.email or "",
                            "phone": request.user.contact_number or "",
                        },
                    }
                }
            },
            timeout=15,
        )
        pm_data = pm_resp.json()
        if pm_resp.status_code >= 400 or "errors" in pm_data:
            error_message = _extract_paymongo_error(pm_resp) or "Payment method creation failed."
            messages.error(request, error_message)
            return redirect("payment_checkout", order_id=order_id)

        pm_id = pm_data["data"]["id"]
        order.paymongo_payment_method_id = pm_id
        order.save(update_fields=["paymongo_payment_method_id", "updated_at"])

        attach_resp = requests.post(
            f"https://api.paymongo.com/v1/payment_intents/{pi_id}/attach",
            auth=(settings.PAYMONGO_SECRET_KEY, ""),
            json={
                "data": {
                    "attributes": {
                        "payment_method": pm_id,
                        "return_url": return_url,
                    }
                }
            },
            timeout=15,
        )
        attach_data = attach_resp.json()
        if attach_resp.status_code >= 400 or "errors" in attach_data:
            error_message = _extract_paymongo_error(attach_resp) or "Payment attachment failed."
            messages.error(request, error_message)
            return redirect("payment_checkout", order_id=order_id)

        attributes = attach_data["data"]["attributes"]
        pi_status = attributes["status"]

        if pi_status == "awaiting_next_action":
            redirect_url = attributes["next_action"]["redirect"]["url"]
            PayMongoTransaction.objects.update_or_create(
                order=order,
                defaults={
                    "payment_intent_id": pi_id,
                    "payment_method_id": pm_id,
                    "payment_method_type": payment_method_type,
                    "amount": amount_centavos,
                    "status": "awaiting_payment_method",
                },
            )
            started = True
            return redirect(redirect_url)

        if pi_status == "succeeded":
            payment_id = ""
            payments = attributes.get("payments") or []
            if payments:
                payment_id = payments[0].get("id", "")
            PayMongoTransaction.objects.update_or_create(
                order=order,
                defaults={
                    "payment_intent_id": pi_id,
                    "payment_method_id": pm_id,
                    "payment_method_type": payment_method_type,
                    "payment_id": payment_id,
                    "amount": amount_centavos,
                    "status": "succeeded",
                },
            )
            started = True
            # Shared path: marks paid and records the e-wallet on a cash-on-delivery order.
            if _mark_order_paid(order, pi_id, payment_id):
                send_email_notification("payment_confirmed", order.customer, order=order)
            return redirect("order_detail", order_id=order.id)

        if pi_status == "processing":
            PayMongoTransaction.objects.update_or_create(
                order=order,
                defaults={
                    "payment_intent_id": pi_id,
                    "payment_method_id": pm_id,
                    "payment_method_type": payment_method_type,
                    "amount": amount_centavos,
                    "status": "processing",
                },
            )
            started = True
            messages.info(request, "Payment is processing. Please refresh the order page in a moment.")
            return redirect("order_detail", order_id=order.id)

        last_payment_error = attributes.get("last_payment_error") or {}
        messages.error(request, last_payment_error.get("message") or f"Payment status: {pi_status}")
        return redirect("payment_checkout", order_id=order_id)

    except Exception:
        logger.exception("Payment processing error for order %s", order_id)
        messages.error(request, "Payment processing failed. Please try again.")
        return redirect("payment_checkout", order_id=order_id)
    finally:
        if not started:
            PayMongoTransaction.objects.filter(order=order, status="processing").update(status="failed")


@role_required('customer')
def payment_return_view(request):
    order_id = request.GET.get("order_id")
    if not order_id:
        return redirect("order_list")

    order = get_object_or_404(Order, id=order_id, customer=request.user)

    if order.paymongo_payment_intent_id:
        try:
            resp = requests.get(
                f"https://api.paymongo.com/v1/payment_intents/{order.paymongo_payment_intent_id}",
                auth=(settings.PAYMONGO_SECRET_KEY, ""),
                timeout=10,
            )
            data = resp.json()
            if resp.status_code < 400 and "data" in data:
                pi_status = data["data"]["attributes"]["status"]
                if pi_status == "succeeded":
                    attributes = data["data"]["attributes"]
                    payment_id = ""
                    payments = attributes.get("payments") or []
                    if payments:
                        payment_id = payments[0].get("id", "")
                    # Only email on the first transition; refreshes / webhook race won't resend.
                    if _mark_order_paid(order, order.paymongo_payment_intent_id, payment_id):
                        send_email_notification("payment_confirmed", order.customer, order=order)
                    messages.success(request, "Payment successful!")
                elif pi_status == "awaiting_payment_method":
                    # Backed out of / failed on the e-wallet page: release the attempt so
                    # delivery, COD and retries aren't blocked.
                    PayMongoTransaction.objects.filter(
                        order=order, payment_intent_id=order.paymongo_payment_intent_id,
                        status__in=PayMongoTransaction.IN_PROGRESS,
                    ).update(status="failed")
                    messages.warning(request, "The payment wasn't completed. You can try again.")
                else:
                    messages.warning(request, f"Payment status: {pi_status}")
        except Exception:
            logger.exception("Failed to verify payment intent %s for order %s", order.paymongo_payment_intent_id, order.id)

    return redirect("order_detail", order_id=order.id)


@csrf_exempt
def paymongo_webhook_view(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    sig_header = request.headers.get("Paymongo-Signature", "")
    raw_body = request.body.decode("utf-8")

    if not settings.PAYMONGO_WEBHOOK_SECRET:
        return HttpResponse(status=503)
    if not sig_header:
        return HttpResponse(status=400)
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(","))
        expected = hmac.new(
            settings.PAYMONGO_WEBHOOK_SECRET.encode(),
            f"{parts.get('t', '')}.{raw_body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        try:
            livemode = bool(json.loads(raw_body)["data"]["attributes"].get("livemode"))
        except (ValueError, KeyError, TypeError):
            livemode = False
        received = parts.get("li", "") if livemode else parts.get("te", "")
        if not hmac.compare_digest(expected, received):
            return HttpResponse(status=400)
    except Exception:
        logger.warning("Failed to parse PayMongo signature header: %s", sig_header)
        return HttpResponse(status=400)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("PayMongo webhook received invalid JSON body")
        return HttpResponse(status=400)

    event_id = payload.get("data", {}).get("id", "")
    event_attrs = payload.get("data", {}).get("attributes", {})
    event_type = event_attrs.get("type", "")
    event_data = event_attrs.get("data", {})

    if not event_type:
        return HttpResponse(status=200)

    try:
        # Event record + processing share one transaction: if processing fails, the
        # WebhookEvent row rolls back so PayMongo's retry is processed, not skipped.
        with transaction.atomic():
            if event_id:
                _, created = WebhookEvent.objects.get_or_create(
                    event_id=event_id,
                    defaults={"event_type": event_type},
                )
                if not created:
                    return HttpResponse(status=200)

            if event_type == "payment.paid":
                pi_id = event_data.get("attributes", {}).get("payment_intent_id", "")
                if pi_id:
                    order = Order.objects.filter(paymongo_payment_intent_id=pi_id).first()
                    if order:
                        payment_id = event_data.get("id", "")
                        newly_paid = _mark_order_paid(order, pi_id, payment_id)
                        PayMongoTransaction.objects.filter(order=order).update(
                            paymongo_event_type=event_type,
                            raw_payload=payload,
                        )
                        if newly_paid:
                            Notification.objects.create(
                                user=order.customer,
                                message=f"Payment confirmed for Order #{order.id}.",
                                link=f"/orders/{order.id}/",
                            )
                            transaction.on_commit(
                                lambda o=order: send_email_notification("payment_confirmed", o.customer, order=o)
                            )

            elif event_type == "payment.failed":
                pi_id = event_data.get("attributes", {}).get("payment_intent_id", "")
                if pi_id:
                    order = Order.objects.filter(paymongo_payment_intent_id=pi_id).exclude(payment_status="paid").first()
                    if order:
                        order.payment_status = "failed"
                        order.save(update_fields=["payment_status", "updated_at"])
                        # Clears the duplicate-payment guard so the customer can retry.
                        PayMongoTransaction.objects.filter(order=order, payment_intent_id=pi_id).update(
                            status="failed",
                            paymongo_event_type=event_type,
                        )

            elif event_type == "refund.succeeded":
                refund_id = event_data.get("id", "")
                if refund_id:
                    refund = Refund.objects.filter(paymongo_refund_id=refund_id).first()
                    if refund:
                        refund.status = "succeeded"
                        refund.save(update_fields=["status"])
                        refund.order.payment_status = "refunded"
                        refund.order.save(update_fields=["payment_status", "updated_at"])
                        PayMongoTransaction.objects.filter(order=refund.order).update(status="refunded")
                        amount_php = Decimal(refund.amount) / 100
                        transaction.on_commit(lambda r=refund, amt=amount_php: send_email_notification(
                            "refund_processed",
                            r.order.customer,
                            order=r.order,
                            extra={"amount": amt},
                        ))

            elif event_type == "refund.failed":
                refund_id = event_data.get("id", "")
                refund = Refund.objects.filter(paymongo_refund_id=refund_id).first() if refund_id else None
                if refund:
                    # Frees the duplicate-refund guard so the shop can retry.
                    refund.status = "failed"
                    refund.save(update_fields=["status"])
                    _notify_refund_needed(refund.order, "the refund failed at PayMongo.")

    except Exception:
        logger.exception("Failed to process PayMongo webhook")
        return HttpResponse(status=500)

    return HttpResponse(status=200)


@role_required('shop_owner')
def initiate_refund_view(request, order_id):
    if request.method != "POST":
        return redirect("shop_dashboard_payments")

    order = get_object_or_404(Order, id=order_id, shop__owner=request.user, shop__status="approved")
    _VALID_REFUND_REASONS = {'duplicate', 'fraudulent', 'others'}
    reason = request.POST.get("reason", "others")
    if reason not in _VALID_REFUND_REASONS:
        reason = "others"
    ok, msg = refund_order(order, reason=reason, initiated_by=request.user)
    (messages.success if ok else messages.error)(request, msg)
    return redirect("shop_dashboard_payments")
