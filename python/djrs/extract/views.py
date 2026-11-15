"""Views, and everything their code refers to.

For each view the extractor records its kind, its HTTP methods, DRF settings
(authentication, permissions, renderers, parsers, throttles), its source, and
what every global name in that source refers to at runtime. The generator
never imports the project, so this is how it knows that `Response` is DRF's
Response or that `Note` is the model `notes.Note`.

Project code reached from a view (helpers, serializers) is recorded too, a few
levels deep, so the generator can translate or reject it.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import textwrap

from .describe import SourceLocator, dotted_path, get_source, is_secret_name, to_json

HTTP_METHODS = ["get", "post", "put", "patch", "delete", "head", "options", "trace"]
DRF_CLASS_ATTRS = [
    "authentication_classes",
    "permission_classes",
    "renderer_classes",
    "parser_classes",
    "throttle_classes",
]
DRF_SINGLE_ATTRS = [
    "content_negotiation_class",
    "metadata_class",
    "versioning_class",
    "pagination_class",
    "serializer_class",
]
MAX_DEPTH = 3


def _paths(items):
    try:
        return [dotted_path(x) for x in items or []]
    except TypeError:
        return [{"$type": dotted_path(type(items))}]


def _closure_vars(func):
    out = {}
    code = getattr(func, "__code__", None)
    cells = getattr(func, "__closure__", None)
    if code is None or not cells:
        return out
    for name, cell in zip(code.co_freevars, cells):
        try:
            out[name] = cell.cell_contents
        except ValueError:
            pass
    return out


def _unwrap_chain(func):
    """The function and every wrapper around it, outermost first."""
    chain = [func]
    seen = {id(func)}
    while hasattr(chain[-1], "__wrapped__"):
        nxt = chain[-1].__wrapped__
        if id(nxt) in seen:
            break
        seen.add(id(nxt))
        chain.append(nxt)
    return chain


def _wrapper_facts(chain):
    """What can be learned about decorators from their closures."""
    facts = []
    for layer in chain[:-1]:
        cv = _closure_vars(layer)
        fact = {"wrapper": getattr(layer, "__qualname__", None)}
        if "request_method_list" in cv:
            fact["require_http_methods"] = sorted(m.upper() for m in cv["request_method_list"])
        names = sorted(k for k in cv if not k.startswith("_"))
        fact["closure"] = names
        facts.append(fact)
    return facts


def _api_view_function(cls):
    """The function decorated by DRF's @api_view, from WrappedAPIView's handlers."""
    for method in HTTP_METHODS:
        handler = cls.__dict__.get(method)
        if handler is None:
            continue
        cv = _closure_vars(handler)
        func = cv.get("func")
        if func is not None:
            return func
    return None


def _model_exception(cls):
    """Model.DoesNotExist / Model.MultipleObjectsReturned, described by model."""
    from django.apps import apps

    name = cls.__name__
    if name not in ("DoesNotExist", "MultipleObjectsReturned"):
        return None
    for model in apps.get_models(include_auto_created=True):
        if model.__dict__.get(name) is cls:
            return {"kind": "model_exception", "label": model._meta.label, "exception": name, "path": dotted_path(cls)}
    return None


class LocalNames(ast.NodeVisitor):
    """Names bound inside a function body (approximate, conservative)."""

    def __init__(self):
        self.names = set()

    def _target(self, node):
        if isinstance(node, ast.Name):
            self.names.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                self._target(elt)
        elif isinstance(node, ast.Starred):
            self._target(node.value)

    def visit_FunctionDef(self, node):
        args = node.args
        for a in list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs):
            self.names.add(a.arg)
        if args.vararg:
            self.names.add(args.vararg.arg)
        if args.kwarg:
            self.names.add(args.kwarg.arg)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.visit_FunctionDef(node)

    def visit_Assign(self, node):
        for t in node.targets:
            self._target(t)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._target(node.target)
        self.generic_visit(node)

    def visit_For(self, node):
        self._target(node.target)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_comprehension(self, node):
        self._target(node.target)
        self.generic_visit(node)

    def visit_withitem(self, node):
        if node.optional_vars is not None:
            self._target(node.optional_vars)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            self.names.add((alias.asname or alias.name).split(".")[0])

    visit_ImportFrom = visit_Import

    def visit_NamedExpr(self, node):  # pragma: no cover - 3.8+
        self._target(node.target)
        self.generic_visit(node)


def _attribute_chain(node):
    """['a', 'b', 'c'] for `a.b.c`, or None if the root isn't a plain name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return None


def _free_references(source):
    """Global names and attribute chains used by `source`, sorted."""
    tree = ast.parse(textwrap.dedent(source))
    body = tree.body[0] if tree.body else tree
    locals_ = LocalNames()
    locals_.visit(body)
    # A class body's own methods see the class's attributes through self, not as globals.
    if isinstance(body, ast.ClassDef):
        locals_.names.discard(body.name)
    chains = set()
    names = set()
    for node in ast.walk(body):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            chain = _attribute_chain(node)
            if chain and chain[0] not in locals_.names:
                for i in range(2, len(chain) + 1):
                    chains.add(tuple(chain[:i]))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in locals_.names:
                names.add(node.id)
    for chain in list(chains):
        names.add(chain[0])
    return sorted(names), sorted(chains)


class Extractor:
    """Collects views, serializers and project functions, deduplicated and sorted."""

    def __init__(self, locator: SourceLocator):
        self.locator = locator
        self.views = {}  # id -> entry
        self._view_ids = {}  # id(callback) -> view id
        self.functions = {}  # dotted path -> entry (project functions)
        self.classes = {}  # dotted path -> entry (project classes)
        self._pending = []  # (obj, depth)

    # ---- references -------------------------------------------------------

    def describe_ref(self, obj, depth):
        """What a global name refers to, and queue project code for extraction."""
        from django.conf import settings as dj_settings
        from django.db import models

        if inspect.ismodule(obj):
            return {"kind": "module", "path": obj.__name__}
        if obj is dj_settings:
            return {"kind": "settings"}
        if inspect.isclass(obj):
            ref = {"kind": "class", "path": dotted_path(obj)}
            try:
                if issubclass(obj, models.Model) and hasattr(obj, "_meta"):
                    ref["kind"] = "model"
                    ref["label"] = obj._meta.label
                    return ref
            except TypeError:
                pass
            model_exc = _model_exception(obj)
            if model_exc is not None:
                return model_exc
            ref["mro"] = [dotted_path(k) for k in obj.__mro__[1:6]]
            ref["exception"] = issubclass(obj, BaseException)
            if self.locator.is_project_object(obj):
                ref["project"] = True
                self._queue(obj, depth + 1)
            return ref
        if inspect.isfunction(obj) or inspect.ismethod(obj):
            ref = {"kind": "function", "path": dotted_path(obj)}
            if self.locator.is_project_object(obj):
                ref["project"] = True
                self._queue(obj, depth + 1)
            return ref
        if inspect.isbuiltin(obj):
            if getattr(builtins, getattr(obj, "__name__", ""), None) is obj:
                return {"kind": "builtin", "name": obj.__name__}
            return {"kind": "function", "path": dotted_path(obj)}
        if isinstance(obj, (bool, int, float, str, type(None))):
            return {"kind": "value", "value": to_json(obj)}
        if isinstance(obj, (list, tuple, dict, set, frozenset)):
            return {"kind": "value", "value": to_json(obj)}
        # Celery tasks, managers, lazy objects, instances.
        ref = {"kind": "object", "type": dotted_path(type(obj))}
        task_name = getattr(obj, "name", None)
        if hasattr(obj, "delay") and hasattr(obj, "apply_async") and isinstance(task_name, str):
            ref["kind"] = "celery_task"
            ref["task_name"] = task_name
            run = getattr(obj, "run", None)
            if run is not None and self.locator.is_project_object(run):
                ref["function"] = dotted_path(run)
                self._queue(run, depth + 1)
        return ref

    def resolve_references(self, source, namespace, depth):
        if not source:
            return {"error": "no source"}
        try:
            names, chains = _free_references(source)
        except SyntaxError as exc:
            return {"error": "SyntaxError: %s" % exc}
        refs = {}
        for name in names:
            if name in namespace:
                refs[name] = self.describe_ref(namespace[name], depth)
            elif hasattr(builtins, name):
                refs[name] = {"kind": "builtin", "name": name}
            else:
                refs[name] = {"kind": "unresolved"}
        from django.conf import settings as dj_settings

        for chain in chains:
            root = namespace.get(chain[0])
            if root is None:
                continue
            obj = root
            ok = True
            for i, part in enumerate(chain[1:], start=1):
                if obj is dj_settings:
                    # settings.X: record the name; the value only if harmless.
                    setting = part
                    entry = {"kind": "setting", "name": setting}
                    if not is_secret_name(setting) and hasattr(dj_settings, setting):
                        value = getattr(dj_settings, setting)
                        if isinstance(value, (bool, int, float, str, type(None))):
                            entry["value"] = value
                    refs[".".join(chain[: i + 1])] = entry
                    ok = False
                    break
                if not (inspect.ismodule(obj) or inspect.isclass(obj)):
                    ok = False
                    break
                try:
                    obj = getattr(obj, part)
                except Exception:
                    ok = False
                    break
            if ok:
                refs[".".join(chain)] = self.describe_ref(obj, depth)
        return dict(sorted(refs.items()))

    # ---- queued project code ----------------------------------------------

    def _queue(self, obj, depth):
        if depth <= MAX_DEPTH:
            self._pending.append((obj, depth))

    def drain(self):
        while self._pending:
            obj, depth = self._pending.pop(0)
            path = dotted_path(obj)
            table = self.classes if inspect.isclass(obj) else self.functions
            if path in table:
                continue
            table[path] = None  # reserve before recursing
            try:
                if inspect.isclass(obj):
                    table[path] = self.describe_class(obj, depth)
                else:
                    table[path] = self.describe_function(obj, depth)
            except Exception as exc:
                table[path] = {"path": path, "error": "%s: %s" % (type(exc).__name__, exc)}

    def describe_function(self, func, depth):
        chain = _unwrap_chain(func)
        inner = chain[-1]
        file, line = self.locator.locate(inner)
        source = get_source(inner)
        # Names a decorator's inner function reads from its closure, not globals.
        namespace = dict(getattr(inner, "__globals__", {}))
        namespace.update(_closure_vars(inner))
        return {
            "path": dotted_path(func),
            "file": file,
            "line": line,
            "is_async": inspect.iscoroutinefunction(inner),
            "decorators": _wrapper_facts(chain),
            "source": source,
            "refs": self.resolve_references(source, namespace, depth),
        }

    def describe_class(self, cls, depth):
        import sys

        file, line = self.locator.locate(cls)
        source = get_source(cls)
        module = sys.modules.get(cls.__module__)
        namespace = dict(vars(module)) if module else {}
        entry = {
            "path": dotted_path(cls),
            "file": file,
            "line": line,
            "bases": [dotted_path(b) for b in cls.__bases__],
            "mro": [dotted_path(k) for k in cls.__mro__],
            "source": source,
            "refs": self.resolve_references(source, namespace, depth),
        }
        serializer = self.describe_serializer(cls)
        if serializer is not None:
            entry["serializer"] = serializer
        return entry

    # ---- DRF serializers ----------------------------------------------------

    def describe_serializer(self, cls):
        try:
            from rest_framework import serializers
        except Exception:
            return None
        if not (inspect.isclass(cls) and issubclass(cls, serializers.BaseSerializer)):
            return None
        out = {}
        meta = getattr(cls, "Meta", None)
        if meta is not None:
            model = getattr(meta, "model", None)
            out["meta"] = {
                "model": model._meta.label if model is not None and hasattr(model, "_meta") else None,
                "fields": to_json(getattr(meta, "fields", None)),
                "exclude": to_json(getattr(meta, "exclude", None)),
                "read_only_fields": to_json(getattr(meta, "read_only_fields", None)),
                "depth": to_json(getattr(meta, "depth", None)),
                "extra_kwargs": to_json(getattr(meta, "extra_kwargs", None)),
            }
        try:
            instance = cls()
            fields = instance.fields
        except Exception as exc:
            out["error"] = "%s: %s" % (type(exc).__name__, exc)
            return out
        out["fields"] = []
        for name, field in fields.items():
            try:
                out["fields"].append(self._serializer_field(name, field))
            except Exception as exc:
                out["fields"].append({"name": name, "error": "%s: %s" % (type(exc).__name__, exc)})
        return out

    def _serializer_field(self, name, field):
        from rest_framework import serializers

        entry = {
            "name": name,
            "class": dotted_path(type(field)),
            "source": field.source,
            "read_only": bool(field.read_only),
            "write_only": bool(field.write_only),
            "required": bool(field.required),
            "allow_null": bool(getattr(field, "allow_null", False)),
        }
        default = getattr(field, "default", serializers.empty)
        entry["default"] = {"$none": True} if default is serializers.empty else to_json(default)
        for attr in (
            "method_name",
            "format",
            "max_digits",
            "decimal_places",
            "coerce_to_string",
            "max_length",
            "min_length",
            "allow_blank",
            "trim_whitespace",
            "max_value",
            "min_value",
            "pk_field",
        ):
            if hasattr(field, attr):
                entry[attr] = to_json(getattr(field, attr))
        if isinstance(field, serializers.ListSerializer):
            entry["many"] = True
            entry["child"] = dotted_path(type(field.child))
            child_cls = type(field.child)
            if self.locator.is_project_object(child_cls):
                self._queue(child_cls, 1)
        elif isinstance(field, serializers.BaseSerializer) and self.locator.is_project_object(type(field)):
            self._queue(type(field), 1)
        if hasattr(field, "queryset") and getattr(field, "queryset", None) is not None:
            try:
                entry["queryset_model"] = field.queryset.model._meta.label
            except Exception:
                pass
        # RelatedField.choices runs the field's queryset, so never touch it.
        is_related = isinstance(field, (serializers.RelatedField, serializers.ManyRelatedField))
        choices = None if is_related else getattr(field, "choices", None)
        if choices:
            entry["choices"] = to_json(list(choices.items()) if isinstance(choices, dict) else list(choices))
        return entry

    # ---- views ------------------------------------------------------------

    def register(self, callback):
        key = id(callback)
        if key in self._view_ids:
            return self._view_ids[key]
        try:
            entry = self.describe_view(callback)
        except Exception as exc:
            # One view we can't describe must not hide the rest of the URL tree;
            # the generator forwards it.
            entry = {
                "kind": "opaque",
                "reason": "extractor error: %s: %s" % (type(exc).__name__, exc),
                "lookup": dotted_path(callback),
                "csrf_exempt": bool(getattr(callback, "csrf_exempt", False)),
            }
        base = entry["lookup"]
        view_id = base
        n = 2
        while view_id in self.views:
            view_id = "%s#%d" % (base, n)
            n += 1
        entry["id"] = view_id
        self.views[view_id] = entry
        self._view_ids[key] = view_id
        self.drain()
        return view_id

    def describe_view(self, callback):
        import functools

        out = {"csrf_exempt": bool(getattr(callback, "csrf_exempt", False))}
        if isinstance(callback, functools.partial):
            out["kind"] = "opaque"
            out["reason"] = "functools.partial view"
            out["lookup"] = dotted_path(callback)
            return out

        drf_cls = getattr(callback, "cls", None)
        view_class = getattr(callback, "view_class", None)
        try:
            from rest_framework.views import APIView
        except Exception:
            APIView = None

        if drf_cls is not None and APIView is not None and inspect.isclass(drf_cls) and issubclass(drf_cls, APIView):
            out["drf"] = self._drf_settings(drf_cls)
            out["initkwargs"] = to_json(getattr(callback, "initkwargs", None))
            # @api_view builds a class named WrappedAPIView, then renames it
            # (__name__, __module__) after the decorated function; the
            # qualified name keeps the original.
            is_wrapped = "WrappedAPIView" in getattr(drf_cls, "__qualname__", "")
            func = _api_view_function(drf_cls) if is_wrapped else None
            if func is not None:
                out["kind"] = "drf_function"
                out["lookup"] = dotted_path(func)
                out["methods"] = sorted(m.upper() for m in HTTP_METHODS if m in drf_cls.__dict__)
                out["function"] = self.describe_function(func, 0)
            else:
                actions = getattr(callback, "actions", None)
                out["kind"] = "drf_viewset" if actions else "drf_class"
                out["lookup"] = dotted_path(drf_cls)
                if actions:
                    out["actions"] = {k.upper(): v for k, v in sorted(actions.items())}
                    out["methods"] = sorted(k.upper() for k in actions)
                else:
                    out["methods"] = sorted(m.upper() for m in drf_cls.http_method_names if hasattr(drf_cls, m))
                out["class"] = self.describe_class(drf_cls, 0)
                out["view_attrs"] = self._view_attrs(drf_cls)
            return out

        if view_class is not None and inspect.isclass(view_class):
            out["kind"] = "django_class"
            out["lookup"] = dotted_path(view_class)
            out["methods"] = sorted(m.upper() for m in view_class.http_method_names if hasattr(view_class, m))
            out["initkwargs"] = to_json(getattr(callback, "view_initkwargs", None))
            out["class"] = self.describe_class(view_class, 0)
            return out

        if inspect.isfunction(callback) or inspect.ismethod(callback):
            chain = _unwrap_chain(callback)
            out["kind"] = "django_function"
            out["lookup"] = dotted_path(chain[-1])
            out["function"] = self.describe_function(callback, 0)
            methods = None
            for fact in out["function"]["decorators"]:
                if "require_http_methods" in fact:
                    methods = fact["require_http_methods"]
            out["methods"] = methods  # None = any method
            return out

        out["kind"] = "opaque"
        out["reason"] = "callable object of type %s" % dotted_path(type(callback))
        out["lookup"] = dotted_path(type(callback))
        return out

    def _drf_settings(self, cls):
        out = {}
        for attr in DRF_CLASS_ATTRS:
            out[attr] = _paths(getattr(cls, attr, None))
        for attr in DRF_SINGLE_ATTRS:
            value = getattr(cls, attr, None)
            out[attr] = dotted_path(value) if value is not None else None
        return out

    def _view_attrs(self, cls):
        out = {}
        for attr in ("lookup_field", "lookup_url_kwarg", "filter_backends", "ordering", "search_fields"):
            if hasattr(cls, attr):
                out[attr] = to_json(getattr(cls, attr))
        queryset = getattr(cls, "queryset", None)
        if queryset is not None and hasattr(queryset, "model"):
            out["queryset_model"] = queryset.model._meta.label
        return out

    def result(self):
        self.drain()
        return (
            dict(sorted(self.views.items())),
            dict(sorted((k, v) for k, v in self.functions.items() if v is not None)),
            dict(sorted((k, v) for k, v in self.classes.items() if v is not None)),
        )
