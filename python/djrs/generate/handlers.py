"""`src/handlers.rs`: registers Rust handlers for views.

For now the handlers come from the project's `djrs.toml` (hand-written Rust,
"shims"); the translator will produce the same shape. A view is registered
only when its DRF configuration is something djrs-runtime reproduces exactly.

djrs.toml:

    [shims]
    # Rust files compiled into the generated crate, as top-level modules.
    notes_shims = "rust/notes_shims.rs"

    [drf_handlers."notes.views.count_notes"]
    GET = "notes_shims::count_notes"
"""

from __future__ import annotations

import os
import re

from .rust import raw_str

AUTH = {
    "rest_framework.authentication.TokenAuthentication": "Token",
    "rest_framework.authentication.SessionAuthentication": "Session",
    "rest_framework.authentication.BasicAuthentication": "Basic",
}
PERMS = {
    "rest_framework.permissions.AllowAny": "AllowAny",
    "rest_framework.permissions.IsAuthenticated": "IsAuthenticated",
    "rest_framework.permissions.IsAdminUser": "IsAdminUser",
    "rest_framework.permissions.IsAuthenticatedOrReadOnly": "IsAuthenticatedOrReadOnly",
}
RENDERERS = {
    "rest_framework.renderers.JSONRenderer": "Json",
    "rest_framework.renderers.BrowsableAPIRenderer": "BrowsableApi",
}
PARSERS = {
    "rest_framework.parsers.JSONParser": "Json",
    "rest_framework.parsers.FormParser": "Form",
    "rest_framework.parsers.MultiPartParser": "MultiPart",
}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def drf_view_problems(view):
    """Reasons the view's DRF setup can't be reproduced (empty = fine)."""
    if view.get("kind") not in ("drf_function", "drf_class"):
        return ["only DRF views can have Rust handlers so far"]
    drf = view.get("drf") or {}
    out = []
    for cls in drf.get("authentication_classes") or []:
        if cls not in AUTH:
            out.append("authentication class %s is not supported" % cls)
    for cls in drf.get("permission_classes") or []:
        if cls not in PERMS:
            out.append("permission class %s is not supported" % cls)
    for cls in drf.get("renderer_classes") or []:
        if cls not in RENDERERS:
            out.append("renderer %s is not supported" % cls)
    for cls in drf.get("parser_classes") or []:
        if cls not in PARSERS:
            out.append("parser %s is not supported" % cls)
    if drf.get("throttle_classes"):
        out.append("throttling is not supported yet")
    if drf.get("content_negotiation_class") != "rest_framework.negotiation.DefaultContentNegotiation":
        out.append("custom content negotiation")
    if drf.get("versioning_class"):
        out.append("API versioning is not supported yet")
    return out


def _const_name(i):
    return "VIEW_%d" % i


def build(data, config, config_dir, src_dir_final, blockers):
    """Returns (handlers_rs_body, shim_mod_decls, status) where status maps
    view id -> list of reasons it is NOT answered in Rust ([] = it is)."""
    config = config or {}
    shims = config.get("shims") or {}
    mapping = config.get("drf_handlers") or {}
    status = {}
    statics = []
    registrations = []
    for i, (view_id, methods) in enumerate(sorted(mapping.items())):
        view = data["views"].get(view_id)
        if view is None:
            status[view_id] = ["djrs.toml names a view that doesn't exist"]
            continue
        problems = list(blockers) + drf_view_problems(view)
        bad = sorted(m for m in methods if m not in METHODS)
        if bad:
            problems.append("djrs.toml: unsupported methods %s" % ", ".join(bad))
        if problems:
            status[view_id] = problems
            continue
        drf = view["drf"]
        name = _const_name(i)
        handler_list = ", ".join(
            "(%s, crate::%s)" % (raw_str(m), methods[m]) for m in sorted(methods)
        )
        statics.append(
            "static %s: DrfView = DrfView {\n" % name
            + "    id: %s,\n" % raw_str(view_id)
            + "    allowed_methods: &[%s],\n" % ", ".join(raw_str(m) for m in view.get("allowed_methods") or [])
            + "    authenticators: &[%s],\n" % ", ".join("Authenticator::" + AUTH[c] for c in drf["authentication_classes"])
            + "    permissions: &[%s],\n" % ", ".join("Permission::" + PERMS[c] for c in drf["permission_classes"])
            + "    renderers: &[%s],\n" % ", ".join("Renderer::" + RENDERERS[c] for c in drf["renderer_classes"])
            + "    parsers: &[%s],\n" % ", ".join("Parser::" + PARSERS[c] for c in drf["parser_classes"])
            + "};\n"
            + "static %s_HANDLERS: &[(&str, DrfHandler)] = &[%s];\n" % (name, handler_list)
        )
        registrations.append("        .drf_view(&%s, %s_HANDLERS)" % (name, name))
        status[view_id] = []

    body = (
        "#[allow(unused_imports)]\n"
        "use djrs_runtime::auth::Authenticator;\n"
        "#[allow(unused_imports)]\n"
        "use djrs_runtime::drf::{DrfHandler, DrfView, Parser, Permission, Renderer};\n"
        "use djrs_runtime::App;\n\n"
        + "\n".join(statics)
        + ("\n" if statics else "")
        + "/// Registers every Rust handler. Views without one are forwarded to Django.\n"
        + "pub fn register(app: App) -> App {\n"
        + "    app" + ("\n" + "\n".join(registrations) if registrations else "") + "\n"
        + "}\n"
    )
    decls = []
    for mod_name, rel in sorted(shims.items()):
        if not re.match(r"^[a-z_][a-z0-9_]*$", mod_name):
            raise ValueError("djrs.toml: shim module name %r must be a Rust identifier" % mod_name)
        target = os.path.normpath(os.path.join(config_dir, rel))
        path = os.path.relpath(target, src_dir_final).replace(os.sep, "/")
        decls.append("#[path = %s]\nmod %s;\n" % ('"%s"' % path, mod_name))
    return body, "".join(decls), status


def load_config(path):
    if not path:
        return None, None
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover
        try:
            import tomli as tomllib
        except ImportError:
            raise SystemExit("reading djrs.toml needs Python 3.11+ or the tomli package")
    with open(path, "rb") as f:
        return tomllib.load(f), os.path.dirname(os.path.abspath(path))
