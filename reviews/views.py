from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.db import IntegrityError, transaction
from accounts.decorators import role_required
from notifications.models import Notification
from orders.models import Order
from .models import Review


@role_required('customer')
def submit_review_view(request, order_id):
    order = get_object_or_404(Order, id=order_id, customer=request.user, status='delivered')
    if hasattr(order, 'review'):
        messages.info(request, 'You have already reviewed this order.')
        return redirect('order_detail', order_id=order_id)

    if request.method == 'POST':
        try:
            rating = int(request.POST.get('rating', 5))
        except (TypeError, ValueError):
            messages.error(request, 'Rating must be between 1 and 5.')
            return render(request, 'orders/review.html', {'order': order})
        comment = request.POST.get('comment', '').strip()[:2000]
        if rating < 1 or rating > 5:
            messages.error(request, 'Rating must be between 1 and 5.')
            return render(request, 'orders/review.html', {'order': order})
        try:
            with transaction.atomic():
                Review.objects.create(
                    order=order,
                    customer=request.user,
                    shop=order.shop,
                    rating=rating,
                    comment=comment,
                )
        except IntegrityError:          # double submit: the first one already saved it
            messages.info(request, 'You have already reviewed this order.')
            return redirect('order_detail', order_id=order_id)
        Notification.objects.create(
            user=order.shop.owner,
            message=f'New {"★" * rating} review received for Order #{order.id}.',
            link=f'/shop-dashboard/orders/{order.id}/',
        )
        messages.success(request, 'Thank you for your review!')
        return redirect('order_detail', order_id=order_id)

    return render(request, 'orders/review.html', {'order': order})
