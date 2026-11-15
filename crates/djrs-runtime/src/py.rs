//! Python values, as Django and DRF see them, and Python's `json.dumps`.
//!
//! Generated handlers compute with these values so that Python's rules hold:
//! dict order, truthiness, `QueryDict.get` returning the last value, and JSON
//! output that is byte-identical to what DRF's renderer writes.

use indexmap::IndexMap;

/// A Python value (the subset that request data and responses are made of).
#[derive(Clone, Debug, PartialEq)]
pub enum Py {
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    List(Vec<Py>),
    Dict(IndexMap<String, Py>),
}

impl Py {
    /// Python's `bool(x)`.
    pub fn truthy(&self) -> bool {
        match self {
            Py::None => false,
            Py::Bool(b) => *b,
            Py::Int(i) => *i != 0,
            Py::Float(f) => *f != 0.0,
            Py::Str(s) => !s.is_empty(),
            Py::List(l) => !l.is_empty(),
            Py::Dict(d) => !d.is_empty(),
        }
    }

    pub fn dict() -> Py {
        Py::Dict(IndexMap::new())
    }

    /// `d.get(key)` on a dict; None for anything that isn't one.
    pub fn get(&self, key: &str) -> Option<&Py> {
        match self {
            Py::Dict(d) => d.get(key),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Py::Str(s) => Some(s),
            _ => None,
        }
    }

    /// From parsed JSON, the way Python's `json.loads` would build it.
    /// Integers outside i64 are refused (Python would keep them exactly).
    pub fn from_json(v: serde_json::Value) -> Option<Py> {
        Some(match v {
            serde_json::Value::Null => Py::None,
            serde_json::Value::Bool(b) => Py::Bool(b),
            serde_json::Value::Number(n) => {
                if let Some(i) = n.as_i64() {
                    Py::Int(i)
                } else if n.is_f64() {
                    Py::Float(n.as_f64()?)
                } else {
                    return None; // u64 above i64::MAX
                }
            }
            serde_json::Value::String(s) => Py::Str(s),
            serde_json::Value::Array(a) => Py::List(
                a.into_iter()
                    .map(Py::from_json)
                    .collect::<Option<Vec<_>>>()?,
            ),
            serde_json::Value::Object(o) => {
                let mut d = IndexMap::new();
                for (k, v) in o {
                    // json.loads keeps the last value for a repeated key, at the first key's position.
                    d.insert(k, Py::from_json(v)?);
                }
                Py::Dict(d)
            }
        })
    }
}

impl From<&str> for Py {
    fn from(s: &str) -> Py {
        Py::Str(s.to_string())
    }
}
impl From<String> for Py {
    fn from(s: String) -> Py {
        Py::Str(s)
    }
}
impl From<bool> for Py {
    fn from(b: bool) -> Py {
        Py::Bool(b)
    }
}
impl From<i64> for Py {
    fn from(i: i64) -> Py {
        Py::Int(i)
    }
}
impl From<i32> for Py {
    fn from(i: i32) -> Py {
        Py::Int(i as i64)
    }
}
impl From<i16> for Py {
    fn from(i: i16) -> Py {
        Py::Int(i as i64)
    }
}
impl From<u16> for Py {
    fn from(i: u16) -> Py {
        Py::Int(i as i64)
    }
}
impl From<u32> for Py {
    fn from(i: u32) -> Py {
        Py::Int(i as i64)
    }
}
impl From<f64> for Py {
    fn from(f: f64) -> Py {
        Py::Float(f)
    }
}
impl<T: Into<Py>> From<Option<T>> for Py {
    fn from(o: Option<T>) -> Py {
        o.map(Into::into).unwrap_or(Py::None)
    }
}

/// Build an ordered dict: `pydict! { "a" => 1, "b" => "x" }`.
#[macro_export]
macro_rules! pydict {
    ($($k:expr => $v:expr),* $(,)?) => {{
        #[allow(unused_mut)]
        let mut d = $crate::indexmap::IndexMap::<String, $crate::py::Py>::new();
        $( d.insert(($k).to_string(), $crate::py::Py::from($v)); )*
        $crate::py::Py::Dict(d)
    }};
}

/// Django's QueryDict / MultiValueDict: ordered keys, each with a list of values.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct QueryDict {
    items: IndexMap<String, Vec<String>>,
}

impl QueryDict {
    /// Parse `a=1&b=2&a=3` like `QueryDict(query_string)`: blank values kept,
    /// '+' is a space, invalid UTF-8 replaced.
    pub fn parse(qs: &str) -> QueryDict {
        let mut items: IndexMap<String, Vec<String>> = IndexMap::new();
        for (k, v) in form_urlencoded::parse(qs.as_bytes()) {
            items
                .entry(k.into_owned())
                .or_default()
                .push(v.into_owned());
        }
        QueryDict { items }
    }

    /// `QueryDict.get(key)`: the last value for the key.
    pub fn get(&self, key: &str) -> Option<&str> {
        self.items
            .get(key)
            .and_then(|v| v.last())
            .map(|s| s.as_str())
    }

    pub fn getlist(&self, key: &str) -> &[String] {
        self.items.get(key).map(|v| v.as_slice()).unwrap_or(&[])
    }

    pub fn contains(&self, key: &str) -> bool {
        self.items.contains_key(key)
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn keys(&self) -> impl Iterator<Item = &str> {
        self.items.keys().map(|k| k.as_str())
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum JsonError {
    /// NaN or infinity with allow_nan=False (Python raises ValueError).
    NotFinite,
}

#[derive(Clone, Copy, Debug)]
pub struct JsonOptions {
    pub ensure_ascii: bool,
    pub item_sep: &'static str,
    pub key_sep: &'static str,
    pub allow_nan: bool,
}

impl JsonOptions {
    /// `json.dumps(x)` with no arguments.
    pub const PYTHON_DEFAULT: JsonOptions = JsonOptions {
        ensure_ascii: true,
        item_sep: ", ",
        key_sep: ": ",
        allow_nan: true,
    };
    /// DRF's JSONRenderer with default settings (UNICODE_JSON, COMPACT_JSON, STRICT_JSON).
    pub const DRF: JsonOptions = JsonOptions {
        ensure_ascii: false,
        item_sep: ",",
        key_sep: ":",
        allow_nan: false,
    };
}

/// Python's `json.dumps(value, ensure_ascii=..., separators=..., allow_nan=...)`.
pub fn dumps(value: &Py, opts: JsonOptions) -> Result<String, JsonError> {
    let mut out = String::new();
    write_value(&mut out, value, opts)?;
    Ok(out)
}

/// What DRF's JSONRenderer returns for `data` (before encoding to UTF-8).
pub fn drf_render(value: &Py, opts: JsonOptions) -> Result<String, JsonError> {
    let s = dumps(value, opts)?;
    // DRF always escapes these two so the output is also valid JavaScript.
    Ok(s.replace('\u{2028}', "\\u2028")
        .replace('\u{2029}', "\\u2029"))
}

fn write_value(out: &mut String, v: &Py, o: JsonOptions) -> Result<(), JsonError> {
    match v {
        Py::None => out.push_str("null"),
        Py::Bool(true) => out.push_str("true"),
        Py::Bool(false) => out.push_str("false"),
        Py::Int(i) => out.push_str(&i.to_string()),
        Py::Float(f) => {
            if f.is_nan() || f.is_infinite() {
                if !o.allow_nan {
                    return Err(JsonError::NotFinite);
                }
                out.push_str(if f.is_nan() {
                    "NaN"
                } else if *f > 0.0 {
                    "Infinity"
                } else {
                    "-Infinity"
                });
            } else {
                out.push_str(&float_repr(*f));
            }
        }
        Py::Str(s) => write_str(out, s, o.ensure_ascii),
        Py::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(o.item_sep);
                }
                write_value(out, item, o)?;
            }
            out.push(']');
        }
        Py::Dict(d) => {
            out.push('{');
            for (i, (k, val)) in d.iter().enumerate() {
                if i > 0 {
                    out.push_str(o.item_sep);
                }
                write_str(out, k, o.ensure_ascii);
                out.push_str(o.key_sep);
                write_value(out, val, o)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

fn write_str(out: &mut String, s: &str, ensure_ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if ensure_ascii && (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", unit));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Python's `repr(float)`: the shortest digits that round-trip, written in
/// fixed notation when the decimal exponent is in (-4, 16], else scientific.
pub fn float_repr(f: f64) -> String {
    if f == 0.0 {
        return if f.is_sign_negative() {
            "-0.0".into()
        } else {
            "0.0".into()
        };
    }
    // Rust's `{:e}` gives the same shortest round-trip digits as Python.
    let sci = format!("{:e}", f);
    let (mantissa, exp) = sci.split_once('e').expect("exponent");
    let exp: i32 = exp.parse().expect("exponent value");
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(|c| c.is_ascii_digit()).collect();
    let decpt = exp + 1; // value = 0.DIGITS x 10^decpt
    let mut out = String::new();
    if negative {
        out.push('-');
    }
    if decpt > -4 && decpt <= 16 {
        if decpt <= 0 {
            out.push_str("0.");
            for _ in 0..(-decpt) {
                out.push('0');
            }
            out.push_str(&digits);
        } else if (decpt as usize) >= digits.len() {
            out.push_str(&digits);
            for _ in 0..(decpt as usize - digits.len()) {
                out.push('0');
            }
            out.push_str(".0");
        } else {
            out.push_str(&digits[..decpt as usize]);
            out.push('.');
            out.push_str(&digits[decpt as usize..]);
        }
    } else {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let e = decpt - 1;
        out.push('e');
        out.push(if e < 0 { '-' } else { '+' });
        out.push_str(&format!("{:02}", e.abs()));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_match_python_repr() {
        let cases = [
            (0.1, "0.1"),
            (1.0, "1.0"),
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (123456789012345680.0, "1.2345678901234568e+17"),
            (1e-05, "1e-05"),
            (0.0001, "0.0001"),
            (1.5e-07, "1.5e-07"),
            (-2.5, "-2.5"),
            (3.0e22, "3e+22"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (5e-324, "5e-324"),
            (0.30000000000000004, "0.30000000000000004"),
            (100.0, "100.0"),
            (1234.5678, "1234.5678"),
            (-0.0, "-0.0"),
            (12345678901234567.0, "1.2345678901234568e+16"),
        ];
        for (f, expected) in cases {
            assert_eq!(float_repr(f), expected, "{f}");
        }
    }

    #[test]
    fn strings_match_python() {
        let s = Py::from("a\"b\\c\n\t\u{1}\u{1f}\u{e9}\u{2028}\u{1F600}/");
        assert_eq!(
            dumps(&s, JsonOptions::DRF).unwrap(),
            "\"a\\\"b\\\\c\\n\\t\\u0001\\u001f\u{e9}\u{2028}\u{1F600}/\""
        );
        assert_eq!(
            drf_render(&s, JsonOptions::DRF).unwrap(),
            "\"a\\\"b\\\\c\\n\\t\\u0001\\u001f\u{e9}\\u2028\u{1F600}/\""
        );
        assert_eq!(
            dumps(&s, JsonOptions::PYTHON_DEFAULT).unwrap(),
            "\"a\\\"b\\\\c\\n\\t\\u0001\\u001f\\u00e9\\u2028\\ud83d\\ude00/\""
        );
    }

    #[test]
    fn separators() {
        let v = pydict! { "a" => Py::List(vec![Py::Int(1), Py::None, Py::Bool(true)]), "b" => pydict!{ "c" => "d" } };
        assert_eq!(
            dumps(&v, JsonOptions::DRF).unwrap(),
            r#"{"a":[1,null,true],"b":{"c":"d"}}"#
        );
        assert_eq!(
            dumps(&v, JsonOptions::PYTHON_DEFAULT).unwrap(),
            r#"{"a": [1, null, true], "b": {"c": "d"}}"#
        );
    }

    #[test]
    fn strict_json_rejects_nan() {
        assert_eq!(
            dumps(&Py::Float(f64::NAN), JsonOptions::DRF),
            Err(JsonError::NotFinite)
        );
        assert_eq!(
            dumps(&Py::Float(f64::INFINITY), JsonOptions::PYTHON_DEFAULT).unwrap(),
            "Infinity"
        );
    }

    #[test]
    fn querydict_last_value_wins() {
        let q = QueryDict::parse("a=1&b=&a=2&c=x+y%21");
        assert_eq!(q.get("a"), Some("2"));
        assert_eq!(q.getlist("a"), ["1", "2"]);
        assert_eq!(q.get("b"), Some(""));
        assert_eq!(q.get("c"), Some("x y!"));
        assert_eq!(q.get("z"), None);
        assert_eq!(q.keys().collect::<Vec<_>>(), ["a", "b", "c"]);
    }

    #[test]
    fn truthiness() {
        assert!(!Py::None.truthy());
        assert!(!Py::from("").truthy());
        assert!(Py::from("0").truthy());
        assert!(!Py::Int(0).truthy());
        assert!(!pydict! {}.truthy());
    }
}
