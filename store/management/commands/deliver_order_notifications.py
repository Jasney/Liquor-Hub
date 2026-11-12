from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from store.models import NotificationOutbox, Order, Payment


class Command(BaseCommand):
    help = "Deliver queued verified-purchase email notifications with retry tracking."

    def handle(self, *args, **options):
        recipient = getattr(settings, "ORDER_NOTIFICATION_EMAIL", "")
        if not recipient:
            self.stdout.write("ORDER_NOTIFICATION_EMAIL is not configured; notifications remain queued.")
            return
        due = NotificationOutbox.objects.filter(status__in=["pending", "failed"], next_attempt_at__lte=timezone.now())[:100]
        for event in due:
            try:
                order = Order.objects.get(pk=event.payload["order_id"], payment_status=Order.PaymentStatus.PAID)
                payment = Payment.objects.get(pk=event.payload["payment_id"], status=Payment.Status.SUCCEEDED)
                items = "\n".join(f"{line.quantity} x {line.variant}" for line in order.items.select_related("variant__product"))
                send_mail(
                    f"Paid order {order.order_number}",
                    f"Order: {order.order_number}\nAmount: {payment.currency} {payment.amount}\nMethod: {payment.provider_method}\nCustomer: {order.customer_name} / {order.phone_number}\nDelivery: {order.delivery_address}\n\n{items}",
                    settings.DEFAULT_FROM_EMAIL, [recipient], fail_silently=False,
                )
                event.status = "delivered"
                event.delivered_at = timezone.now()
                event.last_error = ""
            except Exception as error:
                event.status = "failed"
                event.last_error = str(error)[:1000]
                event.attempts += 1
                event.next_attempt_at = timezone.now() + timedelta(minutes=min(60, 2 ** min(event.attempts, 6)))
            event.save(update_fields=["status", "delivered_at", "last_error", "attempts", "next_attempt_at"])
