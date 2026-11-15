//! HTTP/1.1 server loop with graceful shutdown on SIGTERM / Ctrl-C.

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;

use crate::app::Dispatcher;

pub async fn serve(dispatcher: Dispatcher) -> std::io::Result<()> {
    let listen = dispatcher.config().listen;
    let listener = TcpListener::bind(listen).await?;
    serve_on(listener, dispatcher, shutdown_signal()).await
}

/// Serve on an already-bound listener until `shutdown` resolves.
pub async fn serve_on(
    listener: TcpListener,
    dispatcher: Dispatcher,
    shutdown: impl std::future::Future<Output = ()>,
) -> std::io::Result<()> {
    let dispatcher = Arc::new(dispatcher);
    let graceful = hyper_util::server::graceful::GracefulShutdown::new();
    tokio::pin!(shutdown);
    tracing::info!(
        listen = %listener.local_addr()?,
        upstream = %dispatcher.config().upstream,
        app = dispatcher.app().name,
        native_views = dispatcher.app().native_views(),
        "djrs gateway started"
    );
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let (stream, peer) = match accepted {
                    Ok(x) => x,
                    Err(err) => {
                        tracing::warn!(error = %err, "accept failed");
                        continue;
                    }
                };
                let _ = stream.set_nodelay(true);
                let d = dispatcher.clone();
                let service = service_fn(move |req: hyper::Request<Incoming>| {
                    let d = d.clone();
                    async move {
                        let req = req.map(Body::new);
                        Ok::<_, std::convert::Infallible>(d.handle(req, peer).await)
                    }
                });
                let conn = http1::Builder::new()
                    .preserve_header_case(true)
                    .keep_alive(true)
                    .serve_connection(TokioIo::new(stream), service);
                let watched = graceful.watch(conn);
                tokio::spawn(async move {
                    if let Err(err) = watched.await {
                        tracing::debug!(error = %err, peer = %peer, "connection closed with error");
                    }
                });
            }
            _ = &mut shutdown => {
                tracing::info!("shutting down");
                break;
            }
        }
    }
    drop(listener);
    tokio::select! {
        _ = graceful.shutdown() => {}
        _ = tokio::time::sleep(Duration::from_secs(30)) => {
            tracing::warn!("timed out waiting for connections to close");
        }
    }
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        if let Ok(mut s) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            s.recv().await;
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! {
        _ = ctrl_c => {}
        _ = term => {}
    }
}
