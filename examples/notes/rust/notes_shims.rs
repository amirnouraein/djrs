//! Hand-written Rust for a view the translator doesn't handle yet.
//!
//! Every other DRF view in the sample is translated automatically (see the
//! generated `src/translated.rs`). This file shows the escape hatch: a
//! handler written by hand and mapped in `djrs.toml`. Run the project's tests
//! through the binary to check it behaves like the Python.

use djrs_runtime::drf::{Ctx, DrfResponse, Stop};
use djrs_runtime::py::Py;
use djrs_runtime::{drf_handler, pydict};
use sea_orm::sea_query::Expr;
use sea_orm::{ColumnTrait, EntityTrait, QueryFilter, QuerySelect};

use crate::entities::notes_note as note;

// @api_view(["GET"])
// def priority_stats(request):
//     stats = Note.objects.filter(owner=request.user).aggregate(top=Max("priority"), total=Count("id"))
//     return Response({"top": stats["top"], "total": stats["total"]})
async fn priority_stats_body(ctx: &mut Ctx) -> Result<DrfResponse, Stop> {
    // owner=request.user raises for AnonymousUser.
    let owner = ctx
        .user_pk_value()
        .ok_or(Stop::Forward("filter(owner=AnonymousUser) raises"))?;
    let (top, total): (Option<i32>, i64) = note::Entity::find()
        .filter(note::Column::OwnerId.eq(owner))
        .select_only()
        .column_as(Expr::col(note::Column::Priority).max(), "top")
        .column_as(Expr::col(note::Column::Id).count(), "total")
        .into_tuple()
        .one(ctx.tx())
        .await?
        .ok_or(Stop::Forward("aggregate returned no row"))?;
    Ok(DrfResponse::ok(
        pydict! { "top" => Py::from(top), "total" => total },
    ))
}
drf_handler!(priority_stats, priority_stats_body);
