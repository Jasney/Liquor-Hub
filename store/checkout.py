"""Transactional checkout and inventory reservation operations."""
import time
from datetime import timedelta
from decimal import Decimal

from django.db import OperationalError, transaction
from django.db.models import F
from django.conf import settings
from django.utils import timezone

from .models import Order, OrderItem, Payment, ProductVariant, StockReservation


class CheckoutError(ValueError):
    pass


def create_reserved_order(*, key, customer_name, email, phone_number, delivery_address, payment_method, lines, return_created=False):
    """Reserve all lines or none. ``lines`` is [(variant_id, quantity), ...]."""
    for attempt in range(4):
        try:
            with transaction.atomic():
                existing = Order.objects.filter(checkout_key=key).first()
                if existing:
                    return (existing, False) if return_created else existing
                total = Decimal("0.00")
                checked = []
                for variant_id, quantity in sorted(lines):
                    variant = ProductVariant.objects.select_related("product", "product__category").get(pk=variant_id)
                    if quantity < 1 or not variant.product.is_active or not variant.product.category.is_active or not variant.is_available:
                        raise CheckoutError(f"{variant.product.name} is no longer available.")
                    changed = ProductVariant.objects.filter(
                        pk=variant.pk, is_available=True, product__is_active=True,
                        product__category__is_active=True, stock_quantity__gte=quantity,
                    ).update(stock_quantity=F("stock_quantity") - quantity)
                    if changed != 1:
                        raise CheckoutError(f"There is not enough stock for {variant.product.name}.")
                    price = variant.current_price
                    total += price * quantity
                    checked.append((variant, quantity, price))
                order = Order.objects.create(
                    checkout_key=key, customer_name=customer_name, email=email,
                    phone_number=phone_number, delivery_address=delivery_address,
                    order_total=total, payment_method=payment_method,
                )
                OrderItem.objects.bulk_create([
                    OrderItem(order=order, variant=v, quantity=q, unit_price=p) for v, q, p in checked
                ])
                expires = timezone.now() + timedelta(minutes=20)
                StockReservation.objects.bulk_create([
                    StockReservation(order=order, variant=v, quantity=q, expires_at=expires) for v, q, _ in checked
                ])
                if payment_method != Order.PaymentMethod.DELIVERY:
                    if settings.PAYMENT_MODE == "pesapal":
                        provider = "pesapal"
                    elif payment_method == Order.PaymentMethod.CARD:
                        provider = "hosted-card-mock"
                    else:
                        provider = "mpesa"
                    Payment.objects.create(order=order, provider=provider, amount=total)
                return (order, True) if return_created else order
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))


@transaction.atomic
def release_reservations(order, *, expired=False):
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.payments.filter(status__in=[Payment.Status.PENDING, Payment.Status.UNCERTAIN]).exists():
        raise CheckoutError("Stock is held while payment status is unresolved.")
    now = timezone.now()
    reservations = list(StockReservation.objects.select_for_update().filter(order=order, status=StockReservation.Status.RESERVED))
    for reservation in reservations:
        ProductVariant.objects.filter(pk=reservation.variant_id).update(stock_quantity=F("stock_quantity") + reservation.quantity)
        reservation.status = StockReservation.Status.EXPIRED if expired else StockReservation.Status.RELEASED
        reservation.released_at = now
        reservation.save(update_fields=["status", "released_at"])
    order.stock_released = bool(reservations) or order.stock_released
    order.save(update_fields=["stock_released"])
    return len(reservations)
