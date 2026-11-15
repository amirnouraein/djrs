from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import Note, Tag


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


class MoreViewsTests(APITestCase):
    """Views that exercise more of what the translator supports."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-alice")
        self.bob = User.objects.create_user("bob", password="pw-bob")
        self.a1 = Note.objects.create(owner=self.alice, title="Groceries", body="milk")
        self.a2 = Note.objects.create(owner=self.alice, title="Gym plan", pinned=True)
        self.b1 = Note.objects.create(owner=self.bob, title="Bob's secret")
        self.client.force_authenticate(self.alice)

    # -- save() ----------------------------------------------------------------

    def test_update_note(self):
        before = Note.objects.get(pk=self.a1.pk).updated_at
        res = self.client.post(
            "/api/notes/update/%d" % self.a1.id, {"title": "Groceries 2", "color": "blue", "priority": "5"}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(
            {k: data[k] for k in ("id", "title", "color", "pinned", "priority", "owner")},
            {"id": self.a1.id, "title": "Groceries 2", "color": "blue", "pinned": False, "priority": 5, "owner": self.alice.id},
        )
        stored = Note.objects.get(pk=self.a1.pk)
        self.assertEqual((stored.title, stored.color, stored.priority), ("Groceries 2", "blue", 5))
        self.assertGreater(stored.updated_at, before)

    def test_update_keeps_color_when_not_given(self):
        Note.objects.filter(pk=self.a1.pk).update(color="green")
        res = self.client.post("/api/notes/update/%d" % self.a1.id, {"priority": 2}, format="json")
        self.assertEqual(res.json()["color"], "green")
        self.assertEqual(res.json()["priority"], 2)

    def test_update_blank_title(self):
        res = self.client.post("/api/notes/update/%d" % self.a1.id, {"title": ""}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json(), {"title": ["This field may not be blank."]})
        self.assertEqual(Note.objects.get(pk=self.a1.pk).title, "Groceries")

    def test_update_other_users_note(self):
        res = self.client.post("/api/notes/update/%d" % self.b1.id, {"title": "x"}, format="json")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), {"detail": "Not found."})

    def test_note_color(self):
        url = "/api/notes/%d/color" % self.a1.id
        self.assertEqual(self.client.get(url).json(), {"id": self.a1.id, "color": None, "has_color": False})
        before = Note.objects.get(pk=self.a1.pk).updated_at
        res = self.client.post(url, {"color": "green"}, format="json")
        self.assertEqual(res.json(), {"id": self.a1.id, "color": "green", "has_color": True})
        stored = Note.objects.get(pk=self.a1.pk)
        self.assertEqual(stored.color, "green")
        # save(update_fields=["color"]) leaves auto_now fields alone.
        self.assertEqual(stored.updated_at, before)
        res = self.client.post(url, {}, format="json")
        self.assertEqual(res.json()["color"], None)

    def test_note_color_errors(self):
        url = "/api/notes/%d/color" % self.a1.id
        res = self.client.post(url, {"color": "x" * 21}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json(), {"error": "color is too long"})
        self.assertEqual(self.client.get("/api/notes/%d/color" % self.b1.id).status_code, 404)
        res = self.client.put(url, {}, format="json")
        self.assertEqual(res.status_code, 405)
        self.assertEqual(res.json(), {"detail": 'Method "PUT" not allowed.'})

    # -- update() ----------------------------------------------------------------

    def test_pin_notes(self):
        res = self.client.post("/api/notes/pin", {"ids": [self.a1.id, self.b1.id]}, format="json")
        self.assertEqual(res.json(), {"changed": 1, "pinned": True})
        self.assertTrue(Note.objects.get(pk=self.a1.pk).pinned)
        self.assertFalse(Note.objects.get(pk=self.b1.pk).pinned)
        res = self.client.post("/api/notes/pin", {"ids": [self.a2.id], "pinned": False}, format="json")
        self.assertEqual(res.json(), {"changed": 1, "pinned": False})
        res = self.client.post("/api/notes/pin", {"ids": []}, format="json")
        self.assertEqual(res.json(), {"changed": 0, "pinned": True})

    # -- exclude(), ordering, slicing, first/last/exists ----------------------------

    def _more_notes(self):
        Note.objects.create(owner=self.alice, title="Garden", priority=5, color="red")
        Note.objects.create(owner=self.alice, title="Garage", priority=3)
        Note.objects.create(owner=self.alice, title="Books", priority=3, color="blue")

    def test_search(self):
        self._more_notes()
        body = self.client.get("/api/notes/search").json()
        self.assertEqual(body["count"], 4)
        self.assertEqual([r["label"] for r in body["results"]], ["Books (3)", "Garage (3)", "Groceries (0)"])
        self.assertEqual([r["color"] for r in body["results"]], ["blue", None, None])
        self.assertEqual((body["first"], body["last"], body["more"]), ("Books", "Gym plan", True))

    def test_search_filters(self):
        self._more_notes()
        body = self.client.get("/api/notes/search", {"prefix": "g"}).json()
        self.assertEqual([r["label"] for r in body["results"]], ["Garage (3)", "Groceries (0)", "Gym plan (0)"])
        self.assertEqual(body["more"], False)
        body = self.client.get("/api/notes/search", {"min_priority": "3"}).json()
        self.assertEqual(body["count"], 2)
        body = self.client.get("/api/notes/search", {"prefix": "zzz"}).json()
        self.assertEqual(body, {"count": 0, "results": [], "first": None, "last": None, "more": False})

    # -- datetimes ------------------------------------------------------------------

    def test_recent_notes(self):
        body = self.client.get("/api/notes/recent").json()
        self.assertEqual([n["title"] for n in body["recent"]], ["Gym plan", "Groceries"])
        self.assertEqual(body["older"], 0)
        Note.objects.filter(pk=self.a1.pk).update(created_at=timezone.now() - timedelta(days=30))
        body = self.client.get("/api/notes/recent").json()
        self.assertEqual([n["title"] for n in body["recent"]], ["Gym plan"])
        self.assertEqual(body["older"], 1)

    # -- create(), DRF exceptions, cascading delete -----------------------------------

    def test_create_tag(self):
        res = self.client.post("/api/tags/create", {"name": "work"}, format="json")
        self.assertEqual(res.status_code, 201)
        tag = Tag.objects.get(name="work")
        self.assertEqual(res.json(), {"id": tag.id, "name": "work"})
        res = self.client.post("/api/tags/create", {"name": "WORK"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json(), ["A tag with this name already exists."])
        res = self.client.post("/api/tags/create", {}, format="json")
        self.assertEqual(res.json(), {"name": ["This field is required."]})

    def test_delete_tag_counts_links(self):
        tag = Tag.objects.create(name="home")
        self.a1.tags.add(tag)
        self.a2.tags.add(tag)
        res = self.client.post("/api/tags/%d/delete" % tag.id, format="json")
        self.assertEqual(res.json(), {"deleted": 3})
        self.assertFalse(Tag.objects.filter(pk=tag.pk).exists())
        self.assertEqual(self.a1.tags.count(), 0)
        res = self.client.post("/api/tags/%d/delete" % tag.id, format="json")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), {"detail": "No such tag."})

    # -- a hand-written Rust handler (djrs.toml) --------------------------------------

    def test_priority_stats(self):
        Note.objects.filter(pk=self.a2.pk).update(priority=7)
        self.assertEqual(self.client.get("/api/notes/stats").json(), {"top": 7, "total": 2})
        carol = User.objects.create_user("carol", password="pw")
        self.client.force_authenticate(carol)
        self.assertEqual(self.client.get("/api/notes/stats").json(), {"top": None, "total": 0})
