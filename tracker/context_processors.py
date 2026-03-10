from django.contrib.auth.models import User

def admin_context(request):
    """
    Injects admin-specific variables into the global context if the user is staff.
    This ensures the user filter in the sidebar is consistent across all pages.
    """
    if not request.user.is_authenticated or not request.user.is_staff:
        return {}

    from .views import _get_user_filter
    
    return {
        'all_users': User.objects.all().order_by('username'),
        'selected_user_ids': _get_user_filter(request) or [],
    }
