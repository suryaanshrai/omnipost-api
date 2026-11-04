"""
Django settings for the OmniPost API.

All deployment-sensitive values come from the environment. See infra/.env.example
for the full list. Nothing secret is committed to this file.
"""

import os
import sys
from pathlib import Path

from corsheaders.defaults import default_headers
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# Detected automatically under pytest (PYTEST_CURRENT_TEST is set by pytest
# itself once a test starts; "pytest" in sys.modules covers collection time
# too). Relaxes the production secret requirement and defaults to an
# in-memory sqlite DB so `pytest` runs with zero setup — no Postgres/Redis
# required — while CI's real Postgres-backed job engine tests still opt into
# a real DB by setting DB_HOST explicitly.
TESTING = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


# --------------------------------------------------------------------------
# Environment helpers
# --------------------------------------------------------------------------

def env_bool(name: str, default: bool = False) -> bool:
    # Empty counts as unset: compose passes `${VAR:-}` through as "".
    return (os.environ.get(name) or str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def env_required(name: str) -> str:
    """Read a variable that must be set outside of DEBUG."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(
            f"{name} must be set. Copy infra/.env.example to .env and fill it in."
        )
    return value


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

# Fail safe: production unless explicitly told otherwise.
DEBUG = env_bool("DJANGO_DEBUG", False) or TESTING

if DEBUG:
    SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-local-development-only")
else:
    SECRET_KEY = env_required("DJANGO_SECRET_KEY")

# The KEK that wraps every encrypted credential (see omnipost_api/crypto.py).
# Given a fixed dev-only value under DEBUG/TESTING so `pytest` and local dev
# need no setup; production must supply a real one (openssl rand -base64 32).
if DEBUG and "OMNIPOST_KEK" not in os.environ:
    os.environ["OMNIPOST_KEK"] = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

# Dev/test-only placeholder managed AI keys, mirroring OMNIPOST_KEK above —
# lets `pytest` and local dev exercise the managed-provider path (mocked at
# the HTTP layer in tests) without a real key. Production must supply real
# ones via ANTHROPIC_API_KEY/OPENAI_API_KEY, or leave them unset to disable
# the managed path entirely and require BYOK.
if DEBUG and "ANTHROPIC_API_KEY" not in os.environ:
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-dev-only-placeholder"
if DEBUG and "OPENAI_API_KEY" not in os.environ:
    os.environ["OPENAI_API_KEY"] = "sk-dev-only-placeholder"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set when DJANGO_DEBUG is off.")
# The container healthcheck calls http://localhost:8000/healthz from inside
# the container; without these it gets a 400 DisallowedHost in production.
for _local in ("localhost", "127.0.0.1"):
    if _local not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_local)

# Never allow all origins: the API accepts session cookies for the browsable API.
CORS_ALLOWED_ORIGINS = env_list(
    "DJANGO_CORS_ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173" if DEBUG else "",
)
CORS_ALLOW_CREDENTIALS = True
# The web app sends X-OmniPost-Client with every request (cookie sign-in).
CORS_ALLOW_HEADERS = (*default_headers, "x-omnipost-client")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", ",".join(CORS_ALLOWED_ORIGINS))

AUTH_USER_MODEL = "omnipost_api.User"
SITE_ID = 1
ROOT_URLCONF = "app.urls"
WSGI_APPLICATION = "app.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "omnipost_api",
    "storages",
    "django_rq",
    "rest_framework",
    "rest_framework.authtoken",
    "dj_rest_auth",
    "dj_rest_auth.registration",
    "drf_spectacular",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "corsheaders",
]

# CorsMiddleware must run before CommonMiddleware so preflight responses carry
# the headers. AccountMiddleware must run after AuthenticationMiddleware.
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# --------------------------------------------------------------------------
# DRF
# --------------------------------------------------------------------------

# BasicAuthentication is deliberately absent: it invites credentials on every
# request and no client needs it. SessionAuthentication is kept only in DEBUG so
# the browsable API stays usable locally.
_AUTH_CLASSES = [
    "omnipost_api.authentication.PersonalTokenAuthentication",
    "omnipost_api.authentication.CookieTokenAuthentication",
    "omnipost_api.authentication.ExpiringTokenAuthentication",
]
if DEBUG:
    _AUTH_CLASSES.append("rest_framework.authentication.SessionAuthentication")

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": _AUTH_CLASSES,
    # Closed by default. Endpoints that must be public opt out explicitly.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 25,
    # Login/signup/reset and AI calls get their own tighter scopes
    # (throttle_scope on those views); everything else gets the user/anon caps.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("THROTTLE_ANON", "120/min"),
        "user": os.environ.get("THROTTLE_USER", "3000/hour"),
        "auth": os.environ.get("THROTTLE_AUTH", "10/min"),
        "ai": os.environ.get("THROTTLE_AI", "30/min"),
        "interest": os.environ.get("THROTTLE_INTEREST", "5/hour"),
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "OmniPost",
    "DESCRIPTION": "REST API for the OmniPost project",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # Several models reuse the field names `kind`/`status` with different
    # choice sets; without explicit names spectacular disambiguates with
    # hash suffixes (KindA52Enum) that churn the generated TS types.
    "ENUM_NAME_OVERRIDES": {
        "PostKindEnum": "omnipost_api.models.Post.KIND_CHOICES",
        "PostStatusEnum": "omnipost_api.models.Post.STATUS_CHOICES",
        "PostTargetStatusEnum": "omnipost_api.models.PostTarget.STATUS_CHOICES",
        "MediaKindEnum": "omnipost_api.models.MediaAsset.KIND_CHOICES",
        "PublishAttemptStatusEnum": "omnipost_api.models.PublishAttempt.STATUS_CHOICES",
        "ChannelHealthEnum": "omnipost_api.models.Channel.HEALTH_CHOICES",
        "AIProviderEnum": "omnipost_api.models.ProviderKey.PROVIDER_CHOICES",
    },
}


# --------------------------------------------------------------------------
# Data stores
# --------------------------------------------------------------------------

_default_db: dict[str, str | int]
if TESTING and "DB_HOST" not in os.environ:
    # No Postgres required for `pytest` locally. CI's integration job sets
    # DB_HOST explicitly to run the same tests against real Postgres.
    _default_db = {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
else:
    _default_db = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "omni-db"),
        "USER": os.environ.get("DB_USER", "root"),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "root"),
        "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", "60")),
    }
DATABASES = {"default": _default_db}

_REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
_REDIS_CONN: dict[str, str | int | None] = {
    "HOST": os.environ.get("REDIS_HOST", "localhost"),
    "PORT": int(os.environ.get("REDIS_PORT", "6379")),
    "DB": int(os.environ.get("REDIS_DB", "0")),
    "PASSWORD": _REDIS_PASSWORD or None,
    "DEFAULT_TIMEOUT": 360,
}
# Separate queues so a slow media transcode or metrics poll can't hold up a
# scheduled publish: run `worker` (publish, maintenance, default) and
# `worker-media` (media, metrics, ai) as separate processes (see
# docker/entrypoint.sh). All share one Redis connection.
RQ_QUEUES = {
    "default": _REDIS_CONN,
    "publish": _REDIS_CONN,
    "media": _REDIS_CONN,
    "metrics": _REDIS_CONN,
    "maintenance": _REDIS_CONN,
    "ai": _REDIS_CONN,
}


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

# Was hardcoded to "omnipost-images" inside PostBase.save_to_aws_s3. Also
# doubles as the S3-vs-local switch: self-host without a bucket configured
# falls back to FileSystemStorage, cloud sets this and gets direct-to-S3
# presigned upload (see MediaAssetViewSet.presign in views.py).
AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME", "")
S3_MEDIA_ENABLED = bool(AWS_STORAGE_BUCKET_NAME) and not TESTING
# Without S3, the API serves uploaded media itself so platforms can fetch it
# from PUBLIC_BASE_URL/media/... (fine at self-host scale; put a CDN or S3 in
# front for more).
SERVE_MEDIA = env_bool("SERVE_MEDIA", not S3_MEDIA_ENABLED)

# /admin and the RQ dashboard need a staff login either way; these switch
# them (and the API docs) off entirely.
ENABLE_ADMIN = env_bool("ENABLE_ADMIN", True)
ENABLE_API_DOCS = env_bool("ENABLE_API_DOCS", True)

if S3_MEDIA_ENABLED:
    AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME", "us-east-1")
    # Supports S3-compatible providers (Cloudflare R2, MinIO, Backblaze) as
    # well as real AWS — set AWS_S3_ENDPOINT_URL for anything non-AWS.
    AWS_S3_ENDPOINT_URL = os.environ.get("AWS_S3_ENDPOINT_URL", "") or None
    AWS_S3_CUSTOM_DOMAIN = os.environ.get("AWS_S3_CUSTOM_DOMAIN", "") or None
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = False
    AWS_S3_FILE_OVERWRITE = False
    STORAGES = {
        "default": {"BACKEND": "storages.backends.s3.S3Storage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }
else:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Previously undefined, so uploads landed relative to the working directory and
# could not be served back.
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", BASE_DIR / "mediafiles"))

# Used to turn a locally-stored media file's relative URL into an absolute one
# a connector can fetch or hand to a platform (jobs/context.py). Irrelevant
# once S3-backed storage is configured, since that already returns absolute URLs.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")


# --------------------------------------------------------------------------
# AI
# --------------------------------------------------------------------------

# The managed (server-paid) provider a workspace falls back to when it has no
# BYOK key of its own. Anthropic for text because that's who's operating
# this deployment; OpenAI for image because Anthropic has no image-generation
# endpoint. Either can be left unconfigured (blank key) in a self-host that
# wants to require BYOK for everything — see ai/gateway.py.
AI_DEFAULT_TEXT_PROVIDER = "anthropic"
AI_DEFAULT_TEXT_MODEL = os.environ.get("AI_TEXT_MODEL", "claude-sonnet-5")
AI_MANAGED_TEXT_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

AI_DEFAULT_IMAGE_PROVIDER = "openai"
AI_DEFAULT_IMAGE_MODEL = os.environ.get("AI_IMAGE_MODEL", "gpt-image-1")
AI_MANAGED_IMAGE_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Model used per provider for BYOK text calls (the managed path always uses
# AI_DEFAULT_TEXT_MODEL above, since that's the one model this deployment is
# actually paying for and has picked).
AI_TEXT_MODELS = {
    "anthropic": AI_DEFAULT_TEXT_MODEL,
    "openai": os.environ.get("AI_OPENAI_TEXT_MODEL", "gpt-4o-mini"),
    "gemini": os.environ.get("AI_GEMINI_TEXT_MODEL", "gemini-1.5-flash"),
    "openrouter": os.environ.get("AI_OPENROUTER_TEXT_MODEL", "openai/gpt-4o-mini"),
}

# Free-tier lifetime allotment before a workspace needs its own BYOK key —
# see ai/quota.py's reserve/commit/release.
AI_FREE_TEXT_LIMIT = int(os.environ.get("AI_FREE_TEXT_LIMIT", "3"))
AI_FREE_IMAGE_LIMIT = int(os.environ.get("AI_FREE_IMAGE_LIMIT", "1"))


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Where emails (password reset, verification) link back to.
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

# "optional": a verification email is sent on signup, but an unverified
# address can still sign in. "mandatory" blocks sign-in until verified.
ACCOUNT_EMAIL_VERIFICATION = os.environ.get("ACCOUNT_EMAIL_VERIFICATION", "optional")
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
ACCOUNT_CONFIRM_EMAIL_ON_GET = False
# Without this, subjects read "[example.com] ..." (the default Site's name).
ACCOUNT_EMAIL_SUBJECT_PREFIX = "[OmniPost] "
ACCOUNT_ADAPTER = "omnipost_api.auth_adapter.FrontendAccountAdapter"

# Days a sign-in token stays valid. 0 disables expiry.
AUTH_TOKEN_TTL_DAYS = int(os.environ.get("AUTH_TOKEN_TTL_DAYS", "30"))

# The web app's sign-in cookie (see authentication.CookieTokenAuthentication).
# Lax works when the app and API share a site (app.example.com and
# api.example.com). If they're on different sites, set None (needs HTTPS).
AUTH_COOKIE_NAME = "omnipost_auth"
AUTH_COOKIE_SAMESITE = os.environ.get("AUTH_COOKIE_SAMESITE") or "Lax"
AUTH_COOKIE_SECURE = not DEBUG or AUTH_COOKIE_SAMESITE == "None"
AUTH_COOKIE_DOMAIN = os.environ.get("AUTH_COOKIE_DOMAIN") or None

REST_AUTH = {
    "TOKEN_CREATOR": "omnipost_api.authentication.create_token",
    "PASSWORD_RESET_SERIALIZER": "omnipost_api.auth_serializers.FrontendPasswordResetSerializer",
    "USER_DETAILS_SERIALIZER": "omnipost_api.auth_serializers.UserDetailsSerializer",
    "OLD_PASSWORD_FIELD_ENABLED": True,
    # Sign-in is the token (header or cookie), not a Django session as well.
    "SESSION_LOGIN": False,
}

# Email. Without EMAIL_HOST, mail is printed to the console (dev / self-host
# without SMTP); set EMAIL_HOST etc. to actually deliver.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "OmniPost <no-reply@localhost>")


# --------------------------------------------------------------------------
# i18n
# --------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
# UTC everywhere. Per-workspace timezones belong on the workspace, not here.
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------
# Transport security
# --------------------------------------------------------------------------

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    # Healthchecks are plain HTTP from inside the container.
    SECURE_REDIRECT_EXEMPT = [r"^healthz$", r"^readyz$"]
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact_secrets": {"()": "app.logging_filters.RedactSecretsFilter"},
    },
    "formatters": {
        "console": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console",
            "filters": ["redact_secrets"],
        },
    },
    "root": {"handlers": ["console"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "handlers": ["console"], "propagate": False},
    },
}


# --------------------------------------------------------------------------
# Observability (Phase 7)
# --------------------------------------------------------------------------

# GIT_SHA is set by the CI build (see .github/workflows/docker-image.yml) so
# a Sentry issue can be traced back to the exact image that produced it, the
# same SHA the image is tagged with for rollback.
GIT_SHA = os.environ.get("GIT_SHA", "")

# Opt-in: a self-host with no DSN configured runs with Sentry fully disabled,
# same pattern as the AI/S3 keys above. Every process role (web, worker,
# reconciler, ...) imports settings, so this one init covers all of them.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN and not TESTING:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.rq import RqIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), RqIntegration()],
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production" if not DEBUG else "development"),
        release=GIT_SHA or None,
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
        # Never send request bodies/headers or stack-frame locals — they can
        # carry a channel's decrypted credentials if an exception fires
        # mid-publish.
        send_default_pii=False,
        include_local_variables=False,
    )

# Shared secret gating GET /metrics (Prometheus scrape target — see
# omnipost_api/metrics_exporter.py). Blank means the endpoint is open, same
# default as /healthz and /readyz: none of these expose secrets or PII, only
# aggregate counts, so an open endpoint is an acceptable self-host default —
# a cloud deployment fronted by a scraper should set this.
METRICS_AUTH_TOKEN = os.environ.get("METRICS_AUTH_TOKEN", "")

# --------------------------------------------------------------------------
# Hosted billing (services/billing.py). Leave STRIPE_SECRET_KEY blank on a
# self-hosted install: there are then no plans and no limits.
# --------------------------------------------------------------------------
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_CREATOR = os.environ.get("STRIPE_PRICE_CREATOR", "")
STRIPE_PRICE_PRO = os.environ.get("STRIPE_PRICE_PRO", "")
# Show plans and prices on the landing page. On without Stripe, it's a fake
# door: the buttons record interest (PricingInterest) instead of charging.
CLOUD_PRICING_ENABLED = env_bool("CLOUD_PRICING_ENABLED", bool(STRIPE_SECRET_KEY))
# Shown as-is; placeholders until prices are decided.
PRICE_DISPLAY_CREATOR = os.environ.get("PRICE_DISPLAY_CREATOR") or "$9/mo"
PRICE_DISPLAY_PRO = os.environ.get("PRICE_DISPLAY_PRO") or "$19/mo"
