"""Run inside a Django project and describe it as JSON (the IR).

Usage: python -m djrs extract --settings mysite.settings --project-root . --out djrs.json
"""

from __future__ import annotations

import os
import sys

from djrs import IR_SCHEMA_VERSION, __version__


def setup_django(settings_module, pythonpath=None):
    if pythonpath:
        for p in reversed(pythonpath):
            sys.path.insert(0, os.path.abspath(p))
    if settings_module:
        os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
    import django

    django.setup()


class DatabaseAccessBlocked(Exception):
    pass


def _block_queries(execute, sql, params, many, context):
    raise DatabaseAccessBlocked("djrs extract does not run database queries (tried: %s)" % (sql[:80],))


def extract(project_root):
    import contextlib

    from django.conf import settings
    from django.db import connections

    from . import models, project, signals, urls
    from .describe import SourceLocator
    from .views import Extractor

    locator = SourceLocator(project_root)
    registry = Extractor(locator)
    # The IR must describe code, not data: block every query, so the output
    # can't depend on what a database holds (and a live database is never touched).
    with contextlib.ExitStack() as stack:
        for alias in connections:
            stack.enter_context(connections[alias].execute_wrapper(_block_queries))
        url_tree = urls.extract_url_tree(registry)
        views, functions, classes = registry.result()

    return {
        "djrs": {"version": __version__, "schema": IR_SCHEMA_VERSION},
        "project": {
            "settings_module": os.environ.get("DJANGO_SETTINGS_MODULE"),
            "root_urlconf": getattr(settings, "ROOT_URLCONF", None),
        },
        "versions": project.versions(),
        "settings": project.settings_snapshot(),
        "drf_settings": project.drf_settings(),
        "drf_messages": project.drf_messages(),
        "auth": project.auth_facts(),
        "databases": project.databases(),
        "middleware": project.middleware(),
        "response_headers": project.response_header_probe(),
        "url_semantics": project.url_semantics(),
        "error_handlers": project.error_handlers(),
        "models": models.extract_models(locator),
        "signals": signals.extract_signals(),
        "urls": url_tree,
        "views": views,
        "functions": functions,
        "classes": classes,
    }
