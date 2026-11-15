"""Python `re` syntax to Rust `fancy-regex` syntax, for URL patterns.

Only differences that matter for URL patterns are translated. Anything this
module isn't sure about is reported, and the pattern becomes opaque: requests
that reach it go to Django.
"""

from __future__ import annotations


def translate(src: str):
    """(rust_source, None) or (None, reason)."""
    out = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            if i + 1 >= n:
                return None, "trailing backslash"
            nxt = src[i + 1]
            if nxt == "Z":
                out.append(r"\z")  # Python \Z = end of string = Rust \z
            elif nxt in "0123456789" and nxt != "0":
                return None, "numbered backreference"
            else:
                out.append(c + nxt)
            i += 2
            continue
        if src.startswith("(?P=", i):
            return None, "named backreference"
        if src.startswith("(?(", i):
            return None, "conditional group"
        if c == "(" and src.startswith("(?", i):
            # Inline flags Python has and Rust doesn't (a = ASCII, L = locale, u = unicode).
            j = i + 2
            flags = ""
            while j < n and src[j].isalpha():
                flags += src[j]
                j += 1
            if flags and any(f in "aLu" for f in flags):
                return None, "inline flag (?%s)" % flags
        out.append(c)
        i += 1
    return "".join(out), None
