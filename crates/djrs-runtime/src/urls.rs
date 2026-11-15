//! Django's URL resolution, reproduced.
//!
//! The tree keeps Django's shape: a resolver matches a prefix of the path with
//! `search`, then tries its children in order on the rest of the path. The
//! first endpoint that matches wins. Mirrors `URLResolver.resolve` and
//! `URLPattern.resolve` in `django/urls/resolvers.py`.
//!
//! Anything this module can't decide with certainty (a pattern that didn't
//! compile, a custom path converter, a regex that hit the backtracking limit)
//! returns `Resolution::Unknown`, and the request goes to Django. It never
//! guesses.

use fancy_regex::Regex;
use serde_json::{Map, Value};

/// How this project's Django version matches patterns. Probed from Django by
/// the extractor, never inferred from a version number.
#[derive(Clone, Copy, Debug, Default)]
pub struct Semantics {
    /// Django >= 4.x: a regex endpoint ending in `$` must match the whole rest of the path.
    pub regex_endpoint_fullmatch_on_dollar: bool,
    /// Unmatched optional named groups are dropped from kwargs.
    pub drop_none_kwargs: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Converter {
    Int,
    Str,
    Slug,
    Uuid,
    Path,
    /// A project's own converter: its `to_python` may reject a match, so a
    /// match through it can't be decided here.
    Custom,
}

impl Converter {
    pub fn from_class(path: &str) -> Converter {
        match path {
            "django.urls.converters.IntConverter" => Converter::Int,
            "django.urls.converters.StringConverter" => Converter::Str,
            "django.urls.converters.SlugConverter" => Converter::Slug,
            "django.urls.converters.UUIDConverter" => Converter::Uuid,
            "django.urls.converters.PathConverter" => Converter::Path,
            _ => Converter::Custom,
        }
    }
}

enum Kind {
    Regex,
    Route(Vec<(String, Converter)>),
    /// Couldn't be represented (locale prefix, unknown class, regex that didn't compile).
    Opaque(String),
}

pub struct Pattern {
    kind: Kind,
    source: String,
    is_endpoint: bool,
    search: Option<Regex>,
    full: Option<Regex>,
}

impl Pattern {
    fn compile(kind: Kind, source: &str, is_endpoint: bool) -> Pattern {
        if let Kind::Opaque(_) = kind {
            return Pattern {
                kind,
                source: source.to_string(),
                is_endpoint,
                search: None,
                full: None,
            };
        }
        let search = Regex::new(source);
        let full = Regex::new(&format!(r"\A(?:{})\z", source));
        match (search, full) {
            (Ok(search), Ok(full)) => Pattern {
                kind,
                source: source.to_string(),
                is_endpoint,
                search: Some(search),
                full: Some(full),
            },
            (Err(e), _) | (_, Err(e)) => Pattern {
                kind: Kind::Opaque(format!("regex did not compile: {e}")),
                source: source.to_string(),
                is_endpoint,
                search: None,
                full: None,
            },
        }
    }

    /// A `re_path()` / `url()` pattern. `source` is already translated to Rust regex syntax.
    pub fn regex(source: &str, is_endpoint: bool) -> Pattern {
        Pattern::compile(Kind::Regex, source, is_endpoint)
    }

    /// A `path()` pattern: Django's own regex for the route, plus its converters.
    pub fn route(source: &str, is_endpoint: bool, converters: &[(&str, &str)]) -> Pattern {
        let conv = converters
            .iter()
            .map(|(name, class)| (name.to_string(), Converter::from_class(class)))
            .collect();
        Pattern::compile(Kind::Route(conv), source, is_endpoint)
    }

    pub fn opaque(reason: &str) -> Pattern {
        Pattern::compile(Kind::Opaque(reason.to_string()), "", false)
    }

    pub fn source(&self) -> &str {
        &self.source
    }

    /// `RegexPattern.match` / `RoutePattern.match`: Some((rest, args, kwargs)),
    /// None for no match, Err for "can't tell".
    fn matches<'p>(
        &self,
        path: &'p str,
        sem: Semantics,
    ) -> Result<Option<(&'p str, Vec<Value>, Map<String, Value>)>, String> {
        let (search, full) = match (&self.search, &self.full, &self.kind) {
            (_, _, Kind::Opaque(reason)) => return Err(reason.clone()),
            (Some(s), Some(f), _) => (s, f),
            _ => return Err("pattern not compiled".into()),
        };
        let use_full = matches!(self.kind, Kind::Regex)
            && self.is_endpoint
            && sem.regex_endpoint_fullmatch_on_dollar
            && self.source.ends_with('$');
        let re = if use_full { full } else { search };
        let caps = match re.captures(path) {
            Ok(Some(c)) => c,
            Ok(None) => return Ok(None),
            Err(e) => return Err(format!("regex error: {e}")),
        };
        let end = caps.get(0).map(|m| m.end()).unwrap_or(0);
        let rest = &path[end..];

        let names: Vec<&str> = re.capture_names().flatten().collect();
        match &self.kind {
            Kind::Regex => {
                // kwargs = groupdict(); args = () if kwargs else groups(); then drop Nones.
                let mut kwargs = Map::new();
                for name in &names {
                    let v = caps
                        .name(name)
                        .map(|m| Value::String(m.as_str().to_string()))
                        .unwrap_or(Value::Null);
                    kwargs.insert((*name).to_string(), v);
                }
                let args = if kwargs.is_empty() {
                    (1..caps.len())
                        .map(|i| {
                            caps.get(i)
                                .map(|m| Value::String(m.as_str().to_string()))
                                .unwrap_or(Value::Null)
                        })
                        .collect()
                } else {
                    Vec::new()
                };
                if sem.drop_none_kwargs {
                    kwargs.retain(|_, v| !v.is_null());
                }
                Ok(Some((rest, args, kwargs)))
            }
            Kind::Route(converters) => {
                let mut kwargs = Map::new();
                for name in &names {
                    let raw = match caps.name(name) {
                        Some(m) => m.as_str(),
                        None => continue,
                    };
                    let conv = converters
                        .iter()
                        .find(|(n, _)| n == name)
                        .map(|(_, c)| *c)
                        .unwrap_or(Converter::Custom);
                    let value = match conv {
                        Converter::Int => match raw.parse::<i64>() {
                            Ok(n) => Value::from(n),
                            // Python ints are unbounded; ours aren't.
                            Err(_) => return Err("int path segment out of range".into()),
                        },
                        Converter::Str | Converter::Slug | Converter::Path => {
                            Value::String(raw.to_string())
                        }
                        Converter::Uuid => Value::String(raw.to_ascii_lowercase()),
                        Converter::Custom => {
                            return Err(format!("custom path converter for '{name}'"))
                        }
                    };
                    kwargs.insert((*name).to_string(), value);
                }
                Ok(Some((rest, Vec::new(), kwargs)))
            }
            Kind::Opaque(reason) => Err(reason.clone()),
        }
    }
}

pub enum Node {
    Resolver {
        pattern: Pattern,
        default_kwargs: Map<String, Value>,
        children: Vec<Node>,
    },
    Endpoint {
        pattern: Pattern,
        default_kwargs: Map<String, Value>,
        view: &'static str,
    },
    /// Something in urlpatterns that isn't a URLPattern or URLResolver.
    Opaque(String),
}

impl Node {
    pub fn resolver(pattern: Pattern, default_kwargs: Value, children: Vec<Node>) -> Node {
        Node::Resolver {
            pattern,
            default_kwargs: as_map(default_kwargs),
            children,
        }
    }

    pub fn endpoint(pattern: Pattern, default_kwargs: Value, view: &'static str) -> Node {
        Node::Endpoint {
            pattern,
            default_kwargs: as_map(default_kwargs),
            view,
        }
    }

    pub fn opaque(reason: &str) -> Node {
        Node::Opaque(reason.to_string())
    }
}

fn as_map(v: Value) -> Map<String, Value> {
    match v {
        Value::Object(m) => m,
        _ => Map::new(),
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Match {
    pub view: &'static str,
    pub args: Vec<Value>,
    pub kwargs: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Resolution {
    Matched(Match),
    NoMatch,
    /// Django's answer can't be known here; the request must go to Django.
    Unknown(String),
}

pub struct UrlTree {
    pub semantics: Semantics,
    pub root: Node,
}

impl UrlTree {
    pub fn new(semantics: Semantics, root: Node) -> UrlTree {
        UrlTree { semantics, root }
    }

    /// Resolve a decoded path (`request.path_info`, starting with '/').
    pub fn resolve(&self, path: &str) -> Resolution {
        match resolve_node(&self.root, path, self.semantics) {
            Ok(Some(m)) => Resolution::Matched(m),
            Ok(None) => Resolution::NoMatch,
            Err(reason) => Resolution::Unknown(reason),
        }
    }

    /// Every pattern that failed to compile, for a startup warning.
    pub fn opaque_patterns(&self) -> Vec<String> {
        let mut out = Vec::new();
        collect_opaque(&self.root, &mut out);
        out
    }
}

fn collect_opaque(node: &Node, out: &mut Vec<String>) {
    match node {
        Node::Resolver {
            pattern, children, ..
        } => {
            if let Kind::Opaque(r) = &pattern.kind {
                out.push(format!("{} ({r})", pattern.source));
            }
            for c in children {
                collect_opaque(c, out);
            }
        }
        Node::Endpoint { pattern, view, .. } => {
            if let Kind::Opaque(r) = &pattern.kind {
                out.push(format!("{} -> {view} ({r})", pattern.source));
            }
        }
        Node::Opaque(r) => out.push(r.clone()),
    }
}

fn resolve_node(node: &Node, path: &str, sem: Semantics) -> Result<Option<Match>, String> {
    match node {
        Node::Opaque(reason) => Err(reason.clone()),
        Node::Endpoint {
            pattern,
            default_kwargs,
            view,
        } => {
            // URLPattern.resolve: kwargs.update(default_args)
            match pattern.matches(path, sem)? {
                None => Ok(None),
                Some((_rest, args, mut kwargs)) => {
                    for (k, v) in default_kwargs {
                        kwargs.insert(k.clone(), v.clone());
                    }
                    Ok(Some(Match { view, args, kwargs }))
                }
            }
        }
        Node::Resolver {
            pattern,
            default_kwargs,
            children,
        } => {
            let (rest, args, kwargs) = match pattern.matches(path, sem)? {
                None => return Ok(None),
                Some(m) => m,
            };
            for child in children {
                if let Some(sub) = resolve_node(child, rest, sem)? {
                    // sub_match_dict = {**kwargs, **self.default_kwargs}; .update(sub.kwargs)
                    let mut merged = kwargs.clone();
                    for (k, v) in default_kwargs {
                        merged.insert(k.clone(), v.clone());
                    }
                    for (k, v) in sub.kwargs {
                        merged.insert(k, v);
                    }
                    let sub_args = if merged.is_empty() {
                        let mut a = args.clone();
                        a.extend(sub.args);
                        a
                    } else {
                        sub.args
                    };
                    return Ok(Some(Match {
                        view: sub.view,
                        args: sub_args,
                        kwargs: merged,
                    }));
                }
            }
            Ok(None)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tree(sem: Semantics) -> UrlTree {
        UrlTree::new(
            sem,
            Node::resolver(
                Pattern::regex("^/", false),
                json!({}),
                vec![
                    Node::endpoint(Pattern::route(r"^health\z", true, &[]), json!({}), "health"),
                    Node::resolver(
                        Pattern::regex("^api/", false),
                        json!({}),
                        vec![
                            Node::endpoint(
                                Pattern::regex("^notes/load-pinned", true),
                                json!({}),
                                "pinned",
                            ),
                            Node::endpoint(Pattern::regex("^notes/load", true), json!({}), "load"),
                            Node::endpoint(
                                Pattern::regex(r"^notes/get/(?P<note_id>\d+)$", true),
                                json!({}),
                                "get",
                            ),
                            Node::endpoint(
                                Pattern::route(
                                    r"^notes/(?P<note_id>[0-9]+)/\z",
                                    true,
                                    &[("note_id", "django.urls.converters.IntConverter")],
                                ),
                                json!({"extra": 1}),
                                "detail",
                            ),
                            Node::endpoint(
                                Pattern::regex(r"^pos/(\d+)/(\d+)$", true),
                                json!({}),
                                "pos",
                            ),
                        ],
                    ),
                ],
            ),
        )
    }

    fn view(t: &UrlTree, p: &str) -> Option<&'static str> {
        match t.resolve(p) {
            Resolution::Matched(m) => Some(m.view),
            _ => None,
        }
    }

    #[test]
    fn order_and_prefixes() {
        let t = tree(Semantics::default());
        assert_eq!(view(&t, "/health"), Some("health"));
        assert_eq!(view(&t, "/health/"), None);
        assert_eq!(view(&t, "/api/notes/load-pinned"), Some("pinned"));
        assert_eq!(view(&t, "/api/notes/load"), Some("load"));
        assert_eq!(view(&t, "/api/notes/loadXYZ"), Some("load"));
        assert_eq!(view(&t, "/api/notes/get/12"), Some("get"));
        assert_eq!(view(&t, "/api/notes/get/abc"), None);
        assert_eq!(view(&t, "/nope"), None);
    }

    #[test]
    fn kwargs_and_converters() {
        let t = tree(Semantics::default());
        match t.resolve("/api/notes/7/") {
            Resolution::Matched(m) => {
                assert_eq!(m.view, "detail");
                assert_eq!(m.kwargs.get("note_id"), Some(&json!(7)));
                assert_eq!(m.kwargs.get("extra"), Some(&json!(1)));
            }
            other => panic!("{other:?}"),
        }
        match t.resolve("/api/pos/1/2") {
            Resolution::Matched(m) => assert_eq!(m.args, vec![json!("1"), json!("2")]),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn dollar_semantics_differ_by_version() {
        // Python's `$` also matches before a trailing newline; the dispatcher
        // never sends such paths here, but fullmatch vs search still differs on
        // alternations.
        let src = r"^(a|ab)$";
        let old = UrlTree::new(
            Semantics::default(),
            Node::endpoint(Pattern::regex(src, true), json!({}), "v"),
        );
        let new = UrlTree::new(
            Semantics {
                regex_endpoint_fullmatch_on_dollar: true,
                drop_none_kwargs: true,
            },
            Node::endpoint(Pattern::regex(src, true), json!({}), "v"),
        );
        assert_eq!(view(&old, "ab"), Some("v"));
        assert_eq!(view(&new, "ab"), Some("v"));
    }

    #[test]
    fn opaque_means_unknown() {
        let t = UrlTree::new(
            Semantics::default(),
            Node::resolver(
                Pattern::regex("^/", false),
                json!({}),
                vec![
                    Node::endpoint(Pattern::opaque("locale prefix"), json!({}), "x"),
                    Node::endpoint(Pattern::regex("^a", true), json!({}), "a"),
                ],
            ),
        );
        assert!(matches!(t.resolve("/a"), Resolution::Unknown(_)));
    }

    #[test]
    fn bad_regex_is_opaque() {
        let t = UrlTree::new(
            Semantics::default(),
            Node::endpoint(Pattern::regex("(", true), json!({}), "x"),
        );
        assert!(matches!(t.resolve("x"), Resolution::Unknown(_)));
        assert_eq!(t.opaque_patterns().len(), 1);
    }

    #[test]
    fn custom_converter_is_unknown() {
        let t = UrlTree::new(
            Semantics::default(),
            Node::endpoint(
                Pattern::route(
                    r"^y/(?P<v>[0-9]{4})\z",
                    true,
                    &[("v", "app.converters.YearConverter")],
                ),
                json!({}),
                "y",
            ),
        );
        assert!(matches!(t.resolve("y/2024"), Resolution::Unknown(_)));
        assert_eq!(t.resolve("y/abc"), Resolution::NoMatch);
    }
}
