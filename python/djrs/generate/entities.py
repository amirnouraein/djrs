"""SeaORM entities for the project's models.

Each concrete model becomes `entities::<app>_<model>` with one field per
database column, typed so that values round-trip exactly as Django stores
them on this project's database vendor. A field type djrs doesn't know is left
out of the entity and reported; such a model can be read but never inserted.
"""

from __future__ import annotations

import keyword
import re

from .rust import raw_str

RUST_KEYWORDS = {
    "as", "break", "const", "continue", "crate", "else", "enum", "extern", "false", "fn", "for", "if", "impl", "in",
    "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return", "self", "Self", "static", "struct", "super",
    "trait", "true", "type", "unsafe", "use", "where", "while", "async", "await", "dyn", "abstract", "become", "box",
    "do", "final", "macro", "override", "priv", "typeof", "unsized", "virtual", "yield", "try", "gen",
}  # fmt: skip

STRING_TYPES = {
    "CharField", "TextField", "SlugField", "EmailField", "URLField", "FilePathField", "FileField", "ImageField",
    "GenericIPAddressField", "IPAddressField",
}  # fmt: skip


def module_name(label):
    app, model = label.split(".", 1)
    return re.sub(r"[^a-z0-9_]", "_", ("%s_%s" % (app, model)).lower())


def field_ident(name):
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if ident in RUST_KEYWORDS or keyword.iskeyword(ident) or ident[:1].isdigit():
        ident = ident + "_"
    return ident


def _int_type(internal, vendor):
    unsigned = vendor == "mysql"
    return {
        "AutoField": "i32",
        "BigAutoField": "i64",
        "SmallAutoField": "i16",
        "IntegerField": "i32",
        "BigIntegerField": "i64",
        "SmallIntegerField": "i16",
        "PositiveIntegerField": "u32" if unsigned else "i32",
        "PositiveSmallIntegerField": "u16" if unsigned else "i16",
        "PositiveBigIntegerField": "u64" if unsigned else "i64",
    }.get(internal)


def rust_type(field, models_by_label, vendor):
    """(rust type without Option, None) or (None, reason)."""
    internal = field.get("internal_type")
    if field.get("is_relation") and (field.get("many_to_one") or field.get("one_to_one")):
        target = models_by_label.get(field.get("related_model"))
        if target is None:
            return None, "relation to a model djrs can't see"
        target_col = field.get("target_column")
        tf = next((f for f in target["fields"] if f.get("concrete") and f.get("column") == target_col), None)
        if tf is None:
            return None, "relation target column not found"
        return rust_type(tf, models_by_label, vendor)
    t = _int_type(internal, vendor)
    if t:
        return t, None
    if internal in STRING_TYPES:
        return "String", None
    if internal == "BooleanField":
        return "bool", None
    if internal == "NullBooleanField":
        return "bool", None
    if internal == "DateTimeField":
        return "djrs_runtime::db::DjDateTime", None
    if internal == "DateField":
        return "chrono::NaiveDate", None
    if internal == "FloatField":
        return "f64", None
    if internal == "UUIDField":
        return "djrs_runtime::db::DjUuid", None
    if internal == "JSONField":
        return "djrs_runtime::db::DjJson", None
    if internal == "DecimalField":
        if vendor == "sqlite":
            # Django round-trips SQLite decimals through floats; not reproduced yet.
            return None, "DecimalField on SQLite is not supported yet"
        return "rust_decimal::Decimal", None
    return None, "field type %s is not supported yet" % internal


def build(data):
    """(rust source, {label: {"module", "complete", "skipped": [...]}}) for every concrete model."""
    vendor = ((data.get("databases") or {}).get("default") or {}).get("vendor")
    models_by_label = {m["label"]: m for m in data.get("models", [])}
    out = []
    report = {}
    for m in data.get("models", []):
        if m.get("proxy"):
            continue
        fields = []
        skipped = []
        # Field classes from outside Django: their column is mapped by its raw
        # type, but their Python value may differ (e.g. a list stored as text).
        custom = sorted(
            "%s (%s)" % (f["name"], f["class"])
            for f in m["fields"]
            if f.get("concrete") and not f.get("class", "").startswith("django.")
        )
        pk_fields = [f for f in m["fields"] if f.get("concrete") and f.get("primary_key")]
        if len(pk_fields) != 1:
            report[m["label"]] = {"module": None, "complete": False, "skipped": ["no single-column primary key"]}
            continue
        for f in m["fields"]:
            if not f.get("concrete") or f.get("many_to_many"):
                continue
            t, reason = rust_type(f, models_by_label, vendor)
            if t is None:
                if f.get("primary_key"):
                    skipped.append("%s: %s (primary key)" % (f["name"], reason))
                    fields = None
                    break
                skipped.append("%s: %s" % (f["name"], reason))
                continue
            nullable = bool(f.get("null")) or f.get("internal_type") == "NullBooleanField"
            attrs = ["column_name = %s" % _attr_str(f["column"])]
            if f.get("primary_key"):
                auto = f.get("internal_type") in ("AutoField", "BigAutoField", "SmallAutoField")
                attrs.insert(0, "primary_key" if auto else "primary_key, auto_increment = false")
            column_type = {
                "djrs_runtime::db::DjDateTime": "DateTime",
                "djrs_runtime::db::DjUuid": "Uuid",
                "djrs_runtime::db::DjJson": "Json",
            }.get(t)
            if column_type:
                attrs.append('column_type = "%s"' % column_type)
            ty = "Option<%s>" % t if nullable else t
            fields.append("        #[sea_orm(%s)]\n        pub %s: %s," % (", ".join(attrs), field_ident(f["attname"]), ty))
        mod = module_name(m["label"])
        if fields is None:
            report[m["label"]] = {"module": None, "complete": False, "skipped": skipped}
            continue
        report[m["label"]] = {
            "module": mod,
            "complete": not skipped,
            "skipped": skipped,
            "custom_fields": custom,
            "table": m["db_table"],
        }
        note = ""
        if skipped:
            note = "    // Columns left out (this model can be read but not inserted):\n" + "".join(
                "    //   %s\n" % s for s in skipped
            )
        out.append(
            "/// `%s` (table `%s`)\n" % (m["label"], m["db_table"])
            + "pub mod %s {\n" % mod
            + "    use sea_orm::entity::prelude::*;\n\n"
            + note
            + "    pub const LABEL: &str = %s;\n" % raw_str(m["label"])
            + "    pub const COMPLETE: bool = %s;\n\n" % ("true" if not skipped else "false")
            + "    #[derive(Clone, Debug, PartialEq, DeriveEntityModel)]\n"
            + "    #[sea_orm(table_name = %s)]\n" % _attr_str(m["db_table"])
            + "    pub struct Model {\n"
            + "\n".join(fields)
            + "\n    }\n\n"
            + "    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]\n"
            + "    pub enum Relation {}\n\n"
            + "    impl ActiveModelBehavior for ActiveModel {}\n"
            + "}\n"
        )
    return "\n".join(out), report


def _attr_str(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')
