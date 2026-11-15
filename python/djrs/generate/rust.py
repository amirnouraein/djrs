"""Helpers for writing Rust source text."""

from __future__ import annotations

import json
import re


def raw_str(s: str) -> str:
    """A Rust raw string literal holding exactly `s`."""
    hashes = 1
    while ('"' + "#" * hashes) in s:
        hashes += 1
    h = "#" * hashes
    return "r%s\"%s\"%s" % (h, s, h)


def json_literal(value) -> str:
    """Rust expression evaluating to this JSON value (serde_json::Value)."""
    if value in ({}, None):
        return "serde_json::Value::Object(Default::default())"
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "serde_json::from_str(%s).expect(\"default kwargs\")" % raw_str(text)


def crate_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower()
    return name or "gateway"
