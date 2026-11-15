"""Models and their fields, read from Django's model metadata."""

from __future__ import annotations

from .describe import dotted_path, to_json

FIELD_ATTRS = [
    "null",
    "blank",
    "unique",
    "primary_key",
    "db_index",
    "editable",
    "max_length",
    "max_digits",
    "decimal_places",
    "auto_now",
    "auto_now_add",
    "db_constraint",
]


def _default(field):
    from django.db.models.fields import NOT_PROVIDED

    default = getattr(field, "default", NOT_PROVIDED)
    if default is NOT_PROVIDED:
        return {"$none": True}
    if callable(default):
        return {"$callable": dotted_path(default)}
    return {"value": to_json(default)}


def _defining_class(model, attr):
    """Where in the MRO `attr` is defined, if not on django.db.models.Model."""
    for klass in model.__mro__:
        if attr in klass.__dict__:
            path = dotted_path(klass)
            if path == "django.db.models.base.Model":
                return None
            return path
    return None


def _field(model, field):
    out = {
        "name": field.name,
        "class": dotted_path(type(field)),
        "concrete": bool(getattr(field, "concrete", False)),
        "auto_created": bool(getattr(field, "auto_created", False)),
        "is_relation": bool(field.is_relation),
    }
    if out["concrete"]:
        out["attname"] = field.attname
        out["column"] = field.column
        try:
            out["internal_type"] = field.get_internal_type()
        except Exception:
            out["internal_type"] = None
        for attr in FIELD_ATTRS:
            if hasattr(field, attr):
                out[attr] = to_json(getattr(field, attr))
        out["default"] = _default(field)
        choices = getattr(field, "choices", None)
        if choices:
            out["choices"] = to_json(list(choices))
        try:
            from django.db import connection

            out["db_type"] = field.db_type(connection)
        except Exception:
            out["db_type"] = None
    if field.is_relation:
        related = field.related_model
        out["related_model"] = related._meta.label if related is not None and hasattr(related, "_meta") else None
        out["many_to_many"] = bool(field.many_to_many)
        out["one_to_many"] = bool(field.one_to_many)
        out["many_to_one"] = bool(field.many_to_one)
        out["one_to_one"] = bool(field.one_to_one)
        remote = getattr(field, "remote_field", None)
        if remote is not None and getattr(remote, "on_delete", None) is not None:
            out["on_delete"] = getattr(remote.on_delete, "__name__", None)
        if out["concrete"] and field.many_to_one or (out["concrete"] and field.one_to_one):
            try:
                out["target_column"] = field.target_field.column
            except Exception:
                pass
        if field.many_to_many and out["concrete"]:
            through = remote.through
            out["through"] = through._meta.label
            out["through_auto_created"] = bool(through._meta.auto_created)
            out["m2m_table"] = field.m2m_db_table()
            out["m2m_column"] = field.m2m_column_name()
            out["m2m_reverse_column"] = field.m2m_reverse_name()
        if not out["concrete"]:
            # Reverse side of a relation: what it's called and which field points here.
            out["accessor_name"] = field.get_accessor_name() if hasattr(field, "get_accessor_name") else None
            remote_field = getattr(field, "field", None)
            out["remote_field_name"] = remote_field.name if remote_field is not None else None
    return out


def extract_models(locator):
    from django.apps import apps

    out = []
    for model in apps.get_models(include_auto_created=True):
        meta = model._meta
        file, line = locator.locate(model)
        entry = {
            "label": meta.label,
            "app_label": meta.app_label,
            "object_name": meta.object_name,
            "module": model.__module__,
            "file": file,
            "line": line,
            "db_table": meta.db_table,
            "managed": bool(meta.managed),
            "proxy": bool(meta.proxy),
            "auto_created": bool(meta.auto_created),
            "concrete_model": meta.concrete_model._meta.label,
            "pk": meta.pk.name if meta.pk is not None else None,
            "ordering": to_json(list(meta.ordering or [])),
            "unique_together": to_json([list(x) for x in (meta.unique_together or [])]),
            "get_latest_by": to_json(meta.get_latest_by),
            "default_manager": dotted_path(type(model._default_manager)),
            "custom_save": _defining_class(model, "save"),
            "custom_delete": _defining_class(model, "delete"),
            "custom_refresh": _defining_class(model, "refresh_from_db"),
            "parents": sorted(p._meta.label for p in meta.parents),
            "fields": [_field(model, f) for f in meta.get_fields(include_hidden=True)],
        }
        out.append(entry)
    out.sort(key=lambda m: m["label"])
    return out
