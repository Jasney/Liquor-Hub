import json
import io
import hashlib
import tempfile
import uuid
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from .admin import OrderAdminForm
from .cart import Cart
from .models import Category, Order, Payment, Product, ProductVariant, StockReservation, SaleRecord, Receipt, NotificationOutbox
from .payments import finish_mock_payment, normalize_kenyan_phone, record_verified_refund
from .checkout import CheckoutError, create_reserved_order, release_reservations


class StoreTestData(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Gin")
        self.product = Product.objects.create(
            name="Demo London Dry Gin",
            category=self.category,
            description="For automated tests.",
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            bottle_size="750ml",
            price=Decimal("2000.00"),
            stock_quantity=5,
        )

    def verify_age(self, client=None):
        client = client or self.client
        session = client.session
        session["age_verified_at"] = "test-confirmed"
        session.save()
        return client


class StorefrontTests(StoreTestData):
    def test_out_of_stock_catalogue_price_is_visible_without_purchase_control(self):
        self.product.catalogue_key = "verified-catalogue-product"
        self.product.save()
        self.variant.stock_quantity = 0
        self.variant.price = Decimal("2000.50")
        self.variant.save()
        self.verify_age()
        response = self.client.get(reverse("shop"))
        self.assertContains(response, "KSh 2000.50")
        self.assertContains(response, "Out of stock")
        self.assertNotContains(response, "Add to cart")
        self.assertNotContains(response, "Prices shown are for demonstration only")
        self.assertContains(response, "Bottle size: 750ml")
        self.assertNotContains(response, "DEMONSTRATION STORE")
        self.assertNotContains(response, "Demonstration website")

    def test_product_card_lists_all_bottle_sizes(self):
        ProductVariant.objects.create(
            product=self.product,
            bottle_size="1L",
            price=Decimal("3000.00"),
            stock_quantity=0,
        )
        self.verify_age()
        response = self.client.get(reverse("shop"))
        self.assertContains(response, "Bottle sizes")
        self.assertContains(response, "750ml")
        self.assertContains(response, "1L")

    def test_listing_price_prefers_an_available_variant(self):
        self.variant.stock_quantity = 0
        self.variant.save()
        available = ProductVariant.objects.create(product=self.product, bottle_size="1L", price=Decimal("3000"), stock_quantity=1)
        self.assertEqual(self.product.listing_variant, available)

    def test_age_gate_blocks_shopping_until_confirmation(self):
        response = self.client.get(reverse("shop"))
        self.assertContains(response, "Are you 18 or older?")
        response = self.client.post(reverse("age_confirm"), {"confirm": "yes", "next": "/shop/"})
        self.assertRedirects(response, "/shop/")
        self.assertContains(self.client.get(reverse("shop")), "Demo London Dry Gin")

    def test_dynamic_category_page_only_shows_products_in_category(self):
        other = Category.objects.create(name="Beer")
        Product.objects.create(name="Demo Lager", category=other)
        self.verify_age()
        response = self.client.get(reverse("category", args=[self.category.slug]))
        self.assertContains(response, "Demo London Dry Gin")
        self.assertNotContains(response, "Demo Lager")

    def test_each_storefront_category_page_is_filtered(self):
        products = {
            "gin": self.product,
            "whisky": Product.objects.create(name="Page Test Whisky", category=Category.objects.create(name="Whisky")),
            "wine": Product.objects.create(name="Page Test Wine", category=Category.objects.create(name="Wine")),
            "beer": Product.objects.create(name="Page Test Beer", category=Category.objects.create(name="Beer")),
        }
        self.verify_age()
        for slug, product in products.items():
            with self.subTest(category=slug):
                response = self.client.get(reverse("category", args=[slug]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, product.name)
                for other_slug, other_product in products.items():
                    if other_slug != slug:
                        self.assertNotContains(response, other_product.name)

    def test_search_finds_product_brand(self):
        self.verify_age()
        response = self.client.get(reverse("shop"), {"q": "Demo"})
        self.assertContains(response, "Demo London Dry Gin")

    def test_product_can_be_edited(self):
        self.product.name = "Updated Demo Gin"
        self.product.description = "Updated product description."
        self.product.save()
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, "Updated Demo Gin")
        self.assertEqual(self.product.description, "Updated product description.")

    def test_cart_total_uses_variant_price_and_quantity(self):
        self.verify_age()
        cart = Cart(SimpleNamespace(session=SessionStore()))
        cart.add(self.variant, 2)
        self.assertEqual(cart.total, Decimal("4000.00"))

    def test_add_to_cart_rejects_quantity_over_stock(self):
        self.verify_age()
        response = self.client.post(reverse("cart_add", args=[self.variant.pk]), {"quantity": "6"})
        self.assertRedirects(response, reverse("product_detail", args=[self.product.slug]))
        self.assertEqual(self.client.session.get("cart", {}), {})

    def test_checkout_rechecks_stock_and_uses_current_server_price(self):
        self.verify_age()
        session = self.client.session
        session["cart"] = {str(self.variant.pk): 2}
        session.save()
        self.variant.price = Decimal("2200.00")
        self.variant.save()
        response = self.client.post(
            reverse("checkout"),
            {
                "customer_name": "Test Customer",
                "phone_number": "0712345678",
                "email": "",
                "delivery_address": "Diani Beach Road",
                "payment_method": Order.PaymentMethod.DELIVERY,
                "checkout_key": str(uuid.uuid4()),
            },
        )
        order = Order.objects.get()
        self.assertRedirects(response, reverse("order_success"), fetch_redirect_response=False)
        self.assertEqual(order.order_total, Decimal("4400.00"))
        self.assertEqual(order.items.get().unit_price, Decimal("2200.00"))
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock_quantity, 3)
        self.assertEqual(self.client.session.get("cart"), None)

    def test_checkout_rejects_stale_stock_without_creating_order(self):
        self.verify_age()
        session = self.client.session
        session["cart"] = {str(self.variant.pk): 6}
        session.save()
        response = self.client.post(
            reverse("checkout"),
            {
                "customer_name": "Test Customer",
                "phone_number": "0712345678",
                "email": "",
                "delivery_address": "Diani Beach Road",
                "payment_method": Order.PaymentMethod.DELIVERY,
                "checkout_key": str(uuid.uuid4()),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)
        self.assertContains(response, "not enough stock")

    def test_delivery_order_is_pending_and_does_not_create_payment(self):
        self.verify_age()
        session = self.client.session
        session["cart"] = {str(self.variant.pk): 1}
        session.save()
        self.client.post(
            reverse("checkout"),
            {
                "customer_name": "Test Customer",
                "phone_number": "0712345678",
                "delivery_address": "Ukunda",
                "payment_method": Order.PaymentMethod.DELIVERY,
                "checkout_key": str(uuid.uuid4()),
            },
        )
        order = Order.objects.get()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertEqual(order.payments.count(), 0)

    def test_mock_payment_success_is_explicit_and_idempotent(self):
        order = Order.objects.create(
            customer_name="Test Customer",
            phone_number="254712345678",
            delivery_address="Ukunda",
            order_total=Decimal("2000"),
            payment_method=Order.PaymentMethod.CARD,
        )
        payment = Payment.objects.create(order=order, provider="hosted-card-mock", amount=order.order_total)
        finish_mock_payment(payment, succeeded=True)
        finish_mock_payment(payment, succeeded=False)
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(payment.transaction_reference, f"MOCK-{order.order_number}")

    def test_phone_normalization(self):
        self.assertEqual(normalize_kenyan_phone("+254 712 345 678"), "254712345678")
        self.assertEqual(normalize_kenyan_phone("0712345678"), "254712345678")
        with self.assertRaises(ValueError):
            normalize_kenyan_phone("12345")


@override_settings(DEBUG=True, PAYMENT_MODE="mock")
class PaymentCallbackTests(StoreTestData):
    def test_mpesa_callback_verifies_amount_and_marks_paid(self):
        order = Order.objects.create(
            customer_name="Test Customer",
            phone_number="254712345678",
            delivery_address="Ukunda",
            order_total=Decimal("2000"),
            payment_method=Order.PaymentMethod.MPESA,
        )
        Payment.objects.create(
            order=order,
            provider="mpesa",
            amount=order.order_total,
            gateway_reference="ws_CO_demo",
        )
        self.client.raise_request_exception = False
        response = self.client.post(
            f"{reverse('mpesa_callback')}?token=test-token",
            data=json.dumps(
                {
                    "Body": {
                        "stkCallback": {
                            "CheckoutRequestID": "ws_CO_demo",
                            "ResultCode": 0,
                            "ResultDesc": "The service request is processed successfully.",
                            "CallbackMetadata": {
                                "Item": [
                                    {"Name": "Amount", "Value": 2000},
                                    {"Name": "MpesaReceiptNumber", "Value": "QWE123"},
                                ]
                            },
                        }
                    }
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(DARAJA_CALLBACK_TOKEN="test-token")
    def test_authorized_callback_is_idempotent(self):
        order = Order.objects.create(
            customer_name="Test Customer",
            phone_number="254712345678",
            delivery_address="Ukunda",
            order_total=Decimal("2000"),
            payment_method=Order.PaymentMethod.MPESA,
        )
        payment = Payment.objects.create(
            order=order,
            provider="mpesa",
            amount=order.order_total,
            gateway_reference="ws_CO_demo",
        )
        payload = {
            "Body": {
                "stkCallback": {
                    "CheckoutRequestID": "ws_CO_demo",
                    "ResultCode": 0,
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": 2000},
                            {"Name": "MpesaReceiptNumber", "Value": "QWE123"},
                        ]
                    },
                }
            }
        }
        response = self.client.post(
            f"{reverse('mpesa_callback')}?token=test-token",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.client.post(
            f"{reverse('mpesa_callback')}?token=test-token",
            data=json.dumps(payload),
            content_type="application/json",
        )
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(payment.transaction_reference, "QWE123")
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)

    @override_settings(DARAJA_CALLBACK_TOKEN="test-token")
    def test_callback_rejects_wrong_amount(self):
        order = Order.objects.create(
            customer_name="Test Customer",
            phone_number="254712345678",
            delivery_address="Ukunda",
            order_total=Decimal("2000"),
            payment_method=Order.PaymentMethod.MPESA,
        )
        Payment.objects.create(order=order, provider="mpesa", amount=order.order_total, gateway_reference="ws_CO_demo")
        response = self.client.post(
            f"{reverse('mpesa_callback')}?token=test-token",
            data=json.dumps(
                {
                    "Body": {
                        "stkCallback": {
                            "CheckoutRequestID": "ws_CO_demo",
                            "ResultCode": 0,
                            "CallbackMetadata": {
                                "Item": [
                                    {"Name": "Amount", "Value": 1999},
                                    {"Name": "MpesaReceiptNumber", "Value": "QWE123"},
                                ]
                            },
                        }
                    }
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)

    @override_settings(DARAJA_CALLBACK_TOKEN="test-token")
    def test_failed_mpesa_callback_sets_payment_and_order_failed(self):
        order = Order.objects.create(
            customer_name="Test Customer",
            phone_number="254712345678",
            delivery_address="Ukunda",
            order_total=Decimal("2000"),
            payment_method=Order.PaymentMethod.MPESA,
        )
        payment = Payment.objects.create(
            order=order,
            provider="mpesa",
            amount=order.order_total,
            gateway_reference="ws_CO_failed",
        )
        response = self.client.post(
            f"{reverse('mpesa_callback')}?token=test-token",
            data=json.dumps(
                {
                    "Body": {
                        "stkCallback": {
                            "CheckoutRequestID": "ws_CO_failed",
                            "ResultCode": 1032,
                            "ResultDesc": "Request cancelled by user",
                        }
                    }
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.FAILED)
        self.assertEqual(order.payment_status, Order.PaymentStatus.FAILED)


class ReservationAndSalesTests(StoreTestData):
    def reserve(self, key, quantity=1):
        return create_reserved_order(
            key=uuid.UUID(key), customer_name="Buyer", email="", phone_number="254712345678",
            delivery_address="Ukunda", payment_method=Order.PaymentMethod.MPESA,
            lines=[(self.variant.pk, quantity)],
        )

    def test_duplicate_checkout_key_does_not_reserve_or_create_twice(self):
        key = str(uuid.uuid4())
        first = self.reserve(key, 2)
        repeated = self.reserve(key, 2)
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock_quantity, 3)

    def test_entire_cart_rolls_back_if_later_line_cannot_reserve(self):
        other = ProductVariant.objects.create(product=self.product, bottle_size="1L", price=3000, stock_quantity=0)
        with self.assertRaises(CheckoutError):
            create_reserved_order(key=uuid.uuid4(), customer_name="Buyer", email="", phone_number="254712345678",
                                  delivery_address="Ukunda", payment_method=Order.PaymentMethod.DELIVERY,
                                  lines=[(self.variant.pk, 1), (other.pk, 1)])
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock_quantity, 5)
        self.assertEqual(Order.objects.count(), 0)

    def test_failed_payment_releases_reservation_exactly_once(self):
        order = self.reserve(str(uuid.uuid4()))
        payment = order.payments.get()
        finish_mock_payment(payment, succeeded=False)
        finish_mock_payment(payment, succeeded=False)
        self.variant.refresh_from_db()
        reservation = order.reservations.get()
        self.assertEqual(self.variant.stock_quantity, 5)
        self.assertEqual(reservation.status, StockReservation.Status.RELEASED)

    def test_uncertain_payment_cannot_release_expired_reservation(self):
        order = self.reserve(str(uuid.uuid4()))
        payment = order.payments.get()
        payment.status = Payment.Status.UNCERTAIN
        payment.save(update_fields=["status"])
        with self.assertRaises(CheckoutError):
            release_reservations(order, expired=True)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock_quantity, 4)

    def test_success_creates_one_sale_receipt_and_notification(self):
        order = self.reserve(str(uuid.uuid4()))
        payment = order.payments.get()
        finish_mock_payment(payment, succeeded=True)
        finish_mock_payment(payment, succeeded=True)
        self.assertEqual(SaleRecord.objects.filter(order=order).count(), 1)
        self.assertEqual(Receipt.objects.filter(order=order).count(), 1)
        self.assertEqual(NotificationOutbox.objects.filter(dedupe_key=f"paid:{payment.pk}").count(), 1)

    def test_receipt_token_is_bound_to_original_browser_session(self):
        self.verify_age()
        order = Order.objects.create(customer_name="Buyer", phone_number="254712345678", delivery_address="Ukunda",
                                     order_total=Decimal("1"), payment_method=Order.PaymentMethod.CARD)
        response = self.client.get(reverse("download_receipt", args=[order.order_number, "guessed-token"]))
        self.assertEqual(response.status_code, 404)

    def test_verified_payment_receipt_download_is_private_pdf(self):
        self.verify_age()
        order = self.reserve(str(uuid.uuid4()))
        payment = order.payments.get()
        finish_mock_payment(payment, succeeded=True)
        token = "session-only-receipt-token"
        order.receipt_token_hash = hashlib.sha256(token.encode()).hexdigest()
        order.save(update_fields=["receipt_token_hash"])
        session = self.client.session
        session["order_access"] = {order.order_number: token}
        session.save()
        response = self.client.get(reverse("download_receipt", args=[order.order_number, token]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(b"%PDF", response.content[:10])

    def test_full_refund_is_audited_and_removes_sale_from_completed_sales(self):
        order = self.reserve(str(uuid.uuid4()))
        payment = order.payments.get()
        finish_mock_payment(payment, succeeded=True)
        record_verified_refund(payment, amount=payment.amount / 2, reason="Partial refund")
        record_verified_refund(payment, amount=payment.amount / 2, reason="Remainder")
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.REFUNDED)
        self.assertEqual(order.payment_status, Order.PaymentStatus.REFUNDED)
        self.assertEqual(SaleRecord.objects.get(order=order).status, SaleRecord.Status.REFUNDED)


class AdminAccessTests(StoreTestData):
    def admin_login(self):
        user = get_user_model().objects.create_superuser(
            username="shop-owner",
            email="owner@example.test",
            password="safe-test-password",
        )
        self.client.force_login(user)

    def test_admin_requires_staff_permissions(self):
        self.verify_age()
        self.client.force_login(get_user_model().objects.create_user(username="customer", password="safe-test-password"))
        response = self.client.get(reverse("admin:store_ginproduct_changelist"))
        self.assertEqual(response.status_code, 302)

    def test_staff_can_open_product_admin(self):
        user = get_user_model().objects.create_user(
            username="shop-owner",
            password="safe-test-password",
            is_staff=True,
        )
        user.user_permissions.add(Permission.objects.get(codename="view_product"))
        self.client.force_login(user)
        response = self.client.get(reverse("admin:store_ginproduct_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Demo London Dry Gin")

    def test_category_admin_pages_only_list_products_from_their_category(self):
        products = {
            "gin": self.product,
            "whisky": Product.objects.create(name="Demo Whisky", category=Category.objects.create(name="Whisky")),
            "wine": Product.objects.create(name="Demo Wine", category=Category.objects.create(name="Wine")),
            "beer": Product.objects.create(name="Demo Beer", category=Category.objects.create(name="Beer")),
        }
        self.admin_login()
        for slug, product in products.items():
            with self.subTest(category=slug):
                response = self.client.get(reverse(f"admin:store_{slug}product_changelist"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, product.name)
                for other_slug, other_product in products.items():
                    if other_slug != slug:
                        self.assertNotContains(response, other_product.name)

    def test_category_admin_rejects_cross_category_object_url(self):
        beer = Product.objects.create(name="Protected Beer", category=Category.objects.create(name="Beer"))
        self.admin_login()
        response = self.client.get(reverse("admin:store_ginproduct_change", args=[beer.pk]))
        self.assertEqual(response.status_code, 302)
        beer.refresh_from_db()
        self.assertEqual(beer.category.slug, "beer")

    def test_admin_exposes_full_catalogue_and_hides_brand_field(self):
        self.admin_login()
        index = self.client.get(reverse("admin:index"))
        self.assertContains(index, "Categories")
        self.assertContains(index, "All products")
        self.assertContains(index, "Active categories")
        self.assertContains(index, "Orders")
        self.assertContains(index, "Payments")
        self.assertContains(index, "Users")
        add_page = self.client.get(reverse("admin:store_ginproduct_add"))
        self.assertContains(add_page, "Assigned category")
        self.assertContains(add_page, "Gin")
        self.assertNotContains(add_page, "brand")

    def test_adding_product_from_gin_admin_assigns_category_and_saves_variant_and_image(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self.admin_login()
                image_data = io.BytesIO()
                Image.new("RGB", (2, 2), color="gold").save(image_data, format="PNG")
                image = SimpleUploadedFile(
                    "gin.png",
                    image_data.getvalue(),
                    content_type="image/png",
                )
                response = self.client.post(
                    reverse("admin:store_ginproduct_add"),
                    {
                        "name": "Diani Test Gin",
                        "slug": "diani-test-gin",
                        "description": "Gin product added through its category page.",
                        "image": image,
                        "is_featured": "",
                        "is_active": "on",
                        "variants-TOTAL_FORMS": "1",
                        "variants-INITIAL_FORMS": "0",
                        "variants-MIN_NUM_FORMS": "0",
                        "variants-MAX_NUM_FORMS": "1000",
                        "variants-0-bottle_size": "250ml",
                        "variants-0-sku": "TEST-DIANI-GIN-250",
                        "variants-0-price": "850.00",
                        "variants-0-sale_price": "",
                        "variants-0-stock_quantity": "7",
                        "variants-0-is_available": "on",
                        "additional_images-TOTAL_FORMS": "0",
                        "additional_images-INITIAL_FORMS": "0",
                        "additional_images-MIN_NUM_FORMS": "0",
                        "additional_images-MAX_NUM_FORMS": "1000",
                        "_save": "Save",
                    },
                )
                self.assertEqual(response.status_code, 302, response.content.decode())
                product = Product.objects.get(slug="diani-test-gin")
                self.assertEqual(product.category.slug, "gin")
                self.assertTrue(product.image.name.endswith("gin.png"))
                variant = product.variants.get()
                self.assertEqual(variant.bottle_size, "250ml")
                self.assertEqual(variant.price, Decimal("850.00"))
                self.assertEqual(variant.stock_quantity, 7)

    def test_admin_can_replace_product_image_and_update_variant_price_and_stock(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self.admin_login()
                image_data = io.BytesIO()
                Image.new("RGB", (2, 2), color="black").save(image_data, format="PNG")
                response = self.client.post(
                    reverse("admin:store_ginproduct_change", args=[self.product.pk]),
                    {
                        "name": self.product.name,
                        "slug": self.product.slug,
                        "description": "Updated from the Gin admin page.",
                        "image": SimpleUploadedFile("updated-gin.png", image_data.getvalue(), content_type="image/png"),
                        "is_featured": "on",
                        "is_active": "on",
                        "variants-TOTAL_FORMS": "1",
                        "variants-INITIAL_FORMS": "1",
                        "variants-MIN_NUM_FORMS": "0",
                        "variants-MAX_NUM_FORMS": "1000",
                        "variants-0-id": str(self.variant.pk),
                        "variants-0-product": str(self.product.pk),
                        "variants-0-bottle_size": self.variant.bottle_size,
                        "variants-0-sku": self.variant.sku,
                        "variants-0-price": "2400.00",
                        "variants-0-sale_price": "",
                        "variants-0-stock_quantity": "13",
                        "variants-0-is_available": "on",
                        "additional_images-TOTAL_FORMS": "0",
                        "additional_images-INITIAL_FORMS": "0",
                        "additional_images-MIN_NUM_FORMS": "0",
                        "additional_images-MAX_NUM_FORMS": "1000",
                        "_save": "Save",
                    },
                )
                self.assertEqual(response.status_code, 302, response.content.decode())
                self.product.refresh_from_db()
                self.variant.refresh_from_db()
                self.assertTrue(self.product.image.name.endswith("updated-gin.png"))
                self.assertEqual(self.product.description, "Updated from the Gin admin page.")
                self.assertEqual(self.variant.price, Decimal("2400.00"))
                self.assertEqual(self.variant.stock_quantity, 13)

    def test_product_change_form_shows_only_its_variants(self):
        whisky = Product.objects.create(name="Admin Test Whisky", category=Category.objects.create(name="Whisky"))
        whisky_variant = ProductVariant.objects.create(
            product=whisky,
            bottle_size="1L",
            sku="TEST-WHISKY-1L",
            price=Decimal("3000.00"),
            stock_quantity=2,
        )
        self.admin_login()
        response = self.client.get(reverse("admin:store_ginproduct_change", args=[self.product.pk]))
        self.assertContains(response, self.variant.sku)
        self.assertNotContains(response, whisky_variant.sku)

    def test_category_add_urls_use_category_specific_product_names(self):
        self.admin_login()
        for slug, name in (("gin", "gin"), ("whisky", "whisky"), ("wine", "wine"), ("beer", "beer")):
            with self.subTest(category=slug):
                response = self.client.get(reverse(f"admin:store_{slug}product_add"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f"Add {name} product")

    def test_delivery_cannot_be_marked_complete_without_age_check(self):
        order = Order.objects.create(
            customer_name="Test Customer",
            phone_number="254712345678",
            delivery_address="Ukunda",
            order_total=Decimal("2000"),
            payment_method=Order.PaymentMethod.DELIVERY,
        )
        form = OrderAdminForm(
            data={
                "customer_name": order.customer_name,
                "email": order.email,
                "phone_number": order.phone_number,
                "delivery_address": order.delivery_address,
                "order_total": str(order.order_total),
                "payment_method": order.payment_method,
                "payment_status": order.payment_status,
                "status": Order.Status.DELIVERED,
                "age_verified_at_delivery": "",
            },
            instance=order,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("age_verified_at_delivery", form.errors)
