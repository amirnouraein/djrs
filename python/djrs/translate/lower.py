"""One DRF function view: Python AST -> the body of a Rust handler.

The translator gives every Python expression a static type. Plain Python
values (what request data, literals and responses are made of) are `Py` in
Rust and go through `djrs_runtime::ops`, which follows CPython exactly or
forwards. Django objects get dedicated types: a queryset is a SeaORM
`Condition` plus its static ordering and slice, a model instance is
`Option<Model>` holding exactly what Django holds in memory, and so on.

Anything outside the supported subset raises `Unsupported(reason)`; the view
is then answered by Django. At run time, anything Python would raise for
becomes `Stop::Forward`, which rolls back and hands the request to Django.
"""

from __future__ import annotations

import ast
import struct
import textwrap

from djrs.generate.rust import raw_str

from .schema import Unsupported

NOCONST = object()

# ---- static types -------------------------------------------------------------


class T:
    def key(self):
        return ()

    def __eq__(self, other):
        return type(self) is type(other) and self.key() == other.key()

    def __hash__(self):
        return hash((type(self).__name__,) + self.key())

    def __repr__(self):
        return "%s%r" % (type(self).__name__, self.key())


class PyT(T):
    """A plain Python value. `kind` is its Python type when statically known."""

    def __init__(self, kind=None):
        self.kind = kind

    def key(self):
        return (self.kind,)


class ModelT(T):
    def __init__(self, label):
        self.label = label

    def key(self):
        return (self.label,)


class ModelClsT(ModelT):
    pass


class ManagerT(ModelT):
    pass


class RowT(ModelT):
    """A model instance or None: Rust `Option<Model>`."""


class RowsT(ModelT):
    """`list(queryset)`: Rust `Vec<Model>`."""


class DoesNotExistT(T):
    def __init__(self, label):
        self.label = label  # None: ObjectDoesNotExist (any model)

    def key(self):
        return (self.label,)


class QsT(T):
    """A queryset: Rust `Condition` (its filters) in `V.r`, its ordering
    (`orm::Ordering`, None for Meta.ordering) in `V.extra`, and a static slice."""

    def __init__(self, label, lo=None, hi=None, distinct=False):
        self.label = label
        self.lo = lo
        self.hi = hi
        self.distinct = distinct

    def key(self):
        return (self.label, self.lo, self.hi, self.distinct)

    def sliced(self):
        return self.lo is not None or self.hi is not None


class Simple(T):
    """Types that are values of exactly one sort."""

    def __init__(self, name):
        self.name = name

    def key(self):
        return (self.name,)


REQUEST = Simple("request")
USER = Simple("request.user")
DATA = Simple("request.data")
QUERY = Simple("request.query_params")
RESPONSE_CLS = Simple("Response")
RESPONSE = Simple("response")
NOW_FN = Simple("timezone.now")
DATETIME = Simple("datetime")
TIMEDELTA_CLS = Simple("timedelta class")
TIMEDELTA = Simple("timedelta")
DELETE_RESULT = Simple("delete() result")
OPAQUE = Simple("unusable value")


class Named(T):
    def __init__(self, what, name):
        self.what = what
        self.name = name

    def key(self):
        return (self.what, self.name)


def builtin(name):
    return Named("builtin", name)


def module(path):
    return Named("module", path)


def exc_cls(name):
    return Named("exception class", name)


EXC = Simple("exception")


class SerClsT(T):
    def __init__(self, path):
        self.path = path

    def key(self):
        return (self.path,)


class SerT(T):
    def __init__(self, path, many):
        self.path = path
        self.many = many

    def key(self):
        return (self.path, self.many)


class V:
    """A lowered expression: its type and Rust code.

    For Py values `r` has Rust type `Py`; `place` says it is a variable (use
    `own()` / `ref()` to get an owned value or a reference)."""

    def __init__(self, t, r=None, place=False, const=NOCONST, extra=None):
        self.t = t
        self.r = r
        self.place = place
        self.const = const
        self.extra = extra


def own(v):
    return "%s.clone()" % v.r if v.place else v.r


def ref(v):
    return "&%s" % v.r if v.place else "&(%s)" % v.r


def py_lit(value):
    if value is None:
        return "Py::None"
    if value is True:
        return "Py::Bool(true)"
    if value is False:
        return "Py::Bool(false)"
    if isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise Unsupported("integer literal beyond 64 bits")
        return "Py::Int(%d)" % value
    if isinstance(value, float):
        bits = struct.unpack("<Q", struct.pack("<d", value))[0]
        return "Py::Float(f64::from_bits(0x%016x))" % bits
    if isinstance(value, str):
        return "Py::Str(%s.to_string())" % raw_str(value)
    raise Unsupported("constant of type %s" % type(value).__name__)


def py_kind(value):
    if value is None:
        return "none"
    return {bool: "bool", int: "int", float: "float", str: "str"}.get(type(value))


IMMUTABLE = ("int", "str", "bool", "float", "none")


def fresh(node):
    """Does this expression always build a new object (nothing else refers to it)?"""
    if isinstance(node, (ast.List, ast.Dict, ast.Tuple, ast.JoinedStr, ast.Constant, ast.BinOp)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("list", "dict", "str", "int", "bool", "len"):
        return True
    if isinstance(node, ast.Attribute) and node.attr == "data" and isinstance(node.value, ast.Call):
        return True  # Serializer(...).data
    return False


def mutated_names(fn):
    """Variables changed in place: `x[k] = v`, `x.append(v)`, `x += v`."""
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name):
            out.add(node.value.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            if node.func.attr in ("append",):
                out.add(node.func.value.id)
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def unify(a, b, name):
    if a is None:
        return b
    if a == b:
        return a
    if isinstance(a, PyT) and isinstance(b, PyT):
        return PyT(None)
    # `x = None` then `x = Model.objects.first()`: an Option<Model> variable.
    if isinstance(a, RowT) and b == PyT("none"):
        return a
    if a == PyT("none") and isinstance(b, RowT):
        return b
    raise Unsupported("variable %s holds values of different kinds" % name)


def var_rust_type(t, tr):
    if isinstance(t, PyT):
        return "Py", "Py::None"
    if isinstance(t, RowT):
        return "Option<%s::Model>" % tr.schema.model(t.label).entity, "None"
    if isinstance(t, RowsT):
        return "Vec<%s::Model>" % tr.schema.model(t.label).entity, "Vec::new()"
    if isinstance(t, QsT):
        return "Condition", "Condition::all()"  # plus `o_<name>: orm::Ordering`
    if t == RESPONSE:
        return "Option<DrfResponse>", "None"
    if t == DATETIME:
        return "DateTime<Utc>", "DateTime::<Utc>::MIN_UTC"
    if t == OPAQUE:
        return None, None
    raise Unsupported("a variable holding a %s" % (getattr(t, "name", None) or type(t).__name__))


# ---- emitting -----------------------------------------------------------------


class Out:
    def __init__(self, indent):
        self.lines = []
        self.indent = indent

    def line(self, text):
        self.lines.append("    " * self.indent + text)

    def open(self, head):
        self.line(head + " {")
        self.indent += 1

    def close(self, tail="}"):
        self.indent -= 1
        self.line(tail)


def diverges(stmts):
    """Statically: does this statement list always return or raise?"""
    for s in stmts:
        if isinstance(s, (ast.Return, ast.Raise)):
            return True
        if isinstance(s, ast.If) and s.orelse and diverges(s.body) and diverges(s.orelse):
            return True
    return False


def collect_locals(fn):
    names = set(a.arg for a in fn.args.args)
    for node in ast.walk(fn):
        if node is fn:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            raise Unsupported("nested function or class")
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            raise Unsupported("comprehensions")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise Unsupported("global/nonlocal")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise Unsupported("import inside the view")
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        if getattr(ast, "NamedExpr", None) and isinstance(node, ast.NamedExpr):
            raise Unsupported("assignment expressions")
    return names


DRF_DECORATORS = {
    "rest_framework.decorators.api_view",
    "rest_framework.decorators.permission_classes",
    "rest_framework.decorators.authentication_classes",
    "rest_framework.decorators.renderer_classes",
    "rest_framework.decorators.parser_classes",
}
LOOKUPS = {"exact", "iexact", "contains", "icontains", "startswith", "istartswith", "endswith", "iendswith",
           "in", "gt", "gte", "lt", "lte", "isnull"}  # fmt: skip
LIKE = {
    "iexact": "IExact", "contains": "Contains", "icontains": "IContains", "startswith": "StartsWith",
    "istartswith": "IStartsWith", "endswith": "EndsWith", "iendswith": "IEndsWith",
}  # fmt: skip
CMP = {"gt": "gt", "gte": "gte", "lt": "lt", "lte": "lte"}
DRF_EXC = {
    "rest_framework.exceptions.NotFound": "NotFound",
    "rest_framework.exceptions.PermissionDenied": "PermissionDenied",
    "rest_framework.exceptions.ValidationError": "ValidationError",
    "rest_framework.exceptions.ParseError": "ParseError",
    "django.http.response.Http404": "Http404",
    "django.core.exceptions.PermissionDenied": "DjangoPermissionDenied",
}
TIMEDELTA_UNITS = {
    "weeks": 7 * 86400 * 10**6, "days": 86400 * 10**6, "hours": 3600 * 10**6, "minutes": 60 * 10**6,
    "seconds": 10**6, "milliseconds": 1000, "microseconds": 1,
}  # fmt: skip


def parse_function(view):
    fn_ir = view.get("function") or {}
    src = fn_ir.get("source")
    if not src:
        raise Unsupported("view source not found")
    try:
        tree = ast.parse(textwrap.dedent(src))
    except SyntaxError:
        raise Unsupported("view source doesn't parse with this Python")
    fns = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(fns) != 1:
        raise Unsupported("view source isn't one function")
    fn = fns[0]
    if isinstance(fn, ast.AsyncFunctionDef) or fn_ir.get("is_async"):
        raise Unsupported("async views")
    if fn_ir.get("decorators"):
        raise Unsupported("the view is wrapped by other decorators")
    return fn


class FnLower:
    def __init__(self, tr, view, fn, var_types, learning):
        self.tr = tr
        self.view = view
        self.fn = fn
        self.refs = view["function"].get("refs") or {}
        self.var_types = var_types
        self.learning = learning
        self.out = Out(1)
        self.n = 0
        self.assigned = set()
        self.deleted = set()
        self.catch = []  # stack of (labels or None for any model, handler statements)
        self.iterating = []
        self.locals = collect_locals(fn)
        self.mutated = mutated_names(fn)
        self.in_return = False

    # ---- small helpers ---------------------------------------------------------

    def tmp(self, prefix="t"):
        self.n += 1
        return "%s_%d" % (prefix, self.n)

    def let(self, expr, t=None, prefix="t"):
        name = self.tmp(prefix)
        self.out.line("let %s = %s;" % (name, expr))
        return V(t or PyT(), name, place=True)

    def fwd(self, why):
        return "return Err(Stop::Forward(%s))" % raw_str(why)

    def capture(self):
        saved = self.out
        self.out = Out(saved.indent + 1)
        return saved

    def block_expr(self, saved, tail):
        sub = self.out
        self.out = saved
        if not sub.lines:
            return tail
        pad = "    " * saved.indent
        return "{\n" + "\n".join(sub.lines) + "\n" + pad + "    " + tail + "\n" + pad + "}"

    def model(self, label):
        return self.tr.schema.model(label)

    def own(self, v):
        """An owned Py value. Python shares lists and dicts by reference; the
        translation copies. That is only the same while a list or dict changed
        in place isn't also stored somewhere else (except in what is returned)."""
        name = getattr(v, "var", None)
        if name in self.mutated and not self.in_return and not self._immutable(name):
            raise Unsupported("a list or dict changed in place is also stored elsewhere")
        return own(v)

    def _immutable(self, name):
        t = self.var_types.get(name)
        return isinstance(t, PyT) and t.kind in IMMUTABLE

    def need_py(self, v, what="value"):
        if not isinstance(v.t, PyT):
            raise Unsupported("%s of kind %s used as a plain Python value" % (what, _tname(v.t)))
        return v

    def var(self, name):
        if not name.isidentifier() or not name.isascii():
            raise Unsupported("non-ASCII variable names")
        return "v_" + name

    # ---- the function ----------------------------------------------------------

    def run(self):
        fn = self.fn
        a = fn.args
        if a.vararg or a.kwarg or a.kwonlyargs or getattr(a, "posonlyargs", None):
            raise Unsupported("*args/**kwargs or keyword-only parameters")
        for d in fn.decorator_list:
            target = d.func if isinstance(d, ast.Call) else d
            path = self._dotted(target)
            ref_ = self.refs.get(path) if path else None
            if not ref_ or ref_.get("path") not in DRF_DECORATORS:
                raise Unsupported("decorator %s" % (path or "expression"))
        if not a.args:
            raise Unsupported("view takes no request parameter")
        self.request = a.args[0].arg
        params = [p.arg for p in a.args[1:]]
        defaults = [None] * (len(params) - len(a.defaults)) + list(a.defaults)
        lits = []
        for d in defaults:
            if d is None:
                lits.append("None")
            elif isinstance(d, ast.Constant):
                lits.append("Some(%s)" % py_lit(d.value))
            else:
                raise Unsupported("non-constant parameter default")
        if params:
            for p in params:
                self._set_type(p, PyT())
            self.out.line(
                "let params = ctx.bind_params(&[%s], &[%s])?;"
                % (", ".join(raw_str(p) for p in params), ", ".join(lits))
            )
            for i, p in enumerate(params):
                self.out.line("%s = params[%d].clone();" % (self.var(p), i))
                self.assigned.add(p)
        self.assigned.add(self.request)
        div = self.block(fn.body)
        if not div:
            # Falling off the end returns None; DRF asserts a Response (a 500).
            self.out.line("%s;" % self.fwd("view returned None"))
        return self.out.lines

    def declarations(self):
        lines = []
        for name, t in sorted(self.var_types.items()):
            rt, init = var_rust_type(t, self.tr)
            if rt is not None:
                lines.append("    let mut %s: %s = %s;" % (self.var(name), rt, init))
            if isinstance(t, QsT):
                lines.append("    let mut o_%s: orm::Ordering = None;" % name)
        return lines

    def _set_type(self, name, t):
        old = self.var_types.get(name)
        new = unify(old, t, name)
        if self.learning:
            self.var_types[name] = new
        elif new != old:
            raise Unsupported("variable %s changes kind" % name)
        return self.var_types.get(name, new)

    # ---- statements --------------------------------------------------------------

    def block(self, stmts):
        for s in stmts:
            if self.stmt(s):
                return True
        return False

    def stmt(self, s):
        m = getattr(self, "s_" + type(s).__name__, None)
        if m is None:
            raise Unsupported("statement %s" % type(s).__name__)
        return m(s)

    def s_Pass(self, s):
        return False

    def s_Expr(self, s):
        if isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
            return False  # docstring
        self.expr(s.value)
        return False

    def s_Return(self, s):
        if s.value is None:
            raise Unsupported("view returns None")
        self.in_return = True
        try:
            v = self.expr(s.value)
        finally:
            self.in_return = False
        if v.t != RESPONSE:
            raise Unsupported("view returns something other than a Response")
        self.out.line("return Ok(%s);" % v.r)
        return True

    def s_Raise(self, s):
        if s.exc is None:
            raise Unsupported("bare raise")
        v = self.expr(s.exc)
        if v.t != EXC:
            if isinstance(v.t, Named) and v.t.what == "exception class":
                v = self.exception(v.t.name, [], {})
            else:
                raise Unsupported("raises something other than a DRF exception")
        self.out.line("return Err(Stop::Api(%s));" % v.r)
        return True

    def s_Assign(self, s):
        v = self.expr(s.value)
        for target in s.targets:
            if isinstance(target, ast.Name):
                self.check_fresh(target.id, s.value, v)
            self.assign(target, v)
        return False

    def check_fresh(self, name, node, v):
        if name in self.mutated and not (isinstance(v.t, PyT) and v.t.kind in IMMUTABLE) and not fresh(node):
            raise Unsupported("a list or dict changed in place starts as another object's alias")

    def s_AugAssign(self, s):
        if not isinstance(s.target, ast.Name):
            raise Unsupported("augmented assignment to an attribute or item")
        cur = self.e_Name(ast.Name(id=s.target.id, ctx=ast.Load()))
        rhs = self.expr(s.value)
        v = self.binop(s.op, cur, rhs)
        self.assign(s.target, v)
        return False

    def s_If(self, s):
        c = self.cond(s.test)
        before = (set(self.assigned), set(self.deleted))
        self.out.open("if %s" % c)
        div_a = self.block(s.body)
        after_a = (set(self.assigned), set(self.deleted))
        self.assigned, self.deleted = set(before[0]), set(before[1])
        if s.orelse:
            self.out.close("} else {")
            self.out.indent += 1
            div_b = self.block(s.orelse)
        else:
            div_b = False
        after_b = (set(self.assigned), set(self.deleted))
        self.out.close()
        branches = [b for b, d in ((after_a, div_a), (after_b, div_b)) if not d]
        if branches:
            self.assigned = set.intersection(*[b[0] for b in branches])
            self.deleted = set.union(*[b[1] for b in branches])
        return div_a and div_b

    def s_For(self, s):
        if s.orelse:
            raise Unsupported("for ... else")
        if not isinstance(s.target, ast.Name):
            raise Unsupported("for loops with unpacking")
        it = self.expr(s.iter)
        name = s.target.id
        if isinstance(it.t, (QsT, ManagerT)):
            rows = self.qs_rows(self.as_qs(it))
            t = self._set_type(name, RowT(it.t.label))
            self.out.open("for it in %s" % rows)
            self.out.line("%s = Some(it);" % self.var(name))
        elif isinstance(it.t, RowsT):
            self._set_type(name, RowT(it.t.label))
            self.out.open("for it in %s.clone()" % it.r)
            self.out.line("%s = Some(it);" % self.var(name))
        elif isinstance(it.t, PyT):
            if name in self.mutated:
                raise Unsupported("changing the loop variable in place")
            self._set_type(name, PyT())
            self.out.open("for it in ops::iter(%s)?" % ref(it))
            self.out.line("%s = it;" % self.var(name))
        else:
            raise Unsupported("iterating a %s" % _tname(it.t))
        before = (set(self.assigned), set(self.deleted))
        self.assigned.add(name)
        self.deleted.discard(name)
        src = s.iter.id if isinstance(s.iter, ast.Name) else None
        self.iterating.append(src)
        self.block(s.body)
        self.iterating.pop()
        deleted_in_body = set(self.deleted)
        self.out.close()
        # The body may run zero times.
        self.assigned = before[0]
        self.deleted = before[1] | deleted_in_body
        return False

    def s_Break(self, s):
        self.out.line("break;")
        return True

    def s_Continue(self, s):
        self.out.line("continue;")
        return True

    def s_Try(self, s):
        if s.finalbody:
            raise Unsupported("try ... finally")
        labels = set()
        for h in s.handlers:
            if h.type is None:
                raise Unsupported("bare except")
            types = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
            for t in types:
                v = self.expr(t)
                if not isinstance(v.t, DoesNotExistT):
                    raise Unsupported("except clauses other than Model.DoesNotExist")
                labels.add(v.t.label)
            if h.name and any(isinstance(n, ast.Name) and n.id == h.name for b in h.body for n in ast.walk(b)):
                raise Unsupported("using the caught exception")
            if not diverges(h.body):
                raise Unsupported("except clause that doesn't return or raise")
        if len(s.handlers) != 1:
            raise Unsupported("several except clauses")
        self.catch.append((None if None in labels else labels, s.handlers[0].body))
        try:
            div = self.block(s.body)
        finally:
            self.catch.pop()
        if s.orelse and not div:
            div = self.block(s.orelse)
        return div

    # ---- assignment ------------------------------------------------------------

    def assign(self, target, v):
        if isinstance(target, ast.Name):
            return self.assign_name(target.id, v)
        if isinstance(target, ast.Tuple):
            if v.t != DELETE_RESULT or len(target.elts) != 2:
                raise Unsupported("tuple unpacking")
            first, second = target.elts
            if not (isinstance(first, ast.Name) and isinstance(second, ast.Name)):
                raise Unsupported("tuple unpacking into attributes")
            self.assign_name(first.id, V(PyT("int"), v.r, place=True))
            self.assign_name(second.id, V(OPAQUE))
            return
        if isinstance(target, ast.Attribute):
            return self.set_attr(target, v)
        if isinstance(target, ast.Subscript):
            base = self.expr(target.value)
            self.need_py(base, "item assignment target")
            if not base.place or (isinstance(target.value, ast.Name) and target.value.id in self.iterating):
                raise Unsupported("item assignment to a temporary or to the sequence being iterated")
            key = self.need_py(self.expr(_slice(target)))
            self.out.line("ops::setitem(&mut %s, %s, %s)?;" % (base.r, ref(key), self.own(self.need_py(v))))
            return
        raise Unsupported("assignment to %s" % type(target).__name__)

    def assign_name(self, name, v):
        if name in self.iterating:
            raise Unsupported("rebinding the name being iterated")
        t = self._set_type(name, v.t)
        var = self.var(name)
        if v.t == OPAQUE:
            pass
        elif isinstance(t, PyT):
            self.out.line("%s = %s;" % (var, self.own(self.need_py(v))))
        elif isinstance(t, RowT):
            if isinstance(v.t, PyT):
                self.out.line("%s = None;" % var)
            else:
                self.out.line("%s = %s;" % (var, self.own(v)))
        elif isinstance(t, QsT):
            self.out.line("%s = %s;" % (var, v.r))
            self.out.line("o_%s = %s;" % (name, v.extra))
        elif isinstance(t, RowsT) or t in (DATETIME,):
            self.out.line("%s = %s;" % (var, v.r))
        elif t == RESPONSE:
            self.out.line("%s = Some(%s);" % (var, v.r))
        else:
            raise Unsupported("a variable holding a %s" % _tname(t))
        self.assigned.add(name)
        self.deleted.discard(name)

    # ---- expressions --------------------------------------------------------------

    def expr(self, n):
        m = getattr(self, "e_" + type(n).__name__, None)
        if m is None:
            raise Unsupported("expression %s" % type(n).__name__)
        return m(n)

    def py(self, n):
        return self.need_py(self.expr(n))

    def e_Constant(self, n):
        value = n.value
        if py_kind(value) is None:
            raise Unsupported("constant of type %s" % type(value).__name__)
        return V(PyT(py_kind(value)), py_lit(value), const=value)

    def e_Name(self, n):
        name = n.id
        if name in self.locals:
            if name not in self.assigned:
                raise Unsupported("variable %s may be unbound here" % name)
            if name in self.deleted:
                raise Unsupported("variable %s used after delete()" % name)
            if name == self.request:
                return V(REQUEST)
            t = self.var_types.get(name)
            if t is None:
                raise Unsupported("variable %s" % name)
            var = self.var(name)
            if isinstance(t, PyT):
                v = V(t, var, place=True)
                v.var = name
                return v
            if isinstance(t, RowT):
                return V(t, var, place=True, extra=name)
            if isinstance(t, QsT):
                return V(t, "%s.clone()" % var, extra="o_%s" % name)
            if isinstance(t, RowsT):
                return V(t, "%s.clone()" % var)
            if t == RESPONSE:
                return V(t, '%s.clone().expect("assigned")' % var)
            if t == DATETIME:
                return V(t, var)
            raise Unsupported("variable %s holds a %s" % (name, _tname(t)))
        return self.ref_value(name)

    def _dotted(self, n):
        parts = []
        while isinstance(n, ast.Attribute):
            parts.append(n.attr)
            n = n.value
        if not isinstance(n, ast.Name):
            return None
        parts.append(n.id)
        return ".".join(reversed(parts))

    def ref_value(self, dotted):
        r = self.refs.get(dotted)
        if r is None:
            raise Unsupported("name %s isn't known" % dotted)
        kind = r.get("kind")
        if kind == "value" or (kind == "setting" and "value" in r):
            value = r.get("value")
            if isinstance(value, dict) and value == {"$none": True}:
                value = None
            if py_kind(value) is None:
                raise Unsupported("constant %s of an unsupported type" % dotted)
            return V(PyT(py_kind(value)), py_lit(value), const=value)
        if kind == "model":
            return V(ModelClsT(r["label"]))
        if kind == "model_exception":
            if r.get("exception") != "DoesNotExist":
                raise Unsupported("%s" % r.get("path"))
            return V(DoesNotExistT(r["label"]))
        if kind == "builtin":
            return V(builtin(r["name"]))
        if kind == "module":
            return V(module(r["path"]))
        if kind == "object":
            base = dotted.rsplit(".", 1)
            if len(base) == 2 and base[1] == "objects":
                owner = self.refs.get(base[0]) or {}
                if owner.get("kind") == "model":
                    m = self.model(owner["label"])
                    if r.get("type") != "django.db.models.manager.Manager" or m.raw.get("default_manager") != r.get("type"):
                        raise Unsupported("custom model manager (%s)" % r.get("type"))
                    return V(ManagerT(owner["label"]))
            raise Unsupported("uses object %s (%s)" % (dotted, r.get("type")))
        if kind == "class":
            path = r.get("path")
            if path == "rest_framework.response.Response":
                return V(RESPONSE_CLS)
            if path in DRF_EXC:
                return V(exc_cls(DRF_EXC[path]))
            if path == "django.core.exceptions.ObjectDoesNotExist":
                return V(DoesNotExistT(None))
            if path == "datetime.timedelta":
                return V(TIMEDELTA_CLS)
            mro = r.get("mro") or []
            if "rest_framework.serializers.BaseSerializer" in mro:
                return V(SerClsT(path))
            raise Unsupported("uses class %s" % path)
        if kind == "function":
            path = r.get("path")
            if path == "django.utils.timezone.now":
                return V(NOW_FN)
            if r.get("project"):
                raise Unsupported("calls a project function (not translated yet)")
            raise Unsupported("calls %s" % path)
        if kind == "celery_task":
            raise Unsupported("queues a Celery task")
        raise Unsupported("uses %s (%s)" % (dotted, kind))

    def e_Attribute(self, n):
        dotted = self._dotted(n)
        if dotted and dotted.split(".")[0] not in self.locals and dotted in self.refs:
            return self.ref_value(dotted)
        base = self.expr(n.value)
        return self.attr(base, n.attr)

    def attr(self, base, name):
        t = base.t
        if t == REQUEST:
            if name == "data":
                return V(DATA)
            if name in ("query_params", "GET"):
                return V(QUERY)
            if name == "user":
                return V(USER)
            if name == "method":
                return self.let("ctx.method_py()", PyT("str"))
            raise Unsupported("request.%s" % name)
        if t == USER:
            if name in ("id", "pk"):
                if self.tr.user_pk_kind != "int":
                    raise Unsupported("user primary key isn't an integer")
                return self.let("ctx.user_pk()")
            if name in ("is_authenticated", "is_anonymous"):
                neg = "!" if name == "is_anonymous" else ""
                return self.let("Py::Bool(%sctx.user.is_some())" % neg, PyT("bool"))
            if name in ("is_staff", "is_superuser", "is_active"):
                return self.let("Py::Bool(ctx.user.as_ref().map_or(false, |u| u.%s))" % name, PyT("bool"))
            raise Unsupported("request.user.%s" % name)
        if t == DATA:
            raise Unsupported("request.data.%s" % name)
        if isinstance(t, RowT):
            return self.read_field(base, name)
        if isinstance(t, SerT):
            if name != "data":
                raise Unsupported("serializer.%s" % name)
            return base.extra()
        if isinstance(t, ModelClsT):
            if name == "DoesNotExist":
                return V(DoesNotExistT(t.label))
            raise Unsupported("Model.%s" % name)
        raise Unsupported("attribute .%s of a %s" % (name, _tname(t)))

    def read_field(self, row, name):
        m = self.model(row.t.label)
        f = m.field(name)
        if f.is_fk and name == f.name and f.name != f.attname:
            raise Unsupported("reading a related object (another query)")
        f.need()
        acc = "r.%s" % f.ident
        conv = {
            "int": "Py::from(x)" if f.rust_base != "u64" else "orm::read_u64(x)?",
            "str": "Py::Str(x.clone())",
            "bool": "Py::Bool(x)",
            "float": "Py::Float(x)",
            "json": "x.0.clone()",
        }.get(f.kind)
        if f.kind == "datetime":
            if f.null:
                raise Unsupported("reading a nullable datetime field")
            return self.let(
                "match &%s { Some(r) => r.%s.0, None => %s }" % (row.r, f.ident, self.fwd("attribute of None")),
                DATETIME,
            )
        if conv is None:
            raise Unsupported("reading a %s field as a value" % f.kind)
        copy = f.kind in ("int", "bool", "float")
        if f.null:
            inner = "match %s%s { Some(x) => %s, None => Py::None }" % ("" if copy else "&", acc, conv)
        else:
            inner = "{ let x = %s%s; %s }" % ("" if copy else "&", acc, conv)
        return self.let("match &%s { Some(r) => %s, None => %s }" % (row.r, inner, self.fwd("attribute of None")))

    def set_attr(self, target, v):
        if not isinstance(target.value, ast.Name):
            raise Unsupported("attribute assignment on an expression")
        row = self.expr(target.value)
        if not isinstance(row.t, RowT):
            raise Unsupported("attribute assignment on a %s" % _tname(row.t))
        m = self.model(row.t.label)
        name = target.attr
        f = m.by_name.get(name)
        if f is None or name == "pk" or f.primary_key:
            raise Unsupported("assigning %s on a model instance" % name)
        val = self.write_value(m, f, name, v)
        self.out.line(
            "match %s.as_mut() { Some(r) => r.%s = %s, None => %s }"
            % (row.r, f.ident, val, self.fwd("attribute assignment on None"))
        )

    # ---- values written to model fields ------------------------------------------

    def write_value(self, m, f, given_name, v):
        """Rust value of the field's entity type for `instance.<given_name> = v`."""
        f.need()
        if f.is_fk and given_name == f.name and f.name != f.attname:
            # Assigning a related object: only request.user, a row of the target, or None.
            if v.t == USER:
                if f.related_model != self.tr.schema.user_model:
                    raise Unsupported("assigning request.user to a relation to another model")
                val = "orm::w_int::<%s>(&ctx.user()?.pk)?" % f.rust_base
            elif isinstance(v.t, RowT):
                if v.t.label != f.related_model:
                    raise Unsupported("assigning an instance of another model to a relation")
                target = self.model(f.related_model)
                tf = next(x for x in target.fields if x.column == f.target_column)
                val = "%s.as_ref().map(|r| r.%s.clone())" % (v.r, tf.ident)
            elif v.const is None:
                val = "None"
            else:
                raise Unsupported("assigning a plain value to a relation")
        else:
            if f.kind in ("int", "str", "bool", "float", "json"):
                self.need_py(v)
            if f.kind == "int":
                val = "orm::w_int::<%s>(%s)?" % (f.rust_base, ref(v))
            elif f.kind == "str":
                ml = "Some(%d)" % f.max_length if f.max_length else "None"
                val = "orm::w_str(%s, %s)?" % (ref(v), ml)
            elif f.kind == "bool":
                val = "orm::w_bool(%s)?" % ref(v)
            elif f.kind == "float":
                val = "orm::w_float(%s)?" % ref(v)
            elif f.kind == "json":
                val = "{ let j = %s; if matches!(j, Py::None) { None } else { Some(DjJson(j)) } }" % self.own(v)
            elif f.kind == "datetime":
                if v.t == DATETIME:
                    val = "Some(DjDateTime(%s))" % v.r
                elif v.const is None:
                    val = "None"
                else:
                    raise Unsupported("assigning a non-datetime to a datetime field")
            else:
                raise Unsupported("writing a %s field" % f.kind)
        t = self.tmp()
        if f.null:
            self.out.line("let %s: %s = %s;" % (t, f.rust_type, val))
        else:
            self.out.line("let %s: %s = orm::not_null(%s)?;" % (t, f.rust_type, val))
        return t

    def default_value(self, f):
        """What Django puts in a field create() wasn't given."""
        if f.auto_now or f.auto_now_add:
            return self._wrap_null(f, "DjDateTime(now())")
        d = f.default
        if d.get("$none"):
            if f.kind == "str" and f.internal != "GenericIPAddressField" and not f.null:
                return '"".to_string()'
            if f.null:
                return "None"
            return "orm::not_null(None)?"
        if "value" in d:
            value = d["value"]
            lit = {
                "str": lambda x: "%s.to_string()" % raw_str(x) if isinstance(x, str) else None,
                "int": lambda x: str(x) if type(x) is int else None,
                "bool": lambda x: ("true" if x else "false") if type(x) is bool else None,
                "float": lambda x: repr(x) if type(x) is float else None,
            }.get(f.kind, lambda x: None)(value)
            if f.kind == "json":
                lit = "DjJson(%s)" % py_lit(value) if py_kind(value) else None
            if lit is None:
                raise Unsupported("default of field %s" % f.name)
            return self._wrap_null(f, lit)
        if "$callable" in d:
            c = d["$callable"]
            if f.kind == "json" and c == "builtins.dict":
                return self._wrap_null(f, "DjJson(Py::dict())")
            if f.kind == "json" and c == "builtins.list":
                return self._wrap_null(f, "DjJson(Py::List(Vec::new()))")
            if f.kind == "datetime" and c == "django.utils.timezone.now":
                return self._wrap_null(f, "DjDateTime(now())")
            raise Unsupported("default %s of field %s" % (c, f.name))
        raise Unsupported("default of field %s" % f.name)

    def _wrap_null(self, f, lit):
        return "Some(%s)" % lit if f.null else lit

    # ---- conditions -------------------------------------------------------------

    def cond(self, n):
        """Rust bool expression for Python truthiness of `n`."""
        if isinstance(n, ast.BoolOp):
            op = " && " if isinstance(n.op, ast.And) else " || "
            parts = [self.cond(n.values[0])]
            for v in n.values[1:]:
                saved = self.capture()
                c = self.cond(v)
                parts.append(self.block_expr(saved, c))
            return "(" + op.join(parts) + ")"
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            return "!(%s)" % self.cond(n.operand)
        if isinstance(n, ast.Compare):
            return self.compare(n)
        v = self.expr(n)
        return self.truthy(v)

    def truthy(self, v):
        t = v.t
        if isinstance(t, PyT):
            return "%s.truthy()" % v.r
        if isinstance(t, RowT):
            if self.model(t.label).raw.get("custom_bool"):
                raise Unsupported("truthiness of a model that defines __bool__/__len__")
            return "%s.is_some()" % v.r
        if isinstance(t, (QsT, ManagerT)):
            if isinstance(t, ManagerT):
                raise Unsupported("truthiness of a manager")
            q = self.select(self.as_qs(v))
            return self.let("%s.one(ctx.tx()).await?.is_some()" % q, PyT("bool")).r
        if isinstance(t, RowsT):
            return "!%s.is_empty()" % v.r
        if t == USER:
            return "true"
        if t == QUERY:
            return "!ctx.query.is_empty()"
        if t == DATETIME:
            return "true"
        raise Unsupported("truthiness of a %s" % _tname(t))

    def compare(self, n):
        operands = [n.left] + list(n.comparators)
        left = self.expr(operands[0])
        parts = []
        saved_stack = []
        for op, right_node in zip(n.ops, operands[1:]):
            if parts:
                saved_stack.append(self.capture())
            right = self.expr(right_node)
            parts.append(self.compare_one(op, left, right))
            left = right
        # Nest later comparisons inside blocks so their operands are evaluated lazily.
        expr = parts[-1]
        for i in range(len(parts) - 1, 0, -1):
            expr = self.block_expr(saved_stack[i - 1], expr)
            expr = "%s && %s" % (parts[i - 1], expr)
        return "(%s)" % expr

    def compare_one(self, op, a, b):
        if isinstance(op, (ast.Is, ast.IsNot)):
            if b.const is not None:
                raise Unsupported("'is' with something other than None")
            neg = isinstance(op, ast.IsNot)
            if isinstance(a.t, PyT):
                c = "matches!(%s, Py::None)" % a.r
            elif isinstance(a.t, RowT):
                c = "%s.is_none()" % a.r
            elif a.t in (USER, DATETIME):
                c = "false"
            else:
                raise Unsupported("'is None' on a %s" % _tname(a.t))
            return "!(%s)" % c if neg else c
        if isinstance(op, (ast.In, ast.NotIn)):
            neg = "!" if isinstance(op, ast.NotIn) else ""
            if b.t in (DATA, QUERY):
                if not isinstance(a.const, str):
                    raise Unsupported("'in request.data' with a non-constant key")
                if b.t == DATA:
                    return "%s%s" % (neg, self.let("ctx.data()?.contains(%s)?" % raw_str(a.const), PyT("bool")).r)
                return "%sctx.query.contains(%s)" % (neg, raw_str(a.const))
            self.need_py(a)
            self.need_py(b)
            return "%sops::contains(%s, %s)?" % (neg, ref(b), ref(a))
        if a.t == DATETIME and b.t == DATETIME:
            sym = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}[type(op)]
            return "(%s %s %s)" % (a.r, sym, b.r)
        self.need_py(a)
        self.need_py(b)
        if isinstance(op, ast.Eq):
            return "ops::eq(%s, %s)" % (ref(a), ref(b))
        if isinstance(op, ast.NotEq):
            return "!ops::eq(%s, %s)" % (ref(a), ref(b))
        cmp = {ast.Lt: "Lt", ast.LtE: "Le", ast.Gt: "Gt", ast.GtE: "Ge"}.get(type(op))
        if cmp is None:
            raise Unsupported("comparison %s" % type(op).__name__)
        return "ops::cmp(%s, %s, ops::Cmp::%s)?" % (ref(a), ref(b), cmp)

    def e_Compare(self, n):
        return self.let("Py::Bool(%s)" % self.compare(n), PyT("bool"))

    def e_BoolOp(self, n):
        # `a or b` is a if a is truthy, else b (b evaluated only then).
        first = self.py(n.values[0])
        acc = self.let(self.own(first))
        for v in n.values[1:]:
            saved = self.capture()
            nxt = self.py(v)
            rhs = self.block_expr(saved, self.own(nxt))
            if isinstance(n.op, ast.Or):
                expr = "if %s.truthy() { %s } else { %s }" % (acc.r, acc.r, rhs)
            else:
                expr = "if %s.truthy() { %s } else { %s }" % (acc.r, rhs, acc.r)
            acc = self.let(expr)
        return acc

    def e_UnaryOp(self, n):
        if isinstance(n.op, ast.Not):
            return self.let("Py::Bool(!(%s))" % self.cond(n.operand), PyT("bool"))
        v = self.py(n.operand)
        if isinstance(n.op, ast.USub):
            if isinstance(v.const, (int, float)) and not isinstance(v.const, bool):
                return V(PyT(py_kind(-v.const)), py_lit(-v.const), const=-v.const)
            return self.let("ops::neg(%s)?" % ref(v))
        if isinstance(n.op, ast.UAdd) and v.const is not NOCONST and type(v.const) in (int, float):
            return v
        raise Unsupported("unary operator %s" % type(n.op).__name__)

    def e_BinOp(self, n):
        return self.binop(n.op, self.expr(n.left), self.expr(n.right))

    def binop(self, op, a, b):
        if a.t == DATETIME and b.t == TIMEDELTA and isinstance(op, (ast.Add, ast.Sub)):
            m = "checked_add_signed" if isinstance(op, ast.Add) else "checked_sub_signed"
            return self.let(
                "match %s.%s(%s) { Some(d) => d, None => %s }" % (a.r, m, b.r, self.fwd("datetime overflow")),
                DATETIME,
            )
        self.need_py(a)
        self.need_py(b)
        fn = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul"}.get(type(op))
        if fn is None:
            raise Unsupported("operator %s" % type(op).__name__)
        kind = a.t.kind if a.t.kind == b.t.kind and a.t.kind in ("int", "str") else None
        return self.let("ops::%s(%s, %s)?" % (fn, ref(a), ref(b)), PyT(kind))

    def e_IfExp(self, n):
        c = self.cond(n.test)
        saved = self.capture()
        a = self.py(n.body)
        a_expr = self.block_expr(saved, self.own(a))
        saved = self.capture()
        b = self.py(n.orelse)
        b_expr = self.block_expr(saved, self.own(b))
        return self.let("if %s { %s } else { %s }" % (c, a_expr, b_expr))

    def e_Dict(self, n):
        items = []
        for k, v in zip(n.keys, n.values):
            if k is None:
                raise Unsupported("** in a dict display")
            kv = self.py(k)
            vv = self.py(v)
            items.append("(%s, %s)" % (self.own(kv), self.own(vv)))
        return self.let("ops::dict(vec![%s])?" % ", ".join(items), PyT("dict"))

    def e_List(self, n):
        items = [self.own(self.py(e)) for e in n.elts]
        return self.let("Py::List(vec![%s])" % ", ".join(items), PyT("list"))

    def e_Tuple(self, n):
        # A tuple is only ever rendered as JSON here (a list); any other use differs from a list.
        v = self.e_List(n)
        v.t = PyT("tuple")
        return v

    def e_JoinedStr(self, n):
        pieces = []
        for part in n.values:
            if isinstance(part, ast.Constant):
                pieces.append("s.push_str(%s);" % raw_str(part.value))
            elif isinstance(part, ast.FormattedValue):
                if part.format_spec is not None or part.conversion not in (-1, 115):
                    raise Unsupported("f-string format specs or !r")
                v = self.py(part.value)
                t = self.let("ops::str_string(%s)?" % ref(v))
                pieces.append("s.push_str(&%s);" % t.r)
            else:
                raise Unsupported("f-string part")
        return self.let("{ let mut s = String::new(); %s Py::Str(s) }" % " ".join(pieces), PyT("str"))

    def e_Subscript(self, n):
        base = self.expr(n.value)
        sl = _slice(n)
        if isinstance(base.t, (QsT, ManagerT)):
            return self.qs_subscript(self.as_qs(base), sl)
        if base.t == DELETE_RESULT:
            if isinstance(sl, ast.Constant) and sl.value == 0:
                return V(PyT("int"), base.r, place=True)
            raise Unsupported("indexing delete()'s result other than [0]")
        if isinstance(sl, ast.Slice):
            raise Unsupported("slicing a plain value")
        if base.t in (DATA, QUERY):
            key = self.expr(sl)
            if not isinstance(key.const, str):
                raise Unsupported("request.data[...] with a non-constant key")
            if base.t == DATA:
                return self.let("ctx.data()?.getitem(%s)?" % raw_str(key.const))
            return self.let(
                "match ctx.query.get(%s) { Some(s) => Py::from(s), None => %s }"
                % (raw_str(key.const), self.fwd("KeyError on request.GET")),
                PyT("str"),
            )
        self.need_py(base)
        if base.t.kind == "tuple":
            raise Unsupported("indexing a tuple")
        key = self.py(sl)
        return self.let("ops::getitem(%s, %s)?" % (ref(base), ref(key)))

    def e_Call(self, n):
        if any(isinstance(a, ast.Starred) for a in n.args) or any(k.arg is None for k in n.keywords):
            raise Unsupported("*args or **kwargs in a call")
        func = n.func
        if isinstance(func, ast.Attribute):
            dotted = self._dotted(func)
            if not (dotted and dotted.split(".")[0] not in self.locals and dotted in self.refs):
                base = self.expr(func.value)
                return self.method(base, func.attr, n)
        callee = self.expr(func)
        return self.call(callee, n)

    def args(self, n):
        pos = [self.expr(a) for a in n.args]
        kw = {}
        for k in n.keywords:
            kw[k.arg] = self.expr(k.value)
        return pos, kw

    # ---- calls --------------------------------------------------------------------

    def call(self, callee, n):
        t = callee.t
        if t == RESPONSE_CLS:
            pos, kw = self.args(n)
            return self.response(pos, kw)
        if isinstance(t, SerClsT):
            return self.serializer(t.path, n)
        if isinstance(t, Named) and t.what == "exception class":
            pos, kw = self.args(n)
            return self.exception(t.name, pos, kw)
        if isinstance(t, Named) and t.what == "builtin":
            return self.builtin(t.name, n)
        if t == NOW_FN:
            if n.args or n.keywords:
                raise Unsupported("timezone.now() with arguments")
            return self.let("now()", DATETIME)
        if t == TIMEDELTA_CLS:
            if n.args:
                raise Unsupported("timedelta() with positional arguments")
            total = 0
            for k in n.keywords:
                if k.arg not in TIMEDELTA_UNITS or not isinstance(k.value, ast.Constant) or type(k.value.value) is not int:
                    raise Unsupported("timedelta() with non-integer or unknown arguments")
                total += k.value.value * TIMEDELTA_UNITS[k.arg]
            if abs(total) >= 2**62:
                raise Unsupported("timedelta beyond range")
            return V(TIMEDELTA, "chrono::Duration::microseconds(%d)" % total)
        if isinstance(t, ModelClsT):
            raise Unsupported("constructing a model instance directly")
        raise Unsupported("calling a %s" % _tname(t))

    def builtin(self, name, n):
        pos, kw = self.args(n)
        if kw or len(pos) != 1:
            raise Unsupported("%s() with these arguments" % name)
        (x,) = pos
        if name == "str":
            return self.let("ops::str_(%s)?" % ref(self.need_py(x)), PyT("str"))
        if name == "int":
            return self.let("ops::int_(%s)?" % ref(self.need_py(x)), PyT("int"))
        if name == "bool":
            return self.let("Py::Bool(%s)" % self.truthy(x), PyT("bool"))
        if name == "len":
            if isinstance(x.t, QsT):
                return self.qs_count(x)
            if isinstance(x.t, RowsT):
                return self.let("Py::Int(%s.len() as i64)" % x.r, PyT("int"))
            return self.let("ops::len_(%s)?" % ref(self.need_py(x)), PyT("int"))
        if name == "list":
            if isinstance(x.t, (QsT, ManagerT)):
                qs = self.as_qs(x)
                return self.let(self.qs_rows(qs), RowsT(qs.t.label))
            if isinstance(x.t, RowsT):
                return x
            return self.let("Py::List(ops::iter(%s)?)" % ref(self.need_py(x)), PyT("list"))
        raise Unsupported("builtin %s()" % name)

    def response(self, pos, kw):
        names = ["data", "status"]
        args = dict(zip(names, pos))
        if len(pos) > 2:
            raise Unsupported("Response() with template_name/headers")
        for k, v in kw.items():
            if k not in names or k in args:
                raise Unsupported("Response(%s=...)" % k)
            args[k] = v
        status = 200
        if "status" in args:
            s = args["status"]
            if s.const is None:
                status = 200
            elif type(s.const) is int and 100 <= s.const <= 599:
                status = s.const
            else:
                raise Unsupported("Response status that isn't a constant")
        data = args.get("data")
        if data is None or data.const is None:
            return V(RESPONSE, "DrfResponse::empty(%d)" % status)
        self.need_py(data, "Response data")
        return V(RESPONSE, "DrfResponse::data(%s).with_status(%d)" % (self.own(data), status))

    def exception(self, name, pos, kw):
        detail = pos[0] if pos else kw.pop("detail", None)
        kw.pop("code", None)
        if len(pos) > 2 or kw:
            raise Unsupported("exception arguments")
        msgs = self.tr.data.get("drf_messages") or {}
        if name in ("Http404", "DjangoPermissionDenied"):
            if pos or detail is not None:
                # DRF versions differ on whether the message is kept.
                raise Unsupported("Http404/PermissionDenied with a message")
            name = "NotFound" if name == "Http404" else "PermissionDenied"
        m = msgs.get(name)
        if not m:
            raise Unsupported("%s's default message isn't known" % name)
        d = "None" if detail is None or detail.const is None else "Some(%s)" % self.own(self.need_py(detail))
        if name == "ValidationError":
            return V(EXC, "ApiError::validation(%s, %s)" % (d, raw_str(m["detail"])))
        return V(EXC, "ApiError::raised(%d, %s, %s)" % (m["status"], d, raw_str(m["detail"])))

    def serializer(self, path, n):
        pos, kw = self.args(n)
        if len(pos) > 1:
            raise Unsupported("serializer with data")
        inst = pos[0] if pos else kw.pop("instance", None)
        many = kw.pop("many", None)
        kw.pop("context", None)
        kw.pop("partial", None)
        if kw:
            raise Unsupported("serializer(%s=...)" % ", ".join(sorted(kw)))
        if many is not None and type(many.const) is not bool:
            raise Unsupported("serializer many= that isn't a constant")
        many = bool(many and many.const)
        if inst is None:
            raise Unsupported("serializer without an instance")
        label = self.tr.serializers.model_of(path)
        fn = self.tr.ser_fn(path)
        if many:
            if isinstance(inst.t, (QsT, ManagerT)):
                qs = self.as_qs(inst)
                if qs.t.label != label:
                    raise Unsupported("serializer of another model's rows")
                rows = self.qs_rows(qs)
            elif isinstance(inst.t, RowsT):
                if inst.t.label != label:
                    raise Unsupported("serializer of another model's rows")
                rows = inst.r
            else:
                raise Unsupported("serializer many=True over a %s" % _tname(inst.t))
            expr = "Py::List(%s.iter().map(%s).collect::<Result<Vec<_>, _>>()?)" % (rows, fn)
        else:
            if not isinstance(inst.t, RowT) or inst.t.label != label:
                raise Unsupported("serializer over a %s" % _tname(inst.t))
            expr = "match &%s { Some(r) => %s(r)?, None => %s }" % (
                inst.r, fn, self.fwd("serializer of None returns initial data"))

        def data():
            return self.let(expr, PyT("dict" if not many else "list"))

        return V(SerT(path, many), extra=data)

    # ---- methods -------------------------------------------------------------------

    def method(self, base, name, n):
        t = base.t
        if isinstance(t, (ManagerT, QsT)):
            return self.qs_method(base, name, n)
        if isinstance(t, RowT):
            return self.row_method(base, name, n)
        if t in (DATA, QUERY):
            if name != "get":
                raise Unsupported("request.data.%s()" % name)
            pos, kw = self.args(n)
            if kw or not 1 <= len(pos) <= 2 or not isinstance(pos[0].const, str):
                raise Unsupported("request.data.get() with a non-constant key")
            key = raw_str(pos[0].const)
            default = self.own(self.need_py(pos[1])) if len(pos) == 2 else "Py::None"
            if t == DATA:
                return self.let("ctx.data()?.get_or(%s, %s)?" % (key, default))
            return self.let("match ctx.query.get(%s) { Some(s) => Py::from(s), None => %s }" % (key, default))
        if isinstance(t, PyT) and name == "append":
            pos, kw = self.args(n)
            target = getattr(base, "var", None)
            if kw or len(pos) != 1 or target is None:
                raise Unsupported(".append() on a temporary or with these arguments")
            if target in self.iterating:
                raise Unsupported("appending to the list being iterated")
            self.out.line("ops::append(&mut %s, %s)?;" % (self.var(target), self.own(self.need_py(pos[0]))))
            return V(PyT("none"), "Py::None", const=None)
        if isinstance(t, PyT):
            if name == "get":
                pos, kw = self.args(n)
                if kw or not 1 <= len(pos) <= 2:
                    raise Unsupported("dict.get() arguments")
                default = self.own(self.need_py(pos[1])) if len(pos) == 2 else "Py::None"
                return self.let("ops::dict_get(%s, %s, %s)?" % (ref(base), ref(self.need_py(pos[0])), default))
            raise Unsupported("method .%s() on a plain value" % name)
        raise Unsupported("method .%s() on a %s" % (name, _tname(t)))

    # ---- querysets ------------------------------------------------------------------

    def as_qs(self, v):
        if isinstance(v.t, ManagerT):
            self.model(v.t.label).need_entity()
            return V(QsT(v.t.label), "Condition::all()", extra="None")
        return v

    def select(self, qs, order=False):
        m = self.model(qs.t.label)
        q = "%s::Entity::find().filter(%s)" % (m.entity, qs.r)
        if order:
            q = "orm::order_by(%s, %s, %s)" % (q, qs.extra, self.default_order(qs))
        if qs.t.sliced():
            lo = qs.t.lo or 0
            if lo:
                q += ".offset(%d)" % lo
            if qs.t.hi is not None:
                q += ".limit(%d)" % max(0, qs.t.hi - lo)
        return q

    def default_order(self, qs):
        """Meta.ordering as a Rust slice, needed unless order_by() was called."""
        if qs.extra.startswith("orm::order("):
            return "&[]"
        return _terms(self.model(qs.t.label).ordering_terms())

    def qs_rows(self, qs):
        return self.let("%s.all(ctx.tx()).await?" % self.select(qs, order=True), RowsT(qs.t.label)).r

    def qs_count(self, qs):
        if qs.t.sliced():
            raise Unsupported("count() on a sliced queryset")
        return self.let("Py::Int(PaginatorTrait::count(%s, ctx.tx()).await? as i64)" % self.select(qs), PyT("int"))

    def qs_subscript(self, qs, sl):
        t = qs.t
        if t.sliced():
            raise Unsupported("slicing a sliced queryset")
        if isinstance(sl, ast.Slice):
            if sl.step is not None:
                raise Unsupported("queryset slice with a step")
            bounds = []
            for b in (sl.lower, sl.upper):
                if b is None:
                    bounds.append(None)
                elif isinstance(b, ast.Constant) and type(b.value) is int and b.value >= 0:
                    bounds.append(b.value)
                else:
                    raise Unsupported("queryset slice bounds that aren't non-negative constants")
            lo, hi = bounds
            if hi is None and lo:
                raise Unsupported("open-ended queryset slice with an offset")
            return V(QsT(t.label, lo or 0, hi, t.distinct), qs.r, extra=qs.extra)
        if isinstance(sl, ast.Constant) and type(sl.value) is int and sl.value >= 0:
            q = self.select(V(QsT(t.label, sl.value, sl.value + 1), qs.r, extra=qs.extra), order=True)
            return self.let(
                "match %s.one(ctx.tx()).await? { Some(r) => Some(r), None => %s }" % (q, self.fwd("IndexError on a queryset")),
                RowT(t.label),
            )
        raise Unsupported("queryset index that isn't a non-negative constant")

    def qs_method(self, base, name, n):
        qs = self.as_qs(base)
        t = qs.t
        m = self.model(t.label)
        if name in ("all", "select_related", "prefetch_related"):
            for a in n.args:
                if not (isinstance(a, ast.Constant) and isinstance(a.value, str)):
                    raise Unsupported("%s() with non-constant arguments" % name)
            if n.keywords:
                raise Unsupported("%s() keywords" % name)
            return qs
        if name == "distinct":
            if n.args or n.keywords:
                raise Unsupported("distinct() on fields")
            return V(QsT(t.label, t.lo, t.hi, True), qs.r, extra=qs.extra)
        if name in ("filter", "exclude"):
            if t.sliced():
                raise Unsupported("filtering a sliced queryset")
            if n.args:
                raise Unsupported("Q objects in %s()" % name)
            parts = []
            for k in n.keywords:
                val = self.expr(k.value)
                parts.append(self.lookup(m, k.arg, val))
            if not parts:
                return qs
            if name == "filter":
                cond = qs.r
                for expr, _ in parts:
                    cond = "%s.add(%s)" % (cond, expr)
            else:
                cond = "%s.add(orm::exclude(vec![%s]))" % (
                    qs.r,
                    ", ".join("(%s, %s)" % (e, "Some(%s)" % raw_str(c) if c else "None") for e, c in parts),
                )
            c = self.let(cond, prefix="c")
            return V(QsT(t.label, t.lo, t.hi, t.distinct), "%s.clone()" % c.r, extra=qs.extra)
        if name == "order_by":
            if t.sliced():
                raise Unsupported("ordering a sliced queryset")
            terms = []
            for a in n.args:
                if not (isinstance(a, ast.Constant) and isinstance(a.value, str)):
                    raise Unsupported("order_by() with non-constant arguments")
                s = a.value
                if s == "?" or "__" in s.lstrip("-"):
                    raise Unsupported("order_by(%r)" % s)
                f = m.field(s.lstrip("-"))
                if f.is_fk and s.lstrip("-") == f.name and f.name != f.attname:
                    raise Unsupported("ordering by a relation orders by the related model's ordering")
                terms.append((f.column, s.startswith("-")))
            return V(QsT(t.label, None, None, t.distinct), qs.r, extra="orm::order(%s)" % _terms(terms))
        pos, kw = self.args(n) if name not in ("get", "update", "create") else ([], {})
        if name == "count":
            self._no_args(n, name)
            return self.qs_count(qs)
        if name == "exists":
            self._no_args(n, name)
            if t.sliced():
                raise Unsupported("exists() on a sliced queryset")
            return self.let("Py::Bool(%s.one(ctx.tx()).await?.is_some())" % self.select(qs), PyT("bool"))
        if name in ("first", "last"):
            self._no_args(n, name)
            if t.sliced():
                raise Unsupported("%s() on a sliced queryset" % name)
            q = "orm::order_first(%s::Entity::find().filter(%s), %s, %s, %s, %s)" % (
                m.entity, qs.r, qs.extra, self.default_order(qs), raw_str(m.pk.column),
                "true" if name == "last" else "false",
            )
            return self.let("%s.one(ctx.tx()).await?" % q, RowT(t.label))
        if name == "get":
            if t.sliced():
                raise Unsupported("get() on a sliced queryset")
            if n.args:
                raise Unsupported("get() with Q objects")
            cond = qs.r
            for k in n.keywords:
                expr, _ = self.lookup(m, k.arg, self.expr(k.value))
                cond = "%s.add(%s)" % (cond, expr)
            rows = self.let("%s::Entity::find().filter(%s).limit(2).all(ctx.tx()).await?" % (m.entity, cond), RowsT(t.label))
            self.out.line("if %s.len() > 1 { %s; }" % (rows.r, self.fwd("get() matched several rows")))
            handler = self.find_handler(t.label)
            if handler is None:
                missing = self.fwd("DoesNotExist (a 500)")
            else:
                missing = self.handler_code(handler)
            return self.let(
                "match %s.into_iter().next() { Some(r) => Some(r), None => %s }" % (rows.r, missing),
                RowT(t.label),
            )
        if name == "delete":
            self._no_args(n, name)
            if t.sliced() or t.distinct:
                raise Unsupported("delete() on a sliced or distinct queryset")
            fn = self.tr.deleter(t.label)
            ids = self.let(
                "%s.all(ctx.tx()).await?.into_iter().map(|r| r.%s).collect::<Vec<_>>()" % (self.select(qs), m.pk.ident),
                OPAQUE,
            )
            return self.let("Py::Int(%s(ctx.tx(), %s).await? as i64)" % (fn, ids.r), DELETE_RESULT)
        if name == "update":
            if t.sliced() or t.distinct:
                raise Unsupported("update() on a sliced or distinct queryset")
            if n.args:
                raise Unsupported("update() with positional arguments")
            return self.qs_update(qs, m, n)
        if name == "create":
            if not isinstance(base.t, (ManagerT, QsT)) or t.sliced():
                raise Unsupported("create() here")
            return self.create(m, n)
        raise Unsupported("queryset .%s()" % name)

    def _no_args(self, n, name):
        if n.args or n.keywords:
            raise Unsupported("%s() with arguments" % name)

    def find_handler(self, label):
        for labels, body in reversed(self.catch):
            if labels is None or label in labels:
                return body
        return None

    def handler_code(self, body):
        """The except clause's code, inline where DoesNotExist is raised. It always diverges."""
        state = (set(self.assigned), set(self.deleted), self.catch)
        # The handler runs outside the try: its own .get() calls aren't caught by it.
        idx = next(i for i in range(len(self.catch) - 1, -1, -1) if self.catch[i][1] is body)
        self.catch = self.catch[:idx]
        saved = self.capture()
        div = self.block(body)
        code = self.block_expr(saved, "")
        self.assigned, self.deleted, self.catch = state
        if not div:
            raise Unsupported("except clause that doesn't return or raise")
        return code

    def lookup(self, m, key, val):
        """A filter keyword -> (Rust SimpleExpr, nullable column for exclude())."""
        parts = key.split("__")
        lookup = "exact"
        if len(parts) > 1 and parts[-1] in LOOKUPS:
            lookup = parts.pop()
        f = m.field(parts[0])
        if len(parts) == 2 and f.is_fk:
            target = self.model(f.related_model)
            tf = next((x for x in target.fields if x.column == f.target_column), None)
            if tf is None or parts[1] not in (tf.name, "pk" if tf.primary_key else None):
                raise Unsupported("lookups across relations (joins)")
            relation_value = False
        elif len(parts) > 1:
            raise Unsupported("lookups across relations (joins)" if f.is_fk else "lookup %s" % key)
        else:
            relation_value = f.is_fk and parts[0] == f.name and f.name != f.attname
        f.need()
        col = raw_str(f.column)
        nullable = f.column if f.null and lookup != "isnull" else None
        if lookup == "isnull":
            if type(val.const) is not bool:
                raise Unsupported("isnull with a non-constant")
            return ("orm::col(%s).%s()" % (col, "is_null" if val.const else "is_not_null"), None)
        if relation_value and val.t in (USER,) or (relation_value and isinstance(val.t, RowT)):
            if lookup != "exact":
                raise Unsupported("%s lookup on a relation" % lookup)
            if val.t == USER:
                if f.related_model != self.tr.schema.user_model:
                    raise Unsupported("comparing a relation to another model with request.user")
                v = self.let(
                    "match ctx.user_pk_value() { Some(v) => v, None => %s }" % self.fwd("lookup by AnonymousUser raises"),
                    OPAQUE,
                )
                return ("orm::exact(%s, Some(%s.clone()))" % (col, v.r), nullable)
            if val.t.label != f.related_model:
                raise Unsupported("comparing a relation with another model's instance")
            target = self.model(f.related_model)
            tf = next(x for x in target.fields if x.column == f.target_column)
            v = self.let("%s.as_ref().map(|r| r.%s.clone())" % (val.r, tf.ident), OPAQUE)
            return ("orm::exact(%s, %s.clone())" % (col, v.r), nullable)
        if val.t == DATETIME:
            if f.kind != "datetime":
                raise Unsupported("datetime compared with a %s field" % f.kind)
            op = {"exact": "eq"}.get(lookup) or CMP.get(lookup)
            if op is None:
                raise Unsupported("%s lookup with a datetime" % lookup)
            return ("orm::col(%s).%s(DjDateTime(%s))" % (col, op, val.r), nullable)
        if relation_value:
            if val.const is None:
                return ("orm::col(%s).is_null()" % col, None)
            raise Unsupported("comparing a relation with a plain value")
        self.need_py(val, "lookup value")
        kind = f.kind
        if lookup == "in":
            conv = {"int": "orm::py_int(x)", "str": "orm::py_char(x)", "bool": "orm::lk_bool(x)"}.get(kind)
            if conv is None:
                raise Unsupported("__in on a %s field" % kind)
            v = self.let(
                "orm::lk_list(%s)?.iter().map(|x| %s.and_then(|o| orm::not_null(o))).collect::<Result<Vec<_>, Stop>>()?"
                % (ref(val), conv),
                OPAQUE,
            )
            return ("orm::is_in(%s, %s.clone())" % (col, v.r), nullable)
        if lookup == "exact":
            conv = {"int": "orm::py_int", "str": "orm::py_char", "bool": "orm::lk_bool"}.get(kind)
            if conv is None:
                raise Unsupported("exact lookup on a %s field with a plain value" % kind)
            v = self.let("%s(%s)?" % (conv, ref(val)), OPAQUE)
            # exact None is `IS NULL`: no extra NOT NULL in exclude().
            return ("orm::exact(%s, %s.clone())" % (col, v.r), nullable if val.const is not None else None)
        if lookup in LIKE:
            if kind != "str":
                raise Unsupported("%s on a %s field" % (lookup, kind))
            if lookup == "iexact":
                v = self.let("orm::py_char(%s)?" % ref(val), OPAQUE)
                return (
                    "match &%s { Some(s) => orm::like(%s, orm::Like::IExact, s), None => orm::col(%s).is_null() }"
                    % (v.r, col, col),
                    nullable,
                )
            v = self.let("orm::lk_pattern(%s)?" % ref(val), OPAQUE)
            return ("orm::like(%s, orm::Like::%s, &%s)" % (col, LIKE[lookup], v.r), nullable)
        if lookup in CMP:
            if kind == "int":
                v = self.let("orm::lk_int_strict(%s)?" % ref(val), OPAQUE)
            elif kind == "str":
                v = self.let("orm::not_null(orm::py_char(%s)?)?" % ref(val), OPAQUE)
            else:
                raise Unsupported("%s on a %s field" % (lookup, kind))
            return ("orm::col(%s).%s(%s.clone())" % (col, CMP[lookup], v.r), nullable)
        raise Unsupported("lookup %s" % lookup)

    # ---- writes ---------------------------------------------------------------------

    def create(self, m, n):
        self.tr.schema.check_writable(m, "create()")
        if n.args:
            raise Unsupported("create() with positional arguments")
        given = {}
        for k in n.keywords:
            v = self.expr(k.value)
            f = m.by_name.get(k.arg)
            if f is None or k.arg == "pk":
                raise Unsupported("create() with %s" % k.arg)
            if f.ident in given:
                raise Unsupported("create() given a field twice")
            if f.primary_key:
                raise Unsupported("create() with an explicit primary key")
            if f.auto_now or f.auto_now_add:
                given[f.ident] = None  # pre_save overwrites it
                continue
            given[f.ident] = self.write_value(m, f, k.arg, v)
        if m.pk.internal not in ("AutoField", "BigAutoField", "SmallAutoField"):
            raise Unsupported("create() on a model whose key isn't auto-incremented")
        values = {}
        for f in m.fields:
            if f.primary_key:
                continue
            if given.get(f.ident):
                values[f.ident] = given[f.ident]
            else:
                t = self.tmp()
                self.out.line("let %s: %s = %s;" % (t, f.rust_type, self.default_value(f)))
                values[f.ident] = t
        am = ", ".join(["%s: NotSet" % m.pk.ident] + ["%s: Set(%s.clone())" % (k, v) for k, v in values.items()])
        pk = self.let("%s::Entity::insert(%s::ActiveModel { %s }).exec(ctx.tx()).await?.last_insert_id" % (m.entity, m.entity, am), OPAQUE)
        fields = ", ".join(["%s: %s" % (m.pk.ident, pk.r)] + ["%s: %s" % (k, v) for k, v in values.items()])
        return self.let("Some(%s::Model { %s })" % (m.entity, fields), RowT(m.label))

    def row_method(self, row, name, n):
        m = self.model(row.t.label)
        if name == "save":
            self.tr.schema.check_writable(m, "save()")
            if n.args:
                raise Unsupported("save() with positional arguments")
            fields = [f for f in m.fields if not f.primary_key]
            for k in n.keywords:
                if k.arg != "update_fields":
                    raise Unsupported("save(%s=...)" % k.arg)
                if not isinstance(k.value, (ast.List, ast.Tuple)) or not all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str) for e in k.value.elts
                ):
                    raise Unsupported("save(update_fields=...) that isn't a constant list")
                names = [e.value for e in k.value.elts]
                chosen = []
                for nm in names:
                    f = m.field(nm)
                    if f.primary_key:
                        raise Unsupported("update_fields with the primary key")
                    if f not in chosen:
                        chosen.append(f)
                fields = [f for f in fields if f in chosen]
                if not fields:
                    return V(PyT("none"), "Py::None", const=None)
            self.out.line("{")
            self.out.indent += 1
            self.out.line(
                "let r = match %s.as_mut() { Some(r) => r, None => %s };" % (row.r, self.fwd("save() on None"))
            )
            for f in fields:
                if f.auto_now:
                    self.out.line("r.%s = %s;" % (f.ident, self._wrap_null(f, "DjDateTime(now())")))
            sets = ", ".join("%s: Set(r.%s.clone())" % (f.ident, f.ident) for f in fields)
            self.out.line(
                "let n = %s::Entity::update_many().set(%s::ActiveModel { %s, ..Default::default() }).filter(orm::exact(%s, Some(r.%s.clone()))).exec(ctx.tx()).await?.rows_affected;"
                % (m.entity, m.entity, sets, raw_str(m.pk.column), m.pk.ident)
            )
            self.out.line("if n == 0 { %s; }" % self.fwd("save() matched no row: Django would INSERT"))
            self.out.close()
            return V(PyT("none"), "Py::None", const=None)
        if name == "delete":
            self._no_args(n, name)
            if m.raw.get("custom_delete"):
                raise Unsupported("the model overrides delete()")
            fn = self.tr.deleter(m.label)
            res = self.let(
                "match &%s { Some(r) => Py::Int(%s(ctx.tx(), vec![r.%s.clone()]).await? as i64), None => %s }"
                % (row.r, fn, m.pk.ident, self.fwd("delete() on None")),
                DELETE_RESULT,
            )
            if row.extra:
                self.deleted.add(row.extra)  # Django sets the instance's pk to None
            return res
        raise Unsupported("instance method .%s()" % name)

    def qs_update(self, qs, m, n):
        m.need_entity()
        sets = []
        for k in n.keywords:
            v = self.expr(k.value)
            f = m.by_name.get(k.arg)
            if f is None or f.primary_key or k.arg == "pk":
                raise Unsupported("update(%s=...)" % k.arg)
            f.need()
            if f.custom:
                raise Unsupported("update() of a custom field")
            # update() converts like get_prep_value; it doesn't touch instances.
            if f.is_fk and k.arg == f.name and f.name != f.attname:
                val = self.write_value(m, f, k.arg, v)
            elif f.kind == "int":
                t = self.let("orm::py_int(%s)?" % ref(self.need_py(v)), OPAQUE)
                val = self._update_val(f, "%s.map(|x| %s::try_from(x)).transpose().map_err(|_| Stop::Forward(\"int out of range\"))?" % (t.r, f.rust_base))
            elif f.kind == "str":
                t = self.let("orm::py_char(%s)?" % ref(self.need_py(v)), OPAQUE)
                val = self._update_val(f, t.r)
            elif f.kind == "bool":
                t = self.let("orm::lk_bool(%s)?" % ref(self.need_py(v)), OPAQUE)
                val = self._update_val(f, t.r)
            elif f.kind in ("datetime", "json"):
                val = self.write_value(m, f, k.arg, v)
            else:
                raise Unsupported("update() of a %s field" % f.kind)
            sets.append("%s: Set(%s.clone())" % (f.ident, val))
        if not sets:
            raise Unsupported("update() with nothing to set")
        return self.let(
            "Py::Int(%s::Entity::update_many().set(%s::ActiveModel { %s, ..Default::default() }).filter(%s).exec(ctx.tx()).await?.rows_affected as i64)"
            % (m.entity, m.entity, ", ".join(sets), qs.r),
            PyT("int"),
        )

    def _update_val(self, f, opt_expr):
        t = self.tmp()
        if f.null:
            self.out.line("let %s: %s = %s;" % (t, f.rust_type, opt_expr))
        else:
            self.out.line("let %s: %s = orm::not_null(%s)?;" % (t, f.rust_type, opt_expr))
        return t


def _terms(terms):
    return "&[%s]" % ", ".join("(%s, %s)" % (raw_str(c), "true" if d else "false") for c, d in terms)


def _slice(n):
    s = n.slice
    if type(s).__name__ == "Index":  # Python 3.8
        return s.value
    return s


def _tname(t):
    if isinstance(t, Simple):
        return t.name
    if isinstance(t, Named):
        return "%s %s" % (t.what, t.name)
    if isinstance(t, ModelT):
        return "%s(%s)" % (type(t).__name__, t.label)
    return type(t).__name__
