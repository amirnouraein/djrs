import uuid

from django.conf import settings
from django.db import models


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        ordering = ["name"]


class Note(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notes")
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")
    pinned = models.BooleanField(default=False)
    priority = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    color = models.CharField(max_length=20, null=True, blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="notes")

    class Meta:
        ordering = ["-created_at", "-id"]


class Reminder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    note = models.ForeignKey(Note, on_delete=models.CASCADE, related_name="reminders")
    label = models.CharField(max_length=100)
    due = models.DateField()

    class Meta:
        ordering = ["due", "label"]
