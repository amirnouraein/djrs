"""What the translator knows about the project's models.

Everything here comes from the IR. A field the translator can't reason about
exactly gets `kind = None` and a reason; using it makes the view forwarded.
"""

from __future__ import annotations

from djrs.generate import entities


class Unsupported(Exception):
    """The view uses something the translator doesn't reproduce exactly."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


INT_TYPES = {
    "AutoField", "BigAutoField", "SmallAutoField", "IntegerField", "BigIntegerField", "SmallIntegerField",
    "PositiveIntegerField", "PositiveSmallIntegerField", "PositiveBigIntegerField",
}  # fmt: skip
AUTO_TYPES = {"AutoField", "BigAutoField", "SmallAutoField"}
# Field classes from outside Django whose Python value works exactly like a
# Django field's, on the given database vendors.
KNOWN_FIELDS = {
    # json.loads on read, json.dumps on write, value_to_string returns the value.
    "django_mysql.models.fields.json.JSONField": ("json", {"mysql"}),
}
STR_TYPES = {"CharField", "TextField", "SlugField", "EmailField", "URLField", "GenericIPAddressField"}
# FileField/ImageField hold FieldFile objects in Python, not str.


class Field:
    def __init__(self, raw, model, schema):
        self.raw = raw
        self.name = raw["name"]
        self.attname = raw.get("attname") or raw["name"]
        self.column = raw.get("column")
        self.internal = raw.get("internal_type")
        self.null = bool(raw.get("null")) or self.internal == "NullBooleanField"
        self.primary_key = bool(raw.get("primary_key"))
        self.max_length = raw.get("max_length")
        self.is_fk = bool(raw.get("is_relation") and (raw.get("many_to_one") or raw.get("one_to_one")))
        self.related_model = raw.get("related_model") if self.is_fk else None
        self.target_column = raw.get("target_column")
        self.on_delete = raw.get("on_delete")
        self.default = raw.get("default") or {"$none": True}
        self.auto_now = bool(raw.get("auto_now"))
        self.auto_now_add = bool(raw.get("auto_now_add"))
        cls = raw.get("class") or ""
        known = KNOWN_FIELDS.get(cls)
        self.custom = not cls.startswith("django.") and not (known and schema.vendor in known[1])
        self.ident = entities.field_ident(self.attname)
        self.kind, self.why = self._kind(model, schema)
        self.rust_base = None
        if self.kind is not None:
            t, reason = entities.rust_type(raw, schema.raw_models, schema.vendor)
            if t is None:
                self.kind, self.why = None, reason
            else:
                self.rust_base = t

    def _kind(self, model, schema):
        if self.custom:
            return None, "custom field class %s" % self.raw.get("class")
        if self.is_fk:
            target = schema.raw_models.get(self.related_model)
            if target is None:
                return None, "relation to a model djrs can't see"
            tf = next((f for f in target["fields"] if f.get("concrete") and f.get("column") == self.target_column), None)
            if tf is None:
                return None, "relation target column not found"
            if tf.get("internal_type") in INT_TYPES:
                return "int", None
            return None, "relation to a non-integer key"
        t = self.internal
        if t in INT_TYPES:
            return "int", None
        if t in STR_TYPES:
            return "str", None
        if t in ("BooleanField", "NullBooleanField"):
            return "bool", None
        if t == "FloatField":
            return "float", None
        if t == "JSONField":
            return "json", None
        if t == "DateTimeField":
            return "datetime", None
        if t == "DateField":
            return "date", None
        return None, "%s fields" % t

    @property
    def rust_type(self):
        return "Option<%s>" % self.rust_base if self.null else self.rust_base

    def need(self):
        if self.kind is None:
            raise Unsupported("field %s: %s" % (self.name, self.why))
        return self


class Model:
    def __init__(self, raw, schema):
        self.raw = raw
        self.label = raw["label"]
        self.table = raw["db_table"]
        self.module = entities.module_name(self.label)
        self.ordering = raw.get("ordering") or []
        self.fields = [
            Field(f, self, schema) for f in raw["fields"] if f.get("concrete") and not f.get("many_to_many")
        ]
        self.by_name = {}
        for f in self.fields:
            self.by_name.setdefault(f.name, f)
            self.by_name.setdefault(f.attname, f)
        pks = [f for f in self.fields if f.primary_key]
        self.pk = pks[0] if len(pks) == 1 else None
        report = schema.entity_report.get(self.label) or {}
        self.has_entity = bool(report.get("module"))
        self.complete = bool(report.get("complete"))
        self.reverse = [
            f for f in raw["fields"]
            if not f.get("concrete") and f.get("is_relation") and (f.get("one_to_many") or f.get("one_to_one"))
        ]  # fmt: skip
        self.generic = [f for f in raw["fields"] if "Generic" in (f.get("class") or "")]

    @property
    def entity(self):
        return "e::%s" % self.module

    def need_entity(self):
        if not self.has_entity:
            raise Unsupported("model %s has no Rust entity" % self.label)
        if self.pk is None or self.pk.kind is None:
            raise Unsupported("model %s: primary key type not supported" % self.label)
        return self

    def field(self, name):
        if name == "pk":
            if self.pk is None:
                raise Unsupported("model %s has no single primary key" % self.label)
            return self.pk
        f = self.by_name.get(name)
        if f is None:
            raise Unsupported("model %s: %s isn't a local field" % (self.label, name))
        return f

    def ordering_terms(self):
        """Meta.ordering as (column, descending) pairs."""
        out = []
        for item in self.ordering:
            if not isinstance(item, str) or "__" in item or item == "?":
                raise Unsupported("model %s: Meta.ordering %r isn't supported" % (self.label, item))
            desc = item.startswith("-")
            f = self.field(item.lstrip("-"))
            if f.is_fk and item.lstrip("-") == f.name:
                raise Unsupported("ordering by a relation orders by the related model's ordering")
            out.append((f.column, desc))
        return out


class Schema:
    def __init__(self, data):
        self.data = data
        self.vendor = ((data.get("databases") or {}).get("default") or {}).get("vendor")
        self.raw_models = {m["label"]: m for m in data.get("models", [])}
        _, self.entity_report = entities.build(data)
        self._models = {}
        self.signals = data.get("signals") or {}
        auth = data.get("auth") or {}
        self.user_model = (auth.get("user") or {}).get("model")

    def model(self, label):
        if label not in self._models:
            raw = self.raw_models.get(label)
            if raw is None:
                raise Unsupported("model %s isn't known" % label)
            self._models[label] = Model(raw, self)
        return self._models[label]

    def has_receivers(self, label, kinds):
        for kind in kinds:
            for r in self.signals.get(kind) or []:
                if r.get("sender") in (None, label):
                    return True
        return False

    def check_readable(self, m):
        """Loading rows runs __init__ and init signals; only plain models are read."""
        if m.raw.get("custom_init"):
            raise Unsupported("reading %s: the model overrides __init__" % m.label)
        if self.has_receivers(m.label, ("pre_init", "post_init")):
            raise Unsupported("reading %s: the model has init signal receivers" % m.label)

    def check_writable(self, m, what):
        """create()/save() reproduce Django's in-memory instance only for plain models."""
        raw = m.raw
        m.need_entity()
        if not m.complete:
            raise Unsupported("%s on %s: some columns have no Rust type" % (what, m.label))
        if raw.get("custom_save"):
            raise Unsupported("%s on %s: the model overrides save()" % (what, m.label))
        if raw.get("custom_init"):
            raise Unsupported("%s on %s: the model overrides __init__" % (what, m.label))
        if raw.get("parents"):
            raise Unsupported("%s on %s: multi-table inheritance" % (what, m.label))
        if raw.get("select_on_save"):
            raise Unsupported("%s on %s: Meta.select_on_save" % (what, m.label))
        if self.has_receivers(m.label, ("pre_save", "post_save", "pre_init", "post_init")):
            raise Unsupported("%s on %s: the model has save signal receivers" % (what, m.label))
        for f in m.fields:
            f.need()
