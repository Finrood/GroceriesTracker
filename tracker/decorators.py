from django.core.exceptions import PermissionDenied
from functools import wraps
from django.shortcuts import get_object_or_404
from .models import Receipt

def receipt_owner_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, receipt_id, *args, **kwargs):
        receipt = get_object_or_404(Receipt.objects.select_related('user'), id=receipt_id)
        if not request.user.is_staff and receipt.user != request.user:
            raise PermissionDenied("You do not have permission to access this receipt.")
        return view_func(request, receipt_id, *args, **kwargs)
    return _wrapped_view
