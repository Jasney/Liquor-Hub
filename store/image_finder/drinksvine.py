"""Bounded, cached Drinks Vine extraction. Source text is data, never instructions."""
import hashlib
import json
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils.text import slugify

from store.models import StagedSourceImage
from .matching import packaging, size_values, tokens
from .network import ImageDownloadError, USER_AGENT, pinned_get
from .providers import DiscoveryError, RequestGate, normalize_result, retry_delay
from .review import optimize_image

BASE = "https://drinksvine.co.ke/"
PERMISSION = "Website owner permission for bulk downloading and commercial reuse, reported by Liquor Hub operator on 2026-10-21; administrator must record and verify evidence before publication."


def source_url(value, base=BASE):
    url = urldefrag(urljoin(base, value))[0]
    p = urlsplit(url)
    if p.scheme == "https" and p.hostname == "drinksvine.co.ke" and not p.username and not p.password and p.port in (None, 443):
        return url
    return None


def permitted_image(url):
    p = urlsplit(url)
    return (p.scheme == "https" and not p.username and not p.password and p.port in (None, 443)
            and ((p.hostname == "ik.imagekit.io" and p.path.startswith("/drinksvine/products/"))
                 or (p.hostname == "drinksvine.co.ke" and p.path.startswith(("/products/", "/uploads/")))))


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links, self.documents, self.images = [], [], []
        self.capture, self.buffer = False, []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "img":
            self.images.append(attrs)
        if tag == "script":
            self.capture = attrs.get("type") == "application/ld+json"
            self.buffer = []

    def handle_data(self, data):
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.capture:
            try:
                self.documents.append(json.loads("".join(self.buffer)))
            except (ValueError, TypeError):
                pass
            self.capture = False


def nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from nodes(item)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "hasVariant"):
            yield from nodes(value.get(key))


def extract_product(html, url):
    page = Page(html)
    data = [n for d in page.documents for n in nodes(d)]
    groups = [n for n in data if n.get("@type") == "ProductGroup"]
    products = [n for n in data if n.get("@type") == "Product" and n.get("name")]
    if not products:
        raise DiscoveryError("No structured product data; retained in extraction errors for manual review.")
    name = str((groups or products)[0]["name"])[:500]
    brand = (groups or products)[0].get("brand", {})
    brand = str(brand.get("name", "")) if isinstance(brand, dict) else str(brand)
    images = {}
    sizes = []
    for product in products:
        size = str(product.get("size") or "")
        if size and size not in sizes:
            sizes.append(size)
        raw = product.get("image", [])
        for image in raw if isinstance(raw, list) else [raw]:
            image = {"url": image} if isinstance(image, str) else image
            if not isinstance(image, dict):
                continue
            image_url = urljoin(url, image.get("contentUrl") or image.get("url") or "")
            if permitted_image(image_url):
                entry = images.setdefault(image_url, {"url": image_url, "sizes": [], "role": "main" if not images else "additional"})
                if size and size not in entry["sizes"]:
                    entry["sizes"].append(size)
    # Gallery images must identify this exact product; related-product cards do not.
    for img in page.images:
        if re.fullmatch(re.escape(name) + r" - \d+ - Drinks Vine", img.get("alt", ""), re.I):
            image_url = urljoin(url, img.get("src", "")).split("?", 1)[0]
            if permitted_image(image_url):
                images.setdefault(image_url, {"url": image_url, "sizes": [], "role": "additional"})
    return {"name": name, "brand": brand, "sizes": sizes, "source_url": url, "images": list(images.values())}


def extract_cached_product_pages(cache_dir):
    """Recover validated cached product-page metadata without opening sockets."""
    cache_dir = Path(cache_dir)
    products, errors, html_pages = [], [], 0
    for meta_path in sorted(cache_dir.glob("*.json")):
        body_path = meta_path.with_suffix(".bin")
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            if "html" not in metadata.get("content_type", "").casefold():
                continue
            html_pages += 1
            body = body_path.read_bytes()
            if hashlib.sha256(body).hexdigest() != metadata.get("sha256"):
                raise ValueError("cached body SHA-256 does not match metadata")
            url = source_url(metadata.get("final_url", ""))
            if not url or not urlsplit(url).path.startswith("/product/"):
                continue
            products.append(extract_product(body.decode("utf-8", "replace"), url))
        except (OSError, ValueError, TypeError, KeyError) as error:
            errors.append({"cache_file": str(meta_path), "error": f"{type(error).__name__}: {error}"})
    return {"html_pages_examined": html_pages, "products": products, "errors": errors}


class SourceClient:
    """Only allowlisted public HTTPS, robots checked per redirect, disk cache."""
    def __init__(self, cache_dir, max_requests=150):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.gate = RequestGate(interval=1.5)
        self.robots = {}
        self.requests = 0
        self.max_requests = max_requests
        self.cache_hits = 0

    def raw(self, url, limit):
        if self.requests >= self.max_requests:
            raise DiscoveryError("Source request limit reached; rerun using the cache.", fatal=True)
        for attempt in range(3):
            if self.requests >= self.max_requests:
                raise DiscoveryError("Source request limit reached.", fatal=True)
            self.requests += 1
            try:
                status, headers, body = pinned_get(url, limit, self.gate)
            except ImageDownloadError as error:
                if error.retryable and attempt < 2:
                    time.sleep(retry_delay({}, attempt))
                    continue
                if "local socket permissions" in str(error):
                    raise DiscoveryError(str(error), fatal=True) from error
                if error.retryable:
                    raise DiscoveryError(f"Source connection failed after {attempt + 1} attempts: {error}", fatal=True) from error
                raise DiscoveryError(str(error), fatal=True) from error
            if status not in {429, 500, 502, 503, 504} or attempt == 2:
                return status, headers, body
            time.sleep(retry_delay(headers, attempt))

    def allowed(self, url):
        p = urlsplit(url)
        root = f"https://{p.netloc}"
        if root not in self.robots:
            status, _, body = self.raw(root + "/robots.txt", 256 * 1024)
            if status == 404:
                self.robots[root] = None
            elif status == 200 and not body.lstrip().startswith(b"<"):
                parser = RobotFileParser()
                parser.parse(body.decode("utf8", "replace").splitlines())
                self.robots[root] = parser
            else:
                raise DiscoveryError(f"Cannot verify robots rules: HTTP {status}.", fatal=True)
        parser = self.robots[root]
        if parser:
            if not parser.can_fetch(USER_AGENT, url):
                raise DiscoveryError("Source robots rules disallow this URL.")
            delay = parser.crawl_delay(USER_AGENT) or 0
            rate = parser.request_rate(USER_AGENT)
            if rate and rate.requests:
                delay = max(delay, rate.seconds / rate.requests)
            if delay > 30:
                raise DiscoveryError("Source requires a longer crawl delay; stop and retry later.", fatal=True)
            self.gate.interval = max(self.gate.interval, delay)

    def get(self, url, image=False):
        current = url
        limit = 8 * 1024 * 1024 if image else 4 * 1024 * 1024
        cache = self.cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        for _ in range(4):
            if not (permitted_image(current) if image else source_url(current)):
                raise DiscoveryError("URL is outside the permitted source scope.")
            self.allowed(current)
            if current == url and cache.exists():
                try:
                    payload = json.loads(cache.read_text())
                    body = (self.cache / (cache.stem + ".bin")).read_bytes()
                    if len(body) <= limit and hashlib.sha256(body).hexdigest() == payload["sha256"]:
                        self.cache_hits += 1
                        return body, payload["content_type"], payload["final_url"]
                except (OSError, ValueError, KeyError, TypeError):
                    # A partial/corrupt cache entry is a miss. Refetching from
                    # the same allowlisted URL keeps interrupted runs resumable.
                    pass
            status, headers, body = self.raw(current, limit)
            if status in {301, 302, 303, 307, 308}:
                current = urljoin(current, headers.get("Location") or headers.get("location") or "")
                continue
            if status != 200:
                raise DiscoveryError(f"Source returned HTTP {status} for {current}")
            content_type = (headers.get("Content-Type") or headers.get("content-type") or "").split(";")[0].lower()
            (self.cache / (cache.stem + ".bin")).write_bytes(body)
            cache.write_text(json.dumps({"content_type": content_type, "sha256": hashlib.sha256(body).hexdigest(), "final_url": current}))
            return body, content_type, current
        raise DiscoveryError("Too many source redirects.")


def discover_urls(client, listing_limit=8):
    urls, errors = set(), []
    sitemaps = [BASE + "sitemap.xml", BASE + "products/sitemap.xml"]
    seen = set()
    while sitemaps and len(seen) < 12:
        url = sitemaps.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            body, _, _ = client.get(url)
            root = ET.fromstring(body)
            for node in root.iter():
                if node.tag.endswith("}loc") or node.tag == "loc":
                    link = source_url(node.text or "")
                    if link and urlsplit(link).path.startswith("/product/"):
                        urls.add(link)
                    elif link and root.tag.endswith("sitemapindex"):
                        sitemaps.append(link)
        except (ValueError, ET.ParseError) as error:
            errors.append({"url": url, "error": str(error)})
            if getattr(error, "fatal", False):
                raise
    listings, seen = [BASE], set()
    while listings and len(seen) < listing_limit:
        url = listings.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            body, _, _ = client.get(url)
            for href in Page(body.decode("utf8", "replace")).links:
                link = source_url(href, url)
                if not link:
                    continue
                path = urlsplit(link).path
                if path.startswith("/product/"):
                    urls.add(link)
                elif (path.strip("/") in {"whisky", "wine", "beers", "champagne", "liqueur", "tequila", "gin", "cognac", "brandy", "vodka", "rum", "extras", "gifts"}
                      or path.startswith(("/category/", "/products/"))) and link not in seen:
                    listings.append(link)
        except ValueError as error:
            errors.append({"url": url, "error": str(error)})
            if getattr(error, "fatal", False):
                raise
    return sorted(urls), errors, bool(listings)


def identity(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ml|litres?|l)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def similarity(name, other):
    a, b = tokens(identity(name)), tokens(identity(other))
    if not a or not b or not a & b:
        return 0
    # Used only for page selection / possible matches, never for approval.
    return .7 * len(a & b) / len(a | b) + .3 * SequenceMatcher(None, identity(name), identity(other)).ratio()


def select_pages(products, urls, limit):
    ranked = {}
    for product in products:
        options = sorted(((similarity(product.name, unquote(urlsplit(url).path.rsplit("/", 1)[-1])), url) for url in urls), reverse=True)
        for score, url in options[:2]:
            if score >= .3:
                ranked[url] = max(ranked.get(url, 0), score)
    return sorted(ranked, key=lambda url: (-ranked[url], url))[:limit]


def stage_image(client, source, image):
    existing = StagedSourceImage.objects.filter(image_url=image["url"]).first()
    if existing and existing.image and existing.image.storage.exists(existing.image.name):
        return existing, False
    # If the staged record exists but its file was removed, preserve the same
    # DB row and restore its asset from the verified cache/network response.
    body, content_type, final = client.get(image["url"], image=True)
    optimized, info = optimize_image(body, content_type)
    digest = hashlib.sha256(optimized).hexdigest()
    identical = StagedSourceImage.objects.filter(sha256=digest).exclude(image="").first()
    name = identical.image.name if identical and identical.image.storage.exists(identical.image.name) else None
    if not name:
        name = default_storage.save(f"image-staging/drinksvine/{slugify(source['name'])[:90]}-{digest[:20]}.webp", ContentFile(optimized))
    asset = existing or StagedSourceImage(image_url=image["url"])
    # Keep one durable row per source URL, even when restoring a missing file.
    # If storage had to choose a collision-safe filename, that returned name
    # is written back below.
    asset.source_name, asset.source_product_name, asset.source_url = "Drinks Vine", source["name"], source["source_url"]
    asset.available_sizes, asset.image, asset.sha256 = image["sizes"], name, digest
    asset.width, asset.height = info["width"], info["height"]
    asset.metadata = {"permission": PERMISSION, "final_url": final, "original_sha256": hashlib.sha256(body).hexdigest(), "source_sizes": source["sizes"], "image_role": image["role"], "shared_size_image": len(image["sizes"]) > 1}
    asset.save()
    return asset, True


class DrinksVineProvider:
    name = "drinksvine"

    def __init__(self, assets):
        self.assets = assets

    def search(self, product, query):
        results = []
        for asset in self.assets:
            if similarity(product.name, asset.source_product_name) < .3:
                continue
            result = normalize_result({"title": asset.source_product_name,
                "source_url": asset.source_url, "image_url": asset.image_url,
                "width": asset.width, "height": asset.height,
                # Shared gallery images do not prove a bottle size shown.
                "attributes": {"size": asset.available_sizes[0] if len(asset.available_sizes) == 1 else ""},
                "rights_claim": PERMISSION}, PERMISSION)
            if result:
                result["metadata"].update(staged_asset_id=asset.pk, staged_sha256=asset.sha256,
                    source_available_sizes=asset.available_sizes, shared_size_image=len(asset.available_sizes) > 1,
                    normalized_identity_exact=identity(product.name) == identity(asset.source_product_name))
                results.append(result)
        return results
