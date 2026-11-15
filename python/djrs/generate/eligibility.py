"""Which views may be answered in Rust at all, before any translation is tried.

Every rule here errs toward forwarding. A reason is a short sentence a person
can act on; it ends up in COVERAGE.md.
"""

from __future__ import annotations

# Django and third-party middleware djrs-runtime reproduces for Rust-answered
# requests. Any other middleware could change any response, so its presence
# keeps every view on Django until the project's config maps it.
KNOWN_MIDDLEWARE = {
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "corsheaders.middleware.CorsMiddleware",
}

SUPPORTED_VENDORS = {"sqlite", "postgresql", "mysql"}


def project_blockers(data):
    out = []
    for mw in data.get("middleware", []):
        if mw["path"] not in KNOWN_MIDDLEWARE:
            out.append("middleware %s is not supported" % mw["path"])
    settings = data.get("settings", {})
    if settings.get("PREPEND_WWW"):
        out.append("PREPEND_WWW is on")
    if settings.get("FORCE_SCRIPT_NAME"):
        out.append("FORCE_SCRIPT_NAME is set")
    if settings.get("SECURE_SSL_REDIRECT"):
        out.append("SECURE_SSL_REDIRECT is on")
    db = (data.get("databases") or {}).get("default") or {}
    if db.get("vendor") not in SUPPORTED_VENDORS:
        out.append("database vendor %s is not supported" % db.get("vendor"))
    if db.get("atomic_requests"):
        out.append("ATOMIC_REQUESTS is on")
    return out


def view_blockers(view, data):
    kind = view.get("kind")
    if kind == "opaque":
        return ["view can't be described: %s" % view.get("reason")]
    if kind == "django_class":
        return ["Django class-based views are not supported yet"]
    if kind == "drf_viewset":
        return ["DRF viewsets are not supported yet"]
    if kind == "drf_class":
        return ["DRF class-based views are not supported yet"]
    func = view.get("function") or {}
    if func.get("is_async"):
        return ["async views are not supported yet"]
    if func.get("error") or (func.get("refs") or {}).get("error"):
        return ["view source could not be read"]
    return []
