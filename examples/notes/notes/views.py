import csv

from django.http import HttpResponse
from django.views.decorators.http import require_GET
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Note
from .serializers import NoteSerializer


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
