"""
URL configuration for GroceriesTracker project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import RedirectView
from django.views.static import serve as static_serve
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('tracker/', include('tracker.urls')),
    path('', RedirectView.as_view(url='/tracker/', permanent=True)),
]

# Media files (uploaded product images).
# History: the VPS served /media/ from nginx via an `alias`; Django's static()
# helper only works with DEBUG=True, and WhiteNoise cannot serve a directory
# that changes at runtime (it indexes at boot, and the enrichment worker writes
# new product images while the app is running). So we serve MEDIA_ROOT from
# Django itself: reads happen per request, and path traversal is guarded by
# django.views.static.serve's internal safe_join.
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', static_serve, {'document_root': settings.MEDIA_ROOT}),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
