from django.contrib.auth.models import User
from rest_framework import serializers

from .models import Note, Reminder, Tag


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name"]


class NoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Note
        fields = ["id", "title", "body", "pinned", "priority", "created_at"]


class NoteDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Note
        fields = ["id", "title", "color", "pinned", "priority", "owner", "updated_at"]


class ReminderSerializer(serializers.ModelSerializer):
    shout = serializers.SerializerMethodField()

    class Meta:
        model = Reminder
        fields = ["id", "note", "label", "due", "shout"]

    def get_shout(self, obj):
        if obj.label:
            return obj.label.upper() + "!"
        return None


class OwnerSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username"]


class NoteOverviewSerializer(serializers.ModelSerializer):
    owner = OwnerSerializer()
    reminders = ReminderSerializer(many=True, read_only=True)

    class Meta:
        model = Note
        fields = ["id", "title", "owner", "reminders"]
