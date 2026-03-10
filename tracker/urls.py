from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('process/', views.process_nfce_url, name='process_nfce'),
    path('confirm_refresh/', views.confirm_refresh, name='confirm_refresh'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('analytics/', views.analytics_dashboard, name='analytics_dashboard'),
    path('smart-cart/', views.smart_cart, name='smart_cart'),
    path('api/charts/', views.api_chart_data, name='api_chart_data'),
    path('api/products/search/', views.product_search_api, name='product_search_api'),
    path('categories/', views.category_list, name='category_list'),
    path('categories/add/', views.category_create, name='category_create'),
    path('categories/update/', views.category_update, name='category_update'),
    path('categories/delete/<int:category_id>/', views.category_delete, name='category_delete'),
    path('product/update-category/', views.update_product_category, name='update_product_category'),
    path('product/update-details/', views.update_product_details, name='update_product_details'),
    path('product/link-variant/', views.link_product_variant, name='link_product_variant'),
    path('market/', views.product_comparison, name='product_comparison'),
    path('inflation/', views.inflation_analysis, name='inflation_analysis'),
    path('optimizer/', views.shopping_optimizer, name='shopping_optimizer'),
    path('receipts/', views.receipt_list, name='receipt_list'),
    path('receipt/<int:receipt_id>/', views.receipt_detail, name='receipt_detail'),
    path('receipt/<int:receipt_id>/delete/', views.delete_receipt, name='delete_receipt'),
    path('receipt/<int:receipt_id>/refresh/', views.refresh_receipt, name='refresh_receipt'),
    path('product/<int:product_id>/', views.product_history, name='product_history'),
    path('maintenance/', views.system_maintenance, name='system_maintenance'),
]
