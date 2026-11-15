"""Canonical JSON for the IR.

The same project and the same djrs version must always produce the same bytes,
so everything that writes IR or generated files goes through here: keys are
sorted, there is no whitespace variation, and the file ends with one newline.
Lists keep their order, because order carries meaning (URL patterns,
middleware); producers sort lists themselves wherever order does not matter.
"""

from __future__ import annotations

import json


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False, separators=(",", ": ")) + "\n"


def write(path, obj) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(obj))


def read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
