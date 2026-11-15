import csv
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Count, Max
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Note, Reminder, Tag
from .serializers import NoteDetailSerializer, NoteOverviewSerializer, NoteSerializer, ReminderSerializer, TagSerializer


@require_GET
def health(request):
    return HttpResponse("ok")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_notes(request):
    notes = Note.objects.filter(owner=request.user)
    q = request.GET.get("q")
    if q:
        notes = notes.filter(title__icontains=q)
    return Response({"status": "success", "data": NoteSerializer(notes, many=True).data})


@api_view(["GET"])
def list_pinned_notes(request):
    notes = Note.objects.filter(owner=request.user, pinned=True)
    return Response({"status": "success", "data": NoteSerializer(notes, many=True).data})


@api_view(["GET"])
def get_note(request, note_id):
    try:
        note = Note.objects.get(pk=note_id, owner=request.user)
    except Note.DoesNotExist:
        return Response({"status": "error", "error": "Note not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response({"status": "success", "data": NoteSerializer(note).data})


@api_view(["POST"])
def create_note(request):
    title = request.data.get("title", "")
    if not title:
        return Response({"status": "error", "error": "Title is required"}, status=status.HTTP_400_BAD_REQUEST)
    note = Note.objects.create(
        owner=request.user,
        title=title,
        body=request.data.get("body", ""),
        pinned=bool(request.data.get("pinned", False)),
    )
    return Response({"status": "success", "data": NoteSerializer(note).data}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def delete_note(request):
    note_id = request.data.get("note_id")
    deleted, _ = Note.objects.filter(pk=note_id, owner=request.user).delete()
    if not deleted:
        return Response({"status": "error", "error": "Note not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response({"status": "success"})


@api_view(["GET"])
def count_notes(request):
    return Response({"count": Note.objects.filter(owner=request.user).count()})


@api_view(["GET"])
@permission_classes([AllowAny])
def public_ping(request):
    return Response({"pong": True})


@api_view(["POST"])
def update_note(request, note_id):
    try:
        note = Note.objects.get(pk=note_id, owner=request.user)
    except Note.DoesNotExist:
        raise NotFound()
    if "title" in request.data:
        title = request.data["title"]
        if not title:
            raise ValidationError({"title": ["This field may not be blank."]})
        note.title = title
    note.color = request.data.get("color", note.color)
    if "priority" in request.data:
        note.priority = int(request.data["priority"])
    note.save()
    return Response(NoteDetailSerializer(note).data)


@api_view(["GET", "POST"])
def note_color(request, note_id):
    note = Note.objects.filter(pk=note_id, owner=request.user).first()
    if not note:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "POST":
        color = request.data.get("color")
        if color is not None and len(color) > 20:
            return Response({"error": "color is too long"}, status=status.HTTP_400_BAD_REQUEST)
        note.color = color
        note.save(update_fields=["color"])
    return Response({"id": note.id, "color": note.color, "has_color": note.color is not None})


@api_view(["POST"])
def pin_notes(request):
    ids = request.data.get("ids", [])
    pinned = bool(request.data.get("pinned", True))
    changed = Note.objects.filter(owner=request.user, id__in=ids).update(pinned=pinned)
    return Response({"changed": changed, "pinned": pinned})


@api_view(["GET"])
def search_notes(request):
    # Notes colored red are hidden; notes without a color are kept (exclude() keeps NULLs).
    notes = Note.objects.filter(owner=request.user).exclude(color="red")
    prefix = request.query_params.get("prefix")
    if prefix is not None:
        notes = notes.filter(title__istartswith=prefix)
    min_priority = request.query_params.get("min_priority")
    if min_priority:
        notes = notes.filter(priority__gte=int(min_priority))
    notes = notes.order_by("-priority", "title")
    results = []
    for note in notes[:3]:
        results.append({"id": note.id, "label": f"{note.title} ({note.priority})", "color": note.color})
    first = notes.first()
    return Response(
        {
            "count": notes.count(),
            "results": results,
            "first": first.title if first else None,
            "last": notes.last().title if notes.exists() else None,
            "more": len(results) < notes.count(),
        }
    )


@api_view(["GET"])
def recent_notes(request):
    since = timezone.now() - timedelta(days=7)
    recent = Note.objects.filter(owner=request.user, created_at__gte=since)
    older = Note.objects.filter(owner=request.user, created_at__lt=since)
    return Response({"recent": NoteDetailSerializer(recent, many=True).data, "older": older.count()})


@api_view(["POST"])
def create_tag(request):
    name = request.data.get("name")
    if not name:
        raise ValidationError({"name": ["This field is required."]})
    if Tag.objects.filter(name__iexact=name).exists():
        raise ValidationError("A tag with this name already exists.")
    tag = Tag.objects.create(name=name)
    return Response(TagSerializer(tag).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def delete_tag(request, tag_id):
    tag = Tag.objects.filter(pk=tag_id).first()
    if tag is None:
        raise NotFound("No such tag.")
    count, _ = tag.delete()
    return Response({"deleted": count})


@api_view(["GET"])
def priority_stats(request):
    # aggregate() isn't translated: this view has a hand-written Rust handler (djrs.toml).
    stats = Note.objects.filter(owner=request.user).aggregate(top=Max("priority"), total=Count("id"))
    return Response({"top": stats["top"], "total": stats["total"]})


def _error(message, code=status.HTTP_400_BAD_REQUEST):
    return Response({"status": "error", "error": message}, status=code)


@api_view(["POST"])
def add_reminder(request, note_id):
    try:
        note = Note.objects.get(pk=note_id, owner=request.user)
    except Note.DoesNotExist:
        return _error("Note not found", code=status.HTTP_404_NOT_FOUND)
    label = request.data.get("label", "")
    try:
        due = datetime.strptime(request.data.get("due"), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return _error("due must be YYYY-MM-DD")
    with transaction.atomic():
        reminder = Reminder.objects.create(note=note, label=label, due=due)
    return Response(ReminderSerializer(reminder).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def get_reminder(request):
    rid = request.query_params.get("id")
    reminder = Reminder.objects.filter(pk=rid).first()
    if reminder is None or reminder.note.owner != request.user:
        return _error("Reminder not found", code=status.HTTP_404_NOT_FOUND)
    return Response(
        {
            "reminder": ReminderSerializer(reminder).data,
            "id": str(reminder.id),
            # A UUID never equals its string form in Python.
            "same_as_query": reminder.id == rid,
            "due": f"{reminder.due}",
        }
    )


@api_view(["GET"])
def note_overview(request, note_id):
    note = Note.objects.filter(pk=note_id, owner=request.user).first()
    if not note:
        return _error("Note not found", code=status.HTTP_404_NOT_FOUND)
    return Response(NoteOverviewSerializer(note).data)


@api_view(["GET"])
def export_notes(request):
    # Uses the csv module and a plain HttpResponse: a view djrs should forward.
    response = HttpResponse(content_type="text/csv")
    writer = csv.writer(response)
    writer.writerow(["id", "title"])
    for note in Note.objects.filter(owner=request.user).order_by("id"):
        writer.writerow([note.id, note.title])
    return response


class NoteDetailView(APIView):
    def get(self, request, note_id):
        try:
            note = Note.objects.get(pk=note_id, owner=request.user)
        except Note.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(NoteSerializer(note).data)

    def delete(self, request, note_id):
        Note.objects.filter(pk=note_id, owner=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
