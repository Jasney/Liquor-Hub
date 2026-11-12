import hashlib
import io
import warnings

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from PIL import Image, ImageOps, UnidentifiedImageError

from store.models import ImageCandidate, ImageReviewEvent, Product, ProductVariant

from .matching import public_https_url, snapshot
from .network import ImageDownloadError, download_image


class ReviewError(ValueError):
    pass


MATCH_CONFIRMATIONS = ("identity_confirmed", "size_confirmed", "packaging_confirmed", "quality_confirmed", "authentic_photo_confirmed")
RIGHTS_FIELDS = ("rights_holder", "license_name", "permission_url", "rights_evidence", "commercial_reuse_confirmed",
                "download_permission_confirmed", "attribution_required", "attribution_text", "attribution_url")


def require_permission(user, permission):
    if not user or not user.is_active or not user.is_staff or not user.has_perm("store." + permission):
        raise ReviewError("You do not have permission for this image review action.")


def event(candidate, user, action, details=None):
    ImageReviewEvent.objects.create(candidate=candidate, user=user, reviewer_name=user.get_username(), action=action, details=details or {})


@transaction.atomic
def review_match(candidate_id, user, decision, confirmations=None):
    require_permission(user, "review_image_matches")
    candidate = ImageCandidate.objects.select_for_update(of=("self",)).select_related("product__category", "variant").get(pk=candidate_id)
    if candidate.published_at:
        raise ReviewError("Withdraw the published image before changing its review decision.")
    if decision not in {"approved", "rejected", "skipped"}:
        raise ReviewError("Invalid match decision.")
    if candidate.variant and candidate.variant.product_id != candidate.product_id:
        raise ReviewError("The chosen variant does not belong to this product.")
    if decision == "approved":
        if candidate.metadata.get("shared_size_image") and not candidate.variant_id:
            raise ReviewError("This source image is shared across sizes. Choose and visually confirm a specific target variant before approval.")
        if not confirmations or not all(confirmations.get(field) is True for field in MATCH_CONFIRMATIONS):
            raise ReviewError("Confirm identity/flavour/edition, size, packaging, image quality and authentic photograph before approving the match.")
        if candidate.product_snapshot != snapshot(candidate.product):
            raise ReviewError("The product details changed after discovery. Run refresh_product_image_checks before reviewing this candidate.")
        checks = dict(candidate.match_checks)
        checks["human_review"] = {**confirmations, "product_snapshot": candidate.product_snapshot,
                                  "variant_id": candidate.variant_id, "image_url_hash": candidate.image_url_hash,
                                  "source_url": candidate.source_url, "reviewer": user.get_username(),
                                  "staged_sha256": candidate.staged_image.sha256 if candidate.staged_image_id else ""}
        candidate.match_checks = checks
    candidate.match_status = decision
    candidate.match_reviewer = user
    candidate.match_reviewed_at = timezone.now()
    candidate.save(update_fields=["match_status", "match_reviewer", "match_reviewed_at", "match_checks"])
    event(candidate, user, "match_" + decision, {"variant_id": candidate.variant_id, "confirmations": confirmations or {}, "notes": candidate.review_notes})
    return candidate


def rights_complete(candidate):
    return (
        candidate.commercial_reuse_confirmed and candidate.download_permission_confirmed
        and bool(candidate.rights_holder.strip() and candidate.license_name.strip() and candidate.rights_evidence.strip())
        and (not candidate.permission_url or public_https_url(candidate.permission_url))
        and (not candidate.attribution_url or public_https_url(candidate.attribution_url))
        and (not candidate.attribution_required or bool(candidate.attribution_text.strip()))
    )


def rights_record(candidate):
    record = {field: getattr(candidate, field) for field in RIGHTS_FIELDS} | {
        "image_url_hash": candidate.image_url_hash, "image_url": candidate.image_url, "source_url": candidate.source_url,
    }
    if candidate.staged_image_id:
        record["staged_sha256"] = candidate.staged_image.sha256
    return record


@transaction.atomic
def verify_rights(candidate_id, user):
    require_permission(user, "verify_image_rights")
    candidate = ImageCandidate.objects.select_for_update().get(pk=candidate_id)
    if candidate.published_at:
        raise ReviewError("Withdraw the image before changing its rights record.")
    if not rights_complete(candidate):
        raise ReviewError("Record the rights holder, licence/agreement, evidence, commercial reuse and download permission; add attribution if required. Permission/attribution links must use public HTTPS.")
    candidate.rights_status = "verified"
    candidate.rights_reviewer = user
    candidate.rights_reviewed_at = timezone.now()
    candidate.save(update_fields=["rights_status", "rights_reviewer", "rights_reviewed_at"])
    event(candidate, user, "rights_verified", rights_record(candidate))
    return candidate


def validate_publication(candidate):
    if candidate.published_at or candidate.stored_image:
        raise ReviewError("This candidate has already been published.")
    if candidate.match_status != "approved" or not candidate.match_reviewer_id or not candidate.match_reviewed_at:
        raise ReviewError("A human product match approval is required before any original image download.")
    human = candidate.match_checks.get("human_review", {})
    if candidate.staged_image_id:
        asset = candidate.staged_image
        if (asset.image_url != candidate.image_url or asset.source_url != candidate.source_url
                or asset.sha256 != human.get("staged_sha256")):
            raise ReviewError("Staged image changed after approval; review the image again.")
    if not all(human.get(field) is True for field in MATCH_CONFIRMATIONS):
        raise ReviewError("The human visual match checklist is incomplete.")
    if candidate.rights_status != "verified" or not candidate.rights_reviewer_id or not candidate.rights_reviewed_at or not rights_complete(candidate):
        raise ReviewError("Verified commercial reuse and download permission are required before downloading.")
    # Check evidence still agrees with the record that was actually verified.
    rights_event = candidate.review_events.filter(action="rights_verified").first()
    if not rights_event or rights_event.details != rights_record(candidate):
        raise ReviewError("Rights evidence changed after verification; verify it again.")
    current = snapshot(candidate.product)
    if candidate.product_snapshot != current or human.get("product_snapshot") != current or human.get("variant_id") != candidate.variant_id:
        raise ReviewError("Product/variant details changed after approval; review the match again.")
    if (candidate.image_url_hash != hashlib.sha256(candidate.image_url.encode()).hexdigest()
            or human.get("image_url_hash") != candidate.image_url_hash or human.get("source_url") != candidate.source_url):
        raise ReviewError("Image/source URL changed after approval; review the match and rights again.")
    if candidate.variant:
        if candidate.variant.product_id != candidate.product_id:
            raise ReviewError("The target variant belongs to another product.")
        if candidate.variant.image:
            raise ReviewError("The variant already has an image. Existing manual images are preserved.")
    elif candidate.product.image:
        raise ReviewError("The product already has an image. Existing manual images are preserved.")
    if not public_https_url(candidate.image_url):
        raise ReviewError("The original image URL must use public HTTPS.")


def optimize_image(data, content_type):
    if not data or len(data) > settings.PRODUCT_IMAGE_MAX_BYTES:
        raise ImageDownloadError("Image is empty or exceeds the download limit.")
    allowed = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                width, height = probe.size
                if allowed.get(content_type) != probe.format:
                    raise ImageDownloadError("Image signature does not match its declared format.")
                if getattr(probe, "n_frames", 1) != 1:
                    raise ImageDownloadError("Animated product images are not supported.")
                if min(width, height) < 300 or max(width, height) < 600 or width * height > settings.PRODUCT_IMAGE_MAX_PIXELS:
                    raise ImageDownloadError("Image dimensions must be at least 300px per side, 600px on the longer side, and at most 25 megapixels.")
                probe.verify()
            with Image.open(io.BytesIO(data)) as original:
                image = ImageOps.exif_transpose(original)
                image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                output = io.BytesIO()
                image.save(output, "WEBP", quality=85, method=6, exif=b"", xmp=b"", icc_profile=None)
                optimized = output.getvalue()
                info = {"source_width": width, "source_height": height, "width": image.width, "height": image.height,
                        "original_bytes": len(data), "stored_bytes": len(optimized), "format": "WEBP",
                        "original_sha256": hashlib.sha256(data).hexdigest(), "sha256": hashlib.sha256(optimized).hexdigest()}
                return optimized, info
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        if isinstance(error, ImageDownloadError):
            raise
        raise ImageDownloadError(f"Invalid or unsafe product image ({type(error).__name__}).") from error


def publish_candidate(candidate_id, user):
    require_permission(user, "publish_product_images")
    candidate = ImageCandidate.objects.select_related("product__category", "variant").get(pk=candidate_id)
    validate_publication(candidate)  # Both gates checked BEFORE network access.
    if candidate.staged_image_id:
        asset = candidate.staged_image
        with asset.image.open("rb") as stream:
            data = stream.read(settings.PRODUCT_IMAGE_MAX_BYTES + 1)
        if hashlib.sha256(data).hexdigest() != asset.sha256:
            raise ReviewError("Staged image checksum changed; publication blocked.")
        content_type, final_url = "image/webp", asset.metadata.get("final_url", asset.image_url)
    else:
        data, content_type, final_url = download_image(candidate.image_url)
    optimized, info = optimize_image(data, content_type)
    saved_name, storage = None, None
    try:
        with transaction.atomic():
            candidate = ImageCandidate.objects.select_for_update(of=("self",)).select_related("product__category", "variant").get(pk=candidate_id)
            candidate.product = Product.objects.select_for_update().select_related("category").get(pk=candidate.product_id)
            if candidate.variant_id:
                candidate.variant = ProductVariant.objects.select_for_update().get(pk=candidate.variant_id)
            validate_publication(candidate)  # Recheck races and manual uploads.
            size = candidate.variant.bottle_size if candidate.variant else "product"
            filename = f"{slugify(candidate.product.name)[:90]}-{slugify(size)[:30]}-{candidate.pk}-{info['sha256'][:12]}.webp"
            candidate.stored_image.save(filename, ContentFile(optimized), save=False)
            saved_name, storage = candidate.stored_image.name, candidate.stored_image.storage
            target = candidate.variant or candidate.product
            target.image.name = saved_name
            target.save(update_fields=["image"])
            candidate.published_at = timezone.now()
            candidate.download_info = info | {"final_url": final_url, "content_type": content_type}
            candidate.match_checks = dict(candidate.match_checks) | {
                "dimensions_are_verified": True,
                "actual_source_dimensions": [info["source_width"], info["source_height"]],
            }
            candidate.save(update_fields=["stored_image", "published_at", "download_info", "match_checks"])
            event(candidate, user, "published", {"filename": saved_name, **candidate.download_info})
        return candidate
    except Exception:
        if saved_name and storage:
            storage.delete(saved_name)
        raise


@transaction.atomic
def withdraw_candidate(candidate_id, user):
    require_permission(user, "publish_product_images")
    candidate = ImageCandidate.objects.select_for_update(of=("self",)).select_related("product", "variant").get(pk=candidate_id)
    if not candidate.published_at or not candidate.stored_image:
        raise ReviewError("This candidate is not published.")
    filename, storage = candidate.stored_image.name, candidate.stored_image.storage
    product = Product.objects.select_for_update().get(pk=candidate.product_id)
    variant = ProductVariant.objects.select_for_update().get(pk=candidate.variant_id) if candidate.variant_id else None
    target = variant or product
    if target.image.name == filename:
        target.image = ""
        target.save(update_fields=["image"])
    if Product.objects.filter(image=filename).exists() or ProductVariant.objects.filter(image=filename).exists():
        raise ReviewError("This file is also referenced elsewhere; remove those references before withdrawal.")
    candidate.stored_image = ""
    candidate.published_at = None
    candidate.rights_status = "unknown"
    candidate.rights_reviewer = None
    candidate.rights_reviewed_at = None
    candidate.match_status = "pending"
    candidate.save(update_fields=["stored_image", "published_at", "rights_status", "rights_reviewer", "rights_reviewed_at", "match_status"])
    event(candidate, user, "withdrawn", {"filename": filename})
    transaction.on_commit(lambda: storage.delete(filename))
    return candidate
