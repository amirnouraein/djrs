"""`src/settings.rs`: the project facts the runtime needs, as one static value."""

from __future__ import annotations

from .rust import raw_str


def _s(v):
    return raw_str(v) if v is not None else None


def _opt(v):
    return "Some(%s)" % raw_str(v) if v is not None else "None"


def _bool(v):
    return "true" if v else "false"


def _alg(v):
    return "djrs_runtime::signing::Algorithm::Sha256" if v == "sha256" else "djrs_runtime::signing::Algorithm::Sha1"


def _model(data, label):
    return next((m for m in data.get("models", []) if m["label"] == label), None)


def _column(model, name):
    if model is None:
        return None
    f = next((f for f in model["fields"] if f.get("name") == name and f.get("concrete")), None)
    return f.get("column") if f else None


def _header_list(pairs):
    return "&[%s]" % ", ".join("(%s, %s)" % (raw_str(k), raw_str(v)) for k, v in pairs or [])


def _meta_to_header(meta):
    # 'HTTP_X_FORWARDED_PROTO' -> 'x-forwarded-proto'
    if meta.startswith("HTTP_"):
        meta = meta[5:]
    return meta.lower().replace("_", "-")


def build(data):
    """(rust source, list of problems that block Rust handlers)."""
    problems = []
    settings = data.get("settings", {})
    auth = data.get("auth") or {}
    user = auth.get("user") or {}
    session = auth.get("session") or {}
    db = ((data.get("databases") or {}).get("default") or {})
    conn = db.get("connection") or {}
    drf = data.get("drf_settings") or {}
    msgs = data.get("drf_messages") or {}
    probe = data.get("response_headers") or {}
    if probe.get("error"):
        problems.append("response header probe failed: %s" % probe["error"])

    pk_type = user.get("pk_type")
    if pk_type in ("AutoField", "BigAutoField", "SmallAutoField", "IntegerField", "BigIntegerField"):
        pk_kind = "Int"
    elif pk_type == "UUIDField":
        pk_kind = "Uuid"
    else:
        pk_kind = "Str"

    session_rs = "None"
    if (
        session.get("engine") == "django.contrib.sessions.backends.db"
        and session.get("serializer") == "django.contrib.sessions.serializers.JSONSerializer"
        and session.get("format") in ("legacy", "signed")
    ):
        session_model = _model(data, "sessions.Session")
        session_rs = (
            "Some(SessionSettings {\n"
            "            cookie_name: %s,\n"
            "            table: %s,\n"
            "            format: SessionFormat::%s,\n"
            "            key_salt: %s,\n"
            "            signer_algorithm: %s,\n"
            "            save_every_request: %s,\n"
            "        })"
            % (
                raw_str(settings.get("SESSION_COOKIE_NAME") or "sessionid"),
                raw_str(session_model["db_table"] if session_model else "django_session"),
                "Signed" if session["format"] == "signed" else "Legacy",
                raw_str(session.get("key_salt") or ""),
                _alg(session.get("signer_algorithm")),
                _bool(settings.get("SESSION_SAVE_EVERY_REQUEST")),
            )
        )

    token_model = _model(data, "authtoken.Token")
    token_rs = "None"
    if token_model:
        token_rs = "Some(TokenSettings { table: %s, key_column: %s, user_column: %s })" % (
            raw_str(token_model["db_table"]),
            raw_str(_column(token_model, "key") or "key"),
            raw_str(_column(token_model, "user") or "user_id"),
        )

    hasher = auth.get("default_hasher") or {}
    iterations = hasher.get("iterations") if hasher.get("algorithm") == "pbkdf2_sha256" else None

    ssl = settings.get("SECURE_PROXY_SSL_HEADER")
    ssl_rs = "None"
    if isinstance(ssl, list) and len(ssl) == 2:
        ssl_rs = "Some((%s, %s))" % (raw_str(_meta_to_header(ssl[0])), raw_str(ssl[1]))

    middleware_paths = [m["path"] for m in data.get("middleware", [])]

    def msg(name, fallback):
        m = msgs.get(name) or {}
        return "DrfMessage { detail: %s, code: %s }" % (raw_str(m.get("detail") or fallback), raw_str(m.get("code") or ""))

    backends = settings.get("AUTHENTICATION_BACKENDS") or ["django.contrib.auth.backends.ModelBackend"]
    src = (
        "use djrs_runtime::auth::PkKind;\n"
        "use djrs_runtime::db::DjangoDbSettings;\n"
        "use djrs_runtime::settings::*;\n\n"
        "pub static SETTINGS: ProjectSettings = ProjectSettings {\n"
        "    allowed_hosts: &[%s],\n" % ", ".join(raw_str(h) for h in settings.get("ALLOWED_HOSTS") or [])
        + "    debug: %s,\n" % _bool(settings.get("DEBUG"))
        + "    secure_proxy_ssl_header: %s,\n" % ssl_rs
        + "    use_x_forwarded_host: %s,\n" % _bool(settings.get("USE_X_FORWARDED_HOST"))
        + "    cors_middleware: %s,\n" % _bool("corsheaders.middleware.CorsMiddleware" in middleware_paths)
        + "    response_headers: HeaderProbe {\n"
        + "        http: %s,\n" % _header_list(probe.get("http"))
        + "        https: %s,\n" % _header_list(probe.get("https"))
        + "        http_session_read: %s,\n" % _header_list(probe.get("http_session_read"))
        + "    },\n"
        + "    auth: AuthSettings {\n"
        + "        user: UserTable {\n"
        + "            table: %s,\n" % raw_str(user.get("table") or "auth_user")
        + "            pk_column: %s,\n" % raw_str(user.get("pk_column") or "id")
        + "            pk_kind: PkKind::%s,\n" % pk_kind
        + "            password_column: %s,\n" % _opt(user.get("password_column"))
        + "            is_active_column: %s,\n" % _opt(user.get("is_active_column"))
        + "            is_staff_column: %s,\n" % _opt(user.get("is_staff_column"))
        + "            is_superuser_column: %s,\n" % _opt(user.get("is_superuser_column"))
        + "            username_column: %s,\n" % _opt(user.get("username_column"))
        + "        },\n"
        + "        backends: &[%s],\n" % ", ".join(raw_str(b) for b in backends)
        + "        session: %s,\n" % session_rs
        + "        session_auth_hash: %s,\n" % _alg(auth.get("session_auth_hash"))
        + "        token: %s,\n" % token_rs
        + "        pbkdf2_iterations: %s,\n" % ("Some(%d)" % iterations if iterations else "None")
        + "    },\n"
        + "    db: DjangoDbSettings {\n"
        + "        charset: %s,\n" % _opt(conn.get("charset"))
        + "        init_command: %s,\n" % _opt(conn.get("init_command"))
        + "        isolation_level: %s,\n" % _opt(conn.get("isolation_level") if isinstance(conn.get("isolation_level"), str) else None)
        + "        sql_auto_is_null_off: %s,\n" % _bool(conn.get("sql_auto_is_null_off"))
        + "        pg_timezone: %s,\n" % _opt(conn.get("timezone_name"))
        + "    },\n"
        + "    drf: DrfSettings {\n"
        + "        unicode_json: %s,\n" % _bool(drf.get("UNICODE_JSON", True))
        + "        compact_json: %s,\n" % _bool(drf.get("COMPACT_JSON", True))
        + "        strict_json: %s,\n" % _bool(drf.get("STRICT_JSON", True))
        + "        not_authenticated: %s,\n" % msg("NotAuthenticated", "Authentication credentials were not provided.")
        + "        authentication_failed: %s,\n" % msg("AuthenticationFailed", "Incorrect authentication credentials.")
        + "        permission_denied: %s,\n" % msg("PermissionDenied", "You do not have permission to perform this action.")
        + "        not_found: %s,\n" % msg("NotFound", "Not found.")
        + "        method_not_allowed: %s,\n" % msg("MethodNotAllowed", 'Method "{method}" not allowed.')
        + "    },\n"
        + "};\n"
    )
    if settings.get("TIME_ZONE") not in ("UTC", "Etc/UTC") or not settings.get("USE_TZ"):
        problems.append("TIME_ZONE is not UTC with USE_TZ on (datetime output not reproduced yet)")
    lang = (settings.get("LANGUAGE_CODE") or "en-us").lower()
    if settings.get("USE_I18N") and not lang.startswith("en"):
        problems.append("LANGUAGE_CODE %s: error messages would be translated" % lang)
    return src, problems
