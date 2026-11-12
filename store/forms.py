from django import forms
import uuid

from .models import Order
from .payments import normalize_kenyan_phone


class CheckoutForm(forms.Form):
    checkout_key = forms.UUIDField(widget=forms.HiddenInput)
    customer_name = forms.CharField(max_length=150, label="Full name")
    phone_number = forms.CharField(max_length=30, label="Kenyan mobile number")
    email = forms.EmailField(required=False)
    delivery_address = forms.CharField(
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Delivery location / address",
    )
    payment_method = forms.ChoiceField(choices=Order.PaymentMethod.choices, widget=forms.RadioSelect)

    def __init__(self, *args, **kwargs):
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("checkout_key", uuid.uuid4())
        super().__init__(*args, **kwargs)

    def clean_phone_number(self):
        try:
            return normalize_kenyan_phone(self.cleaned_data["phone_number"])
        except ValueError as error:
            raise forms.ValidationError(str(error)) from error
