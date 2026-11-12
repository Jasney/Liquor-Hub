import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured
from django.templatetags.static import static
from django.urls import reverse_lazy
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def database_config():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
    parsed = urlparse(database_url)
    if parsed.scheme in {"postgres", "postgresql", "postgresql+psycopg"}:
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "",
            "PORT": parsed.port or "",
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"sslmode": "require"} if env_bool("DATABASE_SSL", False) else {},
        }
    raise ValueError("DATABASE_URL must use a PostgreSQL URL.")


SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-local-development-only")
DEBUG = env_bool("DEBUG", True)
ALLOWED_HOSTS = [host.strip() for host in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host.strip()]
if not DEBUG and SECRET_KEY == "django-insecure-local-development-only":
    raise ImproperlyConfigured("Set a strong SECRET_KEY when DEBUG=False.")
if not DEBUG and not os.getenv("ALLOWED_HOSTS"):
    raise ImproperlyConfigured("Set ALLOWED_HOSTS when DEBUG=False.")
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]
AGE_VERIFICATION_REQUIRED = env_bool("AGE_VERIFICATION_REQUIRED", True)
REQUIRE_DELIVERY_AGE_VERIFICATION = env_bool("REQUIRE_DELIVERY_AGE_VERIFICATION", True)
PAYMENT_MODE = os.getenv("PAYMENT_MODE", "mock").strip().lower()
PRODUCT_IMAGE_REQUEST_INTERVAL = max(0.1, float(os.getenv("PRODUCT_IMAGE_REQUEST_INTERVAL", "1.1")))
PRODUCT_IMAGE_RETRIES = max(0, min(3, int(os.getenv("PRODUCT_IMAGE_RETRIES", "2"))))
PRODUCT_IMAGE_MAX_BYTES = 8 * 1024 * 1024
PRODUCT_IMAGE_MAX_PIXELS = 25_000_000
if PAYMENT_MODE not in {"mock", "daraja", "pesapal"}:
    raise ImproperlyConfigured("PAYMENT_MODE must be 'mock', 'daraja' or 'pesapal'.")
PESAPAL_BASE_URL = os.getenv("PESAPAL_BASE_URL", "https://cybqa.pesapal.com/pesapalv3").rstrip("/")
PESAPAL_CONSUMER_KEY = os.getenv("PESAPAL_CONSUMER_KEY", "")
PESAPAL_CONSUMER_SECRET = os.getenv("PESAPAL_CONSUMER_SECRET", "")
PESAPAL_IPN_ID = os.getenv("PESAPAL_IPN_ID", "")
ORDER_NOTIFICATION_EMAIL = os.getenv("ORDER_NOTIFICATION_EMAIL", "")

INSTALLED_APPS = [
    "unfold",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "store.apps.StoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "store.middleware.AgeGateMiddleware",
]
if not DEBUG:
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "store.context_processors.store_info",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DATABASES = {"default": database_config()}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-ke"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedStaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/admin/login/"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

UNFOLD = {
    "SITE_TITLE": "Liquor Hub Diani",
    "SITE_HEADER": "Liquor Hub Diani",
    "SITE_URL": "/",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": "Dashboard",
                "items": [
                    {
                        "title": "Overview",
                        "icon": "dashboard",
                        "link": reverse_lazy("admin:index"),
                    },
                ],
            },
            {
                "title": "Products",
                "items": [
                    {
                        "title": "All products",
                        "icon": "inventory_2",
                        "link": reverse_lazy("admin:store_product_changelist"),
                        "permission": lambda request: request.user.has_perm("store.view_product")
                        or request.user.has_perm("store.change_product")
                        or request.user.has_perm("store.add_product"),
                    },
                    {
                        "title": "Categories",
                        "icon": "category",
                        "link": reverse_lazy("admin:store_category_changelist"),
                        "permission": lambda request: request.user.has_perm("store.view_category")
                        or request.user.has_perm("store.change_category")
                        or request.user.has_perm("store.add_category"),
                    },
                ],
            },
            {
                "title": "Sales",
                "items": [
                    {
                        "title": "Orders",
                        "icon": "shopping_bag",
                        "link": reverse_lazy("admin:store_order_changelist"),
                        "permission": lambda request: request.user.has_perm("store.view_order")
                        or request.user.has_perm("store.change_order"),
                    },
                    {
                        "title": "Payments",
                        "icon": "payments",
                        "link": reverse_lazy("admin:store_payment_changelist"),
                        "permission": lambda request: request.user.has_perm("store.view_payment")
                        or request.user.has_perm("store.change_payment"),
                    },
                ],
            },
            {
                "title": "Administration",
                "items": [
                    {
                        "title": "Users",
                        "icon": "people",
                        "link": reverse_lazy("admin:auth_user_changelist"),
                        "permission": lambda request: request.user.is_superuser
                        or request.user.has_perm("auth.view_user")
                        or request.user.has_perm("auth.change_user"),
                    },
                ],
            },
        ],
    },
    "DASHBOARD_CALLBACK": "store.admin.dashboard_callback",
    "STYLES": [lambda request: static("admin/css/liquorhub-admin.css")],
    "COLORS": {
        "primary": {
            "50": "255 251 235",
            "100": "254 243 199",
            "200": "253 230 138",
            "300": "252 211 77",
            "400": "251 191 36",
            "500": "217 164 65",
            "600": "180 128 33",
            "700": "146 96 22",
            "800": "120 75 18",
            "900": "92 57 16",
            "950": "69 41 12",
        },
    },
}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "orders@liquorhub.co.ke")

DARAJA_CONSUMER_KEY = os.getenv("DARAJA_CONSUMER_KEY", "")
DARAJA_CONSUMER_SECRET = os.getenv("DARAJA_CONSUMER_SECRET", "")
DARAJA_SHORTCODE = os.getenv("DARAJA_SHORTCODE", "")
DARAJA_PASSKEY = os.getenv("DARAJA_PASSKEY", "")
DARAJA_CALLBACK_URL = os.getenv("DARAJA_CALLBACK_URL", "")
DARAJA_CALLBACK_TOKEN = os.getenv("DARAJA_CALLBACK_TOKEN", "")
DARAJA_BASE_URL = os.getenv("DARAJA_BASE_URL", "https://sandbox.safaricom.co.ke")

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
