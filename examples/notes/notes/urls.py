from django.urls import path, re_path

from . import views

# Order matters: Django tries these top to bottom, and a regex without '$'
# matches any path that starts with it. '^notes/load-pinned' must come before
# '^notes/load', or '/api/notes/load-pinned' would reach list_notes.
urlpatterns = [
    re_path(r"^notes/load-pinned", views.list_pinned_notes),
    re_path(r"^notes/load", views.list_notes),
    re_path(r"^notes/get/(?P<note_id>\d+)$", views.get_note),
    re_path(r"^notes/create", views.create_note),
    re_path(r"^notes/delete", views.delete_note),
    re_path(r"^notes/count", views.count_notes),
    re_path(r"^notes/export", views.export_notes),
    path("notes/<int:note_id>/", views.NoteDetailView.as_view()),
    path("ping", views.public_ping),
]
