"""Command line: djrs extract | generate | check."""

from __future__ import annotations

import argparse
import sys


def _cmd_extract(args):
    from djrs import ir
    from djrs.extract import extract, setup_django

    setup_django(args.settings, args.pythonpath)
    data = extract(args.project_root)
    if args.out == "-":
        sys.stdout.write(ir.dumps(data))
    else:
        ir.write(args.out, data)
        print("wrote %s: %d views, %d models" % (args.out, len(data["views"]), len(data["models"])), file=sys.stderr)


def _cmd_generate(args):
    from djrs import ir
    from djrs.generate import generate

    from djrs.generate.handlers import load_config

    data = ir.read(args.ir)
    config, config_dir = load_config(args.config)
    report = generate(data, args.out, name=args.name, runtime=args.runtime, config=config, config_dir=config_dir)
    print(report.summary(), file=sys.stderr)


def _cmd_check(args):
    """Regenerate into a temporary directory and fail if it differs from --out."""
    import filecmp
    import os
    import tempfile

    from djrs import ir
    from djrs.generate import generate

    from djrs.generate.handlers import load_config

    data = ir.read(args.ir)
    config, config_dir = load_config(args.config)
    with tempfile.TemporaryDirectory() as tmp:
        generate(data, tmp, name=args.name, runtime=args.runtime, path_base=args.out, config=config, config_dir=config_dir)
        diffs = []
        for root, _dirs, files in os.walk(tmp):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), tmp)
                other = os.path.join(args.out, rel)
                if not os.path.exists(other) or not filecmp.cmp(os.path.join(root, f), other, shallow=False):
                    diffs.append(rel)
        if diffs:
            print("generated code is out of date: " + ", ".join(sorted(diffs)), file=sys.stderr)
            sys.exit(1)
    print("generated code is up to date", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="djrs")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("extract", help="describe a Django project as JSON (run in the project's environment)")
    p.add_argument("--settings", help="DJANGO_SETTINGS_MODULE (default: from the environment)")
    p.add_argument("--pythonpath", action="append", help="directory to put on sys.path (repeatable)")
    p.add_argument("--project-root", default=".", help="files under this directory count as project code")
    p.add_argument("--out", default="-", help="output file, or - for stdout")
    p.set_defaults(func=_cmd_extract)

    for name, func, helptext in (
        ("generate", _cmd_generate, "write the Rust crate from the JSON"),
        ("check", _cmd_check, "fail if the committed Rust crate is out of date"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("ir", help="JSON written by `djrs extract`")
        p.add_argument("--out", required=True, help="crate directory")
        p.add_argument("--name", default=None, help="crate and binary name (default: from the settings module)")
        p.add_argument("--config", default=None, help="the project's djrs.toml (hand-written handlers, shims)")
        p.add_argument(
            "--runtime",
            default=None,
            help="djrs-runtime dependency: a path, or 'git' for the djrs repository (default: git)",
        )
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        sys.exit(2)
    args.func(args)
