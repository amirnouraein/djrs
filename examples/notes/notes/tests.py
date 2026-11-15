from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import Note


class NotesApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-alice")
        self.bob = User.objects.create_user("bob", password="pw-bob")
        self.a1 = Note.objects.create(owner=self.alice, title="Groceries", body="milk")
        self.a2 = Note.objects.create(owner=self.alice, title="Gym plan", pinned=True)
        self.b1 = Note.objects.create(owner=self.bob, title="Bob's secret")

    # -- plain Django view ---------------------------------------------------

    def test_health(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"ok")

    def test_health_rejects_post(self):
        res = self.client.post("/health")
        self.assertEqual(res.status_code, 405)

    # -- auth ----------------------------------------------------------------

    def test_list_requires_auth(self):
        res = self.client.get("/api/notes/load")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json(), {"detail": "Authentication credentials were not provided."})

    def test_ping_is_public(self):
        res = self.client.get("/api/ping")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"pong": True})

    def test_token_auth(self):
        token = Token.objects.create(user=self.alice)
        self.client.credentials(HTTP_AUTHORIZATION="Token " + token.key)
        res = self.client.get("/api/notes/count")
        self.assertEqual(res.json(), {"count": 2})

    def test_bad_token(self):
        self.client.credentials(HTTP_AUTHORIZATION="Token nope")
        res = self.client.get("/api/notes/count")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json(), {"detail": "Invalid token."})

    def test_session_login(self):
        self.assertTrue(self.client.login(username="bob", password="pw-bob"))
        res = self.client.get("/api/notes/count")
        self.assertEqual(res.json(), {"count": 1})

    def test_session_post_without_csrf_token(self):
        # The in-process test client skips CSRF checks; this must stay true
        # when the tests run through the binary.
        self.client.login(username="bob", password="pw-bob")
        res = self.client.post("/api/notes/create", {"title": "From session"}, format="json")
        self.assertEqual(res.status_code, 201)

    # -- reading -------------------------------------------------------------

    def test_list_only_own_notes_newest_first(self):
        self.client.force_authenticate(self.alice)
        res = self.client.get("/api/notes/load")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual([n["title"] for n in body["data"]], ["Gym plan", "Groceries"])
        self.assertEqual(set(body["data"][0]), {"id", "title", "body", "pinned", "priority", "created_at"})

    def test_list_filter(self):
        self.client.force_authenticate(self.alice)
        res = self.client.get("/api/notes/load", {"q": "groc"})
        self.assertEqual([n["title"] for n in res.json()["data"]], ["Groceries"])

    def test_route_order_load_pinned(self):
        # '^notes/load-pinned' is listed before '^notes/load'.
        self.client.force_authenticate(self.alice)
        res = self.client.get("/api/notes/load-pinned")
        self.assertEqual([n["title"] for n in res.json()["data"]], ["Gym plan"])

    def test_unanchored_prefix_still_matches(self):
        # '^notes/load' has no '$', so anything after it still reaches list_notes.
        self.client.force_authenticate(self.alice)
        res = self.client.get("/api/notes/loadXYZ")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["data"]), 2)

    def test_get_note(self):
        self.client.force_authenticate(self.alice)
        res = self.client.get("/api/notes/get/%d" % self.a1.id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["body"], "milk")

    def test_get_other_users_note_is_404(self):
        self.client.force_authenticate(self.alice)
        res = self.client.get("/api/notes/get/%d" % self.b1.id)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), {"status": "error", "error": "Note not found"})

    def test_non_numeric_id_is_django_404(self):
        self.client.force_authenticate(self.alice)
        res = self.client.get("/api/notes/get/abc")
        self.assertEqual(res.status_code, 404)

    def test_response_data_attribute(self):
        self.client.force_authenticate(self.alice)
        res = self.client.get("/api/notes/count")
        self.assertEqual(res.data["count"], 2)

    # -- writing -------------------------------------------------------------

    def test_create_json(self):
        self.client.force_authenticate(self.alice)
        res = self.client.post("/api/notes/create", {"title": "New", "body": "b", "pinned": True}, format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]
        self.assertEqual((data["title"], data["body"], data["pinned"]), ("New", "b", True))
        self.assertTrue(Note.objects.filter(owner=self.alice, title="New", pinned=True).exists())

    def test_create_form_encoded(self):
        self.client.force_authenticate(self.alice)
        res = self.client.post("/api/notes/create", {"title": "Form"})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["data"]["pinned"], False)

    def test_create_requires_title(self):
        self.client.force_authenticate(self.alice)
        res = self.client.post("/api/notes/create", {"body": "no title"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json(), {"status": "error", "error": "Title is required"})

    def test_create_wrong_method(self):
        self.client.force_authenticate(self.alice)
        res = self.client.get("/api/notes/create")
        self.assertEqual(res.status_code, 405)

    def test_delete(self):
        self.client.force_authenticate(self.alice)
        res = self.client.post("/api/notes/delete", {"note_id": self.a1.id}, format="json")
        self.assertEqual(res.json(), {"status": "success"})
        self.assertFalse(Note.objects.filter(pk=self.a1.id).exists())

    def test_delete_other_users_note(self):
        self.client.force_authenticate(self.alice)
        res = self.client.post("/api/notes/delete", {"note_id": self.b1.id}, format="json")
        self.assertEqual(res.status_code, 404)
        self.assertTrue(Note.objects.filter(pk=self.b1.id).exists())

    # -- class-based view, CSV, unknown paths --------------------------------

    def test_detail_view(self):
        self.client.force_authenticate(self.alice)
        res = self.client.get("/api/notes/%d/" % self.a2.id)
        self.assertEqual(res.json()["title"], "Gym plan")
        res = self.client.delete("/api/notes/%d/" % self.a2.id)
        self.assertEqual(res.status_code, 204)
        self.assertFalse(Note.objects.filter(pk=self.a2.id).exists())

    def test_detail_view_method_not_allowed(self):
        self.client.force_authenticate(self.alice)
        res = self.client.put("/api/notes/%d/" % self.a2.id, {}, format="json")
        self.assertEqual(res.status_code, 405)

    def test_export_csv(self):
        self.client.force_authenticate(self.alice)
        res = self.client.get("/api/notes/export")
        self.assertEqual(res["Content-Type"], "text/csv")
        lines = res.content.decode().splitlines()
        self.assertEqual(lines[0], "id,title")
        self.assertEqual(len(lines), 3)

    def test_unknown_path(self):
        res = self.client.get("/nope")
        self.assertEqual(res.status_code, 404)
