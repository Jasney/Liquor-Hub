from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("age-confirm/", views.age_confirm, name="age_confirm"),
    path("shop/", views.shop, name="shop"),
    path("shop/<slug:category_slug>/", views.shop, name="category"),
    path("product/<slug:slug>/", views.product_detail, name="product_detail"),
    path("cart/", views.cart_detail, name="cart"),
    path("cart/add/<int:variant_id>/", views.cart_add, name="cart_add"),
    path("checkout/", views.checkout, name="checkout"),
    path("order/success/", views.order_success, name="order_success"),
    path("payments/mock/<str:order_number>/", views.mock_payment, name="mock_payment"),
    path("receipt/<str:order_number>/<str:token>/", views.download_receipt, name="download_receipt"),
    path("mpesa/callback/", views.mpesa_callback, name="mpesa_callback"),
    path("payments/pesapal/callback/", views.pesapal_callback, name="pesapal_callback"),
    path("contact/", views.contact, name="contact"),
]
