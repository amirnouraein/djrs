//! Django ORM semantics that SeaORM doesn't give for free: field lookups
//! written the way each Django backend writes them, and the Python value
//! coercions Django applies to field values (`int(x)` for integer fields,
//! `str(x)` for text fields).

use sea_orm::sea_query::{Alias, Expr, Order, SimpleExpr};
use sea_orm::{EntityTrait, QueryOrder, Select, Value};

use crate::db::{quote, vendor, Vendor};
use crate::drf::Stop;
use crate::py::{float_repr, Py};

/// A column of the queried table, by its database name.
pub fn col(name: &'static str) -> Expr {
    Expr::col(Alias::new(name))
}

/// `<column>=<value>`; a None value is `IS NULL`, as Django turns `exact None` into `isnull`.
pub fn exact<V: Into<Value>>(column: &'static str, v: Option<V>) -> SimpleExpr {
    match v {
        Some(v) => col(column).eq(v),
        None => col(column).is_null(),
    }
}

/// `<column>__in=<values>` (None already left out by the caller).
pub fn is_in<V: Into<Value>>(column: &'static str, vs: Vec<V>) -> SimpleExpr {
    col(column).is_in(vs)
}

/// `exclude(a=.., b=..)` as Django writes it: NOT (a AND b), where each
/// lookup on a nullable column also says `col IS NOT NULL` inside the NOT, so
/// rows with NULL there are kept.
pub fn exclude(parts: Vec<(SimpleExpr, Option<&'static str>)>) -> SimpleExpr {
    let mut all: Option<SimpleExpr> = None;
    for (cond, nullable) in parts {
        let c = match nullable {
            Some(n) => cond.and(col(n).is_not_null()),
            None => cond,
        };
        all = Some(match all {
            None => c,
            Some(a) => a.and(c),
        });
    }
    all.unwrap_or_else(|| Expr::value(true)).not()
}

/// A queryset's `order_by()`: None means the model's Meta.ordering.
pub type Ordering = Option<&'static [(&'static str, bool)]>;

/// `order_by(...)` with these (column, descending) terms.
pub const fn order(terms: &'static [(&'static str, bool)]) -> Ordering {
    Some(terms)
}

/// Apply a queryset's ordering when it is evaluated.
pub fn order_by<E: EntityTrait>(
    q: Select<E>,
    order: Ordering,
    default: &'static [(&'static str, bool)],
) -> Select<E> {
    ordered(q, order.unwrap_or(default))
}

/// `first()` / `last()`: the queryset's ordering (by pk when it has none),
/// reversed for `last()`.
pub fn order_first<E: EntityTrait>(
    q: Select<E>,
    order: Ordering,
    default: &'static [(&'static str, bool)],
    pk: &'static str,
    last: bool,
) -> Select<E> {
    let mut terms: Vec<(&'static str, bool)> = order.unwrap_or(default).to_vec();
    if terms.is_empty() {
        terms.push((pk, false));
    }
    if last {
        for t in terms.iter_mut() {
            t.1 = !t.1;
        }
    }
    ordered(q, &terms)
}

/// `ORDER BY` terms: (column, descending).
pub fn ordered<E: EntityTrait>(q: Select<E>, terms: &[(&'static str, bool)]) -> Select<E> {
    let mut q = q;
    for (c, desc) in terms {
        let e: SimpleExpr = col(c).into();
        q = QueryOrder::order_by(q, e, if *desc { Order::Desc } else { Order::Asc });
    }
    q
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Like {
    IExact,
    Contains,
    IContains,
    StartsWith,
    IStartsWith,
    EndsWith,
    IEndsWith,
}

/// The pattern-matching lookups, with the operator and left-hand side each
/// Django backend writes (`DatabaseWrapper.operators` and `lookup_cast`).
pub fn like(column: &str, lookup: Like, value: &str) -> SimpleExpr {
    use Like::*;
    let v = vendor();
    let insensitive = matches!(lookup, IExact | IContains | IStartsWith | IEndsWith);
    // iexact on PostgreSQL compares with `=` and doesn't escape the value.
    let escaped = if lookup == IExact && v == Vendor::Postgres {
        value.to_string()
    } else {
        like_escape(value)
    };
    let pattern = match lookup {
        IExact => escaped,
        Contains | IContains => format!("%{escaped}%"),
        StartsWith | IStartsWith => format!("{escaped}%"),
        EndsWith | IEndsWith => format!("%{escaped}"),
    };
    let c = quote(v, column);
    let sql = match v {
        Vendor::Sqlite => format!("{c} LIKE ? ESCAPE '\\'"),
        Vendor::MySql => {
            if insensitive {
                format!("{c} LIKE ?")
            } else {
                format!("{c} LIKE BINARY ?")
            }
        }
        Vendor::Postgres => match lookup {
            IExact => format!("UPPER({c}::text) = UPPER(?)"),
            _ if insensitive => format!("UPPER({c}::text) LIKE UPPER(?)"),
            _ => format!("{c}::text LIKE ?"),
        },
    };
    Expr::cust_with_values(sql, [pattern])
}

/// `BooleanField.get_prep_value`: None stays None; True/False and things equal
/// to them; 't'/'True'/'1' and 'f'/'False'/'0'. Anything else raises.
pub fn lk_bool(v: &Py) -> Result<Option<bool>, Stop> {
    match v {
        Py::None => Ok(None),
        Py::Bool(b) => Ok(Some(*b)),
        Py::Int(1) => Ok(Some(true)),
        Py::Int(0) => Ok(Some(false)),
        Py::Float(f) if *f == 1.0 => Ok(Some(true)),
        Py::Float(f) if *f == 0.0 => Ok(Some(false)),
        Py::Str(s) if s == "t" || s == "True" || s == "1" => Ok(Some(true)),
        Py::Str(s) if s == "f" || s == "False" || s == "0" => Ok(Some(false)),
        _ => Err(Stop::Forward(
            "BooleanField lookup value raises ValidationError",
        )),
    }
}

/// An integer lookup value for `gt`/`gte`/`lt`/`lte`: Django versions round
/// float values differently there, so only exact ints are taken.
pub fn lk_int_strict(v: &Py) -> Result<i64, Stop> {
    match v {
        Py::Int(i) => Ok(*i),
        Py::Bool(b) => Ok(*b as i64),
        Py::Str(s) => parse_python_int(s).ok_or(Stop::Forward("int() of this string raises")),
        _ => Err(Stop::Forward("comparison lookup value isn't an int")),
    }
}

/// The right-hand side of `__in`: a list whose items are all hashable and
/// not None (versions differ on None in `__in`).
pub fn lk_list(v: &Py) -> Result<Vec<Py>, Stop> {
    match v {
        Py::List(items) => {
            if items
                .iter()
                .any(|x| matches!(x, Py::None | Py::List(_) | Py::Dict(_)))
            {
                return Err(Stop::Forward("__in with None or unhashable items"));
            }
            Ok(items.clone())
        }
        _ => Err(Stop::Forward("__in with a value that isn't a list")),
    }
}

/// A pattern lookup's value: `str()` of it; None raises ("Cannot use None as a query value").
pub fn lk_pattern(v: &Py) -> Result<String, Stop> {
    match py_char(v)? {
        Some(s) => Ok(s),
        None => Err(Stop::Forward("None in a pattern lookup raises")),
    }
}

// ---- values written to model fields ------------------------------------------
//
// A translated view keeps a model instance as the row it stores. That is only
// the same as Django's in-memory instance when the value assigned already has
// the column's Python type, so anything else is forwarded.

/// A str for a text column; None only where the column is nullable.
pub fn w_str(v: &Py, max_length: Option<usize>) -> Result<Option<String>, Stop> {
    match v {
        Py::None => Ok(None),
        Py::Str(s) => {
            if let Some(m) = max_length {
                // MySQL without strict mode would truncate; PostgreSQL would raise.
                if s.chars().count() > m {
                    return Err(Stop::Forward("value longer than max_length"));
                }
            }
            Ok(Some(s.clone()))
        }
        _ => Err(Stop::Forward("non-str value for a text field")),
    }
}

/// An int for an integer column (bool isn't taken: it would stay a bool in memory).
pub fn w_int<T: TryFrom<i64>>(v: &Py) -> Result<Option<T>, Stop> {
    match v {
        Py::None => Ok(None),
        Py::Int(i) => T::try_from(*i)
            .map(Some)
            .map_err(|_| Stop::Forward("int out of the column's range")),
        _ => Err(Stop::Forward("non-int value for an integer field")),
    }
}

pub fn w_bool(v: &Py) -> Result<Option<bool>, Stop> {
    match v {
        Py::None => Ok(None),
        Py::Bool(b) => Ok(Some(*b)),
        _ => Err(Stop::Forward("non-bool value for a boolean field")),
    }
}

pub fn w_float(v: &Py) -> Result<Option<f64>, Stop> {
    match v {
        Py::None => Ok(None),
        Py::Float(f) => Ok(Some(*f)),
        _ => Err(Stop::Forward("non-float value for a float field")),
    }
}

/// A NOT NULL column given None: the INSERT/UPDATE would raise IntegrityError.
pub fn not_null<T>(v: Option<T>) -> Result<T, Stop> {
    v.ok_or(Stop::Forward("None for a NOT NULL column (IntegrityError)"))
}

/// A column's value as Python sees it on the instance.
pub fn read_u64(v: u64) -> Result<Py, Stop> {
    i64::try_from(v)
        .map(Py::Int)
        .map_err(|_| Stop::Forward("unsigned value beyond i64"))
}

/// `prep_for_like_query`: escape LIKE wildcards in a user value.
pub fn like_escape(s: &str) -> String {
    s.replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}

/// `<column>__icontains=<value>` as Django's backend for this database writes it.
pub fn icontains(column: &str, value: &str) -> SimpleExpr {
    like(column, Like::IContains, value)
}

/// Python's `int(x)` as an IntegerField applies it to a lookup value.
/// Ok(None) for None (the lookup becomes IS NULL and matches nothing on a pk);
/// Stop::Forward where Python would raise or the result doesn't fit in i64.
pub fn py_int(v: &Py) -> Result<Option<i64>, Stop> {
    match v {
        Py::None => Ok(None),
        Py::Bool(b) => Ok(Some(*b as i64)),
        Py::Int(i) => Ok(Some(*i)),
        Py::Float(f) if f.is_finite() && f.abs() < 9.0e18 => Ok(Some(f.trunc() as i64)),
        Py::Str(s) => parse_python_int(s)
            .map(Some)
            .ok_or(Stop::Forward("int() of this string raises in Python")),
        _ => Err(Stop::Forward("int() of this value raises in Python")),
    }
}

/// `int("...")`: whitespace around, optional sign, ASCII digits with single
/// underscores between them.
pub fn parse_python_int(s: &str) -> Option<i64> {
    let t = s.trim_matches(|c: char| c.is_whitespace());
    let (neg, digits) = match t.as_bytes().first()? {
        b'-' => (true, &t[1..]),
        b'+' => (false, &t[1..]),
        _ => (false, t),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
    {
        return None;
    }
    if !digits.chars().all(|c| c.is_ascii_digit() || c == '_') {
        return None;
    }
    let clean: String = digits.chars().filter(|c| *c != '_').collect();
    let n: i64 = clean.parse().ok()?;
    Some(if neg { -n } else { n })
}

/// `CharField.to_python`: strings as they are, other scalars through `str()`.
pub fn py_char(v: &Py) -> Result<Option<String>, Stop> {
    match v {
        Py::None => Ok(None),
        Py::Str(s) => Ok(Some(s.clone())),
        Py::Int(i) => Ok(Some(i.to_string())),
        Py::Bool(b) => Ok(Some(if *b { "True" } else { "False" }.into())),
        Py::Float(f) => Ok(Some(float_repr(*f))),
        _ => Err(Stop::Forward(
            "str() of a container: Python's repr isn't reproduced",
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_int_parsing() {
        assert_eq!(parse_python_int(" 12 "), Some(12));
        assert_eq!(parse_python_int("+7"), Some(7));
        assert_eq!(parse_python_int("-1_000"), Some(-1000));
        assert_eq!(parse_python_int("1__0"), None);
        assert_eq!(parse_python_int("abc"), None);
        assert_eq!(parse_python_int("1.5"), None);
        assert_eq!(parse_python_int(""), None);
    }

    #[test]
    fn coercions() {
        assert_eq!(py_int(&Py::Float(5.9)).unwrap(), Some(5));
        assert_eq!(py_int(&Py::Bool(true)).unwrap(), Some(1));
        assert_eq!(py_int(&Py::None).unwrap(), None);
        assert!(py_int(&Py::List(vec![])).is_err());
        assert_eq!(py_char(&Py::Int(5)).unwrap().as_deref(), Some("5"));
        assert_eq!(py_char(&Py::Bool(false)).unwrap().as_deref(), Some("False"));
        assert_eq!(py_char(&Py::Float(1.0)).unwrap().as_deref(), Some("1.0"));
    }

    #[test]
    fn lookup_values() {
        assert_eq!(lk_bool(&Py::Str("t".into())).unwrap(), Some(true));
        assert_eq!(lk_bool(&Py::Int(0)).unwrap(), Some(false));
        assert!(lk_bool(&Py::Str("yes".into())).is_err());
        assert!(lk_list(&Py::List(vec![Py::None])).is_err());
        assert!(w_str(&Py::Int(1), None).is_err());
        assert!(w_str(&Py::Str("abc".into()), Some(2)).is_err());
        assert_eq!(w_int::<i32>(&Py::Int(5)).unwrap(), Some(5));
        assert!(w_int::<i32>(&Py::Bool(true)).is_err());
        assert!(w_int::<u16>(&Py::Int(-1)).is_err());
    }

    #[test]
    fn like_escaping() {
        assert_eq!(like_escape(r"50%_a\b"), r"50\%\_a\\b");
    }
}
