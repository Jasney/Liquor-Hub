import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from pathlib import Path

from django.conf import settings

from .matching import public_https_url, tokens


class DiscoveryError(ValueError):
    def __init__(self, message, *, fatal=False):
        super().__init__(message)
        self.fatal = fatal


def retry_delay(headers, attempt, error_class=DiscoveryError):
    value = headers.get("Retry-After") or headers.get("retry-after")
    delay = 2 ** attempt
    if value:
        try:
            delay = int(value) if str(value).isdigit() else (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            pass
    if delay > 30:
        raise error_class("Source requests a longer Retry-After delay; stop this run and retry later rather than ignore its limit.")
    return max(0, delay)


class RequestGate:
    def __init__(self, interval=None):
        self.interval = max(0.0, settings.PRODUCT_IMAGE_REQUEST_INTERVAL if interval is None else interval)
        self.last = {}

    def wait(self, host):
        delay = self.interval - (time.monotonic() - self.last.get(host, float("-inf")))
        if delay > 0:
            time.sleep(delay)
        self.last[host] = time.monotonic()


def positive_int(value):
    try:
        return max(0, min(100000, int(value or 0)))
    except (ValueError, TypeError):
        return 0


def normalize_result(item, feed_permission=""):
    if not isinstance(item, dict):
        return None
    source_url, image_url = item.get("source_url", ""), item.get("image_url", "")
    title, thumbnail_url = item.get("title", ""), item.get("thumbnail_url", "")
    width, height = item.get("width"), item.get("height")
    permitted_preview = bool(feed_permission and item.get("preview_permitted") is True)
    raw_attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    attributes = {k: str(v)[:500] for k, v in raw_attributes.items()
                  if k in {"brand", "name", "flavour", "edition", "size", "packaging"}}
    metadata = {"feed_permission_reference": feed_permission,
                "rights_claim": str(item.get("rights_claim", "Unverified"))[:2000]}
    if not isinstance(title, str) or not title.strip() or not public_https_url(source_url) or not public_https_url(image_url):
        return None
    if not public_https_url(thumbnail_url) or thumbnail_url == image_url:
        thumbnail_url, permitted_preview = "", False
    return {"title": title[:500], "source_url": source_url, "image_url": image_url,
            "thumbnail_url": thumbnail_url, "preview_permitted": permitted_preview,
            "width": positive_int(width), "height": positive_int(height), "attributes": attributes,
            "metadata": metadata | {"attributes": attributes}}


class CandidateProvider(Protocol):
    """Source adapter contract. Return normalized metadata; never download originals.

    New supplier adapters implement this interface and use normalize_result before
    returning candidates. Matching, review and publication remain source independent.
    """
    name: str

    def search(self, product, query: str) -> list[dict]: ...


class FeedProvider:
    name = "feed"

    def __init__(self, path):
        path = Path(path)
        try:
            if path.stat().st_size > 20 * 1024 * 1024:
                raise DiscoveryError("Supplier feed exceeds 20 MB.")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DiscoveryError("Could not read the supplier JSON feed.") from error
        if not isinstance(payload, dict) or not payload.get("permission_reference") or not isinstance(payload.get("items"), list):
            raise DiscoveryError("Supplier feed requires permission_reference and an items array; this is discovery permission, not automatic commercial image rights.")
        self.items = payload["items"]
        self.permission = str(payload["permission_reference"])[:2000]

    def search(self, product, query):
        results = []
        wanted = tokens(product.name)
        for item in self.items:
            result = normalize_result(item, self.permission)
            if not result:
                continue
            evidence = tokens(result["title"] + " " + " ".join(result["attributes"].values()))
            if len(wanted & evidence) / max(1, len(wanted)) >= .5:
                results.append(result)
        return results
