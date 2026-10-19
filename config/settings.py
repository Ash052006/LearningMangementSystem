"""Django settings for the staff-training project.

Secrets and deployment-specific values come from environment variables via
django-environ.  ``.env.example`` documents the variables; ``.env`` holds the
real values and is never committed.
"""

from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# Load the local .env file when present.  A missing file simply means the
# environment already provides every variable (e.g. in CI or production).
env = environ.Env()
env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))

# SECURITY: there is deliberately no fallback key.  A deployment that forgets
# the variable must fail loudly at startup instead of signing sessions, cookies
# and CSRF tokens with a value published in this repository.  Generate one with
#     python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
_secret_key = env("SECRET_KEY", default="").strip()
if not _secret_key:
    raise ImproperlyConfigured(
        "SECRET_KEY is not set (or is empty).  Set it in the environment or in "
        "a local .env file -- see .env.example -- before running any command."
    )
SECRET_KEY = _secret_key

DEBUG = env.bool("DEBUG", default=False)

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party: the JSON API.
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    # Local apps.  accounts must come before any app with FKs to the user
    # model, and AUTH_USER_MODEL below must be set before the first migration.
    "accounts",
    "courses",
    "quizzes",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves the collected static files in production (and behind
    # gunicorn); it sits directly after SecurityMiddleware, as its docs advise.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Exposes LANGUAGE_CODE / LANGUAGES / LANGUAGE_DIR, so
                # templates can render the current language directly.
                "django.template.context_processors.i18n",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# The database is SQLite.  DATABASE_PATH lets a container point it at a mounted
# volume (see docker-compose.yml) without changing the default for local use.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env.str("DATABASE_PATH", default=str(BASE_DIR / "db.sqlite3")),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en"

# The two languages this site ships in.  The list drives the switcher in the
# header and tells gettext which catalogs to maintain.
LANGUAGES = [
    ("en", "English"),
    ("ml", "Malayalam"),
]

# Where the .po / .mo translation files live (one subfolder per language).
LOCALE_PATHS = [BASE_DIR / "locale"]

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# WhiteNoise compresses the files that `collectstatic` gathers into
# STATIC_ROOT and serves them itself, so no separate web server is needed.  The
# non-manifest storage is used deliberately: the manifest variant raises for any
# static file it cannot find in its manifest, which would make every test that
# renders a template fail unless `collectstatic` had run first.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Custom user model.
AUTH_USER_MODEL = "accounts.User"

# Auth redirects.  LOGIN_URL must exist as a URL name; after login users land
# on "home", which routes by role, so a single redirect setting stays correct
# for both instructors and learners.
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "accounts:login"

# Signup policy: OFF by default.  Instructor accounts grant course-authoring
# rights, so on an internal site they must be granted by an administrator in
# Django admin rather than chosen by the visitor.  Set the variable to True
# only for a throw-away demo environment where self-serve instructors are
# wanted; the signup form then offers the role, and it still cannot be smuggled
# in by a crafted POST while the switch is off (see accounts.forms.SignUpForm).
ALLOW_INSTRUCTOR_SELF_SIGNUP = env.bool("ALLOW_INSTRUCTOR_SELF_SIGNUP", default=False)

# ---------------------------------------------------------------------------
# JSON API (/api/v1/).
# ---------------------------------------------------------------------------
# Session auth serves the browser (the same cookie the web pages use); token
# auth serves scripts.  Every endpoint requires a logged-in user, matching the
# web catalogue, which is also login-only.  Writes go through the same service
# functions as the web views, so the API cannot bypass an access rule.
REST_FRAMEWORK = {
    # Token first so an unauthenticated request gets a 401 with a
    # ``WWW-Authenticate: Token`` header (DRF takes that header from the first
    # authenticator).  Session auth is still tried, so the browsable API works.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 10,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "30/minute",
        "user": "120/minute",
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# drf-spectacular powers the schema at /api/schema/ and the browsable docs at
# /api/docs/.
SPECTACULAR_SETTINGS = {
    "TITLE": "Staff training API",
    "DESCRIPTION": (
        "Read published courses, enrol, track progress and complete lessons. "
        "The same access rules as the web app apply: drafts are hidden (404), "
        "and learner actions require an enrolment (403)."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
