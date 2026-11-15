"""`ModelSerializer(instance).data` as a Rust function.

Only output (`to_representation`) is translated, for serializers whose
fields are model columns with a known DRF representation, nested
serializers over relations, and `SerializerMethodField`s whose method the
translator can translate. Anything else makes the view forwarded.

The generated functions are `async fn ser_X(ctx, &Model) -> Result<Py, Stop>`:
nested serializers and method fields may run queries.
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
        self.tr = None  # the Translator, set by it
        self.classes = data.get("classes") or {}
        drf = data.get("drf_settings") or {}
        self.datetime_format = drf.get("DATETIME_FORMAT")
        self.date_format = drf.get("DATE_FORMAT")
        self.use_tz = (data.get("settings") or {}).get("USE_TZ")
        self.generated = {}  # path -> (rust source, model label)
        self.failed = {}  # path -> reason
        # path -> True when the output holds values Python keeps as UUID
        # objects (DRF renders them as strings, but they compare differently).
        self.tainted = {}
        self.deps = {}  # path -> helpers its source uses: {("ser", path) | ("del", label)}
        self._building = []
        self._taint = [False]

    def model_of(self, path):
        """The model a serializer class serializes (raises if unsupported)."""
        self.need(path)
        return self.generated[path][1]

    def need(self, path):
        if path in self.failed:
            raise Unsupported(self.failed[path])
        if path in self._building:
            raise Unsupported("serializers nested in a cycle")
        if path not in self.generated:
            saved_using = self.tr.using
            self.tr.using = set()
            self._building.append(path)
            self._taint.append(False)
            taint = False
            try:
                self.generated[path] = self._build(path)
                self.tainted[path] = self._taint[-1]
                self.deps[path] = self.tr.using
            except Unsupported as exc:
                self.failed[path] = exc.reason
                raise
            finally:
                self._building.pop()
                taint = self._taint.pop()
                self.tr.using = saved_using
            if taint:
                self._taint[-1] = True
        elif self.tainted.get(path):
            self._taint[-1] = True
        return fn_name(path)

    def _build(self, path):
        cls = self.classes.get(path)
        if cls is None:
            raise Unsupported("serializer %s wasn't extracted" % path)
        if cls.get("mro", [])[1:] != SERIALIZER_MRO:
            raise Unsupported("serializer %s: only direct ModelSerializer subclasses so far" % path)
        tree = self._check_source(path, cls.get("source") or "")
        ser = cls.get("serializer") or {}
        if ser.get("error"):
            raise Unsupported("serializer %s couldn't be introspected" % path)
        meta = ser.get("meta") or {}
        label = meta.get("model")
        if not label:
            raise Unsupported("serializer %s has no Meta.model" % path)
        model = self.schema.model(label).need_entity()
        self.schema.check_readable(model)
        lines = []
        extra = []
        for f in ser.get("fields") or []:
            if f.get("error"):
                raise Unsupported("serializer %s: field %s couldn't be introspected" % (path, f["name"]))
            if f.get("write_only"):
                continue
            expr = self._field(path, cls, tree, f, model, extra)
            lines.append("    let v = %s;\n    d.insert(%s.to_string(), v);" % (expr, raw_str(f["name"])))
        src = (
            "/// `%s(instance).data`\n" % path.rsplit(".", 1)[-1]
            + "#[allow(unused_variables)]\n"
            + "async fn %s(ctx: &mut Ctx, m: &%s::Model) -> Result<Py, Stop> {\n" % (fn_name(path), model.entity)
            + "    let mut d = djrs_runtime::indexmap::IndexMap::new();\n"
            + "\n".join(lines)
            + ("\n" if lines else "")
            + "    Ok(Py::Dict(d))\n"
            + "}\n"
        )
        return "\n".join(extra + [src]), label

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
        return classes[0]

    # ---- fields ---------------------------------------------------------------------

    def _field(self, path, cls_ir, tree, f, model, extra):
        """Rust expression (type Py) for one output field, reading `m`."""
        cls = f.get("class")
        name = f["name"]
        source = f.get("source")
        where = "serializer %s field %s" % (path.rsplit(".", 1)[-1], name)
        if cls == "rest_framework.fields.SerializerMethodField":
            return self._method_field(path, cls_ir, tree, f, model, extra, where)
        if f.get("many") or cls == "rest_framework.serializers.ListSerializer":
            return self._nested_many(f, model, where)
        if not source or "." in source or source == "*":
            raise Unsupported("%s: source=%r" % (where, source))
        if cls in self.classes and "rest_framework.serializers.BaseSerializer" in (self.classes[cls].get("mro") or []):
            return self._nested_one(cls, source, model, where)
        mf = model.by_name.get(source)
        if mf is None:
            raise Unsupported("%s: not a model column (property or method)" % where)
        if mf.kind is None:
            raise Unsupported("%s: %s" % (where, mf.why))
        acc = "m.%s" % mf.ident
        # The FK's column itself (`user_id`), read as the plain key value.
        by_attname = mf.is_fk and source == mf.attname and mf.attname != mf.name

        def nullable(inner_of_x):
            """Apply `inner_of_x` (Rust expression using `x`) to the value; None stays None."""
            if mf.null:
                return "match &%s { Some(x) => %s, None => Py::None }" % (acc, inner_of_x)
            return "{ let x = &%s; %s }" % (acc, inner_of_x)

        if cls == "rest_framework.relations.PrimaryKeyRelatedField":
            if not mf.is_fk or f.get("pk_field") is not None:
                raise Unsupported("%s: PrimaryKeyRelatedField with pk_field" % where)
            if mf.kind == "uuid":
                self._taint[-1] = True  # value.pk is a UUID object
                return nullable("Py::Str(x.hyphenated())")
            return nullable(_int_py(mf))
        if mf.is_fk and not by_attname:
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
            if mf.kind == "uuid":
                return nullable("Py::Str(x.hyphenated())")
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
        if cls == "rest_framework.fields.UUIDField":
            if mf.kind != "uuid" or f.get("uuid_format", "hex_verbose") != "hex_verbose":
                raise Unsupported("%s: UUIDField format" % where)
            return nullable("Py::Str(x.hyphenated())")
        if cls == "rest_framework.fields.ModelField":
            # to_representation returns the value for None/int/float/..., else
            # value_to_string(obj), which JSON fields define as the value itself.
            if mf.kind == "json":
                return nullable("x.0.clone()")
            raise Unsupported("%s: ModelField over a %s column" % (where, mf.kind))
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
        if cls == "rest_framework.fields.ReadOnlyField":
            conv = {
                "str": "Py::Str(x.clone())",
                "bool": "Py::Bool(*x)",
                "float": "Py::Float(*x)",
                "json": "x.0.clone()",
            }.get(mf.kind)
            if mf.kind == "int":
                conv = _int_py(mf)
            if mf.kind == "uuid":
                self._taint[-1] = True  # the value is a UUID object
                conv = "Py::Str(x.hyphenated())"
            if conv is None:
                raise Unsupported("%s: ReadOnlyField over a %s column" % (where, mf.kind))
            return nullable(conv)
        raise Unsupported("%s: %s" % (where, cls))

    def _nested_one(self, child_path, source, model, where):
        """A serializer field over a forward relation: the related row, loaded as
        Django loads it, serialized by the child serializer (None stays None)."""
        mf = model.by_name.get(source)
        if mf is None or not mf.is_fk or source != mf.name:
            raise Unsupported("%s: nested serializer over something other than a relation" % where)
        mf.need()
        target = self.schema.model(mf.related_model).need_entity()
        self.schema.check_readable(target)
        if mf.target_column != target.pk.column:
            raise Unsupported("%s: relation using to_field" % where)
        child = self.need(child_path)
        self.tr.using.add(("ser", child_path))
        if self.generated[child_path][1] != target.label:
            raise Unsupported("%s: nested serializer of another model" % where)
        load = (
            "match %s::Entity::find().filter(orm::exact(%s, Some(id.clone()))).one(ctx.tx()).await? "
            "{ Some(x) => %s(ctx, &x).await?, None => return Err(Stop::Forward(\"related object does not exist\")) }"
            % (target.entity, raw_str(target.pk.column), child)
        )
        if mf.null:
            return "match &m.%s { Some(id) => %s, None => Py::None }" % (mf.ident, load)
        return "{ let id = &m.%s; %s }" % (mf.ident, load)

    def _nested_many(self, f, model, where):
        """`children = ChildSerializer(many=True)` over a reverse relation:
        `instance.<accessor>.all()`, in the child model's default ordering."""
        child_path = f.get("child")
        source = f.get("source")
        rel = next((r for r in model.reverse if r.get("accessor_name") == source), None)
        if rel is None or not child_path:
            raise Unsupported("%s: many=True over something other than a reverse relation" % where)
        child_model = self.schema.model(rel["related_model"]).need_entity()
        self.schema.check_readable(child_model)
        fk = child_model.field(rel["remote_field_name"])
        if fk.target_column != model.pk.column:
            raise Unsupported("%s: relation using to_field" % where)
        child = self.need(child_path)
        self.tr.using.add(("ser", child_path))
        if self.generated[child_path][1] != child_model.label:
            raise Unsupported("%s: nested serializer of another model" % where)
        terms = ", ".join("(%s, %s)" % (raw_str(c), "true" if d else "false") for c, d in child_model.ordering_terms())
        return (
            "{ let rows = orm::order_by(%s::Entity::find().filter(orm::exact(%s, Some(m.%s.clone()))), None, &[%s]).all(ctx.tx()).await?; "
            "let mut out = Vec::new(); for r in rows.iter() { out.push(%s(ctx, r).await?); } Py::List(out) }"
            % (child_model.entity, raw_str(fk.column), model.pk.ident, terms, child)
        )

    def _method_field(self, path, cls_ir, tree, f, model, extra, where):
        """`SerializerMethodField`: the method, translated like a project
        function called with `(self, obj)`; `self` can't be used."""
        from .lower import SER_SELF, FnLower, Out, PyT, RowT  # noqa: circular at import time

        method = f.get("method_name") or "get_" + f["name"]
        fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == method), None)
        if fn is None:
            raise Unsupported("%s: method %s not found in the class" % (where, method))
        a = fn.args
        if fn.decorator_list or a.vararg or a.kwarg or a.kwonlyargs or len(a.args) != 2 or a.defaults:
            raise Unsupported("%s: method signature" % where)
        view = {"function": {"refs": cls_ir.get("refs") or {}, "path": "%s.%s" % (path, method)}}
        args = {a.args[0].arg: (SER_SELF, None), a.args[1].arg: (RowT(model.label), "Some(m.clone())")}
        learn = FnLower(self.tr, view, fn, {}, True, iid=1)
        learn.out = Out(2)
        learn.run_inline(args)
        real = FnLower(self.tr, view, fn, learn.var_types, False, iid=1)
        real.ret_type = learn.ret_type
        real.out = Out(2)
        body = real.run_inline(args)
        if not isinstance(real.ret_type, PyT):
            raise Unsupported("%s: method returns a %s" % (where, real.ret_type))
        if real.ret_type.tainted:
            self._taint[-1] = True
        name = "serm_%s_%s" % (re.sub(r"[^A-Za-z0-9]", "_", path), method)
        extra.append(
            "/// `%s.%s(obj)`\n" % (path.rsplit(".", 1)[-1], method)
            + "#[allow(unused_mut, unused_variables, unused_assignments, unreachable_code, unused_parens, unused_braces, unused_labels, clippy::all)]\n"
            + "async fn %s(ctx: &mut Ctx, m: &%s::Model) -> Result<Py, Stop> {\n" % (name, model.entity)
            + "    let r = 'inl_1: {\n"
            + "".join(l + "\n" for l in real.declarations_at(2))
            + "".join(l + "\n" for l in body)
            + "    };\n"
            + "    Ok(r)\n"
            + "}\n"
        )
        return "%s(ctx, m).await?" % name


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
