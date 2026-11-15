//! Transparent reverse proxy to Django.
//!
//! The request goes out with the same method, path, query string, headers and
//! body (streamed, never buffered), and the response comes back the same way.
//! Header name case is kept in both directions. Only hop-by-hop headers are
//! dropped, as HTTP requires of any proxy.

use std::net::SocketAddr;

use axum::body::Body;
use http::header::{HeaderMap, HeaderName, HeaderValue};
use http::{Request, Response, StatusCode, Uri};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;

use crate::config::ForwardedHeaders;

const HOP_BY_HOP: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

#[derive(Clone)]
pub struct Forwarder {
    client: Client<HttpConnector, Body>,
    upstream: String,
    mode: ForwardedHeaders,
}

impl Forwarder {
    pub fn new(upstream: &str, mode: ForwardedHeaders) -> Forwarder {
        let mut connector = HttpConnector::new();
        connector.set_nodelay(true);
        let client = Client::builder(TokioExecutor::new())
            .http1_preserve_header_case(true)
            .http1_title_case_headers(false)
            .pool_max_idle_per_host(64)
            .build(connector);
        Forwarder {
            client,
            upstream: upstream.to_string(),
            mode,
        }
    }

    pub async fn forward(&self, mut req: Request<Body>, peer: SocketAddr) -> Response<Body> {
        let path_and_query = req
            .uri()
            .path_and_query()
            .map(|p| p.as_str())
            .unwrap_or("/")
            .to_string();
        let uri: Uri = match format!("{}{}", self.upstream, path_and_query).parse() {
            Ok(u) => u,
            Err(_) => return plain(StatusCode::BAD_REQUEST, "Bad Request"),
        };
        *req.uri_mut() = uri;
        *req.version_mut() = http::Version::HTTP_11;
        strip_hop_by_hop(req.headers_mut());
        if self.mode == ForwardedHeaders::Append {
            add_forwarded(req.headers_mut(), peer);
        }
        match self.client.request(req).await {
            Ok(resp) => {
                let (mut parts, body) = resp.into_parts();
                strip_hop_by_hop(&mut parts.headers);
                Response::from_parts(parts, Body::new(body))
            }
            Err(err) => {
                tracing::error!(error = %err, "upstream request failed");
                plain(StatusCode::BAD_GATEWAY, "Bad Gateway")
            }
        }
    }
}

fn strip_hop_by_hop(headers: &mut HeaderMap) {
    // Headers named in Connection are hop-by-hop too.
    let listed: Vec<HeaderName> = headers
        .get_all(http::header::CONNECTION)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(','))
        .filter_map(|name| HeaderName::from_bytes(name.trim().as_bytes()).ok())
        .collect();
    for name in listed {
        headers.remove(name);
    }
    for name in HOP_BY_HOP {
        headers.remove(*name);
    }
}

fn add_forwarded(headers: &mut HeaderMap, peer: SocketAddr) {
    let ip = peer.ip().to_string();
    let xff = match headers.get("x-forwarded-for").and_then(|v| v.to_str().ok()) {
        Some(existing) if !existing.is_empty() => format!("{existing}, {ip}"),
        _ => ip,
    };
    if let Ok(v) = HeaderValue::from_str(&xff) {
        headers.insert("x-forwarded-for", v);
    }
    if !headers.contains_key("x-forwarded-proto") {
        headers.insert("x-forwarded-proto", HeaderValue::from_static("http"));
    }
    if !headers.contains_key("x-forwarded-host") {
        if let Some(host) = headers.get(http::header::HOST).cloned() {
            headers.insert("x-forwarded-host", host);
        }
    }
}

pub fn plain(status: StatusCode, text: &'static str) -> Response<Body> {
    Response::builder()
        .status(status)
        .header("content-type", "text/plain; charset=utf-8")
        .body(Body::from(text))
        .expect("static response")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drops_hop_by_hop_and_connection_listed() {
        let mut h = HeaderMap::new();
        h.insert(
            "connection",
            HeaderValue::from_static("keep-alive, x-secret-hop"),
        );
        h.insert("x-secret-hop", HeaderValue::from_static("1"));
        h.insert("transfer-encoding", HeaderValue::from_static("chunked"));
        h.insert("x-keep", HeaderValue::from_static("1"));
        strip_hop_by_hop(&mut h);
        assert!(h.get("x-secret-hop").is_none());
        assert!(h.get("transfer-encoding").is_none());
        assert!(h.get("connection").is_none());
        assert!(h.get("x-keep").is_some());
    }

    #[test]
    fn appends_forwarded_for() {
        let mut h = HeaderMap::new();
        h.insert("x-forwarded-for", HeaderValue::from_static("1.2.3.4"));
        add_forwarded(&mut h, "10.0.0.1:5000".parse().unwrap());
        assert_eq!(h.get("x-forwarded-for").unwrap(), "1.2.3.4, 10.0.0.1");
        assert_eq!(h.get("x-forwarded-proto").unwrap(), "http");
    }
}
