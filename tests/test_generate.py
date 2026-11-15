"""Unit tests for the generator. Standard library only: python -m unittest discover tests"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from djrs.generate import generate  # noqa: E402
from djrs.generate import pyregex  # noqa: E402
from djrs.generate.rust import raw_str  # noqa: E402


class PyRegexTests(unittest.TestCase):
    def test_end_anchor(self):
        self.assertEqual(pyregex.translate(r"^ping\Z"), (r"^ping\z", None))

    def test_escaped_backslash_before_z(self):
        # `\\Z` is a literal backslash followed by Z, not an anchor.
        self.assertEqual(pyregex.translate(r"^a\\Z"), (r"^a\\Z", None))

    def test_named_groups_pass_through(self):
        src = r"^notes/get/(?P<note_id>\d+)$"
        self.assertEqual(pyregex.translate(src), (src, None))

    def test_rejects_backreferences_and_ascii_flag(self):
        self.assertIsNone(pyregex.translate(r"(?P<a>x)(?P=a)")[0])
        self.assertIsNone(pyregex.translate(r"(a)\1")[0])
        self.assertIsNone(pyregex.translate(r"(?a)\w+")[0])


class RustLiteralTests(unittest.TestCase):
    def test_raw_str_picks_enough_hashes(self):
        self.assertEqual(raw_str("plain"), 'r#"plain"#')
        self.assertEqual(raw_str('has "# inside'), 'r##"has "# inside"##')


def _ir():
    return {
        "djrs": {"version": "0", "schema": 1},
        "project": {"settings_module": "demo.settings"},
        "settings": {},
        "databases": {"default": {"vendor": "sqlite"}},
        "middleware": [{"path": "django.middleware.common.CommonMiddleware"}],
        "url_semantics": {"regex_endpoint_fullmatch_on_dollar": True, "drop_none_kwargs": True},
        "urls": {
            "type": "resolver",
            "pattern": {"kind": "regex", "regex": "^/", "is_endpoint": False},
            "default_kwargs": {},
            "children": [
                {
                    "type": "endpoint",
                    "pattern": {"kind": "regex", "regex": r"^a\Z", "is_endpoint": True},
                    "default_kwargs": {"x": 1},
                    "view": "demo.views.a",
                },
                {
                    "type": "endpoint",
                    "pattern": {"kind": "regex", "regex": r"(?P<a>x)(?P=a)", "is_endpoint": True},
                    "default_kwargs": {},
                    "view": "demo.views.b",
                },
            ],
        },
        "views": {
            "demo.views.a": {"kind": "drf_function", "methods": ["GET"], "function": {"refs": {}}},
            "demo.views.b": {"kind": "django_class", "methods": ["GET"]},
        },
    }


class GenerateTests(unittest.TestCase):
    def _gen(self, data, **kw):
        tmp = tempfile.mkdtemp()
        report = generate(data, tmp, runtime=tmp, **kw)
        files = {}
        for root, _d, names in os.walk(tmp):
            for n in names:
                p = os.path.join(root, n)
                with open(p, encoding="utf-8") as f:
                    files[os.path.relpath(p, tmp)] = f.read()
        return report, files

    def test_deterministic(self):
        _, a = self._gen(_ir())
        _, b = self._gen(_ir())
        self.assertEqual(a, b)

    def test_route_table_and_opaque_patterns(self):
        report, files = self._gen(_ir())
        routes = files[os.path.join("src", "routes.rs")]
        self.assertIn(r'Pattern::regex(r#"^a\z"#, true)', routes)
        self.assertIn("Pattern::opaque(", routes)
        self.assertIn('"x":1', routes)
        cov = json.loads(files["djrs-coverage.json"])
        self.assertEqual(cov["summary"], {"views": 2, "rust": 0, "django": 2})
        self.assertTrue(cov["opaque_patterns"])
        self.assertEqual(cov["views"]["demo.views.b"]["reasons"], ["Django class-based views are not supported yet"])

    def test_unknown_middleware_blocks_project(self):
        data = _ir()
        data["middleware"].append({"path": "myapp.middleware.Custom"})
        report, files = self._gen(data)
        cov = json.loads(files["djrs-coverage.json"])
        self.assertEqual(cov["project_blockers"], ["middleware myapp.middleware.Custom is not supported"])
        # Project blockers are listed once, not repeated on each view.
        self.assertNotIn("middleware myapp.middleware.Custom is not supported", cov["views"]["demo.views.a"]["reasons"])

    def test_crate_name_from_settings_module(self):
        _, files = self._gen(_ir())
        self.assertIn('name = "demo-gateway"', files["Cargo.toml"])


if __name__ == "__main__":
    unittest.main()
