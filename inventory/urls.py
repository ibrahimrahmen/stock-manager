from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("scan/shipping/", views.shipping_scan, name="shipping_scan"),
    path("scan/return/", views.return_scan, name="return_scan"),
    path("scan/payment/", views.payment_scan, name="payment_scan"),
    path("stock-value/", views.stock_value, name="stock_value"),
    path("api/scan/shipping/", views.api_scan_shipping, name="api_scan_shipping"),
    path("api/scan/reception/", views.api_scan_reception, name="api_scan_reception"),
    path("api/scan/return/", views.api_scan_return, name="api_scan_return"),
    path("api/scan/return/multiple/", views.api_return_multiple, name="api_return_multiple"),
    path("api/scan/payment/", views.api_scan_payment, name="api_scan_payment"),
    path("api/scan/payment/confirm/", views.api_confirm_payment, name="api_confirm_payment"),
    path("api/order/remove-unit/", views.api_remove_from_order, name="api_remove_from_order"),
    path("api/order/close/", views.api_close_order, name="api_close_order"),
    path("orders/<int:pk>/", views.order_detail, name="order_detail"),
    path("api/orders/<int:pk>/update-amount/", views.api_update_order_amount, name="api_update_order_amount"),
    path("api/orders/<int:pk>/fix-units/", views.api_fix_order_units, name="api_fix_order_units"),
    path("products/", views.products_list, name="products_list"),
    path("admin-panel/", views.admin_panel, name="admin_panel"),
    path("revenue/", views.revenue, name="revenue"),
    path("products/<int:pk>/", views.product_detail, name="product_detail"),
    path("search/", views.search, name="search"),
    path("api/search/", views.api_search, name="api_search"),
]
