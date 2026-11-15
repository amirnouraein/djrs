"""Django side of the test hook. Installed only by DjrsTestRunner, never in production.

The in-process test client can authenticate a user without credentials
(`force_authenticate`, DRF) and skips CSRF checks by default. Over real HTTP
those shortcuts are carried as headers that only the runner adds, and this
middleware turns them back into exactly what the in-process client would have
set on the request.
"""

from __future__ import annotations

USER_HEADER = "HTTP_X_DJRS_TEST_USER"
TOKEN_HEADER = "HTTP_X_DJRS_TEST_TOKEN"
NO_CSRF_HEADER = "HTTP_X_DJRS_TEST_NO_CSRF"
HTTPS_HEADER = "HTTP_X_DJRS_TEST_HTTPS"


class TestHooksMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        meta = request.META
        if meta.pop(NO_CSRF_HEADER, None) == "1":
            request._dont_enforce_csrf_checks = True
        if meta.pop(HTTPS_HEADER, None) == "1":
            meta["wsgi.url_scheme"] = "https"
        user_pk = meta.pop(USER_HEADER, None)
        token_key = meta.pop(TOKEN_HEADER, None)
        if user_pk is not None:
            from django.contrib.auth import get_user_model

            User = get_user_model()
            request._force_auth_user = User._default_manager.get(pk=User._meta.pk.to_python(user_pk))
        if token_key is not None:
            try:
                from rest_framework.authtoken.models import Token

                request._force_auth_token = Token.objects.get(key=token_key)
            except Exception:
                request._force_auth_token = token_key
        return self.get_response(request)
