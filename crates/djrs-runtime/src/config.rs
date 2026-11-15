//! Settings read from the environment at startup.

use std::collections::HashSet;
use std::net::SocketAddr;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ForwardedHeaders {
    /// Pass X-Forwarded-* through untouched (default: Django sees what it saw before).
    Preserve,
    /// Append the client's address to X-Forwarded-For, and set
    /// X-Forwarded-Proto/Host when absent.
    Append,
}

#[derive(Clone, Debug)]
pub struct Config {
    pub listen: SocketAddr,
    /// Base URL of Django, e.g. http://127.0.0.1:8001
    pub upstream: String,
    /// Send every request to Django, whatever the route table says.
    pub forward_all: bool,
    /// View ids that always go to Django.
    pub force_forward: HashSet<String>,
    pub forwarded_headers: ForwardedHeaders,
    /// Largest request body a Rust handler reads into memory; bigger bodies go to Django.
    pub max_native_body: usize,
    /// Honour the test-only headers sent by `djrs.testing`. Never set in production.
    pub test_mode: bool,
}

#[derive(Debug)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl Config {
    pub fn from_env() -> Result<Config, ConfigError> {
        Config::from_lookup(|k| std::env::var(k).ok())
    }

    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Config, ConfigError> {
        let listen = get("DJRS_LISTEN").unwrap_or_else(|| "0.0.0.0:8000".into());
        let listen: SocketAddr = listen
            .parse()
            .map_err(|e| ConfigError(format!("DJRS_LISTEN={listen}: {e}")))?;
        let upstream = get("DJRS_UPSTREAM").ok_or_else(|| {
            ConfigError("DJRS_UPSTREAM is required, e.g. http://127.0.0.1:8001".into())
        })?;
        let upstream = upstream.trim_end_matches('/').to_string();
        if !upstream.starts_with("http://") {
            return Err(ConfigError(format!(
                "DJRS_UPSTREAM must be an http:// URL, got {upstream}"
            )));
        }
        let force = get("DJRS_FORCE_FORWARD").unwrap_or_default();
        let forward_all = force.trim() == "*" || truthy(get("DJRS_FORWARD_ALL"));
        let force_forward = force
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty() && s != "*")
            .collect();
        let forwarded_headers = match get("DJRS_FORWARDED_HEADERS").as_deref() {
            None | Some("preserve") => ForwardedHeaders::Preserve,
            Some("append") => ForwardedHeaders::Append,
            Some(other) => {
                return Err(ConfigError(format!(
                    "DJRS_FORWARDED_HEADERS must be preserve or append, got {other}"
                )))
            }
        };
        let max_native_body = match get("DJRS_MAX_NATIVE_BODY") {
            None => 2_621_440, // Django's DATA_UPLOAD_MAX_MEMORY_SIZE default
            Some(v) => v
                .parse()
                .map_err(|e| ConfigError(format!("DJRS_MAX_NATIVE_BODY={v}: {e}")))?,
        };
        Ok(Config {
            listen,
            upstream,
            forward_all,
            force_forward,
            forwarded_headers,
            max_native_body,
            test_mode: truthy(get("DJRS_TEST_MODE")),
        })
    }
}

fn truthy(v: Option<String>) -> bool {
    matches!(v.as_deref().map(|s| s.to_ascii_lowercase()), Some(ref s) if s == "1" || s == "true" || s == "yes")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn cfg(pairs: &[(&str, &str)]) -> Result<Config, ConfigError> {
        let m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        Config::from_lookup(|k| m.get(k).cloned())
    }

    #[test]
    fn requires_upstream() {
        assert!(cfg(&[]).is_err());
        let c = cfg(&[("DJRS_UPSTREAM", "http://127.0.0.1:8001/")]).unwrap();
        assert_eq!(c.upstream, "http://127.0.0.1:8001");
        assert!(!c.forward_all);
        assert_eq!(c.forwarded_headers, ForwardedHeaders::Preserve);
    }

    #[test]
    fn switches() {
        let c = cfg(&[
            ("DJRS_UPSTREAM", "http://x:1"),
            ("DJRS_FORCE_FORWARD", "a.b, c.d"),
        ])
        .unwrap();
        assert!(c.force_forward.contains("a.b") && c.force_forward.contains("c.d"));
        let c = cfg(&[("DJRS_UPSTREAM", "http://x:1"), ("DJRS_FORCE_FORWARD", "*")]).unwrap();
        assert!(c.forward_all);
    }
}
