from django.urls import include, path, re_path

from notes import views

urlpatterns = [
    path("health", views.health),
    re_path(r"^api/", include("notes.urls")),
]
