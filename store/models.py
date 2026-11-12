import uuid
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="categories/", blank=True)
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]
        verbose_name_plural = "categories"

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            candidate = base_slug
            suffix = 2
            while Category.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base_slug}-{suffix}"
                suffix += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Product(models.Model):
    catalogue_key = models.CharField(max_length=40, unique=True, null=True, blank=True, editable=False)
    name = models.CharField(max_length=180)
    slug = models.SlugField(max_length=200, unique=True, blank=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="products/", blank=True)
    is_featured = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            candidate = base_slug
            suffix = 2
            while Product.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base_slug}-{suffix}"
                suffix += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    @property
    def available_variants(self):
        return self.variants.filter(is_available=True, stock_quantity__gt=0)

    @property
    def listing_variant(self):
        variants = list(self.variants.all())
        available = [variant for variant in variants if variant.in_stock]
        return min(available or variants, key=lambda variant: variant.current_price, default=None)

    @property
    def display_image(self):
        if self.image:
            return self.image
        return next((variant.image for variant in self.variants.all() if variant.image), None)

    @property
    def display_image_credit(self):
        image = self.display_image
        return self.image_credit_for(image.name if image else "")

    def image_credit_for(self, filename):
        if not filename or not filename.startswith("products/discovered/"):
            return None
        return next((candidate for candidate in self.image_candidates.all()
                     if candidate.stored_image.name == filename and candidate.published_at
                     and candidate.match_status == "approved" and candidate.rights_status == "verified"), None)

    def __str__(self):
        return self.name


class GinProduct(Product):
    class Meta:
        proxy = True
        verbose_name = "gin product"
        verbose_name_plural = "Gin"


class WhiskyProduct(Product):
    class Meta:
        proxy = True
        verbose_name = "whisky product"
        verbose_name_plural = "Whisky"


class WineProduct(Product):
    class Meta:
        proxy = True
        verbose_name = "wine product"
        verbose_name_plural = "Wine"


class BeerProduct(Product):
    class Meta:
        proxy = True
        verbose_name = "beer product"
        verbose_name_plural = "Beer"


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="additional_images")
    image = models.ImageField(upload_to="products/gallery/")
    alt_text = models.CharField(max_length=180, blank=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "id"]

    def __str__(self):
        return f"Image for {self.product}"


class ProductVariant(models.Model):
    catalogue_key = models.CharField(max_length=40, unique=True, null=True, blank=True, editable=False)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    bottle_size = models.CharField(max_length=40)
    image = models.ImageField(upload_to="products/variants/", blank=True)
    unit = models.CharField(max_length=40, blank=True, help_text="Original catalogue packaging unit; separate from the size.")
    sku = models.CharField(max_length=80, unique=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    sale_price = models.DecimalField(
        max_digits=10, decimal_places=2, blank=True, null=True, validators=[MinValueValidator(Decimal("0.01"))]
    )
    stock_quantity = models.PositiveIntegerField(default=0)
    is_available = models.BooleanField(default=True)

    class Meta:
        ordering = ["product__name", "bottle_size"]
        constraints = [
            models.CheckConstraint(condition=models.Q(stock_quantity__gte=0), name="variant_stock_nonnegative"),
            models.CheckConstraint(condition=models.Q(price__gt=0), name="variant_price_positive"),
            models.CheckConstraint(
                condition=models.Q(sale_price__isnull=True) | models.Q(sale_price__gt=0),
                name="variant_sale_price_positive",
            ),
        ]

    @property
    def current_price(self):
        if self.sale_price is not None and self.sale_price < self.price:
            return self.sale_price
        return self.price

    @property
    def in_stock(self):
        return self.is_available and self.stock_quantity > 0 and self.product.is_active

    @property
    def display_image(self):
        return self.image or self.product.image

    @property
    def display_image_credit(self):
        image = self.display_image
        return self.product.image_credit_for(image.name if image else "")

    def save(self, *args, **kwargs):
        if not self.sku and self.product_id:
            self.sku = f"LH-{self.product_id}-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product} — {self.bottle_size}"


class ImageSource(models.Model):
    class Kind(models.TextChoices):
        MANUFACTURER = "manufacturer", "Official manufacturer"
        SUPPLIER = "supplier", "Authorized supplier"
        RETAILER = "retailer", "Reputable retailer"

    name = models.CharField(max_length=180)
    domain = models.CharField(max_length=253, unique=True, help_text="Hostname only, e.g. manufacturer.example; no scheme or path.")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    brand_keywords = models.CharField(max_length=500, blank=True, help_text="Comma-separated brand names; restricts preferred searches to relevant products.")
    verification_evidence = models.TextField(help_text="Record how the official/authorized/reputable source status was checked. This does not establish image reuse rights.")
    priority = models.PositiveSmallIntegerField(default=50)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["priority", "name"]

    def clean(self):
        from .image_finder.matching import public_https_url
        self.domain = self.domain.strip().lower()
        if not public_https_url("https://" + self.domain) or any(char in self.domain for char in "/:@?#"):
            raise ValidationError({"domain": "Enter a public hostname without a scheme, port or path."})
        if not self.verification_evidence.strip():
            raise ValidationError({"verification_evidence": "Source verification evidence is required."})
        if self.kind == self.Kind.MANUFACTURER and not self.brand_keywords.strip():
            raise ValidationError({"brand_keywords": "List this manufacturer's brand names so its domain is used only for relevant products."})

    def __str__(self):
        return f"{self.name} ({self.domain})"


class ImageSearchRun(models.Model):
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    provider = models.CharField(max_length=30)
    options = models.JSONField(default=dict)
    statistics = models.JSONField(default=dict)
    errors = models.JSONField(default=list)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Image discovery {self.pk} · {self.provider}"


class StagedSourceImage(models.Model):
    """Permitted source assets, including ones without a catalogue match."""
    source_name = models.CharField(max_length=100)
    source_product_name = models.CharField(max_length=500)
    source_url = models.URLField(max_length=2048)
    image_url = models.URLField(max_length=2048, unique=True)
    available_sizes = models.JSONField(default=list)
    image = models.ImageField(upload_to="image-staging/")
    sha256 = models.CharField(max_length=64)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    queue = models.CharField(max_length=16, choices=[("unmatched", "Unmatched source image"), ("possible", "Uncertain match"), ("exact", "Exact metadata match — review required")], default="unmatched")
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.source_product_name


class ImageCandidate(models.Model):
    class Match(models.TextChoices):
        PENDING = "pending", "Needs review"
        APPROVED = "approved", "Product match approved"
        REJECTED = "rejected", "Rejected"
        SKIPPED = "skipped", "Skipped"

    class Rights(models.TextChoices):
        UNKNOWN = "unknown", "Unverified — not licensed for publication"
        VERIFIED = "verified", "Commercial reuse and download permission verified"

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="image_candidates")
    variant = models.ForeignKey(ProductVariant, on_delete=models.SET_NULL, null=True, blank=True, related_name="image_candidates")
    source = models.ForeignKey(ImageSource, on_delete=models.SET_NULL, null=True, blank=True)
    staged_image = models.ForeignKey(StagedSourceImage, on_delete=models.SET_NULL, null=True, blank=True, related_name="candidates")
    search_run = models.ForeignKey(ImageSearchRun, on_delete=models.SET_NULL, null=True, blank=True)
    provider = models.CharField(max_length=30)
    query = models.TextField(blank=True)
    title = models.CharField(max_length=500)
    source_url = models.URLField(max_length=2048)
    image_url = models.URLField(max_length=2048)
    image_url_hash = models.CharField(max_length=64)
    thumbnail_url = models.URLField(max_length=2048, blank=True)
    preview_permitted = models.BooleanField(default=False)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    confidence = models.PositiveSmallIntegerField(default=0)
    match_checks = models.JSONField(default=dict)
    product_snapshot = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict)
    match_status = models.CharField(max_length=12, choices=Match.choices, default=Match.PENDING)
    match_reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="image_match_reviews")
    match_reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)
    rights_status = models.CharField(max_length=12, choices=Rights.choices, default=Rights.UNKNOWN)
    rights_holder = models.CharField(max_length=250, blank=True)
    license_name = models.CharField(max_length=250, blank=True)
    permission_url = models.URLField(max_length=2048, blank=True)
    rights_evidence = models.TextField(blank=True, help_text="Record the licence, written permission or agreement, its scope and how it permits commercial reuse and downloading.")
    commercial_reuse_confirmed = models.BooleanField(default=False)
    download_permission_confirmed = models.BooleanField(default=False)
    attribution_required = models.BooleanField(default=False)
    attribution_text = models.CharField(max_length=500, blank=True)
    attribution_url = models.URLField(max_length=2048, blank=True)
    rights_reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="image_rights_reviews")
    rights_reviewed_at = models.DateTimeField(null=True, blank=True)
    stored_image = models.ImageField(upload_to="products/discovered/", blank=True)
    download_info = models.JSONField(default=dict)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["product__name", "-confidence", "pk"]
        constraints = [models.UniqueConstraint(fields=["product", "image_url_hash"], name="unique_product_image_candidate")]
        permissions = [
            ("review_image_matches", "Can approve, reject and skip product image matches"),
            ("verify_image_rights", "Can verify commercial image reuse and download permission"),
            ("publish_product_images", "Can publish and withdraw verified product images"),
        ]

    @property
    def credit_text(self):
        return self.attribution_text or f"Image: {self.rights_holder} · {self.license_name}"

    @property
    def credit_url(self):
        return self.attribution_url or self.source_url

    def __str__(self):
        size = f" · {self.variant.bottle_size}" if self.variant else ""
        return f"{self.product.name}{size} · {self.confidence}%"


class ImageReviewEvent(models.Model):
    candidate = models.ForeignKey(ImageCandidate, on_delete=models.CASCADE, related_name="review_events")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    reviewer_name = models.CharField(max_length=250)
    action = models.CharField(max_length=40)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-pk"]


class Order(models.Model):
    class PaymentMethod(models.TextChoices):
        MPESA = "mpesa", "M-Pesa"
        CARD = "card", "Card (hosted checkout)"
        DELIVERY = "delivery", "Pay on Delivery"

    class PaymentStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        UNCERTAIN = "uncertain", "Needs payment reconciliation"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    class Status(models.TextChoices):
        PLACED = "placed", "Placed"
        CONFIRMED = "confirmed", "Confirmed"
        PREPARING = "preparing", "Preparing"
        OUT_FOR_DELIVERY = "out_for_delivery", "Out for delivery"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"

    order_number = models.CharField(max_length=40, unique=True, editable=False)
    customer_name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone_number = models.CharField(max_length=20)
    delivery_address = models.TextField()
    order_total = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PLACED)
    checkout_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    receipt_token_hash = models.CharField(max_length=64, blank=True, editable=False)
    age_verified_at_delivery = models.BooleanField(default=False)
    stock_released = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = f"LH{timezone.now():%y%m%d}{uuid.uuid4().hex.upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.order_number


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="order_items")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="order_item_quantity_positive"),
        ]

    @property
    def subtotal(self):
        return self.quantity * self.unit_price

    def __str__(self):
        return f"{self.quantity} × {self.variant}"


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        UNCERTAIN = "uncertain", "Needs reconciliation"
        REFUNDED = "refunded", "Refunded"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=30)
    provider_method = models.CharField(max_length=30, blank=True)
    idempotency_key = models.UUIDField(editable=False, null=True, blank=True)
    merchant_reference = models.CharField(max_length=50, unique=True, null=True, blank=True)
    gateway_reference = models.CharField(max_length=120, blank=True, db_index=True)
    transaction_reference = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    currency = models.CharField(max_length=3, default="KES")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    metadata = models.JSONField(default=dict, blank=True)
    initiated_at = models.DateTimeField(null=True, blank=True)
    uncertain_since = models.DateTimeField(null=True, blank=True)
    last_reconciled_at = models.DateTimeField(null=True, blank=True)
    reconciliation_attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["idempotency_key"], condition=models.Q(idempotency_key__isnull=False), name="unique_payment_idempotency_key"),
            models.UniqueConstraint(
                fields=["gateway_reference"],
                condition=~models.Q(gateway_reference=""),
                name="unique_nonempty_gateway_reference",
            ),
            models.UniqueConstraint(
                fields=["transaction_reference"],
                condition=~models.Q(transaction_reference=""),
                name="unique_nonempty_transaction_reference",
            ),
        ]

    def __str__(self):
        return f"{self.provider} payment for {self.order}"

    def save(self, *args, **kwargs):
        if not self.idempotency_key:
            self.idempotency_key = uuid.uuid4()
        if not self.merchant_reference:
            self.merchant_reference = f"LH-{uuid.uuid4().hex.upper()}"
        super().save(*args, **kwargs)


class StockReservation(models.Model):
    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        CONFIRMED = "confirmed", "Confirmed"
        RELEASED = "released", "Released"
        EXPIRED = "expired", "Expired"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="reservations")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="reservations")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RESERVED)
    expires_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["order", "variant"], name="unique_order_stock_reservation")]
        indexes = [models.Index(fields=["status", "expires_at"], name="stock_res_status_exp_idx")]

    def __str__(self):
        return f"{self.quantity} × {self.variant} for {self.order}"


class SaleRecord(models.Model):
    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        REFUNDED = "refunded", "Refunded"

    order_item = models.OneToOneField(OrderItem, on_delete=models.PROTECT, related_name="sale_record")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="sales")
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="sales")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="KES")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.COMPLETED)
    sold_at = models.DateTimeField(default=timezone.now)
    refunded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-sold_at", "pk"]
        indexes = [models.Index(fields=["sold_at", "status"], name="sale_sold_status_idx")]

    def __str__(self):
        return f"Sale {self.order.order_number} · {self.total} {self.currency}"


class Receipt(models.Model):
    payment = models.OneToOneField(Payment, on_delete=models.PROTECT, related_name="receipt")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="receipts")
    receipt_number = models.CharField(max_length=40, unique=True)
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.receipt_number


class NotificationOutbox(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"

    event_type = models.CharField(max_length=50)
    dedupe_key = models.CharField(max_length=120, unique=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="notify_status_next_idx")]

    def __str__(self):
        return f"{self.event_type} · {self.dedupe_key}"


class RefundRecord(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refund_records")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    provider_reference = models.CharField(max_length=120, blank=True)
    reason = models.TextField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recorded_at", "pk"]

    def __str__(self):
        return f"Refund {self.amount} {self.payment.currency} · {self.payment}"
