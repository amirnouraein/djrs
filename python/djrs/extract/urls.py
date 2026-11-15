"""The URL tree, in the order Django tries it.

The tree is kept as a tree (not flattened), because Django matches a
resolver's prefix with `search` and then hands the rest of the path to its
children, and an unanchored child pattern can match anywhere in that rest.
"""

from __future__ import annotations

from .describe import dotted_path, to_json


def describe_pattern(pattern):
    cls = dotted_path(type(pattern))
    out = {"class": cls, "is_endpoint": bool(getattr(pattern, "_is_endpoint", False))}
    name = type(pattern).__name__
    if name == "RoutePattern":
        out["kind"] = "route"
        out["route"] = str(pattern._route)
        out["regex"] = pattern.regex.pattern
        out["converters"] = {k: dotted_path(type(v)) for k, v in sorted(pattern.converters.items())}
    elif name in ("RegexPattern", "RegexURLPattern", "RegexURLResolver"):
        out["kind"] = "regex"
        out["regex"] = pattern.regex.pattern
    elif name == "LocalePrefixPattern":
        # Depends on the active language at request time.
        out["kind"] = "opaque"
        out["reason"] = "locale prefix"
    elif hasattr(pattern, "regex"):
        out["kind"] = "regex"
        out["regex"] = pattern.regex.pattern
        out["note"] = "unknown pattern class with a regex"
    else:
        out["kind"] = "opaque"
        out["reason"] = "unknown pattern class"
    return out


def extract_url_tree(registry):
    from django.urls import get_resolver
    from django.urls.resolvers import URLPattern, URLResolver

    def walk(patterns):
        nodes = []
        for p in patterns:
            if isinstance(p, URLResolver):
                try:
                    children = walk(p.url_patterns)
                    error = None
                except Exception as exc:  # broken include(); keep going
                    children = []
                    error = "%s: %s" % (type(exc).__name__, exc)
                node = {
                    "type": "resolver",
                    "pattern": describe_pattern(p.pattern),
                    "namespace": p.namespace,
                    "app_name": p.app_name,
                    "default_kwargs": to_json(p.default_kwargs),
                    "children": children,
                }
                if error:
                    node["error"] = error
                nodes.append(node)
            elif isinstance(p, URLPattern):
                nodes.append(
                    {
                        "type": "endpoint",
                        "pattern": describe_pattern(p.pattern),
                        "name": p.name,
                        "default_kwargs": to_json(p.default_args),
                        "view": registry.register(p.callback),
                    }
                )
            else:
                nodes.append({"type": "opaque", "class": dotted_path(type(p))})
        return nodes

    root = get_resolver()
    return {
        "type": "resolver",
        "pattern": describe_pattern(root.pattern),
        "namespace": None,
        "app_name": None,
        "default_kwargs": {},
        "children": walk(root.url_patterns),
    }
