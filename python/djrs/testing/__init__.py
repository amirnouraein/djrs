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
* When the binary answers nothing in Rust (forward-only), Django's in-process
  server uses the test's own database connection, so TestCase transactions
  behave exactly as they do in-process.
* When the binary has Rust handlers, test data must be committed so its own
  database connection can see it: TestCase then behaves like
  TransactionTestCase (setUpTestData runs per test, the database is flushed
  between tests), and SQLite test databases are put in a file. Code that
  depends on transactions (on_commit, select_for_update) can behave
  differently in this mode. DJRS_TEST_COMMIT=0/1 overrides the choice.

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
    """The project's WSGI app on a free port, in a background thread.

    With `shared` connections, every request thread uses the test thread's
    database connections (the test thread is blocked waiting for the response,
    so they are never used at the same time). Requests then see the data of
    the running TestCase transaction, as in-process requests do.
    """

    def __init__(self, shared=None):
        from django.core.handlers.wsgi import WSGIHandler
        from django.core.servers.basehttp import ThreadedWSGIServer, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log_message(self, *args):
                pass

        app = WSGIHandler()
        if shared:
            from django.db import connections

            def shared_app(environ, start_response):
                for alias, conn in shared.items():
                    connections[alias] = conn
                return app(environ, start_response)

            handler = shared_app
        else:
            handler = app
        self.port = _free_port()
        self.httpd = ThreadedWSGIServer(("127.0.0.1", self.port), QuietHandler, allow_reuse_address=False)
        self.httpd.set_app(handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="djrs-django", daemon=True)

    def start(self):
        self.thread.start()
        _wait_for_port(self.port, None)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def binary_info(binary):
    """What the binary says about itself (`--djrs-info`)."""
    import json

    try:
        out = subprocess.run([binary, "--djrs-info"], capture_output=True, timeout=30, check=True).stdout
        return json.loads(out.decode("utf-8"))
    except Exception:
        return {}


class DjrsTestRunner(DiscoverRunner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.binary = os.environ.get("DJRS_BINARY")
        self._tmpdir = None
        self._django = None
        self._proc = None
        self._shared = None
        self.commit = False
        if self.binary:
            choice = os.environ.get("DJRS_TEST_COMMIT", "auto")
            if choice == "auto":
                self.commit = binary_info(self.binary).get("native_views", 1) > 0
            else:
                self.commit = choice == "1"

    def run_tests(self, *args, **kwargs):
        from . import transport

        failures = super().run_tests(*args, **kwargs)
        if os.environ.get("DJRS_COMPARE_STRICT") == "1" and transport.mismatches:
            failures += len(transport.mismatches)
        return failures

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

        if not self.commit:
            return
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
        from django.conf import settings

        # Test-only copies of the secrets, so the binary can read sessions.
        env["DJRS_SECRET_KEY"] = settings.SECRET_KEY
        fallbacks = getattr(settings, "SECRET_KEY_FALLBACKS", None) or []
        env["DJRS_SECRET_KEY_FALLBACKS"] = ",".join(fallbacks)
        conf = connections["default"].settings_dict
        env["DJRS_DATABASE_ENGINE"] = conf.get("ENGINE", "")
        env["DJRS_DATABASE_NAME"] = str(conf.get("NAME", ""))
        for key in ("USER", "PASSWORD", "HOST", "PORT"):
            env["DJRS_DATABASE_" + key] = str(conf.get(key) or "")
        return env

    def _start(self):
        shared = None
        if not self.commit:
            from django.core.signals import request_finished, request_started
            from django.db import close_old_connections, connections

            # The in-process test client does the same: requests must not
            # close the test's connection.
            request_started.disconnect(close_old_connections)
            request_finished.disconnect(close_old_connections)
            shared = {}
            for alias in connections:
                conn = connections[alias]
                conn.inc_thread_sharing()
                shared[alias] = conn
            self._shared = shared
        self._django = _DjangoServer(shared)
        self._django.start()
        listen = _free_port()
        self._proc = subprocess.Popen([self.binary], env=self._binary_env(self._django.port, listen))
        _wait_for_port(listen, self._proc)
        compare = os.environ.get("DJRS_COMPARE") == "1"
        install_transport("http://127.0.0.1:%d" % listen, "http://127.0.0.1:%d" % self._django.port if compare else None)
        if self.verbosity >= 1:
            mode = "test data committed" if self.commit else "forward-only, shared test connection"
            print("djrs: tests go through %s on port %d (Django on port %d; %s)" % (self.binary, listen, self._django.port, mode))

    def _stop(self):
        from . import transport

        uninstall_transport()
        if os.environ.get("DJRS_COMPARE") == "1":
            print("djrs: compared %d GET responses with Django: %d differ" % (transport.compared[0], len(transport.mismatches)))
            for m in transport.mismatches:
                print("  %s" % m["url"])
                for d in m["diffs"]:
                    print("    %s" % d)
            report = os.environ.get("DJRS_COMPARE_REPORT")
            if report:
                import json

                with open(report, "w") as f:
                    json.dump({"compared": transport.compared[0], "mismatches": transport.mismatches}, f, indent=1)
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._django is not None:
            self._django.stop()
        if self._shared:
            from django.core.signals import request_finished, request_started
            from django.db import close_old_connections

            for conn in self._shared.values():
                conn.dec_thread_sharing()
            request_started.connect(close_old_connections)
            request_finished.connect(close_old_connections)
            self._shared = None
