//! Kalshi request signing — a port of `kalshi._sign_request` / `_load_private_key`.
//!
//! RSA-PSS over `"{timestamp_ms}{METHOD}{path}"` with SHA-256 and salt length = digest length
//! (32 bytes), base64-encoded. `path` is the FULL signed path including the api-base prefix
//! (e.g. `/trade-api/v2/portfolio/balance`) and excludes any query string.

use anyhow::{Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use rsa::pkcs1::DecodeRsaPrivateKey;
use rsa::pkcs8::DecodePrivateKey;
use rsa::pss::SigningKey;
use rsa::signature::{RandomizedSigner, SignatureEncoding};
use rsa::RsaPrivateKey;
use sha2::Sha256;
use std::path::Path;

/// Holds the access key id + the loaded PSS signing key.
pub struct Signer {
    pub key_id: String,
    signing_key: SigningKey<Sha256>,
}

impl Signer {
    /// Load from an access key id + a PEM private-key file (PKCS#8 first, then PKCS#1).
    pub fn load(key_id: impl Into<String>, pem_path: impl AsRef<Path>) -> Result<Self> {
        let p = pem_path.as_ref();
        let pem = std::fs::read_to_string(p)
            .with_context(|| format!("reading private key {}", p.display()))?;
        Self::from_pem(key_id, &pem)
    }

    /// Build a signer from an in-memory PEM string (used by tests + callers that hold the PEM).
    pub fn from_pem(key_id: impl Into<String>, pem: &str) -> Result<Self> {
        let key = RsaPrivateKey::from_pkcs8_pem(pem)
            .or_else(|_| RsaPrivateKey::from_pkcs1_pem(pem))
            .context("parsing RSA private key (tried PKCS#8 then PKCS#1)")?;
        // PSS salt length defaults to the digest size (32 for SHA-256) — matches the Python signer
        // (cryptography `PSS(salt_length=DIGEST_LENGTH)`).
        Ok(Self { key_id: key_id.into(), signing_key: SigningKey::<Sha256>::new(key) })
    }

    /// `base64( RSA-PSS-SHA256( "{ts}{METHOD}{path}" ) )`.
    pub fn sign(&self, timestamp_ms: i64, method: &str, path: &str) -> String {
        let msg = format!("{timestamp_ms}{}{path}", method.to_uppercase());
        let mut rng = rand::thread_rng();
        let sig = self.signing_key.sign_with_rng(&mut rng, msg.as_bytes());
        STANDARD.encode(sig.to_bytes())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rsa::pkcs8::EncodePrivateKey;
    use rsa::pss::{Signature, VerifyingKey};
    use rsa::signature::Verifier;

    #[test]
    fn signs_a_valid_pss_sha256_signature() {
        // A small key is enough to prove the scheme/padding/hash are correct (the live dry-run is
        // the real "Kalshi accepts it" proof; PSS is randomized so we can't byte-match Python).
        let mut rng = rand::thread_rng();
        let key = RsaPrivateKey::new(&mut rng, 1024).expect("keygen");
        let pem = key.to_pkcs8_pem(rsa::pkcs8::LineEnding::LF).unwrap().to_string();
        let signer = Signer::from_pem("test-key-id", &pem).unwrap();

        let ts = 1_700_000_000_000_i64;
        let b64 = signer.sign(ts, "get", "/trade-api/v2/portfolio/balance");
        let sig_bytes = STANDARD.decode(&b64).unwrap();

        // method is upper-cased in the signed string
        let msg = format!("{ts}GET/trade-api/v2/portfolio/balance");
        let vk = VerifyingKey::<Sha256>::new(key.to_public_key());
        let sig = Signature::try_from(sig_bytes.as_slice()).unwrap();
        vk.verify(msg.as_bytes(), &sig).expect("signature must verify");
    }
}
