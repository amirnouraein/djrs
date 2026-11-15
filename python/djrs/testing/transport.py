"""Send Django test-client requests over real HTTP.

The test client builds a WSGI environ and hands it to a ClientHandler. We
replace `ClientHandler.__call__` (which DRF's ForceAuthClientHandler inherits)
so the environ becomes an HTTP request to the binary, and the HTTP response
becomes a Django HttpResponse the client can use as usual. Everything the
client does around the handler (cookies, redirects, exception re-raising,
`.json()`) is untouched.
"""

from __future__ import annotations

import http.client
import json
from http.cookies import SimpleCookie
from urllib.parse import quote, urlsplit

from .middleware import HTTPS_HEADER, NO_CSRF_HEADER, TOKEN_HEADER, USER_HEADER

_original_call = None
_base = None
# Compare mode: GET requests are also sent straight to Django, and any
# difference in status, headers or body is recorded.
_compare_base = None
mismatches = []
compared = [0]
IGNORED_HEADERS = {"date", "server", "connection", "keep-alive"}

# Characters Django's test client leaves unescaped in PATH_INFO round trips.
_PATH_SAFE = "/:@!$&'()*+,;=-._~"


def _header_name(meta_key):
    return "-".join(part.capitalize() for part in meta_key.split("_"))


def environ_to_request(environ):
    method = environ["REQUEST_METHOD"]
    # PATH_INFO is a decoded WSGI string (latin-1 code points); re-encode it.
    path = quote(environ.get("PATH_INFO", "/").encode("iso-8859-1"), safe=_PATH_SAFE)
    script = environ.get("SCRIPT_NAME", "")
    if script:
        path = quote(script.encode("iso-8859-1"), safe=_PATH_SAFE) + path
    query = environ.get("QUERY_STRING", "")
    url = path + ("?" + query if query else "")

    headers = []
    for key, value in environ.items():
        if not isinstance(value, str):
            continue
        if key.startswith("HTTP_"):
            headers.append((_header_name(key[5:]), value))
        elif key == "CONTENT_TYPE" and value:
            headers.append(("Content-Type", value))
    if "HTTP_HOST" not in environ:
        host = environ.get("SERVER_NAME", "testserver")
        port = str(environ.get("SERVER_PORT", "80"))
        scheme = environ.get("wsgi.url_scheme", "http")
        if (scheme, port) not in (("http", "80"), ("https", "443")):
            host = "%s:%s" % (host, port)
        headers.append(("Host", host))

    length = int(environ.get("CONTENT_LENGTH") or 0)
    body = environ["wsgi.input"].read(length) if length else b""
    return method, url, headers, body


def _response_from_http(status, reason, header_list, body):
    from django.http import HttpResponse

    response = HttpResponse(content=body, status=status, reason=reason)
    if "Content-Type" in response:
        del response["Content-Type"]
    merged = {}
    order = []
    for name, value in header_list:
        lower = name.lower()
        if lower == "set-cookie":
            response.cookies.load(value)
            continue
        if lower in ("content-length", "connection", "keep-alive", "transfer-encoding", "date", "server"):
            continue
        if lower not in merged:
            order.append(name)
            merged[lower] = value
        else:
            merged[lower] = merged[lower] + ", " + value
    for name in order:
        response[name] = merged[name.lower()]
    location = merged.get("location")
    if status in (301, 302, 303, 307, 308) and location is not None:
        response.url = location  # HttpResponseRedirect.url
    content_type = merged.get("content-type", "")
    if content_type.startswith("application/json") or "+json" in content_type:
        try:
            response.data = json.loads(body.decode("utf-8")) if body else None
        except ValueError:
            pass
    return response


def _http_call(self, environ):
    from django.core.handlers.wsgi import WSGIRequest

    method, url, headers, body = environ_to_request(environ)
    if not getattr(self, "enforce_csrf_checks", False):
        headers.append((_header_name(NO_CSRF_HEADER[5:]), "1"))
    if environ.get("wsgi.url_scheme") == "https":
        headers.append((_header_name(HTTPS_HEADER[5:]), "1"))
    force_user = getattr(self, "_force_user", None)
    force_token = getattr(self, "_force_token", None)
    if force_user is not None:
        headers.append((_header_name(USER_HEADER[5:]), str(force_user.pk)))
    if force_token is not None:
        headers.append((_header_name(TOKEN_HEADER[5:]), str(getattr(force_token, "key", force_token))))

    status, reason, header_list, data = _send(_base, method, url, headers, body)
    if _compare_base and method == "GET":
        _compare(url, headers, (status, header_list, data))
    response = _response_from_http(status, reason, header_list, data)
    response.wsgi_request = WSGIRequest(environ)
    return response


def _send(base, method, url, headers, body):
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=120)
    try:
        conn.putrequest(method, url, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            # WSGI strings are latin-1; tests sometimes put other characters
            # in headers, which only UTF-8 bytes can carry over the wire.
            try:
                conn.putheader(name, value.encode("latin-1"))
            except UnicodeEncodeError:
                conn.putheader(name, value.encode("utf-8"))
        if body or method in ("POST", "PUT", "PATCH"):
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body if body else None)
        resp = conn.getresponse()
        return resp.status, resp.reason, resp.getheaders(), resp.read()
    finally:
        conn.close()


def _comparable(header_list):
    out = []
    for k, v in header_list:
        k = k.lower()
        if k in IGNORED_HEADERS:
            continue
        if k == "allow":
            # @api_view builds its method list from a set: the order differs
            # between Django processes, so any order is Django's answer.
            v = ", ".join(sorted(p.strip() for p in v.split(",")))
        out.append((k, v))
    return out


def _compare(url, headers, got):
    status, header_list, data = got
    d_status, _reason, d_headers, d_data = _send(_compare_base, "GET", url, headers, b"")
    compared[0] += 1
    diffs = []
    if status != d_status:
        diffs.append("status %s vs Django %s" % (status, d_status))
    if _comparable(header_list) != _comparable(d_headers):
        diffs.append("headers %s vs Django %s" % (_comparable(header_list), _comparable(d_headers)))
    if data != d_data:
        diffs.append("body %r vs Django %r" % (data[:300], d_data[:300]))
    if diffs:
        mismatches.append({"url": url, "diffs": diffs})


def install_transport(base_url, compare_base=None):
    global _original_call, _base, _compare_base
    from django.test.client import ClientHandler

    if _original_call is None:
        _original_call = ClientHandler.__call__
    _base = base_url
    _compare_base = compare_base
    ClientHandler.__call__ = _http_call


def uninstall_transport():
    global _original_call
    from django.test.client import ClientHandler

    if _original_call is not None:
        ClientHandler.__call__ = _original_call
        _original_call = None
