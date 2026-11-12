from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from store.checkout import release_reservations
from store.models import Order, Payment, StockReservation


class Command(BaseCommand):
    help = "Release expired reservations only when no payment outcome is unresolved."

    def handle(self, *args, **options):
        order_ids = StockReservation.objects.filter(status=StockReservation.Status.RESERVED,
                                                     expires_at__lte=timezone.now()).values_list("order_id", flat=True).distinct()
        released = held = 0
        for order_id in order_ids:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=order_id)
                pending = order.payments.filter(status=Payment.Status.PENDING)
                unresolved = order.payments.filter(status__in=[Payment.Status.PENDING, Payment.Status.UNCERTAIN])
                if unresolved.exists():
                    pending.update(status=Payment.Status.UNCERTAIN, uncertain_since=timezone.now())
                    order.payment_status = Order.PaymentStatus.UNCERTAIN
                    order.save(update_fields=["payment_status"])
                    held += 1
                    continue
                released += release_reservations(order, expired=True)
        self.stdout.write(f"Released {released} reservation line(s); held {held} unresolved order(s).")
