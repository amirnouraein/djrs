# djrs

A deterministic, rule-based transpiler from Django to Rust (Axum + SeaORM).

djrs produces one Rust binary that sits in front of an existing Django project.
Every request goes to the binary. It answers the routes it has a Rust
translation for, and forwards everything else, unchanged, to Django running on
an internal port. The Python code stays the source of truth: the Rust side is
regenerated from it, never edited by hand.

djrs knows Django and Django REST Framework. It knows nothing about any
particular project, and it is not tied to a Django, DRF or database version.

## Status

| Part | State |
| --- | --- |
| Extractor (`djrs extract`) | Working. Tested on Django 3.0, 4.2 and 5.2 (CI adds 3.2), and on a 650-endpoint production project |
| Rust binary: Django-order URL resolution, forwarding proxy, switches | Working |
| Test hook (`djrs.testing`): a project's own tests run through the binary | Working |
| Translating views to Rust handlers | Not started. Every view is forwarded for now, with the reason in `COVERAGE.md` |

## How it works

```
                   build time                                   request time
 Django project ── djrs extract ──> djrs.json ── djrs generate ──> Rust crate ──> binary
                   (runs inside                  (pure Python,                      │
                    the project)                  no Django)                        ├─ answered in Rust
                                                                                    └─ forwarded to Django
```

1. **`djrs extract`** runs inside the project (its own Python and packages) and
   writes one JSON file: versions, the settings that affect request handling,
   middleware, models and fields, the URL tree in Django's matching order,
   every view (kind, methods, DRF classes, source, and what each name in it
   refers to), serializers, and model signal receivers. It runs no database
   queries (they are blocked while it runs), and it never records secrets.
   The same project gives the same bytes every time.
2. **`djrs generate`** turns that JSON into a Cargo crate on top of the
   `djrs-runtime` crate, plus `COVERAGE.md`, which lists every view, whether it
   is answered in Rust, and if not, why.
3. **The binary** resolves each request's path exactly as Django would (same
   order, same `search` vs. full-match rules, same converters), then either
   calls the generated handler or forwards the request. When it can't be sure
   what Django would do (a regex Rust can't express, a custom path converter,
   a path Django would have to repair), it forwards.

## Quick start

```bash
pip install -e path/to/djrs            # into the project's own environment

cd my-django-project
python -m djrs extract --settings mysite.settings --pythonpath . --out djrs.json
python -m djrs generate djrs.json --out gateway --runtime path/to/djrs/crates/djrs-runtime
cargo build --release --manifest-path gateway/Cargo.toml

gunicorn mysite.wsgi --bind 127.0.0.1:8001 &
DJRS_UPSTREAM=http://127.0.0.1:8001 DJRS_LISTEN=0.0.0.0:8000 gateway/target/release/mysite-gateway
```

`djrs check djrs.json --out gateway` fails if the committed crate is out of
date with the JSON. Use it in CI.

### Running a project's own tests through the binary

```bash
DJRS_BINARY=gateway/target/debug/mysite-gateway \
  python manage.py test --testrunner djrs.testing.DjrsTestRunner
```

No change to the project is needed. The runner serves Django in-process on a
free port, starts the binary in front of it, and makes Django's test client
send real HTTP to the binary. Test data is committed so the binary can see it
(`TestCase` behaves like `TransactionTestCase`), which makes suites slower.
Without `DJRS_BINARY` it is Django's normal runner.

## Binary settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `DJRS_UPSTREAM` | required | Django's address, e.g. `http://127.0.0.1:8001` |
| `DJRS_LISTEN` | `0.0.0.0:8000` | Address the binary listens on |
| `DJRS_FORCE_FORWARD` | empty | Comma-separated view ids that always go to Django; `*` for all |
| `DJRS_FORWARDED_HEADERS` | `preserve` | `preserve` passes `X-Forwarded-*` through untouched; `append` adds the client address |
| `DJRS_MAX_NATIVE_BODY` | `2621440` | Larger request bodies always go to Django |
| `DJRS_LOG` | `info` | Log filter, e.g. `djrs_runtime=debug` shows every forwarding decision |
| `DJRS_TEST_MODE` | off | Set only by `djrs.testing`; never in production |

## Repository layout

| Path | What |
| --- | --- |
| `python/djrs/extract/` | The extractor |
| `python/djrs/generate/` | The generator and the rules deciding what may be answered in Rust |
| `python/djrs/testing/` | The test runner, test transport and test-only middleware |
| `crates/djrs-runtime/` | Rust runtime: URL resolution, proxy, dispatch, server |
| `examples/notes/` | Sample Django + DRF project with HTTP tests |
| `scripts/e2e.sh` | Extract, generate, build, and run the sample tests plain and through the binary |
| `tests/` | Unit tests for the generator |

## Development

```bash
cargo test --workspace
python -m unittest discover tests
PYTHON=/path/to/venv/bin/python scripts/e2e.sh   # a venv with Django, DRF and djrs installed
```
