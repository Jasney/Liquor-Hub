from django import forms
from django.contrib import admin, messages
from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Sum
from django.utils.html import format_html
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.urls import reverse
import json
import csv
from django.http import HttpResponse
from datetime import timedelta
from unfold.admin import ModelAdmin, TabularInline

from .models import (
    BeerProduct,
    Category,
    GinProduct,
    Order,
    OrderItem,
    Payment,
    Product,
    ProductImage,
    ProductVariant,
    WhiskyProduct,
    WineProduct,
    ImageCandidate,
    ImageReviewEvent,
    ImageSearchRun,
    ImageSource,
    StagedSourceImage,
    StockReservation, SaleRecord, Receipt, NotificationOutbox, RefundRecord,
)
from .checkout import release_reservations, CheckoutError
from .payments import record_verified_refund


def dashboard_callback(request, context):
    """Add a small, permission-aware snapshot for the shop operations home."""
    cards = []
    can_view_products = any(
        request.user.has_perm(f"store.{action}_product")
        for action in ("view", "change")
    )
    can_view_categories = any(
        request.user.has_perm(f"store.{action}_category")
        for action in ("view", "change")
    )
    can_view_orders = any(
        request.user.has_perm(f"store.{action}_order")
        for action in ("view", "change")
    )

    if can_view_products:
        active_products = Product.objects.filter(is_active=True).count()
        in_stock = ProductVariant.objects.filter(
            product__is_active=True, is_available=True, stock_quantity__gt=0
        ).count()
        stock_units = ProductVariant.objects.filter(
            product__is_active=True, is_available=True
        ).aggregate(total=Sum("stock_quantity"))["total"] or 0
        cards.extend([
            {
                "label": "Active products",
                "value": f"{active_products:,}",
                "detail": "Across the current catalogue",
                "icon": "inventory_2",
                "href": reverse("admin:store_product_changelist"),
            },
            {
                "label": "Variants in stock",
                "value": f"{in_stock:,}",
                "detail": f"{stock_units:,} units on hand",
                "icon": "shelves",
                "href": reverse("admin:store_product_changelist"),
            },
        ])

    if can_view_orders:
        open_orders = Order.objects.exclude(
            status__in=(Order.Status.DELIVERED, Order.Status.CANCELLED)
        ).count()
        cards.append({
            "label": "Open orders",
            "value": f"{open_orders:,}",
            "detail": "Placed, preparing or on the road",
            "icon": "shopping_bag",
            "href": reverse("admin:store_order_changelist"),
        })

    if can_view_categories:
        active_categories = Category.objects.filter(is_active=True).count()
        cards.append({
            "label": "Active categories",
            "value": f"{active_categories:,}",
            "detail": "Store departments currently shown",
            "icon": "category",
            "href": reverse("admin:store_category_changelist"),
        })

    context["dashboard_cards"] = cards
    context["dashboard_date"] = timezone.localdate().strftime("%A, %d %B %Y")
    context["dashboard_can_view_products"] = can_view_products
    context["dashboard_can_view_orders"] = can_view_orders
    context["dashboard_can_view_categories"] = can_view_categories
    context["dashboard_can_view_payments"] = request.user.has_perm("store.view_payment") or request.user.has_perm("store.change_payment")
    if request.user.has_perm("store.view_salerecord"):
        today = timezone.localdate()
        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)
        sales = SaleRecord.objects.filter(status=SaleRecord.Status.COMPLETED)
        for label, start in (("Sales today", today), ("Sales this week", week_start), ("Sales this month", month_start)):
            total = sales.filter(sold_at__date__gte=start, sold_at__date__lte=today).aggregate(total=Sum("total"))["total"] or 0
            cards.append({"label": label, "value": f"KES {total:,.2f}",
                          "detail": "Verified completed sales", "icon": "monitoring",
                          "href": reverse("admin:store_salerecord_changelist")})
        context["dashboard_best_sellers"] = list(
            sales.values("order_item__variant__product__name")
            .annotate(quantity=Sum("quantity"), revenue=Sum("total"))
            .order_by("-quantity", "order_item__variant__product__name")[:5]
        )
    else:
        context["dashboard_best_sellers"] = []
    context["dashboard_can_view_sales"] = request.user.has_perm("store.view_salerecord")
    return context


class HiddenForNowAdmin(ModelAdmin):
    """Keep the image workflow registered while removing it from the current admin UI."""

    def has_module_permission(self, request):
        return False


class ProductImageInline(TabularInline):
    model = ProductImage
    extra = 0
    fields = ("image", "image_preview", "alt_text", "display_order")
    readonly_fields = ("image_preview",)

    @admin.display(description="Current image")
    def image_preview(self, obj):
        if not obj.pk or not obj.image:
            return "No image uploaded"
        return format_html(
            '<img src="{}" alt="{}" style="max-width:72px;max-height:72px;object-fit:contain;">',
            obj.image.url,
            obj.alt_text or obj.product.name,
        )


class ProductVariantInline(TabularInline):
    model = ProductVariant
    extra = 0
    fields = ("bottle_size", "unit", "image", "sku", "price", "sale_price", "stock_quantity", "is_available")
    verbose_name = "Bottle size / variant"
    verbose_name_plural = "Bottle sizes and stock"

    @staticmethod
    def _product_permission(request, action):
        return request.user.has_perm(f"store.{action}_product")

    def has_view_permission(self, request, obj=None):
        return self._product_permission(request, "view") or self._product_permission(request, "change")

    def has_add_permission(self, request, obj=None):
        return self._product_permission(request, "change" if obj else "add")

    def has_change_permission(self, request, obj=None):
        return self._product_permission(request, "change")

    def has_delete_permission(self, request, obj=None):
        return self._product_permission(request, "change")


class CategoryProductAdmin(ModelAdmin):
    category_slug = ""
    category_name = ""
    list_display = ("name", "image_preview", "price_summary", "stock_status", "is_featured", "is_active")
    list_filter = ("is_active", "is_featured")
    list_editable = ("is_featured", "is_active")
    search_fields = ("name", "description", "variants__sku")
    prepopulated_fields = {"slug": ("name",)}
    inlines = (ProductVariantInline, ProductImageInline)
    list_select_related = ("category",)
    actions = ("deactivate_products",)
    exclude = ("category",)
    fieldsets = (
        ("Product details", {"fields": ("name", "slug", "description")}),
        ("Category", {"fields": ("assigned_category",)}),
        ("Main image", {"fields": ("image", "image_preview")}),
        ("Storefront", {"fields": ("is_featured", "is_active")}),
    )
    readonly_fields = ("assigned_category", "image_preview")

    def category(self):
        try:
            return Category.objects.get(slug=self.category_slug)
        except Category.DoesNotExist as error:
            raise RuntimeError(
                f"The {self.category_name} category is missing. Restore it before adding products."
            ) from error

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(category__slug=self.category_slug)
            .prefetch_related("variants", "additional_images")
        )

    def has_module_permission(self, request):
        return any(
            request.user.has_perm(f"store.{action}_product")
            for action in ("view", "add", "change", "delete")
        )

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("store.view_product") or request.user.has_perm("store.change_product")

    def has_add_permission(self, request):
        return request.user.has_perm("store.add_product") or request.user.has_perm("store.change_product")

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.category.slug != self.category_slug:
            return False
        return request.user.has_perm("store.change_product")

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.category.slug != self.category_slug:
            return False
        return request.user.has_perm("store.delete_product")

    @admin.display(description="Assigned category")
    def assigned_category(self, obj=None):
        return self.category_name

    @admin.display(description="Main image")
    def image_preview(self, obj):
        if not obj or not obj.pk or not obj.image:
            return "No image uploaded"
        return format_html(
            '<img src="{}" alt="{}" style="max-width: 120px; max-height: 120px; object-fit: contain;">',
            obj.image.url,
            obj.name,
        )

    @admin.display(description="Starting price")
    def price_summary(self, obj):
        variant = next(iter(obj.variants.all()), None)
        return f"KSh {variant.current_price:,.0f}" if variant else "Add a bottle size"

    @admin.display(description="Stock status")
    def stock_status(self, obj):
        variants = list(obj.variants.all())
        total_stock = sum(variant.stock_quantity for variant in variants if variant.is_available)
        if total_stock and obj.is_active:
            return format_html('<span style="color:#256029;font-weight:600;">In stock · {}</span>', total_stock)
        return format_html('<span style="color:#a42b20;font-weight:600;">Out of stock</span>')

    @admin.action(description="Deactivate selected products")
    def deactivate_products(self, request, queryset):
        count = queryset.update(is_active=False)
        self.message_user(request, f"{count} product(s) deactivated.", messages.SUCCESS)

    def save_model(self, request, obj, form, change):
        obj.category = self.category()
        super().save_model(request, obj, form, change)


@admin.register(GinProduct)
class GinProductAdmin(CategoryProductAdmin):
    category_slug = "gin"
    category_name = "Gin"


@admin.register(WhiskyProduct)
class WhiskyProductAdmin(CategoryProductAdmin):
    category_slug = "whisky"
    category_name = "Whisky"


@admin.register(WineProduct)
class WineProductAdmin(CategoryProductAdmin):
    category_slug = "wine"
    category_name = "Wine"


@admin.register(BeerProduct)
class BeerProductAdmin(CategoryProductAdmin):
    category_slug = "beer"
    category_name = "Beer"


@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ("products_link", "product_count", "display_order", "is_active", "edit_link")
    list_display_links = ("products_link",)
    list_editable = ("display_order", "is_active")
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_product_count=Count("products"))

    @admin.display(description="Category")
    def products_link(self, obj):
        url = f"{reverse('admin:store_product_changelist')}?category__id__exact={obj.pk}"
        return format_html('<a class="lh-category-products-link" href="{}">{}</a>', url, obj.name)

    @admin.display(description="Products", ordering="_product_count")
    def product_count(self, obj):
        return f"{obj._product_count:,}"

    @admin.display(description="Settings")
    def edit_link(self, obj):
        url = reverse("admin:store_category_change", args=(obj.pk,))
        return format_html('<a class="lh-category-edit-link" href="{}">Edit category</a>', url)

    def get_list_display(self, request):
        fields = ["products_link", "product_count", "display_order", "is_active"]
        if self.has_change_permission(request):
            fields.append("edit_link")
        return fields


@admin.register(Product)
class ProductAdmin(ModelAdmin):
    list_display = ("name", "category", "is_active", "is_featured")
    list_editable = ("is_active", "is_featured")
    list_filter = ("category", "is_active", "is_featured")
    search_fields = ("name", "description", "variants__sku")
    prepopulated_fields = {"slug": ("name",)}
    list_select_related = ("category",)
    inlines = (ProductVariantInline, ProductImageInline)


@admin.register(ImageSource)
class ImageSourceAdmin(HiddenForNowAdmin):
    list_display = ("name", "domain", "kind", "priority", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("name", "domain", "brand_keywords")


class ImageCandidateReviewForm(forms.ModelForm):
    identity_confirmed = forms.BooleanField(required=False, label="I checked the brand, product name, flavour and edition")
    size_confirmed = forms.BooleanField(required=False, label="I checked the bottle size / quantity against the selected target")
    packaging_confirmed = forms.BooleanField(required=False, label="I checked bottle, can, pack, gift set, kit or pod packaging")
    quality_confirmed = forms.BooleanField(required=False, label="I checked clarity, resolution, crop and watermarks")
    authentic_photo_confirmed = forms.BooleanField(required=False, label="I checked this is an authentic product photograph")

    class Meta:
        model = ImageCandidate
        fields = "__all__"

    def clean(self):
        data = super().clean()
        variant = data.get("variant")
        if variant and self.instance.pk and variant.product_id != self.instance.product_id:
            self.add_error("variant", "Choose a variant belonging to this product.")
        return data


class ImageReviewEventInline(TabularInline):
    model = ImageReviewEvent
    extra = 0
    fields = ("created_at", "reviewer_name", "action", "details")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("store.view_imagecandidate") or request.user.has_perm("store.change_imagecandidate")


@admin.register(ImageCandidate)
class ImageCandidateAdmin(HiddenForNowAdmin):
    form = ImageCandidateReviewForm
    change_form_template = "admin/store/imagecandidate/change_form.html"
    list_display = ("product", "target_size", "candidate_preview", "source_link", "confidence", "match_status", "rights_status", "published_at")
    list_filter = ("product__category", "match_status", "rights_status", "provider", "source__kind", "staged_image__queue")
    search_fields = ("product__name", "title", "source_url", "variant__bottle_size")
    list_select_related = ("product", "variant", "source", "staged_image")
    inlines = (ImageReviewEventInline,)
    actions = None
    readonly_fields = (
        "product", "product_sizes", "title", "candidate_preview", "source_link", "source_url", "image_url", "source", "provider", "query", "staged_image",
        "width", "height", "confidence", "checks_display", "metadata_display", "match_status", "match_reviewer", "match_reviewed_at",
        "rights_status", "rights_reviewer", "rights_reviewed_at", "stored_image", "published_at", "download_info", "created_at",
    )
    fieldsets = (
        ("Product and source", {"fields": ("product", "product_sizes", "variant", "title", "candidate_preview", "source_link", "source_url", "image_url", "source", "provider", "query", "staged_image")}),
        ("Match evidence — score is not an approval", {"fields": ("confidence", "width", "height", "checks_display", "metadata_display", "review_notes", "identity_confirmed", "size_confirmed", "packaging_confirmed", "quality_confirmed", "authentic_photo_confirmed", "match_status", "match_reviewer", "match_reviewed_at")}),
        ("Commercial reuse rights — separate verification required", {"fields": ("rights_holder", "license_name", "permission_url", "rights_evidence", "commercial_reuse_confirmed", "download_permission_confirmed", "attribution_required", "attribution_text", "attribution_url", "rights_status", "rights_reviewer", "rights_reviewed_at")}),
        ("Publication", {"fields": ("stored_image", "published_at", "download_info", "created_at")}),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # Keep provenance/review history; use Withdraw to unpublish.

    def get_readonly_fields(self, request, obj=None):
        from .image_finder.review import MATCH_CONFIRMATIONS, RIGHTS_FIELDS
        fields = list(super().get_readonly_fields(request, obj))
        if not request.user.has_perm("store.verify_image_rights") or (obj and obj.published_at):
            fields.extend(RIGHTS_FIELDS)
        if not request.user.has_perm("store.review_image_matches") or (obj and obj.published_at):
            fields.extend(("variant", *MATCH_CONFIRMATIONS))
        if obj and obj.published_at:
            fields.append("review_notes")
        # Declared checkbox form fields cannot become model readonly fields.
        return tuple(field for field in fields if field not in MATCH_CONFIRMATIONS)

    def get_form(self, request, obj=None, **kwargs):
        from .image_finder.review import MATCH_CONFIRMATIONS
        form_class = super().get_form(request, obj, **kwargs)
        if "variant" in form_class.base_fields and obj:
            form_class.base_fields["variant"].queryset = ProductVariant.objects.filter(product_id=obj.product_id)
            form_class.base_fields["variant"].label = "Target variant (blank = main product photo)"
        for name in MATCH_CONFIRMATIONS:
            if name in form_class.base_fields:
                form_class.base_fields[name].disabled = not request.user.has_perm("store.review_image_matches") or bool(obj and obj.published_at)
        return form_class

    @admin.display(description="Product sizes")
    def product_sizes(self, obj):
        return ", ".join(f"{variant.bottle_size} ({variant.unit})" for variant in obj.product.variants.all()) or "No sizes supplied"

    @admin.display(description="Target")
    def target_size(self, obj):
        return obj.variant.bottle_size if obj.variant else "Main product photo"

    @admin.display(description="Review preview")
    def candidate_preview(self, obj):
        from .image_finder.matching import public_https_url
        if obj.stored_image and obj.published_at:
            return format_html('<img src="{}" alt="{}" style="width:110px;height:140px;object-fit:contain">', obj.stored_image.url, obj.title)
        if obj.staged_image_id and obj.staged_image.image:
            return format_html('<img src="{}" alt="{}" style="width:110px;height:140px;object-fit:contain"><br>Local staging image — not published', obj.staged_image.image.url, obj.title)
        if obj.preview_permitted and public_https_url(obj.thumbnail_url):
            return format_html('<img src="{}" alt="{}" loading="lazy" referrerpolicy="no-referrer" style="width:110px;height:140px;object-fit:contain">', obj.thumbnail_url, obj.title)
        return "No permitted thumbnail supplied. Open the source page to review; the original is not downloaded."

    @admin.display(description="Source website")
    def source_link(self, obj):
        from urllib.parse import urlsplit
        from .image_finder.matching import public_https_url
        if not public_https_url(obj.source_url):
            return "Invalid source URL"
        return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>', obj.source_url, urlsplit(obj.source_url).hostname)

    @admin.display(description="Matching checks")
    def checks_display(self, obj):
        return format_html('<pre style="white-space:pre-wrap">{}</pre>', json.dumps(obj.match_checks, indent=2))

    @admin.display(description="Supplier/API claims — unverified")
    def metadata_display(self, obj):
        return format_html('<pre style="white-space:pre-wrap">{}</pre>', json.dumps(obj.metadata, indent=2))

    def save_model(self, request, obj, form, change):
        from .image_finder.review import RIGHTS_FIELDS
        if change and any(field in form.changed_data for field in RIGHTS_FIELDS):
            obj.rights_status, obj.rights_reviewer, obj.rights_reviewed_at = "unknown", None, None
        if change and "variant" in form.changed_data:
            obj.match_status, obj.match_reviewer, obj.match_reviewed_at = "pending", None, None
        super().save_model(request, obj, form, change)

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        context.update({
            "can_review_match": request.user.has_perm("store.review_image_matches"),
            "can_verify_rights": request.user.has_perm("store.verify_image_rights"),
            "can_publish_image": request.user.has_perm("store.publish_product_images"),
        })
        return super().render_change_form(request, context, add, change, form_url, obj)

    def response_change(self, request, obj):
        from .image_finder.review import MATCH_CONFIRMATIONS, ReviewError, publish_candidate, review_match, verify_rights, withdraw_candidate
        from .image_finder.network import ImageDownloadError
        controls = {"_approve_match", "_reject_match", "_skip_match", "_verify_rights", "_publish_image", "_withdraw_image"}
        selected = controls.intersection(request.POST)
        if not selected:
            return super().response_change(request, obj)
        try:
            if len(selected) != 1:
                raise ReviewError("Choose one review action at a time.")
            action = selected.pop()
            if action in {"_approve_match", "_reject_match", "_skip_match"}:
                confirmations = {field: request.POST.get(field) == "on" for field in MATCH_CONFIRMATIONS}
                review_match(obj.pk, request.user, {"_approve_match": "approved", "_reject_match": "rejected", "_skip_match": "skipped"}[action], confirmations)
                result = "Product match decision recorded. Commercial reuse rights remain a separate verification."
            elif action == "_verify_rights":
                verify_rights(obj.pk, request.user)
                result = "Commercial reuse and download permission verification recorded. No image downloaded."
            elif action == "_publish_image":
                publish_candidate(obj.pk, request.user)
                result = "Verified image validated, optimized and published."
            else:
                withdraw_candidate(obj.pk, request.user)
                result = "Image withdrawn. Manual replacement images were preserved."
            self.message_user(request, result, messages.SUCCESS)
        except (ReviewError, ImageDownloadError) as error:
            self.message_user(request, str(error), messages.ERROR)
        return redirect(reverse("admin:store_imagecandidate_change", args=[obj.pk]))


@admin.register(ImageSearchRun)
class ImageSearchRunAdmin(HiddenForNowAdmin):
    list_display = ("id", "provider", "started_at", "finished_at")
    readonly_fields = ("provider", "started_at", "finished_at", "options", "statistics", "errors")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class OrderItemInline(TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    readonly_fields = ("variant", "quantity", "unit_price", "line_total")

    @admin.display(description="Line total")
    def line_total(self, obj):
        return f"KSh {obj.subtotal:,.2f}"


class OrderAdminForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        if (
            settings.REQUIRE_DELIVERY_AGE_VERIFICATION
            and cleaned_data.get("status") == Order.Status.DELIVERED
            and not cleaned_data.get("age_verified_at_delivery")
        ):
            self.add_error(
                "age_verified_at_delivery",
                "Confirm the recipient's age at delivery before marking this order delivered.",
            )
        return cleaned_data


@admin.register(Order)
class OrderAdmin(ModelAdmin):
    form = OrderAdminForm
    list_display = ("order_number", "customer_name", "phone_number", "order_total", "payment_method", "payment_status", "status", "created_at")
    list_filter = ("status", "payment_status", "payment_method", "created_at")
    search_fields = ("order_number", "customer_name", "phone_number", "email")
    readonly_fields = ("order_number", "order_total", "payment_status", "stock_released", "created_at")
    inlines = (OrderItemInline,)
    actions = ()

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        previous_status = None
        if change:
            previous = Order.objects.select_for_update().get(pk=obj.pk)
            previous_status = previous.status
            if obj.status == Order.Status.CANCELLED and previous.payment_status == Order.PaymentStatus.PAID:
                raise ValidationError("Record and reconcile any refund before cancelling a paid order.")
        super().save_model(request, obj, form, change)
        if obj.status == Order.Status.CANCELLED and previous_status != Order.Status.CANCELLED:
            try:
                release_reservations(obj)
            except CheckoutError as error:
                raise ValidationError(str(error)) from error


@admin.register(Payment)
class PaymentAdmin(ModelAdmin):
    list_display = ("order", "provider", "amount", "status", "gateway_reference", "transaction_reference", "created_at")
    list_filter = ("provider", "status", "created_at")
    search_fields = ("order__order_number", "gateway_reference", "transaction_reference")
    readonly_fields = ("order", "provider", "gateway_reference", "transaction_reference", "amount", "status", "metadata", "created_at", "updated_at")


@admin.register(StockReservation)
class StockReservationAdmin(ModelAdmin):
    list_display = ("order", "variant", "quantity", "status", "expires_at", "created_at")
    list_filter = ("status", "expires_at")
    search_fields = ("order__order_number", "variant__product__name")
    readonly_fields = tuple(field.name for field in StockReservation._meta.fields)


@admin.register(SaleRecord)
class SaleRecordAdmin(ModelAdmin):
    list_display = ("sold_at", "order", "order_item", "payment", "quantity", "total", "currency", "status")
    list_filter = ("status", "currency", "sold_at", "payment__provider_method")
    search_fields = ("order__order_number", "order_item__variant__product__name", "payment__transaction_reference")
    date_hierarchy = "sold_at"
    actions = ("export_sales_csv",)
    readonly_fields = tuple(field.name for field in SaleRecord._meta.fields)

    @admin.action(description="Export selected sales as CSV")
    def export_sales_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="liquor-hub-sales.csv"'
        writer = csv.writer(response)
        writer.writerow(("sold_at", "order", "product", "variant", "quantity", "unit_price", "total", "currency", "payment_method", "payment_reference", "status"))
        for sale in queryset.select_related("order", "order_item__variant__product", "payment"):
            writer.writerow((sale.sold_at.isoformat(), sale.order.order_number, sale.order_item.variant.product.name,
                             sale.order_item.variant.bottle_size, sale.quantity, sale.unit_price, sale.total,
                             sale.currency, sale.payment.provider_method, sale.payment.transaction_reference, sale.status))
        return response


@admin.register(Receipt)
class ReceiptAdmin(ModelAdmin):
    list_display = ("receipt_number", "order", "payment", "created_at")
    search_fields = ("receipt_number", "order__order_number")
    readonly_fields = tuple(field.name for field in Receipt._meta.fields)


@admin.register(NotificationOutbox)
class NotificationOutboxAdmin(ModelAdmin):
    list_display = ("event_type", "status", "attempts", "next_attempt_at", "delivered_at")
    list_filter = ("event_type", "status")
    readonly_fields = tuple(field.name for field in NotificationOutbox._meta.fields)


@admin.register(RefundRecord)
class RefundRecordAdmin(ModelAdmin):
    list_display = ("payment", "amount", "provider_reference", "recorded_by", "recorded_at")
    readonly_fields = ("recorded_by", "recorded_at")

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if change:
            raise ValidationError("Refund records are immutable. Add a correction entry with an audit reason.")
        try:
            saved = record_verified_refund(obj.payment, amount=obj.amount, reason=obj.reason,
                                           provider_reference=obj.provider_reference,
                                           recorded_by=request.user)
        except ValueError as error:
            raise ValidationError(str(error)) from error
        obj.pk = saved.pk


@admin.register(StagedSourceImage)
class StagedSourceImageAdmin(HiddenForNowAdmin):
    list_display = ("source_product_name", "local_preview", "available_sizes", "queue", "candidate_reviews")
    list_filter = ("queue", "source_name")
    search_fields = ("source_product_name", "source_url")
    readonly_fields = ("source_name", "source_product_name", "source_url", "image_url", "available_sizes", "image", "local_preview", "sha256", "width", "height", "queue", "metadata", "candidate_reviews", "created_at")
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Staging preview")
    def local_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" alt="{}" style="width:110px;height:140px;object-fit:contain">', obj.image.url, obj.source_product_name)
        return "No staged file"

    @admin.display(description="Existing image review")
    def candidate_reviews(self, obj):
        return format_html('<a href="{}?staged_image__id__exact={}">Review {} candidates</a>', reverse("admin:store_imagecandidate_changelist"), obj.pk, obj.candidates.count())
