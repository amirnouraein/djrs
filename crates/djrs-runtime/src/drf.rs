//! Django REST Framework's request cycle for Rust handlers.
//!
//! `dispatch` reproduces `APIView.dispatch` for a translated view: content
//! negotiation, authentication, permissions, the method check, the handler,
//! exception responses, `finalize_response`, and then the response headers
//! Django's middleware adds. The handler runs inside one database transaction
//! that is committed only after the response is fully rendered; anything that
//! stops before that (an unsupported input, a render error, a failed commit)
//! rolls back and the request goes to Django, as if Rust had never seen it.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use axum::body::Body;
use bytes::Bytes;
use http::{HeaderValue, Method, Response, StatusCode};
use sea_orm::{DatabaseTransaction, TransactionTrait, Value};

use crate::app::{NativeRequest, Outcome, State};
use crate::auth::{self, AuthError, AuthInput, Authenticator, User};
use crate::py::{self, JsonOptions, Py, QueryDict};
use crate::settings::{DrfMessage, ProjectSettings};
use crate::urls::Match;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Permission {
    AllowAny,
    IsAuthenticated,
    IsAdminUser,
    IsAuthenticatedOrReadOnly,
}

impl Permission {
    pub fn from_class(path: &str) -> Option<Permission> {
        match path {
            "rest_framework.permissions.AllowAny" => Some(Permission::AllowAny),
            "rest_framework.permissions.IsAuthenticated" => Some(Permission::IsAuthenticated),
            "rest_framework.permissions.IsAdminUser" => Some(Permission::IsAdminUser),
            "rest_framework.permissions.IsAuthenticatedOrReadOnly" => {
                Some(Permission::IsAuthenticatedOrReadOnly)
            }
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Renderer {
    Json,
    BrowsableApi,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Parser {
    Json,
    Form,
    MultiPart,
}

/// What a translated DRF view needs to know about itself.
pub struct DrfView {
    pub id: &'static str,
    /// The Allow header, in Django's order.
    pub allowed_methods: &'static [&'static str],
    pub authenticators: &'static [Authenticator],
    pub permissions: &'static [Permission],
    pub renderers: &'static [Renderer],
    pub parsers: &'static [Parser],
}

/// A DRF APIException's response.
#[derive(Debug, Clone, PartialEq)]
pub struct ApiError {
    pub status: u16,
    pub data: Py,
    pub www_authenticate: Option<&'static str>,
}

impl ApiError {
    fn from_message(status: u16, m: &DrfMessage) -> ApiError {
        ApiError {
            status,
            data: crate::pydict! { "detail" => m.detail },
            www_authenticate: None,
        }
    }
    /// `raise NotFound()` / `Http404` inside a DRF view.
    pub fn not_found(s: &ProjectSettings) -> ApiError {
        ApiError::from_message(404, &s.drf.not_found)
    }
    pub fn permission_denied(s: &ProjectSettings) -> ApiError {
        ApiError::from_message(403, &s.drf.permission_denied)
    }
}

/// Why a handler stopped early.
#[derive(Debug)]
pub enum Stop {
    /// Hand the request to Django (nothing is committed).
    Forward(&'static str),
    /// A DRF exception: respond with it (writes so far are committed, as in Django).
    Api(ApiError),
}

impl From<sea_orm::DbErr> for Stop {
    fn from(_: sea_orm::DbErr) -> Stop {
        // Django would have hit the same error and answered it; let it.
        Stop::Forward("database error")
    }
}

/// A DRF Response.
#[derive(Debug, Clone, PartialEq)]
pub struct DrfResponse {
    pub status: u16,
    pub data: Option<Py>,
}

impl DrfResponse {
    pub fn ok(data: Py) -> DrfResponse {
        DrfResponse {
            status: 200,
            data: Some(data),
        }
    }
    pub fn with_status(mut self, status: u16) -> DrfResponse {
        self.status = status;
        self
    }
    pub fn empty(status: u16) -> DrfResponse {
        DrfResponse { status, data: None }
    }
}

/// `request.data`: a dict from JSON, or a QueryDict from a form.
#[derive(Debug, Clone)]
pub enum Data {
    Json(Py),
    Form(QueryDict),
}

impl Data {
    /// `request.data.get(key)`; Err when Python would raise (data isn't a dict).
    pub fn get(&self, key: &str) -> Result<Option<Py>, Stop> {
        match self {
            Data::Json(Py::Dict(d)) => Ok(d.get(key).cloned()),
            Data::Json(_) => Err(Stop::Forward("request.data is not a dict")),
            Data::Form(q) => Ok(q.get(key).map(Py::from)),
        }
    }
    /// `request.data.get(key, default)`.
    pub fn get_or(&self, key: &str, default: Py) -> Result<Py, Stop> {
        Ok(self.get(key)?.unwrap_or(default))
    }
}

/// Everything a handler can read, plus its transaction.
pub struct Ctx {
    pub state: Arc<State>,
    pub settings: &'static ProjectSettings,
    pub method: Method,
    pub kwargs: serde_json::Map<String, serde_json::Value>,
    /// `request.query_params` / `request.GET`.
    pub query: QueryDict,
    /// `request.user` when authenticated.
    pub user: Option<User>,
    /// `request.auth` (the token key for TokenAuthentication).
    pub auth: Option<String>,
    content_type: Option<String>,
    body: Bytes,
    parsers: &'static [Parser],
    data: Option<Data>,
    tx: Option<DatabaseTransaction>,
}

impl Ctx {
    pub fn tx(&self) -> &DatabaseTransaction {
        self.tx
            .as_ref()
            .expect("transaction open during the handler")
    }

    /// The authenticated user, or stop: the view assumed one (Python would
    /// raise on AnonymousUser attributes).
    pub fn user(&self) -> Result<&User, Stop> {
        self.user
            .as_ref()
            .ok_or(Stop::Forward("view used request.user while anonymous"))
    }

    /// `request.user.pk` as a query value; AnonymousUser's pk is None.
    pub fn user_pk_value(&self) -> Option<Value> {
        self.user.as_ref().map(|u| u.pk_value.clone())
    }

    /// `request.data`, parsed on first use like DRF does.
    pub fn data(&mut self) -> Result<&Data, Stop> {
        if self.data.is_none() {
            self.data = Some(parse_data(
                self.content_type.as_deref(),
                &self.body,
                self.parsers,
            )?);
        }
        Ok(self.data.as_ref().unwrap())
    }
}

fn parse_data(content_type: Option<&str>, body: &Bytes, parsers: &[Parser]) -> Result<Data, Stop> {
    let media = content_type.map(|c| {
        c.split(';')
            .next()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase()
    });
    let is_form = matches!(
        media.as_deref(),
        Some("application/x-www-form-urlencoded") | Some("multipart/form-data")
    );
    if body.is_empty() || media.as_deref().map_or(true, |m| m.is_empty()) {
        return Ok(if is_form {
            Data::Form(QueryDict::default())
        } else {
            Data::Json(Py::dict())
        });
    }
    let media = media.unwrap();
    let parser = parsers.iter().find(|p| match p {
        Parser::Json => media == "application/json",
        Parser::Form => media == "application/x-www-form-urlencoded",
        Parser::MultiPart => media == "multipart/form-data",
    });
    match parser {
        None => Err(Stop::Forward("unsupported media type (415)")),
        Some(Parser::MultiPart) => Err(Stop::Forward("multipart bodies are parsed by Django")),
        Some(Parser::Form) => {
            let text =
                std::str::from_utf8(body).map_err(|_| Stop::Forward("form body not UTF-8"))?;
            let q = QueryDict::parse(text);
            if q.keys().count() > 1000 {
                return Err(Stop::Forward("too many form fields"));
            }
            Ok(Data::Form(q))
        }
        Some(Parser::Json) => {
            // Python ints are unbounded; JSON with 19+ digit numbers goes to Django.
            let mut run = 0;
            for &b in body.iter() {
                run = if b.is_ascii_digit() { run + 1 } else { 0 };
                if run >= 19 {
                    return Err(Stop::Forward("very long number in JSON"));
                }
            }
            let v: serde_json::Value = serde_json::from_slice(body)
                .map_err(|_| Stop::Forward("JSON parse error (400)"))?;
            Py::from_json(v)
                .map(Data::Json)
                .ok_or(Stop::Forward("JSON number out of range"))
        }
    }
}

pub type DrfHandlerFuture = Pin<Box<dyn Future<Output = (Ctx, Result<DrfResponse, Stop>)> + Send>>;
pub type DrfHandler = fn(Ctx) -> DrfHandlerFuture;

/// Wrap `async fn body(ctx: &mut Ctx) -> Result<DrfResponse, Stop>` as a DrfHandler.
#[macro_export]
macro_rules! drf_handler {
    ($name:ident, $body:path) => {
        pub fn $name(mut ctx: $crate::drf::Ctx) -> $crate::drf::DrfHandlerFuture {
            Box::pin(async move {
                let r = $body(&mut ctx).await;
                (ctx, r)
            })
        }
    };
}

// ---- content negotiation ----------------------------------------------------

/// DRF's DefaultContentNegotiation, enough to know whether JSONRenderer is
/// chosen with no media type parameters. Anything else goes to Django.
fn negotiates_plain_json(accept: Option<&str>, renderers: &[Renderer], query: &QueryDict) -> bool {
    if query.contains("format") || renderers.is_empty() {
        return false;
    }
    let accept = accept.unwrap_or("*/*");
    // Group by DRF precedence: specific with params (3), specific (2), type/* (1), */* (0).
    let mut items: Vec<(u8, String, bool)> = Vec::new();
    for part in accept.split(',') {
        let mut pieces = part.split(';');
        let mt = pieces.next().unwrap_or("").trim().to_ascii_lowercase();
        if mt.is_empty() {
            continue;
        }
        let has_params = pieces.any(|p| !p.trim().is_empty() && !p.trim().starts_with("q="));
        let prec = if mt == "*/*" {
            0
        } else if mt.ends_with("/*") {
            1
        } else if has_params {
            3
        } else {
            2
        };
        items.push((prec, mt, has_params));
    }
    if items.is_empty() {
        items.push((0, "*/*".into(), false));
    }
    for prec in [3u8, 2, 1, 0] {
        let set: Vec<&(u8, String, bool)> = items.iter().filter(|i| i.0 == prec).collect();
        if set.is_empty() {
            continue;
        }
        for r in renderers {
            let media = match r {
                Renderer::Json => "application/json",
                Renderer::BrowsableApi => "text/html",
            };
            for (_, mt, params) in &set {
                let matches = mt == "*/*"
                    || mt == media
                    || (mt.ends_with("/*") && media.starts_with(&mt[..mt.len() - 1]));
                if matches {
                    return *r == Renderer::Json && !params;
                }
            }
        }
    }
    false // 406
}

// ---- dispatch -----------------------------------------------------------------

fn has_permission(perms: &[Permission], user: Option<&User>, method: &Method) -> bool {
    perms.iter().all(|p| match p {
        Permission::AllowAny => true,
        Permission::IsAuthenticated => user.is_some(),
        Permission::IsAdminUser => user.map_or(false, |u| u.is_staff),
        Permission::IsAuthenticatedOrReadOnly => {
            matches!(method.as_str(), "GET" | "HEAD" | "OPTIONS") || user.is_some()
        }
    })
}

struct Rendered {
    status: u16,
    body: Vec<u8>,
    www_authenticate: Option<&'static str>,
}

fn render(status: u16, data: &Option<Py>, settings: &ProjectSettings) -> Result<Vec<u8>, Stop> {
    let opts = JsonOptions {
        ensure_ascii: !settings.drf.unicode_json,
        item_sep: if settings.drf.compact_json { "," } else { ", " },
        key_sep: if settings.drf.compact_json { ":" } else { ": " },
        allow_nan: !settings.drf.strict_json,
    };
    let _ = status;
    match data {
        None => Ok(Vec::new()),
        Some(d) => py::drf_render(d, opts)
            .map(String::into_bytes)
            .map_err(|_| Stop::Forward("value can't be rendered as JSON")),
    }
}

/// `patch_vary_headers`: add `value` to Vary unless already there.
fn patch_vary(headers: &mut Vec<(String, String)>, value: &str) {
    if let Some(h) = headers
        .iter_mut()
        .find(|(k, _)| k.eq_ignore_ascii_case("vary"))
    {
        let present = h.1.split(',').any(|v| v.trim().eq_ignore_ascii_case(value));
        if !present {
            h.1 = format!("{}, {}", h.1, value);
        }
    } else {
        headers.push(("Vary".into(), value.into()));
    }
}

/// The response as Django would send it: view headers, then what the
/// middleware chain adds, in the order the extractor probed.
fn build_response(
    view: &DrfView,
    r: Rendered,
    session_accessed: bool,
    settings: &ProjectSettings,
) -> Response<Body> {
    let mut headers: Vec<(String, String)> = Vec::new();
    if !r.body.is_empty() {
        headers.push(("Content-Type".into(), "application/json".into()));
    }
    if let Some(w) = r.www_authenticate {
        headers.push(("WWW-Authenticate".into(), w.into()));
    }
    headers.push(("Allow".into(), view.allowed_methods.join(", ")));
    if view.renderers.len() > 1 {
        headers.push(("Vary".into(), "Accept".into()));
    }
    let probe = if session_accessed {
        settings.response_headers.http_session_read
    } else {
        settings.response_headers.http
    };
    for (name, value) in probe {
        if name.eq_ignore_ascii_case("content-type") {
            continue;
        }
        if name.eq_ignore_ascii_case("content-length") {
            headers.push(("Content-Length".into(), r.body.len().to_string()));
        } else if name.eq_ignore_ascii_case("vary") {
            for v in value.split(',') {
                patch_vary(&mut headers, v.trim());
            }
        } else if !headers.iter().any(|(k, _)| k.eq_ignore_ascii_case(name)) {
            headers.push((name.to_string(), value.to_string()));
        }
    }
    let mut resp = Response::new(Body::from(r.body));
    *resp.status_mut() =
        StatusCode::from_u16(r.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    for (k, v) in headers {
        if let (Ok(name), Ok(value)) = (
            http::HeaderName::from_bytes(k.as_bytes()),
            HeaderValue::from_str(&v),
        ) {
            resp.headers_mut().append(name, value);
        }
    }
    resp
}

/// Requests a Rust handler must not answer, whatever the view.
fn gate(req: &NativeRequest, settings: &ProjectSettings) -> Result<(), &'static str> {
    let h = &req.parts.headers;
    if settings.cors_middleware && h.contains_key(http::header::ORIGIN) {
        return Err("CORS request");
    }
    let host = if settings.use_x_forwarded_host {
        h.get("x-forwarded-host").or(h.get(http::header::HOST))
    } else {
        h.get(http::header::HOST)
    };
    let host = host.and_then(|v| v.to_str().ok()).ok_or("no Host header")?;
    if !host_allowed(host, settings) {
        return Err("host not in ALLOWED_HOSTS (Django answers 400)");
    }
    // If http and https responses would differ (HSTS), and whether the request
    // counts as secure depends on the WSGI server's trust settings, let Django decide.
    let probes_differ = settings.response_headers.http != settings.response_headers.https;
    if probes_differ {
        let secure_by_setting = settings
            .secure_proxy_ssl_header
            .map(|(name, value)| h.get(name).and_then(|v| v.to_str().ok()) == Some(value))
            .unwrap_or(false);
        let forwarded_proto = [
            "x-forwarded-proto",
            "x-forwarded-protocol",
            "x-forwarded-ssl",
        ]
        .iter()
        .any(|n| h.contains_key(*n));
        if secure_by_setting || forwarded_proto {
            return Err("request may be secure; HSTS headers decided by Django");
        }
    }
    Ok(())
}

/// `django.http.request.validate_host` with ALLOWED_HOSTS.
pub fn host_allowed(host: &str, settings: &ProjectSettings) -> bool {
    let host = host.to_ascii_lowercase();
    // split_domain_port
    let domain = if host.ends_with(']') {
        host.clone()
    } else {
        match host.rsplit_once(':') {
            Some((d, p)) if p.chars().all(|c| c.is_ascii_digit()) => d.to_string(),
            Some(_) => return false,
            None => host.clone(),
        }
    };
    let domain = domain.trim_end_matches('.');
    if domain.is_empty() {
        return false;
    }
    let mut allowed: Vec<&str> = settings.allowed_hosts.to_vec();
    if settings.debug && allowed.is_empty() {
        allowed = vec![".localhost", "127.0.0.1", "[::1]"];
    }
    allowed.iter().any(|pattern| {
        let p = pattern.to_ascii_lowercase();
        p == "*"
            || (p.starts_with('.') && (domain.ends_with(&p) || domain == &p[1..]))
            || p == domain
    })
}

/// Answer a request with a translated DRF view, or hand it back for Django.
pub async fn dispatch(
    state: Arc<State>,
    req: NativeRequest,
    m: Match,
    view: &'static DrfView,
    handlers: &'static [(&'static str, DrfHandler)],
) -> Outcome {
    let settings = match state.settings {
        Some(s) => s,
        None => return Outcome::Forward(req),
    };
    let db = match &state.db {
        Some(db) => db.clone(),
        None => return Outcome::Forward(req),
    };
    if let Err(reason) = gate(&req, settings) {
        tracing::debug!(view = view.id, reason, "forward");
        return Outcome::Forward(req);
    }
    let headers = &req.parts.headers;
    let query = QueryDict::parse(req.parts.uri.query().unwrap_or(""));
    let accept = headers
        .get(http::header::ACCEPT)
        .and_then(|v| v.to_str().ok());
    if !negotiates_plain_json(accept, view.renderers, &query) {
        return Outcome::Forward(req);
    }

    let test_mode = state.config.test_mode;
    let forced_user = if test_mode {
        headers
            .get("x-djrs-test-user")
            .and_then(|v| v.to_str().ok())
            .map(String::from)
    } else {
        None
    };
    let no_csrf =
        test_mode && headers.get("x-djrs-test-no-csrf").map(|v| v.as_bytes()) == Some(b"1");
    if test_mode && headers.contains_key("x-djrs-test-token") {
        return Outcome::Forward(req);
    }
    let cookies = auth::parse_cookies(
        headers
            .get(http::header::COOKIE)
            .and_then(|v| v.to_str().ok()),
    );
    let authorization = headers
        .get(http::header::AUTHORIZATION)
        .map(|v| v.as_bytes().to_vec());

    let tx = match db.conn.begin().await {
        Ok(tx) => tx,
        Err(_) => return Outcome::Forward(req),
    };

    let input = AuthInput {
        method: &req.parts.method,
        authorization: authorization.as_deref(),
        cookies: &cookies,
        secrets: &state.secrets.keys,
        forced_user_pk: forced_user.as_deref(),
        csrf_exempt_for_tests: no_csrf,
    };
    let auth_result = auth::authenticate(&tx, &settings.auth, view.authenticators, &input).await;
    let first_header = if forced_user.is_some() {
        None
    } else {
        view.authenticators
            .first()
            .and_then(|a| a.authenticate_header())
    };

    let (outcome, session_accessed) = match auth_result {
        Err(AuthError::Forward(reason)) => {
            tracing::debug!(view = view.id, reason, "forward");
            let _ = tx.rollback().await;
            return Outcome::Forward(req);
        }
        Err(AuthError::Failed(detail, session_accessed)) => {
            // AuthenticationFailed: 401 with WWW-Authenticate, or 403 without one.
            let status = if first_header.is_some() { 401 } else { 403 };
            let data = crate::pydict! { "detail" => detail };
            (
                Err(ApiError {
                    status,
                    data,
                    www_authenticate: first_header,
                }),
                session_accessed,
            )
        }
        Ok(a) => {
            let session_accessed = a.session_accessed;
            let user = a.user.clone();
            if !has_permission(view.permissions, user.as_ref(), &req.parts.method) {
                // permission_denied(): NotAuthenticated when nobody authenticated.
                let err = if user.is_none() {
                    let m = &settings.drf.not_authenticated;
                    let status = if first_header.is_some() { 401 } else { 403 };
                    ApiError {
                        status,
                        data: crate::pydict! { "detail" => m.detail },
                        www_authenticate: first_header,
                    }
                } else {
                    ApiError::permission_denied(settings)
                };
                (Err(err), session_accessed)
            } else {
                (Ok(a), session_accessed)
            }
        }
    };

    let method = req.parts.method.clone();
    let result: Result<DrfResponse, Stop> = match outcome {
        Err(api) => Err(Stop::Api(api)),
        Ok(a) => match handlers.iter().find(|(m, _)| *m == method.as_str()) {
            None => {
                let template = settings.drf.method_not_allowed.detail;
                let detail = template.replace("{method}", method.as_str());
                Err(Stop::Api(ApiError {
                    status: 405,
                    data: crate::pydict! { "detail" => detail },
                    www_authenticate: None,
                }))
            }
            Some((_, handler)) => {
                let ctx = Ctx {
                    state: state.clone(),
                    settings,
                    method: method.clone(),
                    kwargs: m.kwargs.clone(),
                    query,
                    user: a.user,
                    auth: a.token,
                    content_type: headers
                        .get(http::header::CONTENT_TYPE)
                        .and_then(|v| v.to_str().ok())
                        .map(String::from),
                    body: req.body.clone(),
                    parsers: view.parsers,
                    data: None,
                    tx: Some(tx),
                };
                let (mut ctx, r) = handler(ctx).await;
                let tx = ctx.tx.take().expect("transaction");
                return finish(view, req, tx, r, session_accessed, settings).await;
            }
        },
    };
    finish(view, req, tx, result, session_accessed, settings).await
}

async fn finish(
    view: &DrfView,
    req: NativeRequest,
    tx: DatabaseTransaction,
    result: Result<DrfResponse, Stop>,
    session_accessed: bool,
    settings: &ProjectSettings,
) -> Outcome {
    let rendered = match result {
        Err(Stop::Forward(reason)) => {
            tracing::debug!(view = view.id, reason, "forward");
            let _ = tx.rollback().await;
            return Outcome::Forward(req);
        }
        Ok(r) => match render(r.status, &r.data, settings) {
            Ok(body) => Rendered {
                status: r.status,
                body,
                www_authenticate: None,
            },
            Err(_) => {
                let _ = tx.rollback().await;
                return Outcome::Forward(req);
            }
        },
        Err(Stop::Api(e)) => match render(e.status, &Some(e.data.clone()), settings) {
            Ok(body) => Rendered {
                status: e.status,
                body,
                www_authenticate: e.www_authenticate,
            },
            Err(_) => {
                let _ = tx.rollback().await;
                return Outcome::Forward(req);
            }
        },
    };
    if tx.commit().await.is_err() {
        // Nothing was committed: Django can still answer from scratch.
        return Outcome::Forward(req);
    }
    Outcome::Respond(build_response(view, rendered, session_accessed, settings))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn negotiation() {
        let q = QueryDict::default();
        let json = &[Renderer::Json];
        let both = &[Renderer::Json, Renderer::BrowsableApi];
        assert!(negotiates_plain_json(None, json, &q));
        assert!(negotiates_plain_json(Some("*/*"), json, &q));
        assert!(negotiates_plain_json(Some("application/json"), both, &q));
        assert!(negotiates_plain_json(
            Some("text/html,application/json"),
            both,
            &q
        ));
        assert!(!negotiates_plain_json(Some("text/html"), both, &q));
        assert!(!negotiates_plain_json(Some("text/html, */*"), both, &q));
        assert!(!negotiates_plain_json(
            Some("application/json; indent=4"),
            json,
            &q
        ));
        assert!(!negotiates_plain_json(Some("text/plain"), json, &q));
        assert!(!negotiates_plain_json(
            None,
            json,
            &QueryDict::parse("format=json")
        ));
    }

    #[test]
    fn form_and_json_data() {
        let parsers = &[Parser::Json, Parser::Form, Parser::MultiPart];
        let d = parse_data(
            Some("application/json"),
            &Bytes::from_static(br#"{"a": 1, "a": 2}"#),
            parsers,
        )
        .unwrap();
        assert_eq!(d.get("a").unwrap(), Some(Py::Int(2)));
        let d = parse_data(
            Some("application/x-www-form-urlencoded"),
            &Bytes::from_static(b"a=1&a=2"),
            parsers,
        )
        .unwrap();
        assert_eq!(d.get("a").unwrap(), Some(Py::from("2")));
        let d = parse_data(None, &Bytes::new(), parsers).unwrap();
        assert_eq!(d.get("a").unwrap(), None);
        assert!(parse_data(
            Some("application/json"),
            &Bytes::from_static(b"{bad"),
            parsers
        )
        .is_err());
        assert!(parse_data(
            Some("application/json"),
            &Bytes::from_static(b"[1]"),
            parsers
        )
        .unwrap()
        .get("a")
        .is_err());
        assert!(parse_data(Some("text/plain"), &Bytes::from_static(b"x"), parsers).is_err());
        assert!(parse_data(
            Some("application/json"),
            &Bytes::from_static(b"{\"n\": 12345678901234567890}"),
            parsers
        )
        .is_err());
    }

    #[test]
    fn vary_patching() {
        let mut h = vec![("Vary".to_string(), "Accept".to_string())];
        patch_vary(&mut h, "Cookie");
        patch_vary(&mut h, "accept");
        assert_eq!(h[0].1, "Accept, Cookie");
    }
}
