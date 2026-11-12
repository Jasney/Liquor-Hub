from decimal import Decimal

from .models import ProductVariant


class Cart:
    session_key = "cart"

    def __init__(self, request):
        self.session = request.session
        self.data = self.session.setdefault(self.session_key, {})

    def add(self, variant, quantity=1):
        key = str(variant.pk)
        self.data[key] = int(self.data.get(key, 0)) + int(quantity)
        self.save()

    def set_quantity(self, variant_id, quantity):
        if quantity <= 0:
            self.remove(variant_id)
        else:
            self.data[str(variant_id)] = int(quantity)
            self.save()

    def remove(self, variant_id):
        self.data.pop(str(variant_id), None)
        self.save()

    def save(self):
        self.session[self.session_key] = self.data
        self.session.modified = True

    def clear(self):
        self.session.pop(self.session_key, None)
        self.session.modified = True
        self.data = {}

    def items(self):
        ids = [int(key) for key in self.data if key.isdigit()]
        variants = ProductVariant.objects.filter(
            id__in=ids, product__is_active=True, product__category__is_active=True
        ).select_related("product", "product__category").prefetch_related("product__image_candidates")
        by_id = {str(variant.pk): variant for variant in variants}
        entries = []
        for variant_id, quantity in list(self.data.items()):
            variant = by_id.get(variant_id)
            if variant is None:
                continue
            entries.append(
                {
                    "variant": variant,
                    "quantity": int(quantity),
                    "unit_price": variant.current_price,
                    "subtotal": variant.current_price * int(quantity),
                }
            )
        return entries

    @property
    def total(self):
        return sum((item["subtotal"] for item in self.items()), Decimal("0.00"))

    def __len__(self):
        return sum(int(quantity) for quantity in self.data.values())
