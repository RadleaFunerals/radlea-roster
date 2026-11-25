"""
Settings for the Radlea roster module (standalone mode).

Everything that differs between a laptop, the DigitalOcean test server and
production is read from environment variables, so the same code runs in all
three. See README.md for the list.
"""
import os
import sys
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# Debug is OFF unless DJANGO_DEBUG=1 is set (only ever on your own computer).
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
# The hosting build step gathers the stylesheets (collectstatic) before any
# secrets are available. That step never serves pages, so it may run without one.
BUILDING = len(sys.argv) > 1 and sys.argv[1] == "collectstatic"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG or TESTING or BUILDING:
        SECRET_KEY = "dev-only-insecure-key"
    else:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY (or DJANGO_DEBUG=1 on your own computer).")

# Address of the live site, e.g. https://radlea-roster-abcde.ondigitalocean.app
# On DigitalOcean App Platform set SITE_URL to ${APP_URL}. Its host is allowed
# automatically and it is used for links in roster emails.
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]
if SITE_URL:
    ALLOWED_HOSTS.append(SITE_URL.split("://", 1)[-1].split("/")[0])
    CSRF_TRUSTED_ORIGINS.append(SITE_URL)
# DigitalOcean's own app address always works, even if SITE_URL is missing.
ALLOWED_HOSTS.append(".ondigitalocean.app")
CSRF_TRUSTED_ORIGINS.append("https://*.ondigitalocean.app")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Radlea apps. "org" holds locations and staff and will merge into the
    # FMS foundations app; "roster" is the rostering module itself.
    "org",
    "roster",
]

MIDDLEWARE = [
    "config.health.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
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
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "roster.context_processors.nav",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite on a laptop; PostgreSQL on DigitalOcean via DATABASE_URL
# e.g. postgres://user:pass@host:25060/radlea?sslmode=require
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=600
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Australian English, Queensland time (no daylight saving).
LANGUAGE_CODE = "en-au"
TIME_ZONE = "Australia/Brisbane"
USE_I18N = True
USE_TZ = True
DATE_INPUT_FORMATS = ["%d/%m/%Y", "%Y-%m-%d"]
TIME_INPUT_FORMATS = ["%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"]

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
# Serve the app's CSS straight from the code, so a missed collectstatic can't break the look.
WHITENOISE_USE_FINDERS = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Roster emails. With no EMAIL_HOST set, emails are written to the log
# instead of being sent. For Google Workspace: EMAIL_HOST=smtp.gmail.com,
# EMAIL_HOST_USER=<the sending address>, EMAIL_HOST_PASSWORD=<an app password>.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = True
    EMAIL_TIMEOUT = 15
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL", f"Radlea Roster <{os.environ.get('EMAIL_HOST_USER', 'roster@radlea.com.au')}>"
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "roster:home"
LOGOUT_REDIRECT_URL = "login"

# Staff details are personal information: lock things down when not in DEBUG.
if not DEBUG and not TESTING:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SSL_REDIRECT", "1") == "1"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_AGE = 60 * 60 * 12  # 12 hours
