//! Python operators and builtins on `Py` values, for translated views.
//!
//! Each function does what CPython does for the same operands, or returns
//! `Stop::Forward` where CPython would raise (TypeError, KeyError, ...): the
//! view would then fail in Django too, and Django is the one to answer.

use indexmap::IndexMap;

use crate::drf::Stop;
use crate::orm::parse_python_int;
use crate::py::{float_repr, Py};

fn fwd<T>(why: &'static str) -> Result<T, Stop> {
    Err(Stop::Forward(why))
}

/// `str(x)`.
pub fn str_(v: &Py) -> Result<Py, Stop> {
    Ok(Py::Str(str_string(v)?))
}

pub fn str_string(v: &Py) -> Result<String, Stop> {
    Ok(match v {
        Py::None => "None".into(),
        Py::Bool(true) => "True".into(),
        Py::Bool(false) => "False".into(),
        Py::Int(i) => i.to_string(),
        Py::Float(f) => float_repr(*f),
        Py::Str(s) => s.clone(),
        Py::List(_) | Py::Dict(_) => return fwd("str() of a container: repr isn't reproduced"),
    })
}

/// `int(x)`.
pub fn int_(v: &Py) -> Result<Py, Stop> {
    Ok(Py::Int(match v {
        Py::Bool(b) => *b as i64,
        Py::Int(i) => *i,
        Py::Float(f) if f.is_finite() && f.abs() < 9.0e18 => f.trunc() as i64,
        Py::Str(s) => match parse_python_int(s) {
            Some(i) => i,
            None => return fwd("int() of this string raises"),
        },
        _ => return fwd("int() of this value raises"),
    }))
}

/// `bool(x)`.
pub fn bool_(v: &Py) -> Py {
    Py::Bool(v.truthy())
}

/// `len(x)`: code points for strings, as Python counts them.
pub fn len_(v: &Py) -> Result<Py, Stop> {
    Ok(Py::Int(match v {
        Py::Str(s) => s.chars().count() as i64,
        Py::List(l) => l.len() as i64,
        Py::Dict(d) => d.len() as i64,
        _ => return fwd("len() of a value without a length"),
    }))
}

/// Numbers as Python compares them: bool is an int, int and float compare by value.
enum Num {
    I(i64),
    F(f64),
}

fn num(v: &Py) -> Option<Num> {
    match v {
        Py::Bool(b) => Some(Num::I(*b as i64)),
        Py::Int(i) => Some(Num::I(*i)),
        Py::Float(f) => Some(Num::F(*f)),
        _ => None,
    }
}

fn num_cmp(a: &Num, b: &Num) -> Option<std::cmp::Ordering> {
    match (a, b) {
        (Num::I(x), Num::I(y)) => Some(x.cmp(y)),
        (Num::F(x), Num::F(y)) => x.partial_cmp(y),
        // i64 -> f64 can round; compare exactly the way CPython does.
        (Num::I(x), Num::F(y)) => int_float_cmp(*x, *y),
        (Num::F(x), Num::I(y)) => int_float_cmp(*y, *x).map(|o| o.reverse()),
    }
}

fn int_float_cmp(i: i64, f: f64) -> Option<std::cmp::Ordering> {
    use std::cmp::Ordering;
    if f.is_nan() {
        return None;
    }
    if f == f64::INFINITY {
        return Some(Ordering::Less);
    }
    if f == f64::NEG_INFINITY {
        return Some(Ordering::Greater);
    }
    let t = f.trunc();
    // |t| < 2^63 here unless f is huge; huge floats are beyond any i64.
    if t >= 9.223372036854775808e18 {
        return Some(Ordering::Less);
    }
    if t < -9.223372036854775808e18 {
        return Some(Ordering::Greater);
    }
    let ti = t as i64;
    match i.cmp(&ti) {
        Ordering::Equal => {
            let frac = f - t;
            if frac > 0.0 {
                Some(Ordering::Less)
            } else if frac < 0.0 {
                Some(Ordering::Greater)
            } else {
                Some(Ordering::Equal)
            }
        }
        o => Some(o),
    }
}

/// `a == b`.
pub fn eq(a: &Py, b: &Py) -> bool {
    if let (Some(x), Some(y)) = (num(a), num(b)) {
        return num_cmp(&x, &y) == Some(std::cmp::Ordering::Equal);
    }
    match (a, b) {
        (Py::None, Py::None) => true,
        (Py::Str(x), Py::Str(y)) => x == y,
        (Py::List(x), Py::List(y)) => x.len() == y.len() && x.iter().zip(y).all(|(p, q)| eq(p, q)),
        (Py::Dict(x), Py::Dict(y)) => {
            x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).map_or(false, |w| eq(v, w)))
        }
        _ => false,
    }
}

#[derive(Clone, Copy)]
pub enum Cmp {
    Lt,
    Le,
    Gt,
    Ge,
}

/// `a < b` and friends; TypeError (forward) for operands Python can't order.
pub fn cmp(a: &Py, b: &Py, op: Cmp) -> Result<bool, Stop> {
    use std::cmp::Ordering::*;
    let ord = if let (Some(x), Some(y)) = (num(a), num(b)) {
        match num_cmp(&x, &y) {
            Some(o) => Some(o),
            None => return Ok(false), // NaN: every ordering comparison is False
        }
    } else {
        match (a, b) {
            // UTF-8 byte order is code point order, which is how Python orders str.
            (Py::Str(x), Py::Str(y)) => Some(x.as_bytes().cmp(y.as_bytes())),
            (Py::List(x), Py::List(y)) => {
                // Lexicographic: first unequal pair decides.
                for (p, q) in x.iter().zip(y) {
                    if !eq(p, q) {
                        return cmp(p, q, op);
                    }
                }
                Some(x.len().cmp(&y.len()))
            }
            _ => None,
        }
    };
    let o = match ord {
        Some(o) => o,
        None => return fwd("ordering comparison Python doesn't support"),
    };
    Ok(match op {
        Cmp::Lt => o == Less,
        Cmp::Le => o != Greater,
        Cmp::Gt => o == Greater,
        Cmp::Ge => o != Less,
    })
}

fn hashable(v: &Py) -> bool {
    !matches!(v, Py::List(_) | Py::Dict(_))
}

/// `item in container`.
pub fn contains(container: &Py, item: &Py) -> Result<bool, Stop> {
    match container {
        Py::Str(s) => match item {
            Py::Str(sub) => Ok(s.contains(sub.as_str())),
            _ => fwd("'in <string>' requires a string"),
        },
        Py::List(l) => Ok(l.iter().any(|x| eq(x, item))),
        Py::Dict(d) => {
            if !hashable(item) {
                return fwd("unhashable type in a dict lookup");
            }
            // Keys are strings, so only an equal string can be a key.
            Ok(matches!(item, Py::Str(k) if d.contains_key(k)))
        }
        _ => fwd("'in' on a value that isn't a container"),
    }
}

/// `x[key]`.
pub fn getitem(v: &Py, key: &Py) -> Result<Py, Stop> {
    match v {
        Py::Dict(d) => {
            if !hashable(key) {
                return fwd("unhashable type as a dict key");
            }
            match key {
                Py::Str(k) => d.get(k).cloned().ok_or(Stop::Forward("KeyError")),
                _ => fwd("KeyError"),
            }
        }
        Py::List(l) => {
            let i = index(key, l.len())?;
            Ok(l[i].clone())
        }
        Py::Str(s) => {
            let chars: Vec<char> = s.chars().collect();
            let i = index(key, chars.len())?;
            Ok(Py::Str(chars[i].to_string()))
        }
        _ => fwd("value is not subscriptable"),
    }
}

fn index(key: &Py, len: usize) -> Result<usize, Stop> {
    let i = match key {
        Py::Int(i) => *i,
        Py::Bool(b) => *b as i64,
        _ => return fwd("indices must be integers"),
    };
    let len = len as i64;
    let j = if i < 0 { i + len } else { i };
    if j < 0 || j >= len {
        return fwd("IndexError");
    }
    Ok(j as usize)
}

/// `d.get(key, default)` on a dict.
pub fn dict_get(v: &Py, key: &Py, default: Py) -> Result<Py, Stop> {
    match v {
        Py::Dict(d) => {
            if !hashable(key) {
                return fwd("unhashable type as a dict key");
            }
            Ok(match key {
                Py::Str(k) => d.get(k).cloned().unwrap_or(default),
                _ => default,
            })
        }
        _ => fwd(".get() on a value that isn't a dict"),
    }
}

/// `x[key] = value`.
pub fn setitem(v: &mut Py, key: &Py, value: Py) -> Result<(), Stop> {
    match v {
        Py::Dict(d) => match key {
            // IndexMap::insert keeps an existing key's position, like dict.
            Py::Str(k) => {
                d.insert(k.clone(), value);
                Ok(())
            }
            _ => fwd("dict keys other than strings aren't represented"),
        },
        Py::List(l) => {
            let i = index(key, l.len())?;
            l[i] = value;
            Ok(())
        }
        _ => fwd("value doesn't support item assignment"),
    }
}

/// `str.isspace()` for one character: Unicode White_Space plus the
/// separators U+001C..U+001F, which Python also counts.
pub fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `s.strip()` (no arguments).
pub fn strip(v: &Py) -> Result<Py, Stop> {
    match v {
        Py::Str(s) => Ok(Py::Str(s.trim_matches(py_space).to_string())),
        _ => fwd(".strip() on a value that isn't a str"),
    }
}

/// `s.lower()` / `s.upper()`: exact for ASCII; other text goes to Django
/// (Python's full case mapping isn't reproduced).
pub fn case(v: &Py, upper: bool) -> Result<Py, Stop> {
    match v {
        Py::Str(s) if s.is_ascii() => Ok(Py::Str(if upper {
            s.to_ascii_uppercase()
        } else {
            s.to_ascii_lowercase()
        })),
        Py::Str(_) => fwd("case mapping of non-ASCII text"),
        _ => fwd(".lower()/.upper() on a value that isn't a str"),
    }
}

/// `{**other}` inside a dict display.
pub fn dict_update(d: &mut Py, other: &Py) -> Result<(), Stop> {
    match (d, other) {
        (Py::Dict(d), Py::Dict(o)) => {
            for (k, v) in o {
                d.insert(k.clone(), v.clone());
            }
            Ok(())
        }
        _ => fwd("** of a value that isn't a dict"),
    }
}

/// `x.append(value)`.
pub fn append(v: &mut Py, value: Py) -> Result<(), Stop> {
    match v {
        Py::List(l) => {
            l.push(value);
            Ok(())
        }
        _ => fwd(".append() on a value that isn't a list"),
    }
}

/// A dict display `{k: v, ...}`, evaluated left to right.
pub fn dict(items: Vec<(Py, Py)>) -> Result<Py, Stop> {
    let mut d = IndexMap::with_capacity(items.len());
    for (k, v) in items {
        match k {
            Py::Str(k) => {
                d.insert(k, v);
            }
            _ => return fwd("dict keys other than strings aren't represented"),
        }
    }
    Ok(Py::Dict(d))
}

/// `a + b`.
pub fn add(a: &Py, b: &Py) -> Result<Py, Stop> {
    match (a, b) {
        (Py::Str(x), Py::Str(y)) => Ok(Py::Str(format!("{x}{y}"))),
        (Py::List(x), Py::List(y)) => Ok(Py::List(x.iter().chain(y).cloned().collect())),
        _ => arith(a, b, i64::checked_add, |x, y| x + y),
    }
}

/// `a - b`.
pub fn sub(a: &Py, b: &Py) -> Result<Py, Stop> {
    arith(a, b, i64::checked_sub, |x, y| x - y)
}

/// `a * b` on numbers (sequence repetition is forwarded).
pub fn mul(a: &Py, b: &Py) -> Result<Py, Stop> {
    arith(a, b, i64::checked_mul, |x, y| x * y)
}

fn arith(
    a: &Py,
    b: &Py,
    int_op: fn(i64, i64) -> Option<i64>,
    float_op: fn(f64, f64) -> f64,
) -> Result<Py, Stop> {
    match (num(a), num(b)) {
        (Some(Num::I(x)), Some(Num::I(y))) => int_op(x, y)
            .map(Py::Int)
            .ok_or(Stop::Forward("integer result beyond 64 bits")),
        (Some(x), Some(y)) => {
            let f = |n: Num| match n {
                Num::I(i) => i as f64,
                Num::F(f) => f,
            };
            // int -> float conversion of a huge int would round where Python raises or is exact.
            for n in [&x, &y] {
                if let Num::I(i) = n {
                    if i.unsigned_abs() > (1u64 << 53) {
                        return fwd("float arithmetic with a very large int");
                    }
                }
            }
            Ok(Py::Float(float_op(f(x), f(y))))
        }
        _ => fwd("unsupported operand types"),
    }
}

/// `-x`.
pub fn neg(v: &Py) -> Result<Py, Stop> {
    match v {
        Py::Bool(b) => Ok(Py::Int(-(*b as i64))),
        Py::Int(i) => i
            .checked_neg()
            .map(Py::Int)
            .ok_or(Stop::Forward("integer result beyond 64 bits")),
        Py::Float(f) => Ok(Py::Float(-f)),
        _ => fwd("bad operand type for unary -"),
    }
}

/// `not x`.
pub fn not_(v: &Py) -> Py {
    Py::Bool(!v.truthy())
}

/// Iterating a value: `for x in v`.
pub fn iter(v: &Py) -> Result<Vec<Py>, Stop> {
    match v {
        Py::List(l) => Ok(l.clone()),
        Py::Dict(d) => Ok(d.keys().map(|k| Py::Str(k.clone())).collect()),
        Py::Str(s) => Ok(s.chars().map(|c| Py::Str(c.to_string())).collect()),
        _ => fwd("value is not iterable"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pydict;

    #[test]
    fn equality_follows_python() {
        assert!(eq(&Py::Int(1), &Py::Bool(true)));
        assert!(eq(&Py::Int(1), &Py::Float(1.0)));
        assert!(!eq(&Py::Int(1), &Py::Str("1".into())));
        assert!(!eq(&Py::None, &Py::Bool(false)));
        assert!(eq(
            &pydict! {"a" => 1, "b" => 2},
            &pydict! {"b" => 2, "a" => 1}
        ));
        assert!(!eq(
            &Py::Int(9007199254740993),
            &Py::Float(9007199254740992.0)
        ));
    }

    #[test]
    fn ordering_and_membership() {
        assert!(cmp(&Py::Str("a".into()), &Py::Str("b".into()), Cmp::Lt).unwrap());
        assert!(cmp(&Py::Str("é".into()), &Py::Str("z".into()), Cmp::Gt).unwrap());
        assert!(cmp(&Py::Int(1), &Py::Str("a".into()), Cmp::Lt).is_err());
        assert!(!cmp(&Py::Float(f64::NAN), &Py::Int(1), Cmp::Lt).unwrap());
        assert!(contains(&Py::Str("hello".into()), &Py::Str("ell".into())).unwrap());
        assert!(contains(&Py::List(vec![Py::Int(1)]), &Py::Bool(true)).unwrap());
        assert!(!contains(&pydict! {"1" => 1}, &Py::Int(1)).unwrap());
    }

    #[test]
    fn builtins() {
        assert_eq!(str_(&Py::Float(1e16)).unwrap(), Py::Str("1e+16".into()));
        assert_eq!(str_(&Py::Bool(true)).unwrap(), Py::Str("True".into()));
        assert_eq!(int_(&Py::Str(" -3 ".into())).unwrap(), Py::Int(-3));
        assert_eq!(int_(&Py::Float(-2.7)).unwrap(), Py::Int(-2));
        assert_eq!(len_(&Py::Str("héllo".into())).unwrap(), Py::Int(5));
        assert_eq!(
            getitem(&Py::List(vec![Py::Int(1), Py::Int(2)]), &Py::Int(-1)).unwrap(),
            Py::Int(2)
        );
        assert!(getitem(&pydict! {"a" => 1}, &Py::Str("b".into())).is_err());
        assert_eq!(add(&Py::Int(1), &Py::Float(0.5)).unwrap(), Py::Float(1.5));
        assert!(add(&Py::Int(i64::MAX), &Py::Int(1)).is_err());
        assert!(add(&Py::Str("a".into()), &Py::Int(1)).is_err());
    }

    #[test]
    fn string_methods() {
        assert_eq!(
            strip(&Py::Str("\u{1f} a \u{3000}".into())).unwrap(),
            Py::Str("a".into())
        );
        assert_eq!(
            case(&Py::Str("AbC".into()), false).unwrap(),
            Py::Str("abc".into())
        );
        assert!(case(&Py::Str("İ".into()), false).is_err());
        assert!(strip(&Py::Int(1)).is_err());
    }

    #[test]
    fn dict_display_keeps_first_position() {
        let d = dict(vec![
            (Py::Str("a".into()), Py::Int(1)),
            (Py::Str("b".into()), Py::Int(2)),
            (Py::Str("a".into()), Py::Int(3)),
        ])
        .unwrap();
        assert_eq!(d, pydict! {"a" => 3, "b" => 2});
    }
}
