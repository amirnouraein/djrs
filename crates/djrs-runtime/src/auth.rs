//! DRF authentication: TokenAuthentication, SessionAuthentication (with
//! Django's session and auth middleware underneath) and BasicAuthentication.
//!
//! Anything Django would answer with a write, or that this module can't
//! reproduce exactly, returns `AuthError::Forward`: the request then goes to
//! Django before anything has been written.

use std::collections::HashMap;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use sea_orm::{ConnectionTrait, Value};

use crate::db::{quote, statement, vendor, DjUuid};
use crate::py::Py;
use crate::settings::{AuthSettings, SessionFormat};
use crate::signing::{self, constant_time_eq, hex};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Authenticator {
    Token,
    Session,
    Basic,
}

impl Authenticator {
    /// DRF's authentication class path to the ones reproduced here.
    pub fn from_class(path: &str) -> Option<Authenticator> {
        match path {
            "rest_framework.authentication.TokenAuthentication" => Some(Authenticator::Token),
            "rest_framework.authentication.SessionAuthentication" => Some(Authenticator::Session),
            "rest_framework.authentication.BasicAuthentication" => Some(Authenticator::Basic),
            _ => None,
        }
    }

    /// `authenticate_header()`: what goes in WWW-Authenticate on a 401.
    pub fn authenticate_header(self) -> Option<&'static str> {
        match self {
            Authenticator::Token => Some("Token"),
            Authenticator::Basic => Some("Basic realm=\"api\""),
            Authenticator::Session => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PkKind {
    Int,
    Uuid,
    Str,
}

/// A row of the user table, as much as authentication and permissions need.
#[derive(Clone, Debug, PartialEq)]
pub struct User {
    /// `user.pk` as Python sees it (int, or str for UUIDs and text keys).
    pub pk: Py,
    /// `user.pk` as a query parameter.
    pub pk_value: Value,
    pub is_active: bool,
    pub is_staff: bool,
    pub is_superuser: bool,
    password: Option<String>,
}

#[derive(Debug, PartialEq)]
pub enum AuthError {
    /// DRF AuthenticationFailed with this detail; whether the session had been read.
    Failed(&'static str, bool),
    /// Django must answer (a write would happen, or the outcome is uncertain).
    Forward(&'static str),
}

#[derive(Debug, Default)]
pub struct AuthOutcome {
    pub user: Option<User>,
    /// DRF's `request.successful_authenticator`.
    pub successful: Option<Authenticator>,
    /// The session was read (SessionMiddleware then adds `Vary: Cookie`).
    pub session_accessed: bool,
    /// `request.auth`: the token key for TokenAuthentication.
    pub token: Option<String>,
}

/// Request facts authentication looks at.
pub struct AuthInput<'a> {
    pub method: &'a http::Method,
    pub authorization: Option<&'a [u8]>,
    pub cookies: &'a HashMap<String, String>,
    pub secrets: &'a [String],
    /// Test mode only: `force_authenticate(user)` and CSRF checks off.
    pub forced_user_pk: Option<&'a str>,
    pub csrf_exempt_for_tests: bool,
}

pub fn pk_kind(settings: &AuthSettings) -> PkKind {
    settings.user.pk_kind
}

/// `django.http.cookie.parse_cookie`.
pub fn parse_cookies(header: Option<&str>) -> HashMap<String, String> {
    let mut out = HashMap::new();
    let header = match header {
        Some(h) => h,
        None => return out,
    };
    for chunk in header.split(';') {
        let (k, v) = match chunk.split_once('=') {
            Some((k, v)) => (k, v),
            None => ("", chunk),
        };
        let (k, v) = (k.trim(), v.trim());
        if !k.is_empty() || !v.is_empty() {
            out.insert(k.to_string(), unquote_cookie(v));
        }
    }
    out
}

fn unquote_cookie(v: &str) -> String {
    // http.cookies._unquote: only quoted values are unescaped.
    if v.len() >= 2 && v.starts_with('"') && v.ends_with('"') {
        v[1..v.len() - 1]
            .replace("\\\"", "\"")
            .replace("\\\\", "\\")
    } else {
        v.to_string()
    }
}

/// Python's `bytes.split()` (on any ASCII whitespace).
fn split_whitespace(b: &[u8]) -> Vec<&[u8]> {
    b.split(|c| matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c))
        .filter(|s| !s.is_empty())
        .collect()
}

pub async fn load_user<C: ConnectionTrait>(
    conn: &C,
    s: &AuthSettings,
    pk: &str,
) -> Result<Option<User>, AuthError> {
    let u = &s.user;
    let v = vendor();
    let pk_value: Value = match pk_kind(s) {
        PkKind::Int => match pk.trim().parse::<i64>() {
            Ok(n) => n.into(),
            Err(_) => return Err(AuthError::Forward("user id is not an integer")),
        },
        PkKind::Uuid => match DjUuid::parse(pk) {
            Some(u) => u.into(),
            None => return Err(AuthError::Forward("user id is not a UUID")),
        },
        PkKind::Str => pk.to_string().into(),
    };
    let col = |c: Option<&'static str>, fallback: &str| match c {
        Some(c) => quote(v, c),
        None => fallback.to_string(),
    };
    let sql = format!(
        "SELECT {pk}, {pw}, {active}, {staff}, {superuser} FROM {table} WHERE {pk} = ?",
        pk = quote(v, u.pk_column),
        pw = col(u.password_column, "NULL"),
        active = col(u.is_active_column, "1"),
        staff = col(u.is_staff_column, "0"),
        superuser = col(u.is_superuser_column, "0"),
        table = quote(v, u.table),
    );
    let row = conn
        .query_one(statement(v, &sql, vec![pk_value.clone()]))
        .await
        .map_err(|_| AuthError::Forward("user lookup failed"))?;
    let row = match row {
        Some(r) => r,
        None => return Ok(None),
    };
    let get_bool = |i: usize| -> bool {
        row.try_get_by_index::<bool>(i)
            .or_else(|_| row.try_get_by_index::<i64>(i).map(|n| n != 0))
            .unwrap_or(false)
    };
    let pk_py = match pk_kind(s) {
        PkKind::Int => Py::Int(
            row.try_get_by_index::<i64>(0)
                .map_err(|_| AuthError::Forward("user pk"))?,
        ),
        PkKind::Uuid => Py::Str(
            DjUuid::parse(pk)
                .map(|u| u.hyphenated())
                .unwrap_or_default(),
        ),
        PkKind::Str => Py::Str(pk.to_string()),
    };
    Ok(Some(User {
        pk: pk_py,
        pk_value,
        password: row.try_get_by_index::<Option<String>>(1).ok().flatten(),
        is_active: get_bool(2),
        is_staff: get_bool(3),
        is_superuser: get_bool(4),
    }))
}

/// `request.user` for a request carrying a session cookie, as Django's
/// SessionMiddleware + AuthenticationMiddleware would compute it.
async fn session_user<C: ConnectionTrait>(
    conn: &C,
    s: &AuthSettings,
    input: &AuthInput<'_>,
) -> Result<Option<User>, AuthError> {
    let ss = s
        .session
        .as_ref()
        .ok_or(AuthError::Forward("session engine not supported"))?;
    let key = match input.cookies.get(ss.cookie_name) {
        None => return Ok(None), // no cookie: empty session, nothing written
        Some(k) => k,
    };
    if key.len() < 8 {
        return Err(AuthError::Forward(
            "invalid session key (Django deletes the cookie)",
        ));
    }
    if ss.save_every_request {
        return Err(AuthError::Forward("SESSION_SAVE_EVERY_REQUEST"));
    }
    let v = vendor();
    let sql = format!(
        "SELECT {data} FROM {table} WHERE {key} = ? AND {expire} > ?",
        data = quote(v, "session_data"),
        table = quote(v, ss.table),
        key = quote(v, "session_key"),
        expire = quote(v, "expire_date"),
    );
    let now: Value = crate::db::DjDateTime(crate::db::now()).into();
    let row = conn
        .query_one(statement(v, &sql, vec![key.clone().into(), now]))
        .await
        .map_err(|_| AuthError::Forward("session lookup failed"))?
        .ok_or(AuthError::Forward(
            "unknown or expired session (Django deletes the cookie)",
        ))?;
    let data: String = row
        .try_get_by_index(0)
        .map_err(|_| AuthError::Forward("session row"))?;
    let decoded = match ss.format {
        SessionFormat::Signed => {
            signing::signing_loads(&data, ss.key_salt, input.secrets, ss.signer_algorithm)
        }
        SessionFormat::Legacy => signing::legacy_session_decode(&data, ss.key_salt, input.secrets),
    }
    .ok_or(AuthError::Forward("session data didn't verify"))?;

    let user_id = match decoded.get("_auth_user_id") {
        None => return Ok(None),
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(serde_json::Value::Number(n)) => n.to_string(),
        Some(_) => return Err(AuthError::Forward("odd _auth_user_id")),
    };
    let backend = match decoded.get("_auth_user_backend").and_then(|b| b.as_str()) {
        None => return Ok(None),
        Some(b) => b,
    };
    if !s.backends.contains(&backend) {
        return Ok(None);
    }
    let check_active = match backend {
        "django.contrib.auth.backends.ModelBackend" => true,
        "django.contrib.auth.backends.AllowAllUsersModelBackend" => false,
        _ => return Err(AuthError::Forward("custom auth backend")),
    };
    let user = match load_user(conn, s, &user_id).await? {
        None => return Ok(None),
        Some(u) => u,
    };
    if check_active && !user.is_active {
        return Ok(None);
    }
    // Django flushes the session (a write) when the hash doesn't match the
    // current secret, and rotates it when it matches a fallback secret.
    let stored = decoded
        .get("_auth_user_hash")
        .and_then(|h| h.as_str())
        .unwrap_or("");
    let current = input
        .secrets
        .first()
        .ok_or(AuthError::Forward("DJRS_SECRET_KEY not set"))?;
    let expected = hex(&signing::salted_hmac(
        "django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash",
        user.password.as_deref().unwrap_or("").as_bytes(),
        current,
        s.session_auth_hash,
    ));
    if !constant_time_eq(expected.as_bytes(), stored.as_bytes()) {
        return Err(AuthError::Forward(
            "session auth hash doesn't match the current secret",
        ));
    }
    Ok(Some(user))
}

fn is_safe_method(m: &http::Method) -> bool {
    matches!(m.as_str(), "GET" | "HEAD" | "OPTIONS" | "TRACE")
}

/// DRF's `Request._authenticate()` over the view's authenticators, in order.
pub async fn authenticate<C: ConnectionTrait>(
    conn: &C,
    s: &AuthSettings,
    authenticators: &[Authenticator],
    input: &AuthInput<'_>,
) -> Result<AuthOutcome, AuthError> {
    let mut out = AuthOutcome::default();

    if let Some(pk) = input.forced_user_pk {
        // force_authenticate(): DRF swaps the authenticators for one that always succeeds.
        out.user = load_user(conn, s, pk).await?;
        if out.user.is_none() {
            return Err(AuthError::Forward("forced test user not found"));
        }
        return Ok(out);
    }

    for a in authenticators {
        match a {
            Authenticator::Token => {
                let accessed = out.session_accessed;
                let parts = match input.authorization {
                    Some(h) => split_whitespace(h),
                    None => continue,
                };
                if parts.is_empty() || !parts[0].eq_ignore_ascii_case(b"token") {
                    continue;
                }
                if parts.len() == 1 {
                    return Err(AuthError::Failed(
                        "Invalid token header. No credentials provided.",
                        accessed,
                    ));
                }
                if parts.len() > 2 {
                    return Err(AuthError::Failed(
                        "Invalid token header. Token string should not contain spaces.",
                        accessed,
                    ));
                }
                let key = std::str::from_utf8(parts[1]).map_err(|_| {
                    AuthError::Failed(
                        "Invalid token header. Token string should not contain invalid characters.",
                        accessed,
                    )
                })?;
                let ts = s
                    .token
                    .as_ref()
                    .ok_or(AuthError::Forward("token model not found"))?;
                let v = vendor();
                let sql = format!(
                    "SELECT {user} FROM {table} WHERE {key} = ?",
                    user = quote(v, ts.user_column),
                    table = quote(v, ts.table),
                    key = quote(v, ts.key_column),
                );
                let row = conn
                    .query_one(statement(v, &sql, vec![key.to_string().into()]))
                    .await
                    .map_err(|_| AuthError::Forward("token lookup failed"))?
                    .ok_or(AuthError::Failed("Invalid token.", accessed))?;
                let user_id: String = row
                    .try_get_by_index::<i64>(0)
                    .map(|n| n.to_string())
                    .or_else(|_| row.try_get_by_index::<String>(0))
                    .map_err(|_| AuthError::Forward("token user id"))?;
                let user = load_user(conn, s, &user_id)
                    .await?
                    .ok_or(AuthError::Failed("Invalid token.", accessed))?;
                if !user.is_active {
                    return Err(AuthError::Failed("User inactive or deleted.", accessed));
                }
                out.user = Some(user);
                out.successful = Some(Authenticator::Token);
                out.token = Some(key.to_string());
                return Ok(out);
            }
            Authenticator::Session => {
                // Reading request.user reads the session, even when there is no cookie.
                out.session_accessed = true;
                let user = match session_user(conn, s, input).await? {
                    Some(u) if u.is_active => u,
                    _ => continue,
                };
                if !is_safe_method(input.method) && !input.csrf_exempt_for_tests {
                    return Err(AuthError::Forward(
                        "CSRF check for a session-authenticated write",
                    ));
                }
                out.user = Some(user);
                out.successful = Some(Authenticator::Session);
                return Ok(out);
            }
            Authenticator::Basic => {
                let accessed = out.session_accessed;
                let parts = match input.authorization {
                    Some(h) => split_whitespace(h),
                    None => continue,
                };
                if parts.is_empty() || !parts[0].eq_ignore_ascii_case(b"basic") {
                    continue;
                }
                if parts.len() == 1 {
                    return Err(AuthError::Failed(
                        "Invalid basic header. No credentials provided.",
                        accessed,
                    ));
                }
                if parts.len() > 2 {
                    return Err(AuthError::Failed(
                        "Invalid basic header. Credentials string should not contain spaces.",
                        accessed,
                    ));
                }
                // Python's b64decode is lenient and newer DRF falls back to latin-1;
                // anything but clean base64 + UTF-8 goes to Django.
                let raw = STANDARD
                    .decode(parts[1])
                    .map_err(|_| AuthError::Forward("basic credentials need lenient base64"))?;
                let decoded = String::from_utf8(raw)
                    .map_err(|_| AuthError::Forward("basic credentials not UTF-8"))?;
                let (username, password) = match decoded.split_once(':') {
                    Some((u, p)) => (u.to_string(), p.to_string()),
                    None => (decoded.clone(), String::new()),
                };
                if s.backends != ["django.contrib.auth.backends.ModelBackend"] {
                    return Err(AuthError::Forward("basic auth with custom backends"));
                }
                let ucol = s
                    .user
                    .username_column
                    .ok_or(AuthError::Forward("no username column"))?;
                let v = vendor();
                let sql = format!(
                    "SELECT {pk} FROM {table} WHERE {ucol} = ?",
                    pk = quote(v, s.user.pk_column),
                    table = quote(v, s.user.table),
                    ucol = quote(v, ucol),
                );
                let row = conn
                    .query_one(statement(v, &sql, vec![username.into()]))
                    .await
                    .map_err(|_| AuthError::Forward("user lookup failed"))?
                    // A failed login fires user_login_failed; its receivers are Django's to run.
                    .ok_or(AuthError::Forward(
                        "basic auth failure (user_login_failed signal)",
                    ))?;
                let pk: String = row
                    .try_get_by_index::<i64>(0)
                    .map(|n| n.to_string())
                    .or_else(|_| row.try_get_by_index::<String>(0))
                    .map_err(|_| AuthError::Forward("user pk"))?;
                let user = load_user(conn, s, &pk)
                    .await?
                    .ok_or(AuthError::Forward("user vanished"))?;
                let encoded = user.password.clone().unwrap_or_default();
                let (ok, iterations) = signing::check_pbkdf2_sha256(&password, &encoded)
                    .ok_or(AuthError::Forward("password hasher not supported"))?;
                if !ok || !user.is_active {
                    return Err(AuthError::Forward(
                        "basic auth failure (user_login_failed signal)",
                    ));
                }
                if Some(iterations) != s.pbkdf2_iterations {
                    return Err(AuthError::Forward(
                        "password hash would be upgraded (a write)",
                    ));
                }
                out.user = Some(user);
                out.successful = Some(Authenticator::Basic);
                return Ok(out);
            }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cookie_parsing_like_django() {
        let c = parse_cookies(Some(
            "sessionid=abc12345; csrftoken=x; =lonely; flag; q=\"a\\\"b\"",
        ));
        assert_eq!(c.get("sessionid").unwrap(), "abc12345");
        assert_eq!(c.get("csrftoken").unwrap(), "x");
        assert_eq!(c.get("").unwrap(), "flag");
        assert_eq!(c.get("q").unwrap(), "a\"b");
    }

    #[test]
    fn whitespace_split_like_python_bytes() {
        assert_eq!(
            split_whitespace(b"Token  abc"),
            vec![&b"Token"[..], &b"abc"[..]]
        );
        assert_eq!(
            split_whitespace(b" Token\tabc def "),
            vec![&b"Token"[..], &b"abc"[..], &b"def"[..]]
        );
        assert!(split_whitespace(b"   ").is_empty());
    }
}
