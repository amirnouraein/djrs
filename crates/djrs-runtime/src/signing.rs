//! Django's cryptographic helpers: `salted_hmac`, `django.core.signing`, the
//! pre-3.1 session encoding, and PBKDF2 password hashes.
//!
//! Every function returns None when Django would reject the input; callers
//! then forward the request rather than guess.

use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use base64::Engine;
use hmac::{Hmac, Mac};
use sha1::Sha1;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Algorithm {
    Sha1,
    Sha256,
}

impl Algorithm {
    pub fn parse(s: &str) -> Option<Algorithm> {
        match s {
            "sha1" => Some(Algorithm::Sha1),
            "sha256" => Some(Algorithm::Sha256),
            _ => None,
        }
    }
}

/// `django.utils.crypto.salted_hmac(key_salt, value, secret, algorithm).digest()`.
pub fn salted_hmac(key_salt: &str, value: &[u8], secret: &str, alg: Algorithm) -> Vec<u8> {
    let material = format!("{key_salt}{secret}");
    match alg {
        Algorithm::Sha1 => {
            let key = Sha1::digest(material.as_bytes());
            let mut mac = Hmac::<Sha1>::new_from_slice(&key).expect("any key length");
            mac.update(value);
            mac.finalize().into_bytes().to_vec()
        }
        Algorithm::Sha256 => {
            let key = Sha256::digest(material.as_bytes());
            let mut mac = Hmac::<Sha256>::new_from_slice(&key).expect("any key length");
            mac.update(value);
            mac.finalize().into_bytes().to_vec()
        }
    }
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && bool::from(a.ct_eq(b))
}

/// `django.core.signing.loads(s, salt=salt)` with the JSON serializer and no
/// max_age, trying the current secret and then each fallback.
pub fn signing_loads(
    s: &str,
    salt: &str,
    secrets: &[String],
    alg: Algorithm,
) -> Option<serde_json::Value> {
    // TimestampSigner: "<payload>:<timestamp>:<signature>"; the signed value is "<payload>:<timestamp>".
    let (signed_value, signature) = s.rsplit_once(':')?;
    let ok = secrets.iter().any(|secret| {
        let expected = URL_SAFE_NO_PAD.encode(salted_hmac(
            &format!("{salt}signer"),
            signed_value.as_bytes(),
            secret,
            alg,
        ));
        constant_time_eq(expected.as_bytes(), signature.as_bytes())
    });
    if !ok {
        return None;
    }
    let (payload, _timestamp) = signed_value.rsplit_once(':')?;
    let (compressed, payload) = match payload.strip_prefix('.') {
        Some(rest) => (true, rest),
        None => (false, payload),
    };
    let mut raw = URL_SAFE_NO_PAD.decode(payload.trim_end_matches('=')).ok()?;
    if compressed {
        use std::io::Read;
        let mut out = Vec::new();
        flate2::read::ZlibDecoder::new(&raw[..])
            .read_to_end(&mut out)
            .ok()?;
        raw = out;
    }
    // JSONSerializer.loads decodes as latin-1; dumps only ever wrote ASCII.
    let text: String = raw.iter().map(|&b| b as char).collect();
    serde_json::from_str(&text).ok()
}

/// Django <= 3.0 session data: base64("<hex sha1 hmac>:<json>").
pub fn legacy_session_decode(
    data: &str,
    key_salt: &str,
    secrets: &[String],
) -> Option<serde_json::Value> {
    let raw = STANDARD.decode(data.trim()).ok()?;
    let pos = raw.iter().position(|&b| b == b':')?;
    let (hash, serialized) = (&raw[..pos], &raw[pos + 1..]);
    let ok = secrets.iter().any(|secret| {
        let expected = hex(&salted_hmac(key_salt, serialized, secret, Algorithm::Sha1));
        constant_time_eq(expected.as_bytes(), hash)
    });
    if !ok {
        return None;
    }
    let text: String = serialized.iter().map(|&b| b as char).collect();
    serde_json::from_str(&text).ok()
}

/// Check a password against a Django `pbkdf2_sha256$iterations$salt$hash`.
/// Returns (matches, iterations used). Other hashers return None.
pub fn check_pbkdf2_sha256(password: &str, encoded: &str) -> Option<(bool, u32)> {
    let mut parts = encoded.splitn(4, '$');
    if parts.next()? != "pbkdf2_sha256" {
        return None;
    }
    let iterations: u32 = parts.next()?.parse().ok()?;
    let salt = parts.next()?;
    let expected = parts.next()?;
    let mut out = [0u8; 32];
    pbkdf2::pbkdf2_hmac::<Sha256>(password.as_bytes(), salt.as_bytes(), iterations, &mut out);
    let got = STANDARD.encode(out);
    Some((
        constant_time_eq(got.as_bytes(), expected.as_bytes()),
        iterations,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &str = "test-secret-key";

    #[test]
    fn salted_hmac_matches_django() {
        assert_eq!(
            hex(&salted_hmac("salt", b"value", SECRET, Algorithm::Sha1)),
            "f6ba7b235944c1b42aef9075a96c81377367e835"
        );
        assert_eq!(
            hex(&salted_hmac("salt", b"value", SECRET, Algorithm::Sha256)),
            "2473d62941e8e383bf1a2e86624512d4b656e965934a170ad44121d771a5604a"
        );
    }

    #[test]
    fn legacy_session_from_django_3_0() {
        let data = "ZGFmMWVkODk1ZGI0OGIxZjM1YWJmNDhhMDE3MjM1ZjlhMTMwNDc4Yzp7Il9hdXRoX3VzZXJfaWQiOiI3IiwiX2F1dGhfdXNlcl9iYWNrZW5kIjoiZGphbmdvLmNvbnRyaWIuYXV0aC5iYWNrZW5kcy5Nb2RlbEJhY2tlbmQiLCJfYXV0aF91c2VyX2hhc2giOiJhYmMifQ==";
        let v = legacy_session_decode(
            data,
            "django.contrib.sessionsSessionStore",
            &[SECRET.into()],
        )
        .unwrap();
        assert_eq!(v["_auth_user_id"], "7");
        assert!(legacy_session_decode(
            data,
            "django.contrib.sessionsSessionStore",
            &["other".into()]
        )
        .is_none());
    }

    #[test]
    fn signed_session_from_django_5_2() {
        let salt = "django.contrib.sessions.SessionStore";
        let small = ".eJyrVopPLC3JiC8tTi2Kz0xRslIyV9JBFktKTM5OzQNJpGQl5qXn6yXn55UUZSbpgZToQWWL9XzzU1JznKBqUQzISCzOAOpOTEpWqgUAkJkneQ:1xSSmP:9R3jFYcFWGnNLp8ZcYMWZ1CsXHiA4a8wOtjKW_LEGsQ";
        let v = signing_loads(small, salt, &[SECRET.into()], Algorithm::Sha256).unwrap();
        assert_eq!(
            v["_auth_user_backend"],
            "django.contrib.auth.backends.ModelBackend"
        );
        // A fallback secret (SECRET_KEY_FALLBACKS) still verifies.
        assert!(signing_loads(
            small,
            salt,
            &["new".into(), SECRET.into()],
            Algorithm::Sha256
        )
        .is_some());
        assert!(signing_loads(small, salt, &["new".into()], Algorithm::Sha256).is_none());
        let big = ".eJyrVopPLC3JiC8tTi2Kz0xRslIyV9JBFktKTM5OzQNJpGQl5qXn6yXn55UUZSbpgZToQWWL9XzzU1JznKBqUQzISCzOAOpOTEoGihckpqRk5qUD-RWjYFABpVoAoTXmvg:1xSSmP:9ex_3_AMlGJ6rGM3IxKzgXBAGIVyOfAaC5zqUmbHd-w";
        let v = signing_loads(big, salt, &[SECRET.into()], Algorithm::Sha256).unwrap();
        assert_eq!(v["padding"].as_str().unwrap().len(), 400);
        let tampered = small.replace("9R3", "9R4");
        assert!(signing_loads(&tampered, salt, &[SECRET.into()], Algorithm::Sha256).is_none());
    }

    #[test]
    fn pbkdf2_matches_django() {
        let enc = "pbkdf2_sha256$1000$fixedsalt123$EclIrAFNI/iP257Kw0m0gS+JpOanOeBnzRJmxBDIVyw=";
        assert_eq!(check_pbkdf2_sha256("pw-alice", enc), Some((true, 1000)));
        assert_eq!(check_pbkdf2_sha256("wrong", enc), Some((false, 1000)));
        assert_eq!(check_pbkdf2_sha256("x", "argon2$..."), None);
    }
}
