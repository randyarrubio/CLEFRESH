"""Who may work a shop's counter: its owner, or one of its active attendants."""
from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from accounts.decorators import role_required


def staff_shop(user):
    """The approved shop this user runs or works at, or None."""
    if not user.is_authenticated:
        return None
    if user.role == 'shop_owner':
        return user.shops.filter(status='approved').first()
    if user.role == 'attendant':
        profile = getattr(user, 'attendant_profile', None)
        if profile and profile.is_active and profile.shop.status == 'approved':
            return profile.shop
    return None


def shop_staff_required(view_func):
    """Owner or active attendant of an approved shop; the shop is passed as request.staff_shop."""
    @role_required('shop_owner', 'attendant')
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        shop = staff_shop(request.user)
        if shop is None:
            if request.user.role == 'attendant':
                messages.error(request, 'Your attendant account is inactive. Please contact the shop owner.')
                return redirect('/')
            return redirect('shop_dashboard')
        request.staff_shop = shop
        return view_func(request, *args, **kwargs)
    return wrapper
