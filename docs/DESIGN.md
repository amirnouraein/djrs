# djrs design notes

How djrs works, the rules it follows, and the Django/DRF behaviours it has to
reproduce. Read this before changing the extractor, generator or runtime.

## Principles

1. **Python stays the source of truth.** Rust is regenerated, never edited.
2. **Deterministic.** Same project + same djrs version = byte-identical IR and
   generated crate. Sort where order carries no meaning; never emit
   timestamps, absolute paths or hash-seed-dependent order.
3. **Forward when unsure.** Anything Rust can't reproduce exactly goes to
   Django: at generate time (view not registered, reason in COVERAGE.md) or at
   request time (`Stop::Forward` before anything is committed).
4. **Generic.** djrs knows Django and DRF only. Project-specific code is mapped
   in the project's `djrs.toml` (hand-written "shims").
5. **Not tied to versions.** Version-dependent behaviour is *probed* by running
   Django's own code at extract time, never inferred from version numbers.
6. **The IR never holds secrets** (setting values under secret-looking names are
   redacted; DB credentials are never recorded) and extraction runs **no
   database queries** (blocked with `execute_wrapper`).

## Pipeline

```
djrs extract  (inside the project's env, Python >= 3.7)  -> IR JSON
djrs generate (any Python; tomli needed < 3.11)           -> Cargo crate + COVERAGE.md
djrs check                                                -> fails if crate is stale
binary = generated crate + crates/djrs-runtime
```

### IR (top-level keys)
`djrs`, `project`, `versions`, `settings` (whitelisted request settings),
`drf_settings` (effective api_settings), `drf_messages` (exception default
details), `auth` (user table/columns, default hasher + iterations,
session_auth_hash alg, session format/key_salt/signer alg), `databases`
(engine, vendor, options, `connection` facts: charset, found_rows,
isolation_level, init_command, pg timezone), `middleware`,
`response_headers` (probe: http / https / http_session_read, ordered lists),
`url_semantics` (probed: regex_endpoint_fullmatch_on_dollar,
drop_none_kwargs), `error_handlers`, `models` (fields with column,
internal_type, db_type, relations), `signals`, `urls` (tree, Django order),
`views` (kind, methods, allowed_methods, drf classes, function/class source +
resolved `refs`), `functions`, `classes` (project code reached from views,
depth <= 3, serializers introspected).

View kinds: `drf_function` (@api_view), `drf_class`, `drf_viewset`,
`django_function`, `django_class`, `opaque`.

`refs` map every free name / attribute chain in a view's source to what it is
at runtime: module, class, model, model_exception, function, builtin, value,
setting (value only if harmless), celery_task, object, unresolved.

### Generated crate
`Cargo.toml` (standalone `[workspace]`), `src/main.rs`, `src/routes.rs`
(URL tree), `src/entities.rs` (SeaORM entity per concrete model),
`src/settings.rs` (static `ProjectSettings`), `src/handlers.rs` (DrfView
statics + registration), shim modules via `#[path]`, `djrs-coverage.json`,
`COVERAGE.md`.

## Translator (python/djrs/translate)

`translate(ir)` turns each DRF function view into a Rust handler, or gives
the reason it can't. It runs every view twice: the first pass learns each
variable's type, the second emits code with those types fixed.

* **Static types.** Plain Python values are `Py` (operations in
  `djrs_runtime::ops`). A queryset is a SeaORM `Condition` plus an
  `orm::Ordering` and a static slice. A model instance is `Option<Model>`
  holding exactly Django's in-memory values: writes take only values of the
  column's own Python type, so in-memory and stored values never differ.
  `request`, `request.user`, `request.data` are compile-time aliases.
* **Exceptions.** Whatever Python would raise becomes `Stop::Forward`, so
  Django runs the view again from the start, after the transaction has been
  rolled back. `except Model.DoesNotExist` is the one handler run in Rust:
  its body is inlined where `.get()` finds nothing. Every other except
  clause is left to Django (any error in the try body forwards), which is
  exact as long as the body has no `raise`.
* **Aliasing.** Python lists and dicts are shared by reference and the Rust
  copies. A list or dict changed in place must start as a fresh display and
  may be stored elsewhere only in the `return` expression.
* **Definite assignment.** Reading a variable that may be unbound (or used
  after `delete()`) is refused.
* **Models.** Rows are read only for models without custom `__init__` or
  init signals; written only for complete entities without custom `save`,
  save signals or multi-table inheritance. Deletes reproduce the Collector:
  CASCADE recursively (children first), SET_NULL, PROTECT, DO_NOTHING.
* **Third-party fields** are known by class and vendor in
  `schema.KNOWN_FIELDS` (django_mysql's JSONField on MySQL). Other custom
  fields make any use of the field forward.

Output: `src/translated.rs` (serializer and delete helpers used, then one
handler per view, each preceded by its Python). `djrs.toml` handlers win.

## Runtime (crates/djrs-runtime)

| Module | Job |
| --- | --- |
| `urls` | Django's resolver: tree, `search` vs fullmatch, converters, `Unknown` instead of guessing |
| `app` | Dispatcher: native (raw or DRF view) vs forward; switches; body limits |
| `proxy` | Streaming reverse proxy, keeps header case, drops hop-by-hop only |
| `config` | `DJRS_*` env |
| `py` | Python values, QueryDict, `json.dumps`/DRF renderer byte-exact, float repr |
| `db` | Pools with Django's session settings; `DjDateTime`, `DjUuid`, `DjJson` stored as Django stores them |
| `signing` | salted_hmac, django.core.signing loads, legacy session decode, PBKDF2 |
| `auth` | DRF Token/Session/Basic + Django session/auth middleware semantics |
| `drf` | APIView cycle: negotiation, auth, permissions, 405, errors, request.data, finalize, middleware headers |
| `orm` | Django lookups per backend (as `DatabaseWrapper.operators` writes them), `exclude()`'s NULL rule, ordering, value coercions for lookups (`get_prep_value`) and writes |
| `ops` | Python operators and builtins on `Py`: `==` / `<` across int, float, bool; `in`; indexing; `str()`; `int()`; `len()`; `strip()`; dict displays |
| `settings` | `ProjectSettings` filled by the generator; secrets from env |

Transaction rule: a DRF handler runs in one transaction; render, then commit;
`Stop::Forward` or any failure before commit rolls back and forwards.
`Stop::Api` (a DRF exception) commits writes made so far, as Django would.

## Test runner (djrs.testing)

* Forward-only binary (`--djrs-info` says `native_views: 0`): in-process Django
  server shares the test thread's DB connections and cache objects; no
  semantic change. A 1,053-test production suite passes unchanged.
* Binary with Rust handlers: TestCase commits (behaves like
  TransactionTestCase) so the binary can read data; SQLite test DB in a file.
* `force_authenticate` / CSRF-off travel as `X-Djrs-Test-*` headers, honoured
  only with `DJRS_TEST_MODE=1` (Rust) and the runner's middleware (Django).
* `DJRS_COMPARE=1`: every GET also sent straight to Django; status, headers
  (ordered; Date/Server ignored; Allow compared as a set) and body compared.

## Django/DRF behaviours that matter (found the hard way)

* URL resolution: endpoints without `$` match prefixes; order matters.
  Django >= 4 full-matches regex endpoints ending in `$`; 3.0 uses search.
* `@api_view` builds `http_method_names` from a **set**: `Allow` order differs
  per process. IR uses Django's standard order; comparisons treat it as a set.
  DRF renames the wrapper class after the function (detect via `__qualname__`).
* DRF `RelatedField.choices` runs a DB query: never touch it when introspecting.
* Django keeps cache objects and DB connections **per thread**.
* Sessions: Django <= 3.0 legacy base64("sha1hex:json"), key_salt
  "django.contrib.sessionsSessionStore"; >= 3.1 signed (TimestampSigner, zlib
  if shorter, salt "django.contrib.sessions.SessionStore", sha256).
  Session auth hash: sha1 (3.0) / sha256 (3.1+). Hash mismatch -> Django
  flushes (a write) -> forward. Invalid session key (< 8 chars) or a missing
  session that gets read -> Django deletes the cookie -> forward.
* Reading `request.user` reads the session even without a cookie -> `Vary: Cookie`.
* PBKDF2 iterations differ by version (180k / 600k / 1M); a different stored
  count means Django rehashes on login (a write) -> forward.
* MySQL: Django charset utf8 (3.0) vs utf8mb4 (5.x), always FOUND_ROWS,
  READ COMMITTED, SQL_AUTO_IS_NULL=0. sqlx must not touch sql_mode/time_zone.
* SQLite datetimes stored as `str(naive_utc)`; JSON as Python `json.dumps`.
  UUIDs: hex(32) except native on PostgreSQL. Decimals on SQLite not supported.
* DRF deletes Content-Type when the rendered body is empty. Header order:
  Content-Type, WWW-Authenticate, Allow, Vary(Accept), then middleware (probed).
  CommonMiddleware adds Content-Length (all versions tested).
* DRF auth failure: 401 + WWW-Authenticate if the *first* authenticator has a
  header (Token: `Token`, Basic: `Basic realm="api"`), else 403. Unauthenticated
  permission failure -> NotAuthenticated; otherwise PermissionDenied.
* DRF runs auth and permissions before the method check (unauth wrong method = 401).
* `filter(pk=None)` matches nothing; `int()` accepts " 1_0 ", "+5", floats truncate.
* `Model.objects.create` returns the Python values, not the stored row
  (serializer output uses the in-memory object).
* Delete counts include related rows deleted by the Collector (M2M links).

## Roadmap

1. ~~Extractor~~ 2. ~~Forwarding binary~~ 3. ~~Test hook~~ 4. ~~Runtime basics~~
5. ~~Translator~~, first version. Widen next by what the production
   project's `COVERAGE.md` says most: calls to project helper functions,
   UUID keys, `request.FILES`, `using()`.
6. Side-by-side replay (writes: rows changed, Celery jobs queued).
7. Wider coverage: class-based views, serializers in general, transactions and
   locks, Celery publishing, middleware mapping in `djrs.toml`, CSRF.
