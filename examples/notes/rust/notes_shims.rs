//! Hand-written Rust for some of the sample project's views.
//!
//! This is what the translator is meant to produce from `notes/views.py`.
//! Each function mirrors its Python view line by line; comments quote the
//! Python. Run the project's tests through the binary to check them.

use djrs_runtime::db::{drf_datetime, now, DjDateTime};
use djrs_runtime::drf::{Ctx, DrfResponse, Stop};
use djrs_runtime::orm::{icontains, py_char, py_int};
use djrs_runtime::py::Py;
use djrs_runtime::{drf_handler, pydict};
use sea_orm::ActiveValue::{NotSet, Set};
use sea_orm::{ColumnTrait, EntityTrait, PaginatorTrait, QueryFilter, QueryOrder, Select};

use crate::entities::notes_note as note;
use crate::entities::notes_note_tags as note_tags;

/// `NoteSerializer(note).data`
fn note_data(n: &note::Model) -> Py {
    pydict! {
        "id" => n.id,
        "title" => n.title.clone(),
        "body" => n.body.clone(),
        "pinned" => n.pinned,
        "priority" => n.priority,
        "created_at" => drf_datetime(&n.created_at.0),
    }
}

/// `owner=request.user` (Python raises for AnonymousUser).
fn owner(ctx: &Ctx) -> Result<sea_orm::Value, Stop> {
    ctx.user_pk_value().ok_or(Stop::Forward("filter(owner=AnonymousUser) raises"))
}

fn owner_id(ctx: &Ctx) -> Result<i32, Stop> {
    match &ctx.user()?.pk {
        Py::Int(i) => i32::try_from(*i).map_err(|_| Stop::Forward("user id out of range")),
        _ => Err(Stop::Forward("user pk is not an int")),
    }
}

/// `Note.Meta.ordering = ["-created_at", "-id"]`
fn ordered(q: Select<note::Entity>) -> Select<note::Entity> {
    q.order_by_desc(note::Column::CreatedAt).order_by_desc(note::Column::Id)
}

fn notes_list(rows: &[note::Model]) -> Py {
    Py::List(rows.iter().map(note_data).collect())
}

// @api_view(["GET"]) @permission_classes([IsAuthenticated])
// def list_notes(request):
async fn list_notes_body(ctx: &mut Ctx) -> Result<DrfResponse, Stop> {
    // notes = Note.objects.filter(owner=request.user)
    let mut q = note::Entity::find().filter(note::Column::OwnerId.eq(owner(ctx)?));
    // q = request.GET.get("q"); if q: notes = notes.filter(title__icontains=q)
    if let Some(s) = ctx.query.get("q") {
        if !s.is_empty() {
            q = q.filter(icontains("title", s));
        }
    }
    let rows = ordered(q).all(ctx.tx()).await?;
    Ok(DrfResponse::ok(pydict! { "status" => "success", "data" => notes_list(&rows) }))
}
drf_handler!(list_notes, list_notes_body);

// def list_pinned_notes(request):
async fn list_pinned_notes_body(ctx: &mut Ctx) -> Result<DrfResponse, Stop> {
    // Note.objects.filter(owner=request.user, pinned=True)
    let q = note::Entity::find()
        .filter(note::Column::OwnerId.eq(owner(ctx)?))
        .filter(note::Column::Pinned.eq(true));
    let rows = ordered(q).all(ctx.tx()).await?;
    Ok(DrfResponse::ok(pydict! { "status" => "success", "data" => notes_list(&rows) }))
}
drf_handler!(list_pinned_notes, list_pinned_notes_body);

// def get_note(request, note_id):
async fn get_note_body(ctx: &mut Ctx) -> Result<DrfResponse, Stop> {
    let note_id = match ctx.kwargs.get("note_id") {
        Some(serde_json::Value::String(s)) => py_int(&Py::Str(s.clone()))?,
        _ => return Err(Stop::Forward("note_id kwarg")),
    };
    // try: note = Note.objects.get(pk=note_id, owner=request.user)
    let found = note::Entity::find()
        .filter(note::Column::Id.eq(note_id))
        .filter(note::Column::OwnerId.eq(owner(ctx)?))
        .one(ctx.tx())
        .await?;
    match found {
        // except Note.DoesNotExist: return Response({...}, status=404)
        None => Ok(DrfResponse::ok(pydict! { "status" => "error", "error" => "Note not found" }).with_status(404)),
        Some(n) => Ok(DrfResponse::ok(pydict! { "status" => "success", "data" => note_data(&n) })),
    }
}
drf_handler!(get_note, get_note_body);

// @api_view(["POST"]) def create_note(request):
async fn create_note_body(ctx: &mut Ctx) -> Result<DrfResponse, Stop> {
    // title = request.data.get("title", "")
    let title = ctx.data()?.get_or("title", Py::from(""))?;
    // if not title: return Response({...}, status=400)
    if !title.truthy() {
        return Ok(DrfResponse::ok(pydict! { "status" => "error", "error" => "Title is required" }).with_status(400));
    }
    let body = ctx.data()?.get_or("body", Py::from(""))?;
    let pinned = ctx.data()?.get_or("pinned", Py::Bool(false))?.truthy();
    // CharField values reach the database through str(); None would be an IntegrityError.
    let title = py_char(&title)?.ok_or(Stop::Forward("title None"))?;
    let body = py_char(&body)?.ok_or(Stop::Forward("body None (IntegrityError)"))?;
    let created_at = now();
    // note = Note.objects.create(owner=request.user, title=..., body=..., pinned=...)
    let row = note::ActiveModel {
        id: NotSet,
        owner_id: Set(owner_id(ctx)?),
        title: Set(title.clone()),
        body: Set(body.clone()),
        pinned: Set(pinned),
        priority: Set(0),
        created_at: Set(DjDateTime(created_at)),
    };
    let id = note::Entity::insert(row).exec(ctx.tx()).await?.last_insert_id;
    // NoteSerializer(note).data reads the Python object, not the stored row.
    let data = pydict! {
        "id" => id,
        "title" => title,
        "body" => body,
        "pinned" => pinned,
        "priority" => 0,
        "created_at" => drf_datetime(&created_at),
    };
    Ok(DrfResponse::ok(pydict! { "status" => "success", "data" => data }).with_status(201))
}
drf_handler!(create_note, create_note_body);

// @api_view(["POST"]) def delete_note(request):
async fn delete_note_body(ctx: &mut Ctx) -> Result<DrfResponse, Stop> {
    // note_id = request.data.get("note_id")
    let note_id = ctx.data()?.get("note_id")?;
    let note_id = match note_id {
        None => None,
        Some(v) => py_int(&v)?,
    };
    let owner = owner(ctx)?;
    // deleted, _ = Note.objects.filter(pk=note_id, owner=request.user).delete()
    // pk=None matches nothing. Otherwise Django's Collector selects the notes,
    // deletes their tag links, then the notes; `deleted` counts both.
    let mut deleted = 0u64;
    if let Some(id) = note_id {
        let ids: Vec<i32> = note::Entity::find()
            .filter(note::Column::Id.eq(id))
            .filter(note::Column::OwnerId.eq(owner))
            .all(ctx.tx())
            .await?
            .into_iter()
            .map(|n| n.id)
            .collect();
        if !ids.is_empty() {
            deleted += note_tags::Entity::delete_many()
                .filter(note_tags::Column::NoteId.is_in(ids.clone()))
                .exec(ctx.tx())
                .await?
                .rows_affected;
            deleted += note::Entity::delete_many()
                .filter(note::Column::Id.is_in(ids))
                .exec(ctx.tx())
                .await?
                .rows_affected;
        }
    }
    if deleted == 0 {
        return Ok(DrfResponse::ok(pydict! { "status" => "error", "error" => "Note not found" }).with_status(404));
    }
    Ok(DrfResponse::ok(pydict! { "status" => "success" }))
}
drf_handler!(delete_note, delete_note_body);

// def count_notes(request):
async fn count_notes_body(ctx: &mut Ctx) -> Result<DrfResponse, Stop> {
    // Note.objects.filter(owner=request.user).count()
    let n = note::Entity::find().filter(note::Column::OwnerId.eq(owner(ctx)?)).count(ctx.tx()).await?;
    Ok(DrfResponse::ok(pydict! { "count" => n as i64 }))
}
drf_handler!(count_notes, count_notes_body);

// @permission_classes([AllowAny]) def public_ping(request):
async fn public_ping_body(_ctx: &mut Ctx) -> Result<DrfResponse, Stop> {
    Ok(DrfResponse::ok(pydict! { "pong" => true }))
}
drf_handler!(public_ping, public_ping_body);
