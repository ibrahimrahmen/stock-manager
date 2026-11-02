"""Clean URL map used only on the public storefront domains (see
inventory.middleware.SHOP_HOSTS). On those hosts the store lives at the domain
root: '/', '/produit/<id>/', '/produits/', '/commande/'. The image resizer and
the order API keep their '/shop/...' / '/api/shop/...' paths because templates
and JS reference them directly; they are not user-facing page URLs.
"""
from django.urls import path
from . import views

urlpatterns = [
    path("", views.shop_home, name="shop_home"),
    path("produit/<int:offer_id>/", views.shop_product, name="shop_product"),
    path("produits/", views.shop_collection, name="shop_collection"),
    path("commande/", views.shop_checkout, name="shop_checkout"),
    path("shop/img/", views.shop_img, name="shop_img"),
    path("api/shop/order/", views.api_shop_order, name="api_shop_order"),
]
