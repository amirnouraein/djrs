"""The translator: DRF function views (Python) -> Rust handlers.

Rule-based and deterministic. A view is translated only if every construct
in it is in the supported subset (see `lower.py`); otherwise the reason is
reported and the view stays on Django. The output is one Rust module,
`src/translated.rs`, holding a handler per translated view plus the
serializer and delete helpers they use.
"""

from __future__ import annotations

import re

from djrs.generate.rust import raw_str

from .lower import FnLower, parse_function
from .schema import INT_TYPES, Schema, Unsupported
from .serializers import Serializers

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _ident(view_id):
    return re.sub(r"[^A-Za-z0-9]", "_", view_id)


class Translation:
    def __init__(self):
        self.handlers = {}  # view id -> {method: rust path}
        self.reasons = {}  # view id -> [reason] for views not translated
        self.source = ""


class Translator:
    def __init__(self, data):
        self.data = data
        self.schema = Schema(data)
        self.serializers = Serializers(data, self.schema)
        user = ((data.get("auth") or {}).get("user") or {})
        self.user_pk_kind = "int" if user.get("pk_type") in INT_TYPES else None
        self.del_fns = {}  # label -> (fn name, source, deps)
        self.using = set()

    # ---- shared helpers --------------------------------------------------------

    def ser_fn(self, path):
        fn = self.serializers.need(path)
        self.using.add(("ser", path))
        return fn

    def deleter(self, label, stack=()):
        """Rust fn deleting rows of `label` by primary key the way Django's
        Collector does (cascades first), returning the count Django reports."""
        self.using.add(("del", label))
        if label in self.del_fns:
            return self.del_fns[label][0]
        if label in stack:
            raise Unsupported("delete() with a cycle of cascades")
        if len(stack) > 6:
            raise Unsupported("delete() cascades too deep")
        s = self.schema
        m = s.model(label).need_entity()
        if s.has_receivers(label, ("pre_delete", "post_delete")):
            raise Unsupported("delete() of %s: delete signal receivers" % label)
        if m.raw.get("parents"):
            raise Unsupported("delete() of %s: multi-table inheritance" % label)
        if m.generic:
            raise Unsupported("delete() of %s: generic relations" % label)
        name = "del_" + m.module
        pk_t = m.pk.rust_base
        lines = []
        deps = []
        for rel in sorted(m.reverse, key=lambda r: (r.get("related_model") or "", r.get("remote_field_name") or "")):
            child = s.model(rel["related_model"])
            if label in (child.raw.get("parents") or []):
                raise Unsupported("delete() of %s: a model inherits from it" % label)
            fk = child.field(rel["remote_field_name"])
            if fk.target_column != m.pk.column:
                raise Unsupported("delete() of %s: a relation uses to_field" % label)
            child.need_entity()
            where = "orm::is_in(%s, ids.clone())" % raw_str(fk.column)
            what = "%s.%s" % (child.label, fk.name)
            if fk.on_delete == "CASCADE":
                fn = self.deleter(child.label, stack + (label,))
                deps.append(child.label)
                lines.append("    // CASCADE from %s" % what)
                lines.append(
                    "    let child: Vec<%s> = %s::Entity::find().filter(%s).all(tx).await?.into_iter().map(|r| r.%s).collect();"
                    % (child.pk.rust_base, child.entity, where, child.pk.ident)
                )
                lines.append("    n += %s(tx, child).await?;" % fn)
            elif fk.on_delete == "SET_NULL":
                if not fk.null:
                    raise Unsupported("SET_NULL on a NOT NULL column")
                lines.append("    // SET_NULL on %s" % what)
                lines.append(
                    "    %s::Entity::update_many().set(%s::ActiveModel { %s: Set(None), ..Default::default() }).filter(%s).exec(tx).await?;"
                    % (child.entity, child.entity, fk.ident, where)
                )
            elif fk.on_delete == "PROTECT":
                lines.append("    // PROTECT from %s" % what)
                lines.append(
                    "    if PaginatorTrait::count(%s::Entity::find().filter(%s), tx).await? > 0 { return Err(Stop::Forward(\"ProtectedError\")); }"
                    % (child.entity, where)
                )
            elif fk.on_delete == "DO_NOTHING":
                lines.append("    // DO_NOTHING for %s" % what)
            else:
                raise Unsupported("delete() of %s: on_delete=%s" % (label, fk.on_delete))
        src = (
            "/// Delete `%s` rows by primary key, as Django's Collector does.\n" % label
            + "async fn %s(tx: &DatabaseTransaction, ids: Vec<%s>) -> Result<u64, Stop> {\n" % (name, pk_t)
            + "    if ids.is_empty() {\n        return Ok(0);\n    }\n"
            + "    let mut n: u64 = 0;\n"
            + "".join(l + "\n" for l in lines)
            + "    n += %s::Entity::delete_many().filter(orm::is_in(%s, ids)).exec(tx).await?.rows_affected;\n"
            % (m.entity, raw_str(m.pk.column))
            + "    Ok(n)\n}\n"
        )
        self.del_fns[label] = (name, src, deps)
        return name

    # ---- views --------------------------------------------------------------------

    def translate_view(self, view_id, view):
        """(handler name, Rust source) or raise Unsupported."""
        if view.get("kind") != "drf_function":
            raise Unsupported("only DRF function views are translated so far")
        fn = parse_function(view)
        var_types = {}
        FnLower(self, view, fn, var_types, learning=True).run()
        low = FnLower(self, view, fn, var_types, learning=False)
        self.using = set()
        body = low.run()
        used = set(self.using)
        ident = _ident(view_id)
        py = "\n".join("// " + l if l else "//" for l in view["function"]["source"].rstrip("\n").split("\n"))
        loc = view["function"].get("file")
        header = "// %s%s\n" % (view_id, " (%s:%s)" % (loc, view["function"].get("line")) if loc else "")
        src = (
            header
            + py
            + "\n#[allow(unused_mut, unused_variables, unused_assignments, unreachable_code, unused_parens, unused_braces, clippy::all)]\n"
            + "async fn body_%s(ctx: &mut Ctx) -> Result<DrfResponse, Stop> {\n" % ident
            + "".join(l + "\n" for l in low.declarations())
            + "".join(l + "\n" for l in body)
            + "}\n"
            + "drf_handler!(drf_%s, body_%s);\n" % (ident, ident)
        )
        return "drf_" + ident, src, used


def translate(data, only=None):
    """Translate every DRF function view (or those in `only`)."""
    tr = Translator(data)
    result = Translation()
    pieces = []
    used = set()
    for view_id, view in sorted((data.get("views") or {}).items()):
        if only is not None and view_id not in only:
            continue
        if view.get("kind") != "drf_function":
            continue
        try:
            name, src, u = tr.translate_view(view_id, view)
        except Unsupported as exc:
            result.reasons[view_id] = [exc.reason]
            continue
        methods = [m for m in view.get("methods") or [] if m in METHODS]
        if not methods:
            result.reasons[view_id] = ["no HTTP methods to translate"]
            continue
        result.handlers[view_id] = {m: "translated::%s" % name for m in methods}
        pieces.append(src)
        used |= u
    # Helpers the translated views use (and the delete helpers those call).
    dels = set()
    todo = [label for kind, label in used if kind == "del"]
    while todo:
        label = todo.pop()
        if label in dels:
            continue
        dels.add(label)
        todo.extend(tr.del_fns[label][2])
    sers = sorted(path for kind, path in used if kind == "ser")
    helpers = [tr.serializers.generated[p][0] for p in sers] + [tr.del_fns[l][1] for l in sorted(dels)]
    result.source = (
        "//! Rust translations of the project's views. Each handler follows its\n"
        "//! Python line by line (quoted above it); anything Python would raise\n"
        "//! for hands the request to Django instead.\n"
        "#![allow(dead_code, non_snake_case, unused_imports)]\n\n"
        "use chrono::{DateTime, Utc};\n"
        "use djrs_runtime::db::{now, DjDateTime, DjJson};\n"
        "use djrs_runtime::drf::{ApiError, Ctx, DrfResponse, Stop};\n"
        "use djrs_runtime::py::Py;\n"
        "use djrs_runtime::{drf_handler, ops, orm};\n"
        "use sea_orm::ActiveValue::{NotSet, Set};\n"
        "use sea_orm::{Condition, DatabaseTransaction, EntityTrait, PaginatorTrait, QueryFilter, QuerySelect};\n\n"
        "use crate::entities as e;\n\n"
        + "\n".join(helpers)
        + ("\n" if helpers else "")
        + "\n".join(pieces)
    )
    return result
