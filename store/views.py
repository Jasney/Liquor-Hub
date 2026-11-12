from decimal import Decimal
import hashlib
import secrets
import uuid

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.http import Http404, JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .cart import Cart
from .forms import CheckoutForm
from .models import Category, ImageCandidate, Order, OrderItem, Payment, Product, ProductVariant, Receipt
from .checkout import CheckoutError, create_reserved_order
from .payments import (
    PaymentProviderError,
    daraja_configured,
    finish_mock_payment,
    initiate_mpesa_stk,
    process_mpesa_callback,
    safe_callback_payload,
    valid_callback_token,
    initiate_pesapal,
    reconcile_pesapal,
    pesapal_configured,
)


def active_products():
    return (
        Product.objects.filter(is_active=True, category__is_active=True)
        .annotate(
            has_stock=Exists(
                ProductVariant.objects.filter(product_id=OuterRef("pk"), is_available=True, stock_quantity__gt=0)
            )
        )
        .select_related("category")
        .prefetch_related(
            Prefetch("image_candidates", queryset=ImageCandidate.objects.filter(published_at__isnull=False)),
            Prefetch("variants", queryset=ProductVariant.objects.order_by("price")),
            Prefetch(
                "variants",
                queryset=ProductVariant.objects.filter(is_available=True, stock_quantity__gt=0).order_by("price"),
                to_attr="available_variants",
            ),
        )
    )


def home(request):
    categories = Category.objects.filter(is_active=True).annotate(
        active_product_count=Count("products", filter=Q(products__is_active=True))
    )
    featured = active_products().filter(is_featured=True)[:8]
    if not featured:
        featured = active_products()[:8]
    return render(request, "store/home.html", {"categories": categories, "featured_products": featured})


def shop(request, category_slug=None):
    products = active_products()
    category = None
    if category_slug:
        category = get_object_or_404(Category, slug=category_slug, is_active=True)
        products = products.filter(category=category)
    query = request.GET.get("q", "").strip()
    if query:
        products = products.filter(Q(name__icontains=query) | Q(description__icontains=query))
    page = Paginator(products, 12).get_page(request.GET.get("page"))
    return render(
        request,
        "store/shop.html",
        {"page_obj": page, "category": category, "query": query, "categories": Category.objects.filter(is_active=True)},
    )


def product_detail(request, slug):
    product = get_object_or_404(
        active_products().prefetch_related("additional_images"),
        slug=slug,
    )
    return render(request, "store/product_detail.html", {"product": product})


@require_POST
def cart_add(request, variant_id):
    variant = get_object_or_404(
        ProductVariant.objects.select_related("product", "product__category"),
        pk=variant_id,
        product__is_active=True,
        product__category__is_active=True,
        is_available=True,
        stock_quantity__gt=0,
    )
    try:
        quantity = int(request.POST.get("quantity", "1"))
    except (TypeError, ValueError):
        quantity = 0
    if quantity < 1 or quantity > variant.stock_quantity:
        messages.error(request, f"Choose a quantity between 1 and {variant.stock_quantity}.")
        return redirect("product_detail", slug=variant.product.slug)
    cart = Cart(request)
    existing = int(cart.data.get(str(variant.pk), 0))
    if existing + quantity > variant.stock_quantity:
        messages.error(request, f"Only {variant.stock_quantity} unit(s) are currently available.")
        return redirect("product_detail", slug=variant.product.slug)
    cart.add(variant, quantity)
    messages.success(request, f"{variant.product.name} added to your cart.")
    return redirect("cart")


def cart_detail(request):
    cart = Cart(request)
    if request.method == "POST":
        action = request.POST.get("action")
        variant_id = request.POST.get("variant_id", "")
        if not variant_id.isdigit():
            messages.error(request, "That cart item could not be updated.")
            return redirect("cart")
        if action == "remove":
            cart.remove(variant_id)
            messages.success(request, "Item removed from your cart.")
        elif action == "update":
            try:
                quantity = int(request.POST.get("quantity", "0"))
                variant = ProductVariant.objects.get(pk=variant_id, product__is_active=True)
            except (ValueError, ProductVariant.DoesNotExist):
                messages.error(request, "That product is no longer available.")
                return redirect("cart")
            if quantity < 1 or quantity > variant.stock_quantity or not variant.is_available:
                messages.error(request, f"Choose a quantity between 1 and {variant.stock_quantity}.")
            else:
                cart.set_quantity(variant_id, quantity)
                messages.success(request, "Cart updated.")
        return redirect("cart")
    return render(request, "store/cart.html", {"cart": cart, "cart_items": cart.items(), "cart_total": cart.total})


def checkout(request):
    cart = Cart(request)
    entries = cart.items()
    if request.method == "POST":
        try:
            posted_key = uuid.UUID(request.POST.get("checkout_key", ""))
        except (ValueError, TypeError):
            posted_key = None
        if posted_key:
            previous = Order.objects.filter(checkout_key=posted_key).first()
            if previous and previous.order_number in request.session.get("order_access", {}):
                request.session["last_order_number"] = previous.order_number
                return redirect("order_success")
    if not entries:
        messages.info(request, "Your cart is empty.")
        return redirect("shop")
    form = CheckoutForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            order, created = create_reserved_order(
                key=form.cleaned_data["checkout_key"], customer_name=form.cleaned_data["customer_name"],
                phone_number=form.cleaned_data["phone_number"], email=form.cleaned_data["email"],
                delivery_address=form.cleaned_data["delivery_address"], payment_method=form.cleaned_data["payment_method"],
                lines=[(e["variant"].pk, e["quantity"]) for e in entries], return_created=True,
            )
            payment = order.payments.first()
        except (CheckoutError, ValueError) as error:
            form.add_error(None, str(error))
            entries = cart.items()
        else:
            if created:
                cart.clear()
                token = secrets.token_urlsafe(32)
                order.receipt_token_hash = hashlib.sha256(token.encode()).hexdigest()
                order.save(update_fields=["receipt_token_hash"])
                request.session.setdefault("order_access", {})[order.order_number] = token
                request.session.modified = True
            if created and order.payment_method == Order.PaymentMethod.MPESA and settings.PAYMENT_MODE == "daraja":
                try:
                    initiate_mpesa_stk(payment, order.phone_number)
                except (PaymentProviderError, ValueError) as error:
                    payment.status = Payment.Status.UNCERTAIN
                    payment.uncertain_since = timezone.now()
                    payment.save(update_fields=["status", "uncertain_since", "updated_at"])
                    order.payment_status = Order.PaymentStatus.UNCERTAIN
                    order.save(update_fields=["payment_status"])
                    messages.error(request, str(error))
            elif created and payment and payment.provider == "pesapal":
                try:
                    return redirect(initiate_pesapal(payment, request))
                except PaymentProviderError as error:
                    messages.error(request, str(error))
            return redirect("order_success")
    return render(
        request,
        "store/checkout.html",
        {"form": form, "cart_items": entries, "cart_total": cart.total,
         "pesapal_mode": settings.PAYMENT_MODE == "pesapal"},
    )


def order_success(request):
    number = request.session.get("last_order_number", "")
    if not number:
        number = next(reversed(request.session.get("order_access", {})), "")
    if not number or number not in request.session.get("order_access", {}):
        raise Http404
    order = get_object_or_404(Order.objects.prefetch_related("items__variant__product", "payments"), order_number=number)
    request.session["last_order_number"] = order.order_number
    return render(
        request,
        "store/order_success.html",
        {
            "order": order,
            "payment": order.payments.first(),
            "mock_mode": settings.DEBUG and settings.PAYMENT_MODE == "mock",
            "daraja_ready": settings.PAYMENT_MODE == "daraja" and daraja_configured(),
            "receipt_token": request.session.get("order_access", {}).get(order.order_number, ""),
            "pesapal_ready": settings.PAYMENT_MODE == "pesapal" and pesapal_configured(),
        },
    )


def mock_payment(request, order_number):
    if not settings.DEBUG:
        raise Http404
    if order_number not in request.session.get("order_access", {}):
        raise Http404
    order = get_object_or_404(Order, order_number=order_number)
    payment = order.payments.first()
    if payment is None or payment.provider not in {"mpesa", "hosted-card-mock"}:
        raise Http404
    if request.method == "POST":
        result = request.POST.get("result")
        if result not in {"success", "failed"}:
            messages.error(request, "Choose a valid mock result.")
        else:
            finish_mock_payment(payment, succeeded=result == "success")
            messages.info(request, "Mock payment outcome recorded. No money was transferred.")
        return redirect("order_success")
    return render(request, "store/mock_payment.html", {"order": order, "payment": payment})


def download_receipt(request, order_number, token):
    order = get_object_or_404(Order, order_number=order_number)
    stored = request.session.get("order_access", {}).get(order_number, "")
    if not stored or not secrets.compare_digest(stored, token) or not order.receipt_token_hash or not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), order.receipt_token_hash):
        raise Http404
    receipt = get_object_or_404(Receipt.objects.select_related("payment"), order=order)
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        import io
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        snap = receipt.snapshot
        y = 800
        for line in ["Liquor Hub Diani", f"Receipt {receipt.receipt_number}", f"Order {order.order_number}", f"Paid: {snap['paid_at']}", ""]:
            pdf.drawString(50, y, line); y -= 22
        for item in snap.get("items", []):
            label = f"{item['quantity']} x {item['name']} {item['size']} @ KES {item['unit_price']} = KES {item['subtotal']}"
            pdf.drawString(50, y, label[:110]); y -= 20
        y -= 16
        for line in [f"Total paid: KES {snap['total']}", f"Payment method: {snap['payment_method']}", f"Reference: {snap['transaction_reference']}"]:
            pdf.drawString(50, y, line); y -= 22
        pdf.save()
        response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{receipt.receipt_number}.pdf"'
        response["Cache-Control"] = "private, no-store"
        return response
    except ImportError as error:
        raise Http404("PDF receipt support is not installed.") from error


@require_POST
@csrf_exempt
def mpesa_callback(request):
    if not settings.DARAJA_CALLBACK_TOKEN or not valid_callback_token(request):
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Unauthorized callback"}, status=403)
    try:
        payment = process_mpesa_callback(safe_callback_payload(request))
    except ValueError as error:
        return JsonResponse({"ResultCode": 1, "ResultDesc": str(error)}, status=400)
    return JsonResponse({"ResultCode": 0, "ResultDesc": f"Payment {payment.status}"})


@csrf_exempt
def pesapal_callback(request):
    tracking_id = request.GET.get("OrderTrackingId") or request.POST.get("OrderTrackingId")
    merchant_reference = request.GET.get("OrderMerchantReference") or request.POST.get("OrderMerchantReference")
    if not tracking_id or not merchant_reference:
        return JsonResponse({"error": "Missing provider references"}, status=400)
    try:
        payment = reconcile_pesapal(tracking_id, merchant_reference)
    except (PaymentProviderError, ValueError) as error:
        return JsonResponse({"error": str(error)}, status=503)
    if request.method == "GET" and payment.order.order_number in request.session.get("order_access", {}):
        request.session["last_order_number"] = payment.order.order_number
        return redirect("order_success")
    return JsonResponse({"order": payment.order.order_number, "status": payment.status})


def age_confirm(request):
    if request.method != "POST" or request.POST.get("confirm") != "yes":
        return render(request, "store/age_gate.html", {"invalid_confirmation": request.method == "POST"}, status=400 if request.method == "POST" else 200)
    from django.utils import timezone

    request.session["age_verified_at"] = timezone.now().isoformat()
    next_url = request.POST.get("next", "/")
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        next_url = "/"
    return redirect(next_url)


def contact(request):
    return render(request, "store/contact.html")
