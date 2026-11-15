//! The dispatcher: answer in Rust, or forward to Django.
//!
//! A request is answered in Rust only when every one of these holds:
//! the URL resolves (in Django's order) to a view with a Rust handler for the
//! request's method, the view isn't switched off, the path is plain enough to
//! match exactly as Django would, and the body is small and of known length.
//! A handler may still hand the request back (`Outcome::Forward`) before it
//! has written anything. Everything else goes to Django unchanged.

use std::collections::HashMap;
use std::future::Future;
use std::net::SocketAddr;
use std::pin::Pin;
use std::sync::Arc;

use axum::body::Body;
use bytes::Bytes;
use http::request::Parts;
use http::{Method, Request, Response};
use http_body_util::BodyExt;

use crate::config::Config;
use crate::proxy::Forwarder;
use crate::urls::{Match, Resolution, UrlTree};

/// A request a Rust handler can read: head, whole body, client address.
pub struct NativeRequest {
    pub parts: Parts,
    pub body: Bytes,
    pub peer: SocketAddr,
}

impl NativeRequest {
    fn into_request(self) -> Request<Body> {
        Request::from_parts(self.parts, Body::from(self.body))
    }
}

pub enum Outcome {
    Respond(Response<Body>),
    /// Not handled after all; nothing was written. Django gets the request as received.
    Forward(NativeRequest),
}

pub type HandlerFuture = Pin<Box<dyn Future<Output = Outcome> + Send>>;
pub type Handler = fn(Arc<State>, NativeRequest, Match) -> HandlerFuture;

/// Shared state handed to handlers (database pool and the like, later).
pub struct State {
    pub config: Config,
}

pub struct App {
    pub name: &'static str,
    pub tree: UrlTree,
    handlers: HashMap<(&'static str, Method), Handler>,
}

impl App {
    pub fn new(name: &'static str, tree: UrlTree) -> App {
        App {
            name,
            tree,
            handlers: HashMap::new(),
        }
    }

    pub fn handler(mut self, view: &'static str, method: Method, handler: Handler) -> App {
        self.handlers.insert((view, method), handler);
        self
    }

    pub fn native_views(&self) -> usize {
        let mut views: Vec<&str> = self.handlers.keys().map(|(v, _)| *v).collect();
        views.sort_unstable();
        views.dedup();
        views.len()
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Decision {
    Native(&'static str),
    Forward(&'static str),
}

pub struct Dispatcher {
    app: App,
    state: Arc<State>,
    forwarder: Forwarder,
}

impl Dispatcher {
    pub fn new(app: App, config: Config) -> Dispatcher {
        let forwarder = Forwarder::new(&config.upstream, config.forwarded_headers);
        Dispatcher {
            app,
            state: Arc::new(State { config }),
            forwarder,
        }
    }

    pub fn config(&self) -> &Config {
        &self.state.config
    }

    pub fn app(&self) -> &App {
        &self.app
    }

    /// Which way a request goes, and why (the reason is logged, never sent).
    pub fn decide(&self, method: &Method, raw_path: &str) -> (Decision, Option<(Handler, Match)>) {
        let cfg = &self.state.config;
        if cfg.forward_all {
            return (Decision::Forward("forward-all switch"), None);
        }
        if self.app.handlers.is_empty() {
            return (Decision::Forward("no Rust handlers"), None);
        }
        let path = match path_info(raw_path) {
            Some(p) => p,
            None => return (Decision::Forward("path needs Django's own decoding"), None),
        };
        match self.app.tree.resolve(&path) {
            Resolution::NoMatch => (Decision::Forward("no route"), None),
            Resolution::Unknown(_) => (Decision::Forward("route can't be decided here"), None),
            Resolution::Matched(m) => {
                if cfg.force_forward.contains(m.view) {
                    return (Decision::Forward("view switched to Django"), None);
                }
                match self.app.handlers.get(&(m.view, method.clone())) {
                    Some(h) => (Decision::Native(m.view), Some((*h, m))),
                    None => (Decision::Forward("view or method not translated"), None),
                }
            }
        }
    }

    pub async fn handle(&self, req: Request<Body>, peer: SocketAddr) -> Response<Body> {
        let (decision, native) = self.decide(req.method(), req.uri().path());
        let (handler, m) = match (decision, native) {
            (Decision::Native(_), Some(n)) => n,
            (Decision::Forward(reason), _) | (Decision::Native(reason), None) => {
                tracing::debug!(path = req.uri().path(), reason, "forward");
                return self.forwarder.forward(req, peer).await;
            }
        };
        // Only bodies of known, modest size are read into memory.
        let len = req
            .headers()
            .get(http::header::CONTENT_LENGTH)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<usize>().ok());
        let chunked = req.headers().contains_key(http::header::TRANSFER_ENCODING);
        if chunked || len.map_or(false, |n| n > self.state.config.max_native_body) {
            return self.forwarder.forward(req, peer).await;
        }
        let (parts, body) = req.into_parts();
        let body = match body.collect().await {
            Ok(b) => b.to_bytes(),
            Err(err) => {
                tracing::warn!(error = %err, "client body error");
                return crate::proxy::plain(http::StatusCode::BAD_REQUEST, "Bad Request");
            }
        };
        let native = NativeRequest { parts, body, peer };
        match handler(self.state.clone(), native, m).await {
            Outcome::Respond(resp) => resp,
            Outcome::Forward(native) => {
                let peer = native.peer;
                self.forwarder.forward(native.into_request(), peer).await
            }
        }
    }
}

/// `request.path_info` as Django computes it, or None when Django would have
/// to repair the path (invalid UTF-8) or `$` semantics could differ (control
/// characters such as a newline).
pub fn path_info(raw: &str) -> Option<String> {
    let decoded = percent_encoding::percent_decode_str(raw)
        .decode_utf8()
        .ok()?;
    if decoded.chars().any(|c| c.is_control()) {
        return None;
    }
    if !decoded.starts_with('/') {
        return None;
    }
    Some(decoded.into_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn path_info_decoding() {
        assert_eq!(path_info("/a%20b").as_deref(), Some("/a b"));
        assert_eq!(path_info("/caf%C3%A9").as_deref(), Some("/café"));
        assert_eq!(path_info("/bad%FF"), None);
        assert_eq!(path_info("/nl%0A"), None);
    }
}
