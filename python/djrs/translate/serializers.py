"""`ModelSerializer(instance).data` as a Rust function.

Only output (`to_representation`) is translated, for serializers whose
fields are all plain model fields of kinds whose DRF representation is known
exactly. Anything else makes the view forwarded.
"""

from __future__ import annotations

import ast
import re
import textwrap

from djrs.generate.rust import raw_str

from .schema import Unsupported

SERIALIZER_MRO = [
    "rest_framework.serializers.ModelSerializer",
    "rest_framework.serializers.Serializer",
    "rest_framework.serializers.BaseSerializer",
    "rest_framework.fields.Field",
    "builtins.object",
]
# Methods that change what a serializer outputs.
OUTPUT_METHODS = {
    "__init__", "to_representation", "get_fields", "get_field_names", "build_field", "build_standard_field",
    "build_relational_field", "build_nested_field", "build_property_field", "build_url_field",
    "build_unknown_field", "data", "many_init", "get_attribute",
}  # fmt: skip
CHAR_FIELDS = {
    "rest_framework.fields.CharField", "rest_framework.fields.EmailField", "rest_framework.fields.SlugField",
    "rest_framework.fields.URLField", "rest_framework.fields.RegexField", "rest_framework.fields.IPAddressField",
}  # fmt: skip


def fn_name(path):
    return "ser_" + re.sub(r"[^A-Za-z0-9]", "_", path)


class Serializers:
    def __init__(self, data, schema):
        self.data = data
        self.schema = schema
        self.classes = data.get("classes") or {}
        drf = data.get("drf_settings") or {}
        self.datetime_format = drf.get("DATETIME_FORMAT")
        self.date_format = drf.get("DATE_FORMAT")
        self.use_tz = (data.get("settings") or {}).get("USE_TZ")
        self.generated = {}  # path -> (rust source, model label)
        self.failed = {}  # path -> reason

    def model_of(self, path):
        """The model a serializer class serializes (raises if unsupported)."""
        self.need(path)
        return self.generated[path][1]

    def need(self, path):
        if path in self.failed:
            raise Unsupported(self.failed[path])
        if path not in self.generated:
            try:
                self.generated[path] = self._build(path)
            except Unsupported as exc:
                self.failed[path] = exc.reason
                raise
        return fn_name(path)

    def source(self):
        return "\n".join(src for _, (src, _) in sorted(self.generated.items()))

    def _build(self, path):
        cls = self.classes.get(path)
        if cls is None:
            raise Unsupported("serializer %s wasn't extracted" % path)
        if cls.get("mro", [])[1:] != SERIALIZER_MRO:
            raise Unsupported("serializer %s: only direct ModelSerializer subclasses so far" % path)
        self._check_source(path, cls.get("source") or "")
        ser = cls.get("serializer") or {}
        if ser.get("error"):
            raise Unsupported("serializer %s couldn't be introspected" % path)
        meta = ser.get("meta") or {}
        label = meta.get("model")
        if not label:
            raise Unsupported("serializer %s has no Meta.model" % path)
        model = self.schema.model(label).need_entity()
        lines = []
        for f in ser.get("fields") or []:
            if f.get("error"):
                raise Unsupported("serializer %s: field %s couldn't be introspected" % (path, f["name"]))
            if f.get("write_only"):
                continue
            lines.append("    d.insert(%s.to_string(), %s);" % (raw_str(f["name"]), self._field(path, f, model)))
        src = (
            "/// `%s(instance).data`\n" % path.rsplit(".", 1)[-1]
            + "fn %s(m: &%s::Model) -> Result<Py, Stop> {\n" % (fn_name(path), model.entity)
            + "    let mut d = djrs_runtime::indexmap::IndexMap::new();\n"
            + "\n".join(lines)
            + ("\n" if lines else "")
            + "    Ok(Py::Dict(d))\n"
            + "}\n"
        )
        return src, label

    def _check_source(self, path, source):
        try:
            tree = ast.parse(textwrap.dedent(source))
        except SyntaxError:
            raise Unsupported("serializer %s: source doesn't parse here" % path)
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        if len(classes) != 1:
            raise Unsupported("serializer %s: source not found" % path)
        for node in classes[0].body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in OUTPUT_METHODS:
                raise Unsupported("serializer %s overrides %s" % (path, node.name))
            if isinstance(node, ast.ClassDef) and node.name == "Meta":
                for item in node.body:
                    targets = item.targets if isinstance(item, ast.Assign) else []
                    for t in targets:
                        if isinstance(t, ast.Name) and t.id == "list_serializer_class":
                            raise Unsupported("serializer %s sets list_serializer_class" % path)

    def _field(self, path, f, model):
        """Rust expression (type Py) for one output field, reading `m`."""
        cls = f.get("class")
        name = f["name"]
        source = f.get("source")
        where = "serializer %s field %s" % (path.rsplit(".", 1)[-1], name)
        if source != name or not source or "." in source or source == "*":
            raise Unsupported("%s: source=%r" % (where, source))
        mf = model.by_name.get(source)
        if mf is None:
            raise Unsupported("%s: not a model column (property or method)" % where)
        if mf.kind is None:
            raise Unsupported("%s: %s" % (where, mf.why))
        acc = "m.%s" % mf.ident

        def nullable(inner_of_x):
            """Apply `inner_of_x` (Rust expression using `x`) to the value; None stays None."""
            if mf.null:
                return "match &%s { Some(x) => %s, None => Py::None }" % (acc, inner_of_x)
            return "{ let x = &%s; %s }" % (acc, inner_of_x)

        if cls == "rest_framework.relations.PrimaryKeyRelatedField":
            if not mf.is_fk or f.get("pk_field") is not None:
                raise Unsupported("%s: PrimaryKeyRelatedField with pk_field" % where)
            return nullable(_int_py(mf))
        if mf.is_fk:
            raise Unsupported("%s: %s on a relation" % (where, cls))
        if cls in ("rest_framework.fields.IntegerField",):
            if mf.kind == "int":
                return nullable(_int_py(mf))
            if mf.kind == "bool":
                return nullable("Py::Int(*x as i64)")
            raise Unsupported("%s: IntegerField over a %s column" % (where, mf.kind))
        if cls in CHAR_FIELDS:
            if mf.kind == "str":
                return nullable("Py::Str(x.clone())")
            if mf.kind == "int":
                return nullable("Py::Str(x.to_string())")
            raise Unsupported("%s: CharField over a %s column" % (where, mf.kind))
        if cls in ("rest_framework.fields.BooleanField", "rest_framework.fields.NullBooleanField"):
            if mf.kind == "bool":
                return nullable("Py::Bool(*x)")
            raise Unsupported("%s: BooleanField over a %s column" % (where, mf.kind))
        if cls == "rest_framework.fields.FloatField":
            if mf.kind == "float":
                return nullable("Py::Float(*x)")
            raise Unsupported("%s: FloatField over a %s column" % (where, mf.kind))
        if cls == "rest_framework.fields.DateTimeField":
            if mf.kind != "datetime":
                raise Unsupported("%s: DateTimeField over a %s column" % (where, mf.kind))
            # The extractor records `format` only when the field sets one.
            fmt = f["format"] if "format" in f else self.datetime_format
            if fmt != "iso-8601":
                raise Unsupported("%s: DATETIME_FORMAT %r" % (where, fmt))
            if not self.use_tz:
                raise Unsupported("%s: USE_TZ = False" % where)
            return nullable("Py::Str(djrs_runtime::db::drf_datetime(&x.0))")
        if cls == "rest_framework.fields.DateField":
            if mf.kind != "date":
                raise Unsupported("%s: DateField over a %s column" % (where, mf.kind))
            fmt = f["format"] if "format" in f else self.date_format
            if fmt != "iso-8601":
                raise Unsupported("%s: DATE_FORMAT %r" % (where, fmt))
            return nullable('Py::Str(x.format("%Y-%m-%d").to_string())')
        if cls == "rest_framework.fields.JSONField":
            if mf.kind != "json" or f.get("binary"):
                raise Unsupported("%s: JSONField over a %s column" % (where, mf.kind))
            return nullable("x.0.clone()")
        if cls == "rest_framework.fields.ChoiceField":
            keys = _choice_keys(f.get("choices"))
            if keys is None:
                raise Unsupported("%s: grouped or unknown choices" % where)
            want = {"str": str, "int": int}.get(mf.kind)
            if want is None or not all(type(k) is want for k in keys):
                raise Unsupported("%s: choices whose keys differ in type from the column" % where)
            # to_representation maps str(value) back to the choice key: the same value here.
            if mf.kind == "str":
                return nullable("Py::Str(x.clone())")
            return nullable(_int_py(mf))
        if cls == "rest_framework.fields.ModelField":
            # to_representation returns the value for None/int/float/..., else
            # value_to_string(obj), which JSON fields define as the value itself.
            if mf.kind == "json":
                return nullable("x.0.clone()")
            raise Unsupported("%s: ModelField over a %s column" % (where, mf.kind))
        if cls == "rest_framework.fields.ReadOnlyField":
            conv = {
                "str": "Py::Str(x.clone())",
                "bool": "Py::Bool(*x)",
                "float": "Py::Float(*x)",
                "json": "x.0.clone()",
            }.get(mf.kind)
            if mf.kind == "int":
                conv = _int_py(mf)
            if conv is None:
                raise Unsupported("%s: ReadOnlyField over a %s column" % (where, mf.kind))
            return nullable(conv)
        raise Unsupported("%s: %s" % (where, cls))


def _int_py(mf):
    if mf.rust_base == "u64":
        return "djrs_runtime::orm::read_u64(*x)?"
    return "Py::from(*x)"


def _choice_keys(choices):
    if not isinstance(choices, list):
        return None
    keys = []
    for item in choices:
        if not isinstance(item, list) or len(item) != 2 or isinstance(item[1], (list, dict)):
            return None
        keys.append(item[0])
    return keys
