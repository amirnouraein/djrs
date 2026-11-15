//! Django ORM semantics that SeaORM doesn't give for free: field lookups
//! written the way each Django backend writes them, and the Python value
//! coercions Django applies to field values (`int(x)` for integer fields,
//! `str(x)` for text fields).

use sea_orm::sea_query::{Expr, SimpleExpr};

use crate::db::{quote, vendor, Vendor};
use crate::drf::Stop;
use crate::py::{float_repr, Py};

/// `prep_for_like_query`: escape LIKE wildcards in a user value.
pub fn like_escape(s: &str) -> String {
    s.replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}

/// `<column>__icontains=<value>` as Django's backend for this database writes it.
pub fn icontains(column: &str, value: &str) -> SimpleExpr {
    let pattern = format!("%{}%", like_escape(value));
    let col = quote(vendor(), column);
    match vendor() {
        Vendor::Sqlite => Expr::cust_with_values(format!("{col} LIKE ? ESCAPE '\\'"), [pattern]),
        Vendor::MySql => Expr::cust_with_values(format!("{col} LIKE ?"), [pattern]),
        Vendor::Postgres => {
            Expr::cust_with_values(format!("UPPER({col}::text) LIKE UPPER(?)"), [pattern])
        }
    }
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
    fn like_escaping() {
        assert_eq!(like_escape(r"50%_a\b"), r"50\%\_a\\b");
    }
}
