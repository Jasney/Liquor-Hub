"""Bounded HTTPS downloads with public-IP pinning and robots enforcement."""

import ipaddress
import errno
import socket
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import certifi
import urllib3
from django.conf import settings

from .matching import public_https_url
from .providers import RequestGate, retry_delay


class ImageDownloadError(ValueError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _permission_denied(error):
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if (isinstance(current, PermissionError)
                or getattr(current, "winerror", None) in {5, 10013}
                or getattr(current, "errno", None) in {errno.EACCES, errno.EPERM}):
            return True
        current = current.__cause__ or current.__context__
    return False


USER_AGENT = "LiquorHubImageReview/1.0"


def resolve_public_host(url):
    if not public_https_url(url):
        raise ImageDownloadError("Only public HTTPS URLs on port 443 are allowed.")
    host = urlsplit(url).hostname
    try:
        addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except OSError as error:
        raise ImageDownloadError("Image host DNS lookup failed.", retryable=not _permission_denied(error)) from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ImageDownloadError("Image host resolves to a private, reserved or otherwise non-public address.")
    return host, addresses[0]


def pinned_get(url, limit, gate):
    host, address = resolve_public_host(url)
    gate.wait(host)
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    # Connect to the validated IP, retaining certificate hostname checks + SNI.
    # No second hostname resolution, environment proxy or automatic redirect.
    pool = urllib3.HTTPSConnectionPool(
        address, port=443, server_hostname=host, assert_hostname=host,
        cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
        timeout=urllib3.Timeout(connect=5, read=15), retries=False,
    )
    try:
        response = pool.request("GET", path, headers={"Host": host, "User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
                                preload_content=False, redirect=False, assert_same_host=False)
        try:
            headers = dict(response.headers)
            if response.status != 200:
                return response.status, headers, b""
            length = response.headers.get("Content-Length")
            if length and (not length.isdigit() or int(length) > limit):
                raise ImageDownloadError("Declared download size exceeds the limit or is invalid.")
            data = bytearray()
            started = time.monotonic()
            for chunk in response.stream(65536, decode_content=True):
                data.extend(chunk)
                if len(data) > limit:
                    raise ImageDownloadError("Download exceeds the byte limit.")
                if time.monotonic() - started > 30:
                    raise ImageDownloadError("Download exceeded the overall time limit.")
            return response.status, headers, bytes(data)
        finally:
            response.close()
    except (urllib3.exceptions.HTTPError, OSError) as error:
        if _permission_denied(error):
            raise ImageDownloadError("HTTPS connection blocked by local socket permissions.") from error
        transient = isinstance(error, (urllib3.exceptions.NewConnectionError,
                                      urllib3.exceptions.ConnectTimeoutError,
                                      urllib3.exceptions.ReadTimeoutError,
                                      urllib3.exceptions.ProtocolError,
                                      TimeoutError, ConnectionError))
        raise ImageDownloadError(f"HTTPS image request failed ({type(error).__name__}).", retryable=transient) from error
    finally:
        pool.close()


def bounded_get(url, limit, gate, redirects=3):
    current = url
    for _ in range(redirects + 1):
        for attempt in range(settings.PRODUCT_IMAGE_RETRIES + 1):
            status, headers, data = pinned_get(current, limit, gate)
            if status not in {429, 500, 502, 503, 504} or attempt == settings.PRODUCT_IMAGE_RETRIES:
                break
            time.sleep(retry_delay(headers, attempt, ImageDownloadError))
        if status in {301, 302, 303, 307, 308}:
            location = headers.get("Location") or headers.get("location")
            if not location:
                raise ImageDownloadError("Image redirect has no destination.")
            current = urljoin(current, location)
            continue  # pinned_get validates every redirect destination afresh.
        return status, headers, data, current
    raise ImageDownloadError("Too many image redirects.")


def check_robots(url, gate):
    parts = urlsplit(url)
    robots_url = f"https://{parts.netloc}/robots.txt"
    status, _, body, _ = bounded_get(robots_url, 256 * 1024, gate)
    if status == 404:
        return
    if status != 200:
        raise ImageDownloadError(f"Cannot confirm robots permission (HTTP {status}); download blocked.")
    if body.lstrip().startswith(b"<"):
        raise ImageDownloadError("robots.txt returned an HTML/challenge page; permission could not be confirmed.")
    parser = RobotFileParser()
    parser.parse(body.decode("utf-8", errors="replace").splitlines())
    if not parser.can_fetch(USER_AGENT, url):
        raise ImageDownloadError("Source robots.txt disallows this download.")
    delay = parser.crawl_delay(USER_AGENT)
    request_rate = parser.request_rate(USER_AGENT)
    if request_rate and request_rate.requests > 0:
        delay = max(delay or 0, request_rate.seconds / request_rate.requests)
    if delay:
        if delay > 30:
            raise ImageDownloadError("Source crawl-delay exceeds this worker's limit; use a permitted supplier feed instead.")
        gate.interval = max(gate.interval, delay)


def download_image(url):
    gate = RequestGate()
    current = url
    # Every image redirect gets its own robots check before the image is fetched.
    for _ in range(4):
        resolve_public_host(current)
        check_robots(current, gate)
        for attempt in range(settings.PRODUCT_IMAGE_RETRIES + 1):
            status, headers, body = pinned_get(current, settings.PRODUCT_IMAGE_MAX_BYTES, gate)
            if status not in {429, 500, 502, 503, 504} or attempt == settings.PRODUCT_IMAGE_RETRIES:
                break
            time.sleep(retry_delay(headers, attempt, ImageDownloadError))
        if status in {301, 302, 303, 307, 308}:
            location = headers.get("Location") or headers.get("location")
            if not location:
                raise ImageDownloadError("Image redirect has no destination.")
            current = urljoin(current, location)
            continue
        if status != 200:
            raise ImageDownloadError(f"Image download returned HTTP {status}.")
        content_type = (headers.get("Content-Type") or headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ImageDownloadError("Image response must be JPEG, PNG or WebP.")
        return body, content_type, current
    raise ImageDownloadError("Too many image redirects.")
