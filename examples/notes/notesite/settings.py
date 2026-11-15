"""Sample project used to test djrs. Works on Django 3.0 through 5.x."""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "notes-sample-not-secret")
DEBUG = False
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "rest_framework.authtoken",
    "notes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "notesite.urls"
WSGI_APPLICATION = "notesite.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": os.environ.get("NOTES_DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.environ.get("NOTES_DB_NAME", os.path.join(BASE_DIR, "db.sqlite3")),
        "USER": os.environ.get("NOTES_DB_USER", ""),
        "PASSWORD": os.environ.get("NOTES_DB_PASSWORD", ""),
        "HOST": os.environ.get("NOTES_DB_HOST", ""),
        "PORT": os.environ.get("NOTES_DB_PORT", ""),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}
