"""Run a project's own Django tests through a djrs binary.

    DJRS_BINARY=path/to/gateway python manage.py test --testrunner djrs.testing.DjrsTestRunner

Without DJRS_BINARY the runner is Django's DiscoverRunner, unchanged.

With it:

* Django is served in-process on a free port (so forwarded requests still see
  the test's data, mocks and signal receivers), and the binary is started in
  front of it.
* The test client sends real HTTP to the binary instead of calling Django
  directly. Its shortcuts (force_authenticate, CSRF checks off) travel as
  test-only headers that `djrs.testing.middleware` and the binary's test mode
  turn back into the same request state.
* Test data is committed so the binary's own database connection can see it:
  TestCase behaves like TransactionTestCase (setUpTestData runs per test, the
  database is flushed between tests). SQLite test databases are put in a file.

Known differences from in-process tests: `response.context` is not available,
and a response is a plain HttpResponse rebuilt from the bytes on the wire
(`response.data` is the parsed JSON body, when there is one).
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
import time

from django.test.runner import DiscoverRunner

from .transport import install_transport, uninstall_transport

MIDDLEWARE_PATH = "djrs.testing.middleware.TestHooksMiddleware"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_port(port, proc, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError("djrs binary exited with code %s" % proc.returncode)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("timed out waiting for 127.0.0.1:%d" % port)


class _DjangoServer:
    """The project's WSGI app on a free port, in a background thread."""

    def __init__(self):
        from django.core.handlers.wsgi import WSGIHandler
        from django.core.servers.basehttp import ThreadedWSGIServer, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log_message(self, *args):
                pass

        self.port = _free_port()
        self.httpd = ThreadedWSGIServer(("127.0.0.1", self.port), QuietHandler, allow_reuse_address=False)
        self.httpd.set_app(WSGIHandler())
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="djrs-django", daemon=True)

    def start(self):
        self.thread.start()
        _wait_for_port(self.port, None)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class DjrsTestRunner(DiscoverRunner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.binary = os.environ.get("DJRS_BINARY")
        self._tmpdir = None
        self._django = None
        self._proc = None

    # -- environment ------------------------------------------------------------

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        if not self.binary:
            return
        from django.conf import settings
        from django.db import connections
        from django.test import TestCase

        if MIDDLEWARE_PATH not in settings.MIDDLEWARE:
            settings.MIDDLEWARE = [MIDDLEWARE_PATH] + list(settings.MIDDLEWARE)

        # Commit test data so a second process (the binary) can read it.
        TestCase._databases_support_transactions = classmethod(lambda cls: False)

        self._tmpdir = tempfile.mkdtemp(prefix="djrs-test-")
        for alias in connections:
            conf = connections.databases[alias]
            if conf.get("ENGINE", "").endswith("sqlite3"):
                conf.setdefault("TEST", {})
                conf["TEST"]["NAME"] = os.path.join(self._tmpdir, "test_%s.sqlite3" % alias)

    def setup_databases(self, **kwargs):
        old = super().setup_databases(**kwargs)
        if self.binary:
            self._start()
        return old

    def teardown_databases(self, old_config, **kwargs):
        if self.binary:
            self._stop()
        super().teardown_databases(old_config, **kwargs)

    # -- processes --------------------------------------------------------------

    def _binary_env(self, django_port, listen_port):
        from django.db import connections

        env = dict(os.environ)
        env.update(
            {
                "DJRS_UPSTREAM": "http://127.0.0.1:%d" % django_port,
                "DJRS_LISTEN": "127.0.0.1:%d" % listen_port,
                "DJRS_TEST_MODE": "1",
                "DJRS_LOG": os.environ.get("DJRS_LOG", "warn"),
            }
        )
        conf = connections["default"].settings_dict
        env["DJRS_DATABASE_ENGINE"] = conf.get("ENGINE", "")
        env["DJRS_DATABASE_NAME"] = str(conf.get("NAME", ""))
        for key in ("USER", "PASSWORD", "HOST", "PORT"):
            env["DJRS_DATABASE_" + key] = str(conf.get(key) or "")
        return env

    def _start(self):
        self._django = _DjangoServer()
        self._django.start()
        listen = _free_port()
        self._proc = subprocess.Popen([self.binary], env=self._binary_env(self._django.port, listen))
        _wait_for_port(listen, self._proc)
        install_transport("http://127.0.0.1:%d" % listen)
        if self.verbosity >= 1:
            print("djrs: tests go through %s on port %d (Django on port %d)" % (self.binary, listen, self._django.port))

    def _stop(self):
        uninstall_transport()
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._django is not None:
            self._django.stop()
