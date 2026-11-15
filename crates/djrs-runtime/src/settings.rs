//! What the generated code knows about the project, filled in by `djrs generate`
//! from the extracted description. Secrets are never here: they come from the
//! environment at startup.

use crate::db::DjangoDbSettings;
use crate::signing::Algorithm;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SessionFormat {
    /// Django <= 3.0: base64("<sha1 hmac hex>:<json>").
    Legacy,
    /// Django >= 3.1: django.core.signing with compression.
    Signed,
}

#[derive(Clone, Debug)]
pub struct SessionSettings {
    pub cookie_name: &'static str,
    pub table: &'static str,
    pub format: SessionFormat,
    pub key_salt: &'static str,
    pub signer_algorithm: Algorithm,
    pub save_every_request: bool,
}

#[derive(Clone, Debug)]
pub struct TokenSettings {
    pub table: &'static str,
    pub key_column: &'static str,
    pub user_column: &'static str,
}

#[derive(Clone, Debug)]
pub struct UserTable {
    pub table: &'static str,
    pub pk_column: &'static str,
    /// How the primary key is typed (session user ids are its str()).
    pub pk_kind: crate::auth::PkKind,
    pub password_column: Option<&'static str>,
    pub is_active_column: Option<&'static str>,
    pub is_staff_column: Option<&'static str>,
    pub is_superuser_column: Option<&'static str>,
    pub username_column: Option<&'static str>,
}

#[derive(Clone, Debug)]
pub struct AuthSettings {
    pub user: UserTable,
    /// AUTHENTICATION_BACKENDS.
    pub backends: &'static [&'static str],
    /// None when the session engine isn't the database engine with JSON.
    pub session: Option<SessionSettings>,
    pub session_auth_hash: Algorithm,
    pub token: Option<TokenSettings>,
    /// Iterations of the default pbkdf2_sha256 hasher; a stored hash with a
    /// different count would be upgraded (a write) by Django on login.
    pub pbkdf2_iterations: Option<u32>,
}

#[derive(Clone, Debug)]
pub struct DrfMessage {
    pub detail: &'static str,
    pub code: &'static str,
}

#[derive(Clone, Debug)]
pub struct DrfSettings {
    pub unicode_json: bool,
    pub compact_json: bool,
    pub strict_json: bool,
    pub not_authenticated: DrfMessage,
    pub authentication_failed: DrfMessage,
    pub permission_denied: DrfMessage,
    pub not_found: DrfMessage,
    /// Contains `{method}`.
    pub method_not_allowed: DrfMessage,
}

/// Response headers Django's middleware adds, in order, as probed at extract time.
#[derive(Clone, Debug, Default)]
pub struct HeaderProbe {
    pub http: &'static [(&'static str, &'static str)],
    pub https: &'static [(&'static str, &'static str)],
    pub http_session_read: &'static [(&'static str, &'static str)],
}

#[derive(Clone, Debug)]
pub struct ProjectSettings {
    pub allowed_hosts: &'static [&'static str],
    pub debug: bool,
    /// SECURE_PROXY_SSL_HEADER as (header name, value).
    pub secure_proxy_ssl_header: Option<(&'static str, &'static str)>,
    pub use_x_forwarded_host: bool,
    /// django-cors-headers is installed: requests with an Origin header go to Django.
    pub cors_middleware: bool,
    pub response_headers: HeaderProbe,
    pub auth: AuthSettings,
    pub db: DjangoDbSettings,
    pub drf: DrfSettings,
}

/// Secrets, from the environment: DJRS_SECRET_KEY and DJRS_SECRET_KEY_FALLBACKS
/// (comma-separated), the binary's copies of SECRET_KEY / SECRET_KEY_FALLBACKS.
#[derive(Clone, Debug, Default)]
pub struct Secrets {
    pub keys: Vec<String>,
}

impl Secrets {
    pub fn from_env() -> Secrets {
        let mut keys = Vec::new();
        if let Ok(k) = std::env::var("DJRS_SECRET_KEY") {
            if !k.is_empty() {
                keys.push(k);
            }
        }
        if let Ok(f) = std::env::var("DJRS_SECRET_KEY_FALLBACKS") {
            keys.extend(
                f.split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty()),
            );
        }
        Secrets { keys }
    }
}
