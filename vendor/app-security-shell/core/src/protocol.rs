//! Fixed v1 message transcript and typed HTTP binding; no retry or fallback.
use crate::{
    encoding::{self, decode, encode},
    keys::{self, Keys},
    policy::{self, Identity, Policy},
    Error, Result,
};
use aws_lc_rs::{
    aead, digest, hkdf, kem,
    rand::{SecureRandom, SystemRandom},
    signature::{self, KeyPair},
};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use zeroize::{Zeroize, ZeroizeOnDrop, Zeroizing};

pub const SUITE: &str = "ML-KEM-768/HKDF-SHA256/AES-256-GCM/Ed25519+ML-DSA-65";
fn required_option<'de, D: serde::Deserializer<'de>, T: Deserialize<'de>>(
    d: D,
) -> std::result::Result<Option<T>, D::Error> {
    Option::deserialize(d)
}
#[derive(Clone, Serialize, Deserialize, Zeroize, ZeroizeOnDrop)]
#[serde(deny_unknown_fields)]
pub struct Header {
    pub name: String,
    pub value_b64: String,
}
#[derive(Clone, Serialize, Deserialize, Zeroize, ZeroizeOnDrop)]
#[serde(deny_unknown_fields)]
pub struct HttpPayload {
    pub method: String,
    pub path: String,
    pub query_b64: String,
    pub headers: Vec<Header>,
    pub body_b64: String,
    #[serde(deserialize_with = "required_option")]
    pub status: Option<u16>,
    #[serde(deserialize_with = "required_option")]
    pub response_to: Option<String>,
}
#[derive(Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MessageKeyIds {
    pub ed25519: String,
    pub ml_dsa_65: String,
    pub ml_kem_768: String,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Meta {
    pub protocol: String,
    pub version: u64,
    pub suite: String,
    pub direction: String,
    pub connection_id: String,
    pub policy_revision: u64,
    pub policy_digest: String,
    pub endpoint_id: String,
    pub request_id: String,
    pub issued_at: u64,
    pub expires_at: u64,
    pub sender: Identity,
    pub recipient: Identity,
    pub key_ids: MessageKeyIds,
    #[serde(deserialize_with = "required_option")]
    pub response_to: Option<String>,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Encrypted {
    pub kem_ciphertext: String,
    pub kdf_salt: String,
    pub nonce: String,
    pub ciphertext: String,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Signatures {
    pub ed25519: String,
    pub ml_dsa_65: String,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Wire {
    pub meta: Meta,
    pub enc: Encrypted,
    pub signatures: Signatures,
}

pub struct Context {
    pub policy: Policy,
    keys: Option<Keys>,
}
pub struct Opened {
    pub meta: Meta,
    pub payload: HttpPayload,
    pub commitment: String,
}

fn random<const N: usize>() -> Result<[u8; N]> {
    let mut out = [0u8; N];
    SystemRandom::new()
        .fill(&mut out)
        .map_err(|_| Error::Unavailable)?;
    Ok(out)
}
fn aad(meta: &Meta) -> Result<Vec<u8>> {
    let mut data = b"app-security-v1/aad\0".to_vec();
    data.extend(encoding::canonical(meta)?);
    Ok(data)
}
fn transcript(meta: &Meta, enc: &Encrypted) -> Result<Vec<u8>> {
    #[derive(Serialize)]
    struct Signed<'a> {
        meta: &'a Meta,
        enc: &'a Encrypted,
    }
    let mut data = b"app-security-v1/sign\0".to_vec();
    data.extend(encoding::canonical(&Signed { meta, enc })?);
    Ok(data)
}
fn aead_key(secret: &[u8], salt: &[u8], aad: &[u8]) -> Result<aead::LessSafeKey> {
    let prk = hkdf::Salt::new(hkdf::HKDF_SHA256, salt).extract(secret);
    let mut info = b"app-security-v1/aead\0".to_vec();
    info.extend(digest::digest(&digest::SHA256, aad).as_ref());
    let mut output = Zeroizing::new([0u8; 32]);
    prk.expand(&[&info], &aead::AES_256_GCM)
        .map_err(|_| Error::InvalidMessage)?
        .fill(output.as_mut())
        .map_err(|_| Error::InvalidMessage)?;
    let key = aead::UnboundKey::new(&aead::AES_256_GCM, output.as_ref())
        .map_err(|_| Error::InvalidMessage)?;
    Ok(aead::LessSafeKey::new(key))
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

impl Context {
    pub fn load(directory: &str, config: &[u8], catalog: &[u8]) -> Result<Self> {
        let policy = Policy::parse(config, catalog)?;
        if !policy.requires_keys()? {
            // An OFF-only or unenrolled context retains policy/identity gates,
            // but never opens an envelope credential directory or key file.
            return Ok(Self { policy, keys: None });
        }
        let file = keys::read_protected(directory, &policy.config.credential_snapshot_sha256)?;
        let keys = Keys::import(&file)?;
        let identity = &policy.config.local;
        if identity.node_id != file.node_id
            || identity.app_id != file.app_id
            || identity.app_instance_id != file.app_instance_id
            || identity.generation != file.generation
        {
            return Err(Error::InvalidConfiguration);
        }
        for conn in &policy.config.connections {
            if !policy.connection_requires_keys(conn)? {
                continue;
            }
            for peer in [&conn.sender, &conn.recipient] {
                let public = peer.envelope_keys()?;
                let ed = decode(&public.ed25519, 32)?;
                let dsa = decode(&public.ml_dsa_65, 1952)?;
                let kem = decode(&public.ml_kem_768, 1184)?;
                if ed.len() != 32 || dsa.len() != 1952 || kem.len() != 1184 {
                    return Err(Error::InvalidKey);
                }
                kem::EncapsulationKey::new(&kem::ML_KEM_768, &kem)
                    .map_err(|_| Error::InvalidKey)?;
                if &peer.identity == identity
                    && (ed != keys.ed.public_key().as_ref()
                        || dsa != keys.dsa.public_key().as_ref()
                        || kem != keys.kem_public
                        || public.key_ids.ed25519 != file.ed25519.kid
                        || public.key_ids.ml_dsa_65 != file.ml_dsa_65.kid
                        || public.key_ids.ml_kem_768 != file.ml_kem_768.kid)
                {
                    return Err(Error::InvalidKey);
                }
            }
        }
        Ok(Self {
            policy,
            keys: Some(keys),
        })
    }

    pub fn validate_payload(
        &self,
        endpoint: &str,
        payload: &HttpPayload,
        response: bool,
    ) -> Result<()> {
        let ep = self.policy.endpoint(endpoint)?;
        if payload.method != ep.method
            || self
                .policy
                .resolve(&ep.receiver_app, &payload.method, &payload.path)?
                .endpoint_id
                != ep.endpoint_id
            || !policy::valid_path(&payload.path)
            || payload.headers.len() > 64
            || (response && !matches!(payload.status, Some(200..=599)))
            || (!response && (payload.status.is_some() || payload.response_to.is_some()))
        {
            return Err(Error::InvalidMessage);
        }
        let query = decode(&payload.query_b64, 8192)?;
        if query.iter().any(|b| *b == 0 || *b == b'\r' || *b == b'\n') {
            return Err(Error::InvalidMessage);
        }
        let body = Zeroizing::new(decode(
            &payload.body_b64,
            if response {
                ep.max_response_bytes
            } else {
                encoding::MAX_BODY
            },
        )?);
        if response
            && (payload.method == "HEAD"
                || payload.status == Some(204)
                || payload.status == Some(304))
            && !body.is_empty()
        {
            return Err(Error::InvalidMessage);
        }
        let mut bytes = 0usize;
        let mut singleton = BTreeSet::new();
        for header in &payload.headers {
            if !ep.headers.contains(&header.name)
                || !ep.permits_header(&header.name)
                || (header.name == "set-cookie" && !response)
                || (header.name.starts_with("x-forwarded-") && response)
            {
                return Err(Error::InvalidMessage);
            }
            let value = decode(&header.value_b64, 16384)?;
            bytes += value.len() + header.name.len();
            if bytes > 16384 || value.iter().any(|b| [0, b'\r', b'\n'].contains(b)) {
                return Err(Error::InvalidMessage);
            }
            if matches!(
                header.name.as_str(),
                "content-type" | "authorization" | "x-cell-id" | "x-signatory-trace-id"
            ) && !singleton.insert(&header.name)
            {
                return Err(Error::InvalidMessage);
            }
            if header.name == "content-type"
                && ({
                    let media = value.split(|b| *b == b';').next().unwrap_or_default();
                    let media = media.trim_ascii();
                    media
                        .get(..10)
                        .is_some_and(|s| s.eq_ignore_ascii_case(b"multipart/"))
                        || (media.eq_ignore_ascii_case(b"text/event-stream")
                            && !(response && ep.response_stream))
                })
            {
                return Err(Error::InvalidMessage);
            }
        }
        Ok(())
    }

    pub fn seal_request(
        &self,
        connection: &str,
        endpoint: &str,
        payload: HttpPayload,
        now: u64,
    ) -> Result<Vec<u8>> {
        let conn = self.policy.connection(connection)?;
        if conn.sender.identity != self.policy.config.local
            || !self.policy.encrypted(connection, endpoint)?
            || self.policy.endpoint(endpoint)?.response_stream
        {
            return Err(Error::Denied);
        }
        self.validate_payload(endpoint, &payload, false)?;
        let meta = Meta {
            protocol: "app-security".into(),
            version: 1,
            suite: SUITE.into(),
            direction: "request".into(),
            connection_id: connection.into(),
            policy_revision: conn.policy_revision,
            policy_digest: self.policy.digest(connection)?,
            endpoint_id: endpoint.into(),
            request_id: hex(&random::<16>()?),
            issued_at: now,
            expires_at: now.checked_add(60).ok_or(Error::InvalidMessage)?,
            sender: conn.sender.identity.clone(),
            recipient: conn.recipient.identity.clone(),
            key_ids: MessageKeyIds {
                ed25519: conn.sender.envelope_keys()?.key_ids.ed25519.clone(),
                ml_dsa_65: conn.sender.envelope_keys()?.key_ids.ml_dsa_65.clone(),
                ml_kem_768: conn.recipient.envelope_keys()?.key_ids.ml_kem_768.clone(),
            },
            response_to: None,
        };
        self.seal(meta, &payload, &conn.recipient.envelope_keys()?.ml_kem_768)
    }
    pub fn seal_response(
        &self,
        request: &Opened,
        mut payload: HttpPayload,
        now: u64,
    ) -> Result<Vec<u8>> {
        let conn = self.policy.connection(&request.meta.connection_id)?;
        if request.meta.direction != "request"
            || conn.recipient.identity != self.policy.config.local
            || request.meta.recipient != self.policy.config.local
            || request.meta.sender != conn.sender.identity
        {
            return Err(Error::Denied);
        }
        payload.response_to = Some(request.commitment.clone());
        self.validate_payload(&request.meta.endpoint_id, &payload, true)?;
        let mut meta = request.meta.clone();
        meta.direction = "response".into();
        meta.sender = conn.recipient.identity.clone();
        meta.recipient = conn.sender.identity.clone();
        meta.issued_at = now;
        meta.expires_at = now.checked_add(60).ok_or(Error::InvalidMessage)?;
        meta.response_to = Some(request.commitment.clone());
        meta.key_ids = MessageKeyIds {
            ed25519: conn.recipient.envelope_keys()?.key_ids.ed25519.clone(),
            ml_dsa_65: conn.recipient.envelope_keys()?.key_ids.ml_dsa_65.clone(),
            ml_kem_768: conn.sender.envelope_keys()?.key_ids.ml_kem_768.clone(),
        };
        self.seal(meta, &payload, &conn.sender.envelope_keys()?.ml_kem_768)
    }
    fn seal(&self, meta: Meta, payload: &HttpPayload, recipient_key: &str) -> Result<Vec<u8>> {
        let keys = self.keys.as_ref().ok_or(Error::Denied)?;
        let public = kem::EncapsulationKey::new(&kem::ML_KEM_768, &decode(recipient_key, 1184)?)
            .map_err(|_| Error::InvalidKey)?;
        let (kem_ct, secret) = public.encapsulate().map_err(|_| Error::Unavailable)?;
        let salt = random::<32>()?;
        let nonce = random::<12>()?;
        let aad = aad(&meta)?;
        let mut plaintext = Zeroizing::new(encoding::canonical(payload)?);
        aead_key(secret.as_ref(), &salt, &aad)?
            .seal_in_place_append_tag(
                aead::Nonce::assume_unique_for_key(nonce),
                aead::Aad::from(&aad),
                &mut *plaintext,
            )
            .map_err(|_| Error::Unavailable)?;
        let enc = Encrypted {
            kem_ciphertext: encode(kem_ct.as_ref()),
            kdf_salt: encode(&salt),
            nonce: encode(&nonce),
            ciphertext: encode(&plaintext),
        };
        let data = transcript(&meta, &enc)?;
        let ed_sig = keys.ed.try_sign(&data).map_err(|_| Error::Unavailable)?;
        let mut dsa_sig = vec![0; signature::ML_DSA_65_SIGNING.signature_len()];
        let n = keys
            .dsa
            .sign(&data, &mut dsa_sig)
            .map_err(|_| Error::Unavailable)?;
        let wire_limit = if meta.direction == "response" {
            self.policy
                .endpoint(&meta.endpoint_id)?
                .response_wire_limit()
        } else {
            encoding::MAX_WIRE
        };
        let wire = Wire {
            meta,
            enc,
            signatures: Signatures {
                ed25519: encode(ed_sig.as_ref()),
                ml_dsa_65: encode(&dsa_sig[..n]),
            },
        };
        let bytes = encoding::canonical(&wire)?;
        if bytes.len() > wire_limit {
            return Err(Error::InvalidMessage);
        }
        Ok(bytes)
    }
    pub fn open(&self, bytes: &[u8], direction: &str, now: u64) -> Result<Opened> {
        let parse_limit = if direction == "response" {
            encoding::MAX_RESPONSE_WIRE
        } else {
            encoding::MAX_WIRE
        };
        let wire: Wire = encoding::parse(bytes, parse_limit)?;
        let wire_limit = if direction == "response" {
            self.policy
                .endpoint(&wire.meta.endpoint_id)?
                .response_wire_limit()
        } else {
            encoding::MAX_WIRE
        };
        if bytes.len() > wire_limit {
            return Err(Error::InvalidMessage);
        }
        let meta = &wire.meta;
        if meta.protocol != "app-security"
            || meta.version != 1
            || meta.suite != SUITE
            || meta.direction != direction
            || !matches!(direction, "request" | "response")
            || meta.recipient != self.policy.config.local
            || meta.request_id.len() != 32
            || !meta
                .request_id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || meta.expires_at <= meta.issued_at
            || meta.expires_at - meta.issued_at > 60
            || now >= meta.expires_at
            || meta.issued_at > now.saturating_add(30)
            || (direction == "request" && meta.response_to.is_some())
            || (direction == "response" && meta.response_to.as_ref().is_none_or(|s| s.len() != 64))
        {
            return Err(Error::InvalidMessage);
        }
        let conn = self.policy.connection(&meta.connection_id)?;
        let (sender, recipient) = if direction == "request" {
            (&conn.sender, &conn.recipient)
        } else {
            (&conn.recipient, &conn.sender)
        };
        let expected = MessageKeyIds {
            ed25519: sender.envelope_keys()?.key_ids.ed25519.clone(),
            ml_dsa_65: sender.envelope_keys()?.key_ids.ml_dsa_65.clone(),
            ml_kem_768: recipient.envelope_keys()?.key_ids.ml_kem_768.clone(),
        };
        if self.policy.endpoint(&meta.endpoint_id)?.response_stream
            || meta.sender != sender.identity
            || meta.recipient != recipient.identity
            || meta.key_ids != expected
            || meta.policy_revision != conn.policy_revision
            || meta.policy_digest != self.policy.digest(&meta.connection_id)?
            || !self
                .policy
                .encrypted(&meta.connection_id, &meta.endpoint_id)?
        {
            return Err(Error::Denied);
        }
        let transcript = transcript(meta, &wire.enc)?;
        signature::UnparsedPublicKey::new(
            &signature::ED25519,
            decode(&sender.envelope_keys()?.ed25519, 32)?,
        )
        .verify(&transcript, &decode(&wire.signatures.ed25519, 64)?)
        .map_err(|_| Error::InvalidMessage)?;
        signature::UnparsedPublicKey::new(
            &signature::ML_DSA_65,
            decode(&sender.envelope_keys()?.ml_dsa_65, 1952)?,
        )
        .verify(&transcript, &decode(&wire.signatures.ml_dsa_65, 3309)?)
        .map_err(|_| Error::InvalidMessage)?;
        let kem_ct = decode(&wire.enc.kem_ciphertext, 1088)?;
        if kem_ct.len() != 1088 {
            return Err(Error::InvalidMessage);
        }
        let secret = self
            .keys
            .as_ref()
            .ok_or(Error::Denied)?
            .kem
            .decapsulate(kem_ct.as_slice().into())
            .map_err(|_| Error::InvalidMessage)?;
        let salt = decode(&wire.enc.kdf_salt, 32)?;
        let nonce = decode(&wire.enc.nonce, 12)?;
        if salt.len() != 32 || nonce.len() != 12 {
            return Err(Error::InvalidMessage);
        }
        let mut ciphertext = Zeroizing::new(decode(&wire.enc.ciphertext, wire_limit)?);
        let aad = aad(meta)?;
        let opened = aead_key(secret.as_ref(), &salt, &aad)?
            .open_in_place(
                aead::Nonce::try_assume_unique_for_key(&nonce)
                    .map_err(|_| Error::InvalidMessage)?,
                aead::Aad::from(&aad),
                &mut ciphertext,
            )
            .map_err(|_| Error::InvalidMessage)?;
        let payload: HttpPayload = encoding::parse(opened, wire_limit)?;
        self.validate_payload(&meta.endpoint_id, &payload, direction == "response")?;
        if payload.response_to != meta.response_to {
            return Err(Error::InvalidMessage);
        }
        let commitment = policy::sha256(&encoding::canonical(&wire)?);
        Ok(Opened {
            meta: wire.meta,
            payload,
            commitment,
        })
    }
}

impl HttpPayload {
    pub fn retain_correlation_only(&mut self) {
        self.headers.clear();
        self.body_b64.zeroize();
    }
}
