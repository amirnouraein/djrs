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


def auth_facts():
    """How this Django version signs sessions and hashes passwords, found by
    running Django's own code on throwaway values (no database, no real secret)."""
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.utils.crypto import salted_hmac

    out = {}
    User = get_user_model()
    meta = User._meta

    def column(name):
        try:
            return meta.get_field(name).column
        except Exception:
            return None

    out["user"] = {
        "model": meta.label,
        "table": meta.db_table,
        "pk_column": meta.pk.column,
        "pk_type": meta.pk.get_internal_type(),
        "password_column": column("password"),
        "is_active_column": column("is_active"),
        "is_staff_column": column("is_staff"),
        "is_superuser_column": column("is_superuser"),
        "username_field": getattr(User, "USERNAME_FIELD", None),
        "username_column": column(getattr(User, "USERNAME_FIELD", "username")),
        "is_active_attr": type(getattr(User, "is_active", None)).__name__,
    }

    try:
        from django.contrib.auth.hashers import get_hasher

        hasher = get_hasher("default")
        out["default_hasher"] = {"algorithm": hasher.algorithm, "iterations": getattr(hasher, "iterations", None)}
    except Exception as exc:
        out["default_hasher"] = {"error": type(exc).__name__}

    # Which HMAC get_session_auth_hash uses (sha1 up to 3.0, sha256 after).
    key_salt = "django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash"
    probe = User()
    probe.password = "probe$password$hash"
    try:
        expected = probe.get_session_auth_hash()
        out["session_auth_hash"] = None
        for alg in ("sha256", "sha1"):
            try:
                got = salted_hmac(key_salt, probe.password, algorithm=alg).hexdigest()
            except TypeError:  # Django 3.0: no algorithm argument, always sha1
                got = salted_hmac(key_salt, probe.password).hexdigest() if alg == "sha1" else None
            if got == expected:
                out["session_auth_hash"] = alg
                break
    except Exception as exc:
        out["session_auth_hash"] = {"error": type(exc).__name__}

    # Session storage format: Django 3.1+ uses django.core.signing, older uses
    # a base64 "hash:json" blob.
    try:
        from importlib import import_module

        from django.core import signing

        engine = import_module(settings.SESSION_ENGINE)
        store = engine.SessionStore()
        encoded = store.encode({"probe": 1})
        session = {"engine": settings.SESSION_ENGINE, "serializer": settings.SESSION_SERIALIZER}
        try:
            signing.loads(encoded, salt=store.key_salt, serializer=store.serializer)
            session["format"] = "signed"
            session["key_salt"] = store.key_salt
            signer = signing.Signer(salt="probe")
            session["signer_algorithm"] = getattr(signer, "algorithm", None) or "sha1"
        except Exception:
            session["format"] = "legacy"
            session["key_salt"] = "django.contrib.sessions" + type(store).__name__
        out["session"] = session
    except Exception as exc:
        out["session"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    return out


def drf_messages():
    """Default error messages of DRF's exceptions, as this DRF version says them."""
    try:
        from rest_framework import exceptions
    except Exception:
        return None
    out = {}
    for name in (
        "NotAuthenticated",
        "AuthenticationFailed",
        "PermissionDenied",
        "NotFound",
        "MethodNotAllowed",
        "ParseError",
        "UnsupportedMediaType",
        "NotAcceptable",
    ):
        cls = getattr(exceptions, name, None)
        if cls is not None:
            out[name] = {"detail": str(cls.default_detail), "code": cls.default_code, "status": cls.status_code}
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
        entry["connection"] = _connection_facts(connections[alias])
        entry["atomic_requests"] = bool(conf.get("ATOMIC_REQUESTS"))
        entry["test_mirror"] = (conf.get("TEST") or {}).get("MIRROR")
        out[alias] = entry
    return out


def _connection_facts(conn):
    """Session settings Django applies when it connects (no connection is opened).

    The Rust side must use the same ones, or the same SQL can behave
    differently (character set, matched vs. changed row counts, isolation).
    """
    out = {}
    try:
        params = conn.get_connection_params()
    except Exception as exc:
        return {"error": type(exc).__name__}
    if conn.vendor == "mysql":
        out["charset"] = params.get("charset")
        out["found_rows"] = bool((params.get("client_flag") or 0) & 2)  # MySQLdb CLIENT.FOUND_ROWS
        out["init_command"] = params.get("init_command")
        out["isolation_level"] = getattr(conn, "isolation_level", None)
        out["sql_auto_is_null_off"] = True
    elif conn.vendor == "postgresql":
        out["timezone_name"] = getattr(conn, "timezone_name", None)
        options = conn.settings_dict.get("OPTIONS") or {}
        out["isolation_level"] = to_json(options.get("isolation_level"))
    elif conn.vendor == "sqlite":
        out["foreign_keys"] = True
    return out


# Middleware the probe may run: Django's own and django-cors-headers. Others
# could have side effects (caches, external calls), so they're never run.
PROBE_SAFE_MIDDLEWARE_PREFIXES = ("django.", "corsheaders.")


def response_header_probe():
    """Run the project's middleware chain around a view that returns a small
    JSON response, and record what headers come out, in order.

    Done for plain HTTP, HTTPS, and a request whose session is read (which
    adds `Vary: Cookie`). Django versions differ here (X-XSS-Protection went
    away in 4.0, Cross-Origin-Opener-Policy arrived in 4.0); running Django's
    own code records what this version does, with this project's settings.
    """
    from django.conf import settings
    from django.http import HttpResponse
    from django.test import RequestFactory
    from django.utils.module_loading import import_string

    paths = list(getattr(settings, "MIDDLEWARE", None) or [])
    skipped = [p for p in paths if not p.startswith(PROBE_SAFE_MIDDLEWARE_PREFIXES)]
    host = "testserver"
    for h in settings.ALLOWED_HOSTS or []:
        if h == "*":
            break
        host = "probe" + h if h.startswith(".") else h
        break

    def run(secure, touch_session):
        def view(request):
            if touch_session and hasattr(request, "session"):
                request.session.get("probe")
            return HttpResponse(b'{"probe":1}', content_type="application/json")

        handler = view
        for path in reversed(paths):
            if path in skipped:
                continue
            try:
                handler = import_string(path)(handler)
            except Exception as exc:  # MiddlewareNotUsed and friends
                if type(exc).__name__ != "MiddlewareNotUsed":
                    raise
        factory = RequestFactory()
        extra = {"HTTP_HOST": host}
        if secure:
            extra["wsgi.url_scheme"] = "https"
        request = factory.get("/__djrs_probe__/", **extra)
        response = handler(request)
        return [[k, v] for k, v in response.items()]

    out = {"skipped_middleware": skipped, "host": host}
    try:
        out["http"] = run(False, False)
        out["https"] = run(True, False)
        out["http_session_read"] = run(False, True)
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
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
