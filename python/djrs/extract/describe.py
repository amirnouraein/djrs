"""Small helpers that turn live Python objects into deterministic JSON values."""

from __future__ import annotations

import datetime
import decimal
import enum
import functools
import inspect
import os
import re
import uuid

# Setting and constant names that may hold credentials. Their values never go
# into the IR; only the fact that they exist.
SECRET_NAME_RE = re.compile(r"(SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|_KEY$|^KEY$|CREDENTIAL|DSN|PRIVATE)", re.I)


def dotted_path(obj) -> str:
    """'package.module.QualName' for a class, function or method."""
    if isinstance(obj, functools.partial):
        return "functools.partial(%s)" % dotted_path(obj.func)
    module = getattr(obj, "__module__", None)
    qualname = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None)
    if module is None or qualname is None:
        cls = type(obj)
        return "%s.%s" % (cls.__module__, cls.__qualname__)
    return "%s.%s" % (module, qualname)


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME_RE.search(name or ""))


def to_json(value, depth: int = 0):
    """A JSON-safe, deterministic rendering of a Python value.

    Classes and functions become {"$ref": dotted path}. Values that have no
    stable rendering become {"$type": dotted type name} so that memory
    addresses never leak into the IR.
    """
    if depth > 8:
        return {"$type": "<too deep>"}
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return {"$float": repr(value)}
        return value
    if isinstance(value, decimal.Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, uuid.UUID):
        return {"$uuid": str(value)}
    if isinstance(value, datetime.datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"$date": value.isoformat()}
    if isinstance(value, datetime.time):
        return {"$time": value.isoformat()}
    if isinstance(value, datetime.timedelta):
        return {"$timedelta_seconds": value.total_seconds()}
    if isinstance(value, enum.Enum):
        return {"$enum": dotted_path(type(value)), "value": to_json(value.value, depth + 1)}
    if isinstance(value, bytes):
        return {"$bytes_len": len(value)}
    if isinstance(value, (list, tuple)):
        return [to_json(v, depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        items = [to_json(v, depth + 1) for v in value]
        return sorted(items, key=lambda x: repr(x))
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = k if isinstance(k, str) else repr(to_json(k, depth + 1))
            out[key] = to_json(v, depth + 1)
        return out
    if inspect.isclass(value) or inspect.isfunction(value) or inspect.ismethod(value) or inspect.isbuiltin(value):
        return {"$ref": dotted_path(value)}
    if isinstance(value, functools.partial):
        return {"$ref": dotted_path(value)}
    if inspect.ismodule(value):
        return {"$module": value.__name__}
    if isinstance(value, re.Pattern if hasattr(re, "Pattern") else type(re.compile(""))):
        return {"$regex": value.pattern}
    # Lazy translation strings and similar proxies.
    try:
        from django.utils.functional import Promise

        if isinstance(value, Promise):
            return str(value)
    except Exception:  # pragma: no cover - Django always present when extracting
        pass
    return {"$type": dotted_path(type(value))}


class SourceLocator:
    """Turns source file paths into project-relative paths.

    Files outside the project root (Django, DRF, site-packages) are recorded as
    external, without a path, so the IR never depends on where Python is
    installed on the machine that ran the extractor.
    """

    def __init__(self, project_root: str):
        self.root = os.path.realpath(project_root)

    def relpath(self, path):
        if not path:
            return None
        real = os.path.realpath(path)
        if real == self.root or real.startswith(self.root + os.sep):
            rel = os.path.relpath(real, self.root)
            if "site-packages" in rel.split(os.sep) or "dist-packages" in rel.split(os.sep):
                return None
            return rel.replace(os.sep, "/")
        return None

    def locate(self, obj):
        """(relative file, first line) for a function or class, or (None, None)."""
        try:
            target = inspect.unwrap(obj)
        except Exception:
            target = obj
        try:
            path = inspect.getsourcefile(target)
        except (TypeError, OSError):
            return None, None
        rel = self.relpath(path)
        if rel is None:
            return None, None
        try:
            _, line = inspect.getsourcelines(target)
        except (OSError, TypeError):
            line = None
        return rel, line

    def is_project_object(self, obj) -> bool:
        return self.locate(obj)[0] is not None


def get_source(obj):
    try:
        return inspect.getsource(inspect.unwrap(obj))
    except (OSError, TypeError):
        return None
