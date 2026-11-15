//! Database access with Django's own connection settings.
//!
//! The binary opens the project's database the way Django does: the same
//! character set, isolation level and session variables, matched-row counts on
//! MySQL, foreign keys on SQLite, and UTC on PostgreSQL. Values are stored in
//! the same formats Django writes (for example SQLite datetimes as
//! "YYYY-MM-DD HH:MM:SS[.ffffff]").

use std::sync::OnceLock;
use std::time::Duration;

use chrono::{DateTime, NaiveDateTime, SubsecRound, Utc};
use sea_orm::sea_query::{ArrayType, ColumnType, Nullable, ValueType, ValueTypeErr};
use sea_orm::{
    ColIdx, DatabaseConnection, DbBackend, DbErr, QueryResult, Statement, TryGetError, TryGetable,
    Value,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Vendor {
    Sqlite,
    MySql,
    Postgres,
}

static VENDOR: OnceLock<Vendor> = OnceLock::new();

/// The vendor of the database this process uses (set once, at connect).
pub fn vendor() -> Vendor {
    *VENDOR.get().unwrap_or(&Vendor::Sqlite)
}

/// What Django does when it connects, from the extracted project description.
#[derive(Clone, Debug, Default)]
pub struct DjangoDbSettings {
    pub charset: Option<&'static str>,
    pub init_command: Option<&'static str>,
    pub isolation_level: Option<&'static str>,
    pub sql_auto_is_null_off: bool,
    pub pg_timezone: Option<&'static str>,
}

/// Where the database is. Read from DJRS_DATABASE_URL, or from
/// DJRS_DATABASE_ENGINE / NAME / USER / PASSWORD / HOST / PORT (Django's names).
#[derive(Clone, Debug)]
pub struct DbLocation {
    pub vendor: Vendor,
    pub name: String,
    pub user: String,
    pub password: String,
    pub host: String,
    pub port: Option<u16>,
}

impl DbLocation {
    pub fn from_env() -> Result<Option<DbLocation>, String> {
        let get = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
        let engine = match get("DJRS_DATABASE_ENGINE") {
            Some(e) => e,
            None => return Ok(None),
        };
        let vendor = if engine.ends_with("sqlite3") {
            Vendor::Sqlite
        } else if engine.ends_with("mysql") {
            Vendor::MySql
        } else if engine.contains("postgres") || engine.contains("postgis") {
            Vendor::Postgres
        } else {
            return Err(format!("unsupported database engine {engine}"));
        };
        let port = match get("DJRS_DATABASE_PORT") {
            Some(p) => Some(
                p.parse()
                    .map_err(|e| format!("DJRS_DATABASE_PORT={p}: {e}"))?,
            ),
            None => None,
        };
        Ok(Some(DbLocation {
            vendor,
            name: get("DJRS_DATABASE_NAME").ok_or("DJRS_DATABASE_NAME is required")?,
            user: get("DJRS_DATABASE_USER").unwrap_or_default(),
            password: get("DJRS_DATABASE_PASSWORD").unwrap_or_default(),
            host: get("DJRS_DATABASE_HOST").unwrap_or_else(|| "localhost".into()),
            port,
        }))
    }
}

/// A connection pool plus helpers for writing SQL the way Django's backends do.
#[derive(Clone)]
pub struct Db {
    pub conn: DatabaseConnection,
    pub vendor: Vendor,
}

impl Db {
    pub async fn connect(
        loc: &DbLocation,
        dj: &DjangoDbSettings,
        max_connections: u32,
    ) -> Result<Db, String> {
        let _ = VENDOR.set(loc.vendor);
        let conn = match loc.vendor {
            Vendor::Sqlite => {
                let opts = sqlx::sqlite::SqliteConnectOptions::new()
                    .filename(&loc.name)
                    .foreign_keys(true)
                    .busy_timeout(Duration::from_secs(5));
                let pool = pool_opts(max_connections)
                    .connect_with(opts)
                    .await
                    .map_err(|e| e.to_string())?;
                sea_orm::SqlxSqliteConnector::from_sqlx_sqlite_pool(pool)
            }
            Vendor::MySql => {
                let mut opts = sqlx::mysql::MySqlConnectOptions::new()
                    .host(&loc.host)
                    .username(&loc.user)
                    .password(&loc.password)
                    .database(&loc.name)
                    // Leave sql_mode and time_zone as the server has them, like Django.
                    .pipes_as_concat(false)
                    .no_engine_substitution(false)
                    .timezone(None)
                    .set_names(true)
                    .charset(dj.charset.unwrap_or("utf8"));
                if let Some(p) = loc.port {
                    opts = opts.port(p);
                }
                let mut init: Vec<String> = Vec::new();
                if let Some(cmd) = dj.init_command {
                    init.push(cmd.to_string());
                }
                if dj.sql_auto_is_null_off {
                    init.push("SET SQL_AUTO_IS_NULL = 0".into());
                }
                if let Some(level) = dj.isolation_level {
                    init.push(format!(
                        "SET SESSION TRANSACTION ISOLATION LEVEL {}",
                        level.to_uppercase()
                    ));
                }
                let pool = pool_opts(max_connections)
                    .after_connect(move |c, _meta| {
                        let init = init.clone();
                        Box::pin(async move {
                            for stmt in init {
                                sqlx::Executor::execute(&mut *c, stmt.as_str()).await?;
                            }
                            Ok(())
                        })
                    })
                    .connect_with(opts)
                    .await
                    .map_err(|e| e.to_string())?;
                sea_orm::SqlxMySqlConnector::from_sqlx_mysql_pool(pool)
            }
            Vendor::Postgres => {
                let mut opts = sqlx::postgres::PgConnectOptions::new()
                    .host(&loc.host)
                    .username(&loc.user)
                    .password(&loc.password)
                    .database(&loc.name);
                if let Some(p) = loc.port {
                    opts = opts.port(p);
                }
                if let Some(tz) = dj.pg_timezone {
                    opts = opts.options([("TimeZone", tz)]);
                }
                let pool = pool_opts(max_connections)
                    .connect_with(opts)
                    .await
                    .map_err(|e| e.to_string())?;
                sea_orm::SqlxPostgresConnector::from_sqlx_postgres_pool(pool)
            }
        };
        Ok(Db {
            conn,
            vendor: loc.vendor,
        })
    }

    pub fn backend(&self) -> DbBackend {
        backend(self.vendor)
    }

    /// Quote an identifier the way Django's backend does.
    pub fn quote(&self, ident: &str) -> String {
        quote(self.vendor, ident)
    }

    /// A statement from SQL written with `?` placeholders (rewritten to $n on PostgreSQL).
    pub fn stmt(&self, sql: &str, values: Vec<Value>) -> Statement {
        statement(self.vendor, sql, values)
    }
}

fn pool_opts<DB: sqlx::Database>(n: u32) -> sqlx::pool::PoolOptions<DB> {
    sqlx::pool::PoolOptions::new()
        .max_connections(n)
        .acquire_timeout(Duration::from_secs(10))
}

pub fn backend(v: Vendor) -> DbBackend {
    match v {
        Vendor::Sqlite => DbBackend::Sqlite,
        Vendor::MySql => DbBackend::MySql,
        Vendor::Postgres => DbBackend::Postgres,
    }
}

pub fn quote(v: Vendor, ident: &str) -> String {
    match v {
        Vendor::MySql => format!("`{}`", ident.replace('`', "``")),
        _ => format!("\"{}\"", ident.replace('"', "\"\"")),
    }
}

pub fn statement(v: Vendor, sql: &str, values: Vec<Value>) -> Statement {
    let sql = if v == Vendor::Postgres {
        let mut out = String::with_capacity(sql.len() + 8);
        let mut n = 0;
        for c in sql.chars() {
            if c == '?' {
                n += 1;
                out.push('$');
                out.push_str(&n.to_string());
            } else {
                out.push(c);
            }
        }
        out
    } else {
        sql.to_string()
    };
    Statement::from_sql_and_values(backend(v), sql, values)
}

// ---- datetimes ---------------------------------------------------------------

/// `django.utils.timezone.now()`: UTC, microsecond precision.
pub fn now() -> DateTime<Utc> {
    Utc::now().trunc_subsecs(6)
}

/// How Django's SQLite backend stores a datetime: `str(naive_utc_datetime)`.
pub fn sqlite_datetime(dt: &DateTime<Utc>) -> String {
    let naive = dt.naive_utc();
    if naive.and_utc().timestamp_subsec_micros() == 0 {
        naive.format("%Y-%m-%d %H:%M:%S").to_string()
    } else {
        naive.format("%Y-%m-%d %H:%M:%S%.6f").to_string()
    }
}

/// Parse what Django's SQLite backend stored (and what Django accepts back).
pub fn parse_sqlite_datetime(s: &str) -> Option<DateTime<Utc>> {
    let s = s.trim();
    for fmt in [
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M",
    ] {
        if let Ok(n) = NaiveDateTime::parse_from_str(s, fmt) {
            return Some(n.and_utc());
        }
    }
    DateTime::parse_from_rfc3339(s)
        .ok()
        .map(|d| d.with_timezone(&Utc))
}

/// DRF's DateTimeField output with TIME_ZONE = 'UTC': `isoformat()` with
/// '+00:00' written as 'Z', microseconds only when non-zero.
pub fn drf_datetime(dt: &DateTime<Utc>) -> String {
    let naive = dt.naive_utc();
    if naive.and_utc().timestamp_subsec_micros() == 0 {
        naive.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    } else {
        naive.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
    }
}

/// A DateTimeField value (USE_TZ = True): an aware UTC datetime, stored per backend
/// exactly as Django stores it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct DjDateTime(pub DateTime<Utc>);

impl From<DjDateTime> for Value {
    fn from(v: DjDateTime) -> Value {
        match vendor() {
            Vendor::Sqlite => Value::String(Some(Box::new(sqlite_datetime(&v.0)))),
            Vendor::MySql => Value::ChronoDateTime(Some(Box::new(v.0.naive_utc()))),
            Vendor::Postgres => Value::ChronoDateTimeUtc(Some(Box::new(v.0))),
        }
    }
}

impl Nullable for DjDateTime {
    fn null() -> Value {
        match vendor() {
            Vendor::Sqlite => Value::String(None),
            Vendor::MySql => Value::ChronoDateTime(None),
            Vendor::Postgres => Value::ChronoDateTimeUtc(None),
        }
    }
}

impl ValueType for DjDateTime {
    fn try_from(v: Value) -> Result<Self, ValueTypeErr> {
        match v {
            Value::String(Some(s)) => parse_sqlite_datetime(&s)
                .map(DjDateTime)
                .ok_or(ValueTypeErr),
            Value::ChronoDateTime(Some(n)) => Ok(DjDateTime(n.and_utc())),
            Value::ChronoDateTimeUtc(Some(d)) => Ok(DjDateTime(*d)),
            _ => Err(ValueTypeErr),
        }
    }
    fn type_name() -> String {
        "DjDateTime".into()
    }
    fn array_type() -> ArrayType {
        ArrayType::ChronoDateTimeUtc
    }
    fn column_type() -> ColumnType {
        ColumnType::DateTime
    }
}

impl TryGetable for DjDateTime {
    fn try_get_by<I: ColIdx>(res: &QueryResult, idx: I) -> Result<Self, TryGetError> {
        match vendor() {
            Vendor::Sqlite => {
                let s = String::try_get_by(res, idx)?;
                parse_sqlite_datetime(&s).map(DjDateTime).ok_or_else(|| {
                    TryGetError::DbErr(DbErr::Type(format!("bad SQLite datetime {s:?}")))
                })
            }
            Vendor::MySql => Ok(DjDateTime(NaiveDateTime::try_get_by(res, idx)?.and_utc())),
            Vendor::Postgres => Ok(DjDateTime(DateTime::<Utc>::try_get_by(res, idx)?)),
        }
    }
}

impl sea_orm::sea_query::Iden for DjDateTime {
    fn unquoted(&self, s: &mut dyn std::fmt::Write) {
        let _ = s.write_str("DjDateTime");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn datetime_formats() {
        let whole = Utc.with_ymd_and_hms(2026, 11, 15, 5, 0, 0).unwrap();
        let frac = whole + chrono::Duration::microseconds(123456);
        assert_eq!(sqlite_datetime(&whole), "2026-11-15 05:00:00");
        assert_eq!(sqlite_datetime(&frac), "2026-11-15 05:00:00.123456");
        assert_eq!(drf_datetime(&whole), "2026-11-15T05:00:00Z");
        assert_eq!(drf_datetime(&frac), "2026-11-15T05:00:00.123456Z");
        assert_eq!(
            parse_sqlite_datetime("2026-11-15 05:00:00.123456"),
            Some(frac)
        );
        assert_eq!(parse_sqlite_datetime("2026-11-15 05:00:00"), Some(whole));
    }

    #[test]
    fn placeholders() {
        let s = statement(
            Vendor::Postgres,
            "SELECT 1 WHERE a = ? AND b = ?",
            vec![1.into(), 2.into()],
        );
        assert_eq!(s.sql, "SELECT 1 WHERE a = $1 AND b = $2");
        assert_eq!(quote(Vendor::MySql, "order"), "`order`");
        assert_eq!(quote(Vendor::Sqlite, "order"), "\"order\"");
    }
}

// ---- UUIDs -------------------------------------------------------------------

/// A UUIDField value: native `uuid` on PostgreSQL, 32 lowercase hex characters
/// (`uuid.hex`) everywhere else, as Django stores it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct DjUuid(pub uuid::Uuid);

impl DjUuid {
    /// `str(uuid)`: the hyphenated form Python prints and DRF returns.
    pub fn hyphenated(&self) -> String {
        self.0.hyphenated().to_string()
    }
    /// Python's `uuid.UUID(s)`: accepts hex with or without hyphens, braces, a urn prefix.
    pub fn parse(s: &str) -> Option<DjUuid> {
        let t = s
            .trim()
            .trim_start_matches("urn:")
            .trim_start_matches("uuid:");
        let t = t.trim_matches(|c| c == '{' || c == '}');
        let hex: String = t.chars().filter(|c| *c != '-').collect();
        if hex.len() != 32 {
            return None;
        }
        uuid::Uuid::parse_str(&hex).ok().map(DjUuid)
    }
}

impl From<DjUuid> for Value {
    fn from(v: DjUuid) -> Value {
        match vendor() {
            Vendor::Postgres => Value::Uuid(Some(Box::new(v.0))),
            _ => Value::String(Some(Box::new(v.0.simple().to_string()))),
        }
    }
}

impl Nullable for DjUuid {
    fn null() -> Value {
        match vendor() {
            Vendor::Postgres => Value::Uuid(None),
            _ => Value::String(None),
        }
    }
}

impl ValueType for DjUuid {
    fn try_from(v: Value) -> Result<Self, ValueTypeErr> {
        match v {
            Value::Uuid(Some(u)) => Ok(DjUuid(*u)),
            Value::String(Some(s)) => DjUuid::parse(&s).ok_or(ValueTypeErr),
            _ => Err(ValueTypeErr),
        }
    }
    fn type_name() -> String {
        "DjUuid".into()
    }
    fn array_type() -> ArrayType {
        ArrayType::Uuid
    }
    fn column_type() -> ColumnType {
        ColumnType::Uuid
    }
}

impl TryGetable for DjUuid {
    fn try_get_by<I: ColIdx>(res: &QueryResult, idx: I) -> Result<Self, TryGetError> {
        match vendor() {
            Vendor::Postgres => Ok(DjUuid(uuid::Uuid::try_get_by(res, idx)?)),
            _ => {
                let s = String::try_get_by(res, idx)?;
                DjUuid::parse(&s)
                    .ok_or_else(|| TryGetError::DbErr(DbErr::Type(format!("bad uuid {s:?}"))))
            }
        }
    }
}

impl sea_orm::TryFromU64 for DjUuid {
    fn try_from_u64(_: u64) -> Result<Self, DbErr> {
        Err(DbErr::ConvertFromU64("DjUuid"))
    }
}

// ---- JSON --------------------------------------------------------------------

/// A JSONField value. SQLite stores the text of Python's `json.dumps(value)`
/// (so it must match byte for byte); MySQL and PostgreSQL store JSON natively.
#[derive(Clone, Debug, PartialEq)]
pub struct DjJson(pub crate::py::Py);

fn py_to_serde(p: &crate::py::Py) -> serde_json::Value {
    use crate::py::Py;
    match p {
        Py::None => serde_json::Value::Null,
        Py::Bool(b) => (*b).into(),
        Py::Int(i) => (*i).into(),
        Py::Float(f) => serde_json::Number::from_f64(*f)
            .map(serde_json::Value::Number)
            .unwrap_or(serde_json::Value::Null),
        Py::Str(s) => s.clone().into(),
        Py::List(l) => serde_json::Value::Array(l.iter().map(py_to_serde).collect()),
        Py::Dict(d) => {
            serde_json::Value::Object(d.iter().map(|(k, v)| (k.clone(), py_to_serde(v))).collect())
        }
    }
}

impl From<DjJson> for Value {
    fn from(v: DjJson) -> Value {
        match vendor() {
            Vendor::Postgres => Value::Json(Some(Box::new(py_to_serde(&v.0)))),
            _ => Value::String(Some(Box::new(
                crate::py::dumps(&v.0, crate::py::JsonOptions::PYTHON_DEFAULT)
                    .unwrap_or_else(|_| "null".into()),
            ))),
        }
    }
}

impl Nullable for DjJson {
    fn null() -> Value {
        match vendor() {
            Vendor::Postgres => Value::Json(None),
            _ => Value::String(None),
        }
    }
}

impl ValueType for DjJson {
    fn try_from(v: Value) -> Result<Self, ValueTypeErr> {
        match v {
            Value::Json(Some(j)) => crate::py::Py::from_json(*j).map(DjJson).ok_or(ValueTypeErr),
            Value::String(Some(s)) => serde_json::from_str(&s)
                .ok()
                .and_then(crate::py::Py::from_json)
                .map(DjJson)
                .ok_or(ValueTypeErr),
            _ => Err(ValueTypeErr),
        }
    }
    fn type_name() -> String {
        "DjJson".into()
    }
    fn array_type() -> ArrayType {
        ArrayType::Json
    }
    fn column_type() -> ColumnType {
        ColumnType::Json
    }
}

impl TryGetable for DjJson {
    fn try_get_by<I: ColIdx>(res: &QueryResult, idx: I) -> Result<Self, TryGetError> {
        let bad = |what: &str| TryGetError::DbErr(DbErr::Type(format!("bad JSON value: {what}")));
        match vendor() {
            Vendor::Sqlite => {
                let s = String::try_get_by(res, idx)?;
                let v: serde_json::Value =
                    serde_json::from_str(&s).map_err(|e| bad(&e.to_string()))?;
                crate::py::Py::from_json(v)
                    .map(DjJson)
                    .ok_or_else(|| bad("integer out of range"))
            }
            _ => {
                let v = serde_json::Value::try_get_by(res, idx)?;
                crate::py::Py::from_json(v)
                    .map(DjJson)
                    .ok_or_else(|| bad("integer out of range"))
            }
        }
    }
}
