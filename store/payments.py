import base64
import hmac
import json
import re
import uuid
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.urls import reverse

from .models import (NotificationOutbox, Order, Payment, Receipt, RefundRecord,
                     SaleRecord, StockReservation)


class PaymentProviderError(Exception):
    pass


def normalize_kenyan_phone(value):
    if not re.fullmatch(r"\+?[0-9\s().-]+", value.strip()):
        raise ValueError("Enter a valid Kenyan mobile number, for example 0712 345 678.")
    digits = "".join(character for character in value if character.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif digits.startswith("7") or digits.startswith("1"):
        if len(digits) == 9:
            digits = "254" + digits
    if len(digits) != 12 or not digits.startswith("254") or digits[3] not in {"1", "7"}:
        raise ValueError("Enter a valid Kenyan mobile number, for example 0712 345 678.")
    return digits


def daraja_configured():
    return all(
        (
            settings.DARAJA_CONSUMER_KEY,
            settings.DARAJA_CONSUMER_SECRET,
            settings.DARAJA_SHORTCODE,
            settings.DARAJA_PASSKEY,
            settings.DARAJA_CALLBACK_URL,
            settings.DARAJA_CALLBACK_TOKEN,
        )
    )


def pesapal_configured():
    return bool(settings.PESAPAL_CONSUMER_KEY and settings.PESAPAL_CONSUMER_SECRET and settings.PESAPAL_IPN_ID)


def _pesapal_token():
    if not pesapal_configured():
        raise PaymentProviderError("PesaPal sandbox credentials and registered IPN ID are required.")
    try:
        response = requests.post(f"{settings.PESAPAL_BASE_URL}/api/Auth/RequestToken",
                                 json={"consumer_key": settings.PESAPAL_CONSUMER_KEY,
                                       "consumer_secret": settings.PESAPAL_CONSUMER_SECRET}, timeout=15)
        response.raise_for_status()
        token = response.json().get("token")
    except (requests.RequestException, ValueError) as error:
        raise PaymentProviderError("Could not authenticate with PesaPal; payment status is unknown.") from error
    if not token:
        raise PaymentProviderError("PesaPal did not return an access token.")
    return token


def initiate_pesapal(payment, request):
    """Submit a hosted payment outside the order/reservation database transaction."""
    token = _pesapal_token()
    callback = request.build_absolute_uri(reverse("pesapal_callback"))
    payload = {
        "id": payment.merchant_reference, "currency": payment.currency,
        "amount": float(payment.amount), "description": f"Liquor Hub order {payment.order.order_number}",
        "callback_url": callback, "notification_id": settings.PESAPAL_IPN_ID,
        "billing_address": {"email_address": payment.order.email or "orders@liquorhub.co.ke",
                            "phone_number": payment.order.phone_number,
                            "country_code": "KE", "first_name": payment.order.customer_name[:50],
                            "last_name": "", "line_1": payment.order.delivery_address[:100]},
    }
    payment.initiated_at = timezone.now()
    payment.save(update_fields=["initiated_at", "updated_at"])
    try:
        response = requests.post(f"{settings.PESAPAL_BASE_URL}/api/Transactions/SubmitOrderRequest",
                                 json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as error:
        payment.status = Payment.Status.UNCERTAIN
        payment.uncertain_since = timezone.now()
        payment.save(update_fields=["status", "uncertain_since", "updated_at"])
        payment.order.payment_status = Order.PaymentStatus.UNCERTAIN
        payment.order.save(update_fields=["payment_status"])
        raise PaymentProviderError("PesaPal submission timed out or failed. Do not retry this payment; it requires reconciliation.") from error
    tracking_id = result.get("order_tracking_id")
    redirect_url = result.get("redirect_url")
    if not tracking_id or not redirect_url:
        payment.status = Payment.Status.UNCERTAIN
        payment.uncertain_since = timezone.now()
        payment.metadata = {"submission_error": str(result.get("error", result.get("message", "Missing hosted redirect")))[:300]}
        payment.save(update_fields=["status", "uncertain_since", "metadata", "updated_at"])
        payment.order.payment_status = Order.PaymentStatus.UNCERTAIN
        payment.order.save(update_fields=["payment_status"])
        raise PaymentProviderError("PesaPal did not return a hosted checkout link.")
    payment.gateway_reference = str(tracking_id)
    payment.metadata = {"redirect_url": redirect_url, "merchant_reference": payment.merchant_reference}
    payment.save(update_fields=["gateway_reference", "metadata", "updated_at"])
    return redirect_url


def reconcile_pesapal(tracking_id, merchant_reference):
    token = _pesapal_token()
    try:
        response = requests.get(f"{settings.PESAPAL_BASE_URL}/api/Transactions/GetTransactionStatus",
                                params={"orderTrackingId": tracking_id, "orderMerchantReference": merchant_reference},
                                headers={"Authorization": f"Bearer {token}"}, timeout=15)
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as error:
        raise PaymentProviderError("PesaPal payment status could not be reconciled yet.") from error
    payment = Payment.objects.select_related("order").filter(gateway_reference=str(tracking_id), merchant_reference=merchant_reference).first()
    if not payment:
        raise ValueError("Unknown PesaPal payment reference.")
    status = str(result.get("payment_status_description", "")).upper()
    if status == "COMPLETED":
        amount = Decimal(str(result.get("amount", "0")))
        currency = str(result.get("currency", ""))
        provider_reference = str(result.get("confirmation_code", "")).strip()
        if amount != payment.amount or currency != payment.currency or not provider_reference:
            raise ValueError("PesaPal verified amount, currency or confirmation code did not match this order.")
        return finalize_verified_payment(payment, transaction_reference=provider_reference,
                                         provider_method=str(result.get("payment_method", "hosted")),
                                         metadata={"pesapal_status": status})
    if status in {"FAILED", "INVALID"}:
        payment.status = Payment.Status.FAILED
        payment.last_reconciled_at = timezone.now()
        payment.save(update_fields=["status", "last_reconciled_at", "updated_at"])
        payment.order.payment_status = Order.PaymentStatus.FAILED
        payment.order.save(update_fields=["payment_status"])
        from .checkout import release_reservations
        release_reservations(payment.order)
    else:
        payment.status = Payment.Status.UNCERTAIN
        payment.uncertain_since = payment.uncertain_since or timezone.now()
        payment.last_reconciled_at = timezone.now()
        payment.reconciliation_attempts += 1
        payment.save(update_fields=["status", "uncertain_since", "last_reconciled_at", "reconciliation_attempts", "updated_at"])
        payment.order.payment_status = Order.PaymentStatus.UNCERTAIN
        payment.order.save(update_fields=["payment_status"])
    return payment


def _daraja_access_token():
    url = f"{settings.DARAJA_BASE_URL.rstrip('/')}/oauth/v1/generate?grant_type=client_credentials"
    try:
        response = requests.get(
            url,
            auth=(settings.DARAJA_CONSUMER_KEY, settings.DARAJA_CONSUMER_SECRET),
            timeout=15,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
    except (requests.RequestException, ValueError) as error:
        raise PaymentProviderError("Could not connect to the M-Pesa sandbox. Please try again.") from error
    if not token:
        raise PaymentProviderError("The M-Pesa sandbox did not return an access token.")
    return token


def initiate_mpesa_stk(payment, phone):
    if not daraja_configured():
        raise PaymentProviderError("M-Pesa sandbox credentials are not configured. Use the mock payment mode locally.")
    normalized_phone = normalize_kenyan_phone(phone)
    amount = Decimal(payment.amount)
    if amount != amount.to_integral_value():
        raise PaymentProviderError("M-Pesa STK Push supports whole-KSh amounts only.")
    timestamp = timezone.localtime().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(
        f"{settings.DARAJA_SHORTCODE}{settings.DARAJA_PASSKEY}{timestamp}".encode()
    ).decode()
    callback_url = settings.DARAJA_CALLBACK_URL
    if settings.DARAJA_CALLBACK_TOKEN:
        callback_url += ("&" if "?" in callback_url else "?") + f"token={settings.DARAJA_CALLBACK_TOKEN}"
    payload = {
        "BusinessShortCode": settings.DARAJA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount),
        "PartyA": normalized_phone,
        "PartyB": settings.DARAJA_SHORTCODE,
        "PhoneNumber": normalized_phone,
        "CallBackURL": callback_url,
        "AccountReference": payment.order.order_number,
        "TransactionDesc": f"Liquor Hub order {payment.order.order_number}",
    }
    try:
        response = requests.post(
            f"{settings.DARAJA_BASE_URL.rstrip('/')}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={"Authorization": f"Bearer {_daraja_access_token()}"},
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as error:
        raise PaymentProviderError("The M-Pesa sandbox request failed. Your order remains pending.") from error
    if result.get("ResponseCode") != "0" or not result.get("CheckoutRequestID"):
        raise PaymentProviderError(result.get("CustomerMessage") or "M-Pesa did not accept the STK Push request.")
    payment.gateway_reference = result["CheckoutRequestID"]
    payment.metadata = {
        "merchant_request_id": result.get("MerchantRequestID", ""),
        "customer_message": result.get("CustomerMessage", ""),
    }
    payment.save(update_fields=["gateway_reference", "metadata", "updated_at"])
    return result


@transaction.atomic
def process_mpesa_callback(payload):
    callback = payload.get("Body", {}).get("stkCallback", {})
    checkout_id = callback.get("CheckoutRequestID")
    if not checkout_id:
        raise ValueError("Missing CheckoutRequestID.")
    payment = Payment.objects.select_for_update().select_related("order").filter(gateway_reference=checkout_id).first()
    if payment is None:
        raise ValueError("No pending payment matches this M-Pesa callback.")
    if payment.status == Payment.Status.SUCCEEDED:
        return payment
    if payment.status not in {Payment.Status.PENDING, Payment.Status.UNCERTAIN}:
        return payment
    try:
        result_code = int(callback.get("ResultCode"))
    except (TypeError, ValueError):
        raise ValueError("M-Pesa callback has an invalid result code.")
    if result_code != 0:
        payment.status = Payment.Status.FAILED
        payment.metadata = {**payment.metadata, "result_description": callback.get("ResultDesc", "")}
        payment.save(update_fields=["status", "metadata", "updated_at"])
        payment.order.payment_status = Order.PaymentStatus.FAILED
        payment.order.save(update_fields=["payment_status"])
        from .checkout import release_reservations
        release_reservations(payment.order)
        return payment
    metadata = {
        item.get("Name"): item.get("Value")
        for item in callback.get("CallbackMetadata", {}).get("Item", [])
        if item.get("Name")
    }
    try:
        callback_amount = Decimal(str(metadata["Amount"]))
    except (KeyError, InvalidOperation) as error:
        raise ValueError("M-Pesa callback is missing a valid amount.") from error
    if callback_amount != payment.amount:
        raise ValueError("M-Pesa callback amount does not match the order.")
    receipt = str(metadata.get("MpesaReceiptNumber", "")).strip()
    if not receipt:
        raise ValueError("M-Pesa callback is missing its receipt number.")
    if Payment.objects.filter(transaction_reference=receipt).exclude(pk=payment.pk).exists():
        raise ValueError("This M-Pesa receipt has already been applied to another payment.")
    return finalize_verified_payment(payment, transaction_reference=receipt,
                                     provider_method="mpesa",
                                     metadata={"result_description": callback.get("ResultDesc", "")})


@transaction.atomic
def finish_mock_payment(payment, succeeded):
    payment = Payment.objects.select_for_update().select_related("order").get(pk=payment.pk)
    if payment.status != Payment.Status.PENDING:
        return payment
    if succeeded:
        return finalize_verified_payment(payment, transaction_reference=f"MOCK-{payment.order.order_number}", provider_method="mock")
    else:
        payment.status = Payment.Status.FAILED
        payment.order.payment_status = Order.PaymentStatus.FAILED
        payment.order.save(update_fields=["payment_status"])
        payment.save(update_fields=["status", "updated_at"])
        from .checkout import release_reservations
        release_reservations(payment.order)
    return payment


@transaction.atomic
def finalize_verified_payment(payment, *, transaction_reference, provider_method="", metadata=None):
    """Single idempotent transaction for a provider-verified success."""
    payment = Payment.objects.select_for_update().select_related("order").get(pk=payment.pk)
    order = Order.objects.select_for_update().get(pk=payment.order_id)
    if payment.status == Payment.Status.SUCCEEDED:
        return payment
    if payment.status not in {Payment.Status.PENDING, Payment.Status.UNCERTAIN}:
        return payment
    if Payment.objects.filter(transaction_reference=transaction_reference).exclude(pk=payment.pk).exists():
        raise ValueError("This transaction reference was already recorded.")
    if payment.currency != "KES" or payment.amount != order.order_total:
        raise ValueError("Verified payment amount or currency does not match the order.")
    now = timezone.now()
    payment.status = Payment.Status.SUCCEEDED
    payment.transaction_reference = transaction_reference
    payment.provider_method = provider_method[:30]
    payment.metadata = {**payment.metadata, **(metadata or {})}
    payment.last_reconciled_at = now
    payment.save(update_fields=["status", "transaction_reference", "provider_method", "metadata", "last_reconciled_at", "updated_at"])
    order.payment_status = Order.PaymentStatus.PAID
    order.save(update_fields=["payment_status"])
    order.reservations.filter(status=StockReservation.Status.RESERVED).update(status=StockReservation.Status.CONFIRMED, confirmed_at=now)
    for item in order.items.select_related("variant__product"):
        SaleRecord.objects.get_or_create(order_item=item, defaults={
            "order": order, "payment": payment, "quantity": item.quantity,
            "unit_price": item.unit_price, "total": item.subtotal, "currency": payment.currency,
        })
    receipt_no = f"R-{uuid.uuid4().hex[:14].upper()}"
    Receipt.objects.get_or_create(payment=payment, defaults={
        "order": order, "receipt_number": receipt_no,
        "snapshot": {"order_number": order.order_number, "customer_name": order.customer_name,
                     "email": order.email, "phone_number": order.phone_number,
                     "delivery_address": order.delivery_address, "total": str(payment.amount),
                     "currency": payment.currency, "payment_method": provider_method,
                     "transaction_reference": transaction_reference,
                     "paid_at": now.isoformat(),
                     "items": [{"name": str(i.variant.product), "size": i.variant.bottle_size,
                                "unit": i.variant.unit, "quantity": i.quantity,
                                "unit_price": str(i.unit_price), "subtotal": str(i.subtotal)}
                               for i in order.items.select_related("variant__product")]}
    })
    NotificationOutbox.objects.get_or_create(dedupe_key=f"paid:{payment.pk}", defaults={
        "event_type": "order_paid", "payload": {"order_id": order.pk, "payment_id": payment.pk}
    })
    return payment


@transaction.atomic
def record_verified_refund(payment, *, amount, reason, provider_reference="", recorded_by=None):
    """Record a refund already confirmed with the payment provider or settlement account."""
    payment = Payment.objects.select_for_update().select_related("order").get(pk=payment.pk)
    amount = Decimal(str(amount))
    if payment.status not in {Payment.Status.SUCCEEDED, Payment.Status.REFUNDED} or amount <= 0:
        raise ValueError("Only a successful payment can have a positive refund recorded.")
    refunded = payment.refund_records.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    if refunded + amount > payment.amount:
        raise ValueError("Refund amount exceeds the unrefunded payment balance.")
    entry = RefundRecord.objects.create(payment=payment, amount=amount, reason=reason,
                                        provider_reference=provider_reference, recorded_by=recorded_by)
    if refunded + amount == payment.amount:
        payment.status = Payment.Status.REFUNDED
        payment.save(update_fields=["status", "updated_at"])
        payment.order.payment_status = Order.PaymentStatus.REFUNDED
        payment.order.save(update_fields=["payment_status"])
        payment.sales.filter(status=SaleRecord.Status.COMPLETED).update(status=SaleRecord.Status.REFUNDED,
                                                                        refunded_at=timezone.now())
    return entry


def valid_callback_token(request):
    expected = settings.DARAJA_CALLBACK_TOKEN
    supplied = request.GET.get("token", "")
    return bool(expected) and hmac.compare_digest(expected, supplied)


def safe_callback_payload(request):
    try:
        return json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("Invalid callback JSON.") from error
