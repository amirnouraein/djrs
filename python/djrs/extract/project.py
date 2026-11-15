"""Versions, settings that affect request handling, middleware, and behaviour probes."""

from __future__ import annotations

import platform

from .describe import is_secret_name, to_json

# Settings copied into the IR. Anything not listed is left out, so a project's
# own settings (API keys, hostnames) never end up in generated files.
REQUEST_SETTINGS = [
    "ALLOWED_HOSTS",
    "APPEND_SLASH",
    "AUTH_USER_MODEL",
    "AUTHENTICATION_BACKENDS",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "CSRF_TRUSTED_ORIGINS",
    "CSRF_USE_SESSIONS",
    "DATA_UPLOAD_MAX_MEMORY_SIZE",
    "DATA_UPLOAD_MAX_NUMBER_FIELDS",
    "DEBUG",
    "DEFAULT_CHARSET",
    "DEFAULT_AUTO_FIELD",
    "FILE_UPLOAD_MAX_MEMORY_SIZE",
    "FORCE_SCRIPT_NAME",
    "INSTALLED_APPS",
    "LANGUAGE_CODE",
    "MIDDLEWARE",
    "PASSWORD_HASHERS",
    "PREPEND_WWW",
    "ROOT_URLCONF",
    "SECURE_BROWSER_XSS_FILTER",
    "SECURE_CONTENT_TYPE_NOSNIFF",
    "SECURE_CROSS_ORIGIN_OPENER_POLICY",
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    "SECURE_HSTS_PRELOAD",
    "SECURE_HSTS_SECONDS",
    "SECURE_PROXY_SSL_HEADER",
    "SECURE_REDIRECT_EXEMPT",
    "SECURE_REFERRER_POLICY",
    "SECURE_SSL_HOST",
    "SECURE_SSL_REDIRECT",
    "SESSION_COOKIE_AGE",
    "SESSION_COOKIE_NAME",
    "SESSION_ENGINE",
    "SESSION_EXPIRE_AT_BROWSER_CLOSE",
    "SESSION_SAVE_EVERY_REQUEST",
    "SESSION_SERIALIZER",
    "TIME_ZONE",
    "USE_I18N",
    "USE_TZ",
    "USE_X_FORWARDED_HOST",
    "USE_X_FORWARDED_PORT",
    "X_FRAME_OPTIONS",
    # Third-party settings that change how requests are answered.
    "REST_FRAMEWORK",
    "CORS_ALLOWED_ORIGINS",
    "CORS_ALLOWED_ORIGIN_REGEXES",
    "CORS_ALLOW_ALL_ORIGINS",
    "CORS_ORIGIN_ALLOW_ALL",
    "CORS_ORIGIN_WHITELIST",
    "CORS_ALLOW_CREDENTIALS",
    "CORS_ALLOW_HEADERS",
    "CORS_ALLOW_METHODS",
    "CORS_EXPOSE_HEADERS",
    "CORS_PREFLIGHT_MAX_AGE",
    "CORS_URLS_REGEX",
    "CELERY_TASK_DEFAULT_QUEUE",
    "CELERY_TASK_ROUTES",
    "CELERY_TASK_SERIALIZER",
]


def _scrub(value, key_path=""):
    """Drop values stored under secret-looking keys, at any depth."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and is_secret_name(k) and not isinstance(v, (bool, list, tuple)):
                out[k] = {"$redacted": True}
            else:
                out[k] = _scrub(v, key_path + "." + str(k))
        return out
    return value


def versions():
    import django

    out = {
        "python": platform.python_version(),
        "django": django.get_version(),
    }
    for name, attr in (("rest_framework", "VERSION"), ("celery", "__version__"), ("corsheaders", "__version__")):
        try:
            module = __import__(name)
            out[name] = str(getattr(module, attr, "unknown"))
        except Exception:
            pass
    return out


def settings_snapshot():
    from django.conf import settings

    out = {}
    for name in REQUEST_SETTINGS:
        if hasattr(settings, name):
            out[name] = to_json(_scrub(getattr(settings, name)))
    return out


def drf_settings():
    """DRF's effective settings (user values merged over DRF's own defaults)."""
    try:
        from rest_framework.settings import DEFAULTS, api_settings
    except Exception:
        return None
    out = {}
    for name in sorted(DEFAULTS):
        try:
            value = getattr(api_settings, name)
        except Exception as exc:
            out[name] = {"$error": type(exc).__name__}
            continue
        out[name] = to_json(_scrub({name: value})[name]) if not is_secret_name(name) else {"$redacted": True}
    return out


def databases():
    """Engine and vendor per alias. Never host, user, password or name."""
    from django.db import connections

    out = {}
    for alias in sorted(connections.databases):
        conf = connections.databases[alias]
        entry = {"engine": conf.get("ENGINE")}
        try:
            entry["vendor"] = connections[alias].vendor
        except Exception as exc:  # backend import errors on machines without drivers
            entry["vendor"] = None
            entry["error"] = type(exc).__name__
        options = conf.get("OPTIONS") or {}
        # Session settings matter for parity (sql_mode, isolation level, charset),
        # credentials do not belong here.
        entry["options"] = to_json(_scrub({k: v for k, v in options.items() if k not in ("passwd", "password", "read_default_file")}))
        entry["time_zone"] = conf.get("TIME_ZONE")
        entry["atomic_requests"] = bool(conf.get("ATOMIC_REQUESTS"))
        entry["test_mirror"] = (conf.get("TEST") or {}).get("MIRROR")
        out[alias] = entry
    return out


def middleware():
    """The MIDDLEWARE list in order, each resolved to where it lives."""
    from django.conf import settings
    from django.utils.module_loading import import_string

    out = []
    for path in getattr(settings, "MIDDLEWARE", None) or []:
        entry = {"path": path}
        try:
            obj = import_string(path)
            entry["module"] = getattr(obj, "__module__", None)
            entry["kind"] = "class" if isinstance(obj, type) else "function"
        except Exception as exc:
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        out.append(entry)
    return out


def url_semantics():
    """How this Django version matches URL patterns, found by probing Django
    itself rather than by version number."""
    from django.urls.resolvers import RegexPattern

    probes = {}

    # Django >= 4.x full-matches regex endpoints that end in '$';
    # older versions use search, so 'a$' also matches 'a\n'.
    p = RegexPattern(r"^a$", is_endpoint=True)
    probes["regex_endpoint_fullmatch_on_dollar"] = p.match("a\n") is None

    # Unmatched optional named groups are dropped from kwargs (3.0+).
    p = RegexPattern(r"^(?P<x>a)?b", is_endpoint=True)
    m = p.match("b")
    probes["drop_none_kwargs"] = bool(m) and "x" not in m[2]
    return probes


def error_handlers():
    """handler400/403/404/500 from the root URLconf, if customised."""
    from django.conf import settings

    out = {}
    try:
        module = __import__(settings.ROOT_URLCONF, fromlist=["*"])
    except Exception:
        return out
    for code in ("400", "403", "404", "500"):
        value = getattr(module, "handler" + code, None)
        if value is None:
            continue
        if isinstance(value, str):
            out[code] = value
        else:
            out[code] = to_json(value).get("$ref") if isinstance(to_json(value), dict) else None
    return out
