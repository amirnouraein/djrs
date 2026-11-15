"""Unit tests for the translator: what it accepts, what it refuses, and why.

Standard library only: python -m unittest discover tests. End-to-end parity
(the Rust answering exactly like Django) is checked by scripts/e2e.sh.
"""

import os
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from djrs import translate  # noqa: E402


def _field(name, internal, **kw):
    f = {
        "name": name,
        "attname": kw.pop("attname", name),
        "column": kw.pop("column", name),
        "class": kw.pop("cls", "django.db.models.fields.%s" % internal),
        "internal_type": internal,
        "concrete": True,
        "null": False,
        "primary_key": False,
        "max_length": None,
        "default": {"$none": True},
        "is_relation": False,
    }
    f.update(kw)
    return f


def _model(label, table, fields, **kw):
    m = {
        "label": label,
        "db_table": table,
        "fields": fields,
        "ordering": [],
        "default_manager": "django.db.models.manager.Manager",
        "parents": [],
        "proxy": False,
    }
    m.update(kw)
    return m


USER = _model(
    "auth.User",
    "auth_user",
    [_field("id", "AutoField", primary_key=True), _field("username", "CharField", max_length=150)],
)
ITEM = _model(
    "demo.Item",
    "demo_item",
    [
        _field("id", "AutoField", primary_key=True),
        _field("name", "CharField", max_length=10),
        _field("count", "IntegerField", default={"value": 0}),
        _field("note", "CharField", max_length=20, null=True),
        _field(
            "owner",
            "ForeignKey",
            attname="owner_id",
            column="owner_id",
            cls="django.db.models.fields.related.ForeignKey",
            is_relation=True,
            many_to_one=True,
            related_model="auth.User",
            target_column="id",
            on_delete="CASCADE",
        ),
    ],
    ordering=["-id"],
)

REFS = {
    "api_view": {"kind": "function", "path": "rest_framework.decorators.api_view"},
    "Response": {"kind": "class", "path": "rest_framework.response.Response", "mro": []},
    "Item": {"kind": "model", "label": "demo.Item", "path": "demo.models.Item"},
    "Item.objects": {"kind": "object", "type": "django.db.models.manager.Manager"},
    "Item.DoesNotExist": {"kind": "model_exception", "exception": "DoesNotExist", "label": "demo.Item"},
    "ItemSerializer": {
        "kind": "class",
        "path": "demo.serializers.ItemSerializer",
        "mro": ["rest_framework.serializers.ModelSerializer", "rest_framework.serializers.BaseSerializer"],
        "project": True,
    },
    "len": {"kind": "builtin", "name": "len"},
    "Exception": {"kind": "builtin", "name": "Exception"},
    "str": {"kind": "builtin", "name": "str"},
    "helper": {"kind": "function", "path": "demo.views.helper", "project": True},
    "send_mail_task": {"kind": "celery_task", "path": "demo.tasks.send_mail_task"},
}

SERIALIZER = {
    "path": "demo.serializers.ItemSerializer",
    "mro": [
        "demo.serializers.ItemSerializer",
        "rest_framework.serializers.ModelSerializer",
        "rest_framework.serializers.Serializer",
        "rest_framework.serializers.BaseSerializer",
        "rest_framework.fields.Field",
        "builtins.object",
    ],
    "source": "class ItemSerializer(serializers.ModelSerializer):\n    class Meta:\n        model = Item\n        fields = ['id', 'name']\n",
    "serializer": {
        "meta": {"model": "demo.Item"},
        "fields": [
            {"name": "id", "class": "rest_framework.fields.IntegerField", "source": "id", "write_only": False},
            {"name": "name", "class": "rest_framework.fields.CharField", "source": "name", "write_only": False},
        ],
    },
}


def _ir(views):
    out = {
        "databases": {"default": {"vendor": "sqlite"}},
        "settings": {"USE_TZ": True, "TIME_ZONE": "UTC"},
        "drf_settings": {"DATETIME_FORMAT": "iso-8601"},
        "drf_messages": {"NotFound": {"detail": "Not found.", "status": 404}},
        "auth": {"user": {"model": "auth.User", "pk_type": "AutoField"}},
        "models": [USER, ITEM],
        "signals": {},
        "classes": {SERIALIZER["path"]: SERIALIZER},
        "views": {},
    }
    for name, src in views.items():
        out["views"]["demo.views." + name] = {
            "kind": "drf_function",
            "methods": ["GET", "POST"],
            "function": {"source": textwrap.dedent(src), "refs": REFS, "decorators": [], "file": "demo/views.py", "line": 1},
        }
    return out


def run(src):
    """Translate one view; (rust source or None, reason or None)."""
    r = translate.translate(_ir({"v": src}))
    if "demo.views.v" in r.handlers:
        return r.source, None
    return None, r.reasons["demo.views.v"][0]


class AcceptsTests(unittest.TestCase):
    def test_queryset_and_serializer(self):
        src, why = run(
            """
            @api_view(["GET"])
            def v(request):
                items = Item.objects.filter(owner=request.user)
                q = request.query_params.get("q")
                if q:
                    items = items.filter(name__icontains=q).order_by("name")
                return Response({"items": ItemSerializer(items, many=True).data, "n": items.count()})
            """
        )
        self.assertIsNone(why)
        self.assertIn("orm::like(r#\"name\"#, orm::Like::IContains", src)
        # Ordering is decided at run time: order_by() happens only on one branch.
        self.assertIn('orm::order_by(e::demo_item::Entity::find().filter(v_items.clone()), o_items, &[(r#"id"#, true)])', src)
        self.assertIn("fn ser_demo_serializers_ItemSerializer", src)

    def test_get_with_except_inlines_the_handler(self):
        src, why = run(
            """
            @api_view(["GET"])
            def v(request, pk):
                try:
                    item = Item.objects.get(pk=pk)
                except Item.DoesNotExist:
                    return Response({"error": "missing"}, status=404)
                return Response({"name": item.name})
            """
        )
        self.assertIsNone(why)
        self.assertIn(".limit(2)", src)
        self.assertIn("with_status(404)", src)

    def test_list_built_then_returned(self):
        _, why = run(
            """
            @api_view(["GET"])
            def v(request):
                out = []
                for item in Item.objects.all():
                    out.append({"label": f"{item.name}: {item.count}"})
                return Response({"out": out, "n": len(out)})
            """
        )
        self.assertIsNone(why)

    def test_exclude_keeps_nulls(self):
        src, why = run(
            """
            @api_view(["GET"])
            def v(request):
                return Response({"n": Item.objects.exclude(note="x").count()})
            """
        )
        self.assertIsNone(why)
        self.assertIn('Some(r#"note"#)', src)

    def test_output_is_deterministic(self):
        view = """
            @api_view(["POST"])
            def v(request):
                item = Item.objects.create(owner=request.user, name=request.data.get("name", ""))
                return Response(ItemSerializer(item).data, status=201)
            """
        self.assertEqual(run(view), run(view))
        self.assertIsNone(run(view)[1])


class RefusesTests(unittest.TestCase):
    def check(self, src, fragment):
        out, why = run(src)
        self.assertIsNone(out, "expected the view to be refused")
        self.assertIn(fragment, why)

    def test_aliased_dict_changed_in_place(self):
        self.check(
            """
            @api_view(["GET"])
            def v(request):
                d = {}
                keep = {"d": d}
                d["a"] = 1
                return Response(keep)
            """,
            "changed in place is also stored elsewhere",
        )

    def test_mutating_an_alias_of_request_data(self):
        self.check(
            """
            @api_view(["POST"])
            def v(request):
                ids = request.data.get("ids", [])
                ids.append(1)
                return Response({"ids": ids})
            """,
            "alias",
        )

    def test_maybe_unbound_variable(self):
        self.check(
            """
            @api_view(["GET"])
            def v(request):
                if request.query_params.get("a"):
                    x = 1
                return Response({"x": x})
            """,
            "may be unbound",
        )

    def test_broad_except(self):
        self.check(
            """
            @api_view(["GET"])
            def v(request):
                try:
                    item = Item.objects.get(pk=1)
                except Exception:
                    return Response({}, status=500)
                return Response({})
            """,
            "except clauses other than Model.DoesNotExist",
        )

    def test_project_function_and_celery(self):
        self.check(
            """
            @api_view(["GET"])
            def v(request):
                return Response(helper())
            """,
            "project function",
        )
        self.check(
            """
            @api_view(["POST"])
            def v(request):
                send_mail_task.delay()
                return Response({})
            """,
            "Celery",
        )

    def test_joins_and_comprehensions(self):
        self.check(
            """
            @api_view(["GET"])
            def v(request):
                return Response({"n": Item.objects.filter(owner__username="a").count()})
            """,
            "joins",
        )
        self.check(
            """
            @api_view(["GET"])
            def v(request):
                return Response([i for i in [1, 2]])
            """,
            "comprehensions",
        )

    def test_related_object_access(self):
        self.check(
            """
            @api_view(["GET"])
            def v(request):
                item = Item.objects.first()
                return Response({"owner": item.owner})
            """,
            "related object",
        )

    def test_writes_on_models_with_signals(self):
        ir = _ir(
            {
                "v": """
                @api_view(["POST"])
                def v(request):
                    Item.objects.create(name="x", owner=request.user)
                    return Response({})
                """
            }
        )
        ir["signals"] = {"post_save": [{"receiver": "demo.signals.f", "sender": "demo.Item"}]}
        r = translate.translate(ir)
        self.assertIn("signal receivers", r.reasons["demo.views.v"][0])


if __name__ == "__main__":
    unittest.main()
