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
| Test hook (`djrs.testing`): a project's own tests run through the binary | Working, with a byte-for-byte comparison mode |
| Runtime for Rust handlers: SeaORM entities, DRF request cycle, token / session / basic auth, Django's response headers | Working for DRF function views; proven with hand-written handlers on SQLite, MySQL and PostgreSQL |
| Translator: DRF function views to Rust | Working for a growing subset (see below). Proven on the sample project on Django 3.0, 4.2 and 5.2, SQLite, MySQL and PostgreSQL; on the production project, 43 of 537 DRF function views translate and compile so far. Views outside the subset are forwarded, with the reason in `COVERAGE.md` |

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
send real HTTP to the binary. Without `DJRS_BINARY` it is Django's normal runner.

- If the binary answers nothing in Rust, Django's in-process server shares the
  test's database connection, so tests behave exactly as they do in-process.
- If it has Rust handlers, test data is committed so the binary can read it
  (`TestCase` behaves like `TransactionTestCase`): slower, and code that relies
  on transactions can behave differently. `DJRS_TEST_COMMIT=0|1` overrides.
- `DJRS_COMPARE=1` also sends every GET straight to Django and reports any
  difference in status, headers (in order) or body bytes.
  `DJRS_COMPARE_STRICT=1` makes differences fail the run.

## Rust handlers

A view is answered in Rust when the generated crate has a handler for it.
`djrs generate` translates every DRF function view it can into
`src/translated.rs` (each handler is preceded by its Python, and follows it
line by line). The translator is rule-based: a view is translated only if
everything in it is in the supported subset, and any operation Python would
raise on hands the request to Django at run time. Supported so far:

- `request.data`, `request.query_params` / `GET`, `request.user` (and its
  fields), `request.method`, URL parameters
- querysets: `filter` / `exclude` / `order_by` / slicing / `first` / `last` /
  `get` / `count` / `exists` / `create` / `update` / `delete` (with Django's
  cascades), lookups `exact`, `iexact`, `[i]contains`, `[i]startswith`,
  `[i]endswith`, `in`, `gt(e)`, `lt(e)`, `isnull`, on local fields and
  `fk__pk`; related objects (`note.owner`)
- model instances: field reads and writes, `save()` (also `update_fields`),
  `delete()`; `ModelSerializer(...).data` for plain field types
- `Response`, DRF exceptions, `try/except Model.DoesNotExist` (other except
  clauses are left to Django: any error in the body forwards)
- Python: `if` / `for` / comprehensions, dict and list building, f-strings,
  `str` / `int` / `bool` / `len` / `list` / `isinstance`, `strip` /
  `lower` / `upper`, comparisons and arithmetic with Python's rules,
  `timezone.now()` and `timedelta`

A view the translator can't handle can be given a hand-written handler in the
project's `djrs.toml` (see `examples/notes/djrs.toml`); it takes precedence:

```toml
[shims]
notes_shims = "rust/notes_shims.rs"     # compiled into the generated crate

[drf_handlers."notes.views.count_notes"]
GET = "notes_shims::count_notes"
```

```bash
python -m djrs generate djrs.json --out gateway --config djrs.toml
```

A handler gets a `Ctx` (the request, `request.user`, `request.data`, and a
database transaction) after djrs-runtime has run DRF's cycle: content
negotiation, authentication, permissions and the method check. It returns a
DRF response, a DRF error, or `Stop::Forward`, which rolls the transaction
back and hands the request to Django. The transaction commits only after the
response has been rendered, so any failure before that leaves no trace and
Django answers instead.

A view is registered only when its DRF setup is one djrs-runtime reproduces
(the standard authentication, permission, renderer and parser classes, no
throttling or versioning) and the project has no middleware djrs doesn't know.
`COVERAGE.md` says why each other view goes to Django.

## Binary settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `DJRS_UPSTREAM` | required | Django's address, e.g. `http://127.0.0.1:8001` |
| `DJRS_LISTEN` | `0.0.0.0:8000` | Address the binary listens on |
| `DJRS_FORCE_FORWARD` | empty | Comma-separated view ids that always go to Django; `*` for all |
| `DJRS_FORWARDED_HEADERS` | `preserve` | `preserve` passes `X-Forwarded-*` through untouched; `append` adds the client address |
| `DJRS_MAX_NATIVE_BODY` | `2621440` | Larger request bodies always go to Django |
| `DJRS_LOG` | `info` | Log filter, e.g. `djrs_runtime=debug` shows every forwarding decision |
| `DJRS_DATABASE_ENGINE`, `_NAME`, `_USER`, `_PASSWORD`, `_HOST`, `_PORT` | none | The project's database, as in Django's `DATABASES["default"]`. Without it every request goes to Django |
| `DJRS_DATABASE_POOL` | `16` | Connection pool size |
| `DJRS_SECRET_KEY`, `DJRS_SECRET_KEY_FALLBACKS` | none | The project's `SECRET_KEY` (and fallbacks, comma-separated), to read sessions |
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
