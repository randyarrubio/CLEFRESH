from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('/auth/login/')
            if request.user.role not in roles and not request.user.is_superuser:
                messages.error(request, 'You do not have permission to access this page.')
                return redirect('/')
            # Shop owners must verify their email before accessing any shop-owner area.
            if (
                request.user.role == 'shop_owner'
                and not getattr(request.user, 'email_verified', False)
                and not request.user.is_superuser
            ):
                messages.warning(request, 'Please verify your email to continue.')
                return redirect('verify_email')
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
