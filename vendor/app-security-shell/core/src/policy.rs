//! Finite public policy. Unknown connections and endpoints are denied.
use crate::{
    encoding,
    keys::{identifier, is_uuid},
    Error, Result,
};
use aws_lc_rs::digest;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Identity {
    pub node_id: String,
    pub app_id: String,
    pub app_instance_id: String,
    pub generation: u64,
}
impl Identity {
    pub fn validate(&self) -> Result<()> {
        if !is_uuid(&self.node_id)
            || !is_uuid(&self.app_instance_id)
            || !identifier(&self.app_id)
            || self.generation == 0
        {
            return Err(Error::InvalidConfiguration);
        }
        Ok(())
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Endpoint {
    pub endpoint_id: String,
    pub receiver_app: String,
    pub permitted_peer_apps: Vec<String>,
    pub method: String,
    pub path: String,
    pub headers: Vec<String>,
    pub off_supported: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub response_stream: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub proxy_headers: bool,
    #[serde(
        default = "default_response_bytes",
        skip_serializing_if = "is_default_response_bytes"
    )]
    pub max_response_bytes: usize,
}
fn default_response_bytes() -> usize {
    encoding::MAX_BODY
}
fn is_default_response_bytes(value: &usize) -> bool {
    *value == encoding::MAX_BODY
}
fn is_false(value: &bool) -> bool {
    !*value
}
impl Endpoint {
    pub fn response_wire_limit(&self) -> usize {
        2 * self.max_response_bytes
    }
    pub fn permits_header(&self, name: &str) -> bool {
        valid_header(name)
            || (self.proxy_headers
                && matches!(
                    name,
                    "set-cookie" | "x-forwarded-for" | "x-forwarded-proto" | "x-forwarded-host"
                ))
    }
}
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeyIds {
    pub ed25519: String,
    pub ml_dsa_65: String,
    pub ml_kem_768: String,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicKeys {
    pub key_ids: KeyIds,
    pub ed25519: String,
    pub ml_dsa_65: String,
    pub ml_kem_768: String,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Peer {
    pub identity: Identity,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keys: Option<PublicKeys>,
}
impl Peer {
    pub fn envelope_keys(&self) -> Result<&PublicKeys> {
        self.keys.as_ref().ok_or(Error::InvalidConfiguration)
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Connection {
    pub connection_id: String,
    pub policy_revision: u64,
    pub sender: Peer,
    pub recipient: Peer,
    pub endpoint_ids: Vec<String>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Override {
    pub connection_id: String,
    pub endpoint_id: String,
    pub extra_encryption: bool,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Configuration {
    pub contract_version: u64,
    pub extra_encryption_default: bool,
    pub credentials_file: String,
    pub credential_snapshot_sha256: String,
    pub local: Identity,
    pub connections: Vec<Connection>,
    pub endpoint_overrides: Vec<Override>,
}

pub struct Policy {
    pub config: Configuration,
    pub catalog: Vec<Endpoint>,
}

pub fn valid_path(path: &str) -> bool {
    path.starts_with('/')
        && path.len() <= 4096
        && path.is_ascii()
        && !path
            .bytes()
            .any(|b| b < 0x21 || b == 0x7f || b"\\%?#{}".contains(&b))
        && !path.contains("//")
        && !path.split('/').any(|s| s == "." || s == "..")
}
// Templates select an endpoint; authenticated payloads always retain concrete paths.
pub fn valid_template(path: &str) -> bool {
    if valid_path(path) {
        return true;
    }
    if !path.starts_with('/') || path.len() > 4096 {
        return false;
    }
    let mut names = BTreeSet::new();
    let mut concrete = Vec::new();
    for segment in path.split('/') {
        if segment.starts_with('{') && segment.ends_with('}') {
            let name = &segment[1..segment.len() - 1];
            if name.is_empty()
                || name.len() > 64
                || !name.as_bytes()[0].is_ascii_alphabetic()
                || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
                || !names.insert(name)
            {
                return false;
            }
            concrete.push("placeholder");
        } else {
            concrete.push(segment);
        }
    }
    !names.is_empty() && valid_path(&concrete.join("/"))
}
pub fn path_matches(template: &str, path: &str) -> bool {
    if !valid_path(path) {
        return false;
    }
    let a: Vec<_> = template.split('/').collect();
    let b: Vec<_> = path.split('/').collect();
    a.len() == b.len()
        && a.iter().zip(b).all(|(a, b)| {
            if a.starts_with('{') && a.ends_with('}') {
                !b.is_empty()
            } else {
                *a == b
            }
        })
}
fn templates_overlap(a: &str, b: &str) -> bool {
    if !a.contains('{') || !b.contains('{') {
        return false;
    }
    let a: Vec<_> = a.split('/').collect();
    let b: Vec<_> = b.split('/').collect();
    a.len() == b.len()
        && a.iter()
            .zip(b)
            .all(|(a, b)| *a == b || a.starts_with('{') || b.starts_with('{'))
}
fn valid_method(method: &str) -> bool {
    matches!(
        method,
        "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE" | "OPTIONS"
    )
}
pub fn valid_header(name: &str) -> bool {
    !name.is_empty()
        && name.bytes().all(|b| {
            b.is_ascii_lowercase() || b.is_ascii_digit() || b"!#$%&'*+-.^_`|~".contains(&b)
        })
        && !matches!(
            name,
            "host"
                | "cookie"
                | "forwarded"
                | "connection"
                | "content-length"
                | "transfer-encoding"
                | "upgrade"
                | "te"
                | "trailer"
                | "keep-alive"
                | "proxy-authorization"
                | "proxy-authenticate"
                | "content-encoding"
                | "set-cookie"
                | "x-interapp-envelope"
                | "x-proxy-hop"
                | "x-proxy-from-cell"
        )
        && !name.starts_with("x-forwarded-")
        && !name.starts_with("x-app-security-")
}
pub fn sha256(bytes: &[u8]) -> String {
    digest::digest(&digest::SHA256, bytes)
        .as_ref()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
impl Policy {
    pub fn parse(config: &[u8], catalog: &[u8]) -> Result<Self> {
        let config: Configuration = encoding::parse(config, encoding::MAX_CREDENTIALS)
            .map_err(|_| Error::InvalidConfiguration)?;
        let catalog: Vec<Endpoint> = encoding::parse(catalog, encoding::MAX_CREDENTIALS)
            .map_err(|_| Error::InvalidConfiguration)?;
        let policy = Self { config, catalog };
        policy.validate()?;
        Ok(policy)
    }
    fn validate(&self) -> Result<()> {
        let cfg = &self.config;
        cfg.local.validate()?;
        if cfg.contract_version != 1
            || !cfg.extra_encryption_default
            || cfg.credentials_file != "app_security.credentials.json"
            || cfg.connections.len() > 64
            || self.catalog.len() > 163
        {
            return Err(Error::InvalidConfiguration);
        }
        let mut endpoints = BTreeSet::new();
        let mut routes = BTreeSet::new();
        for ep in &self.catalog {
            if !identifier(&ep.endpoint_id)
                || !identifier(&ep.receiver_app)
                || !valid_template(&ep.path)
                || ep.path == "/_app-security/v1/message"
                || !valid_method(&ep.method)
                || ep.permitted_peer_apps.is_empty()
                || ep.permitted_peer_apps.iter().any(|p| !identifier(p))
                || !endpoints.insert(&ep.endpoint_id)
                || !routes.insert((&ep.receiver_app, &ep.method, &ep.path))
                || ep.headers.len() > 64
                || ep.headers.iter().any(|h| !ep.permits_header(h))
                || (ep.response_stream && ep.method != "GET")
                || !(encoding::MAX_BODY..=encoding::MAX_RESPONSE_BODY)
                    .contains(&ep.max_response_bytes)
                || (ep.max_response_bytes != encoding::MAX_BODY
                    && (ep.method != "GET" || ep.response_stream))
                || ep.headers.iter().collect::<BTreeSet<_>>().len() != ep.headers.len()
            {
                return Err(Error::InvalidConfiguration);
            }
        }
        for (index, ep) in self.catalog.iter().enumerate() {
            for other in &self.catalog[index + 1..] {
                if ep.receiver_app == other.receiver_app
                    && ep.method == other.method
                    && templates_overlap(&ep.path, &other.path)
                {
                    return Err(Error::InvalidConfiguration);
                }
            }
        }
        let mut connections = BTreeSet::new();
        for c in &cfg.connections {
            c.sender.identity.validate()?;
            c.recipient.identity.validate()?;
            if !identifier(&c.connection_id)
                || c.policy_revision == 0
                || !connections.insert(&c.connection_id)
                || c.sender.identity.app_instance_id == c.recipient.identity.app_instance_id
                || (c.sender.identity != cfg.local && c.recipient.identity != cfg.local)
                || c.endpoint_ids.is_empty()
                || c.endpoint_ids.len() > 128
                || c.endpoint_ids.iter().collect::<BTreeSet<_>>().len() != c.endpoint_ids.len()
            {
                return Err(Error::InvalidConfiguration);
            }
            for peer in [&c.sender, &c.recipient] {
                let Some(keys) = &peer.keys else {
                    if self.connection_requires_keys(c)? {
                        return Err(Error::InvalidConfiguration);
                    }
                    continue;
                };
                let ids = [
                    &keys.key_ids.ed25519,
                    &keys.key_ids.ml_dsa_65,
                    &keys.key_ids.ml_kem_768,
                ];
                if ids.iter().any(|k| !identifier(k))
                    || ids.iter().collect::<BTreeSet<_>>().len() != 3
                {
                    return Err(Error::InvalidConfiguration);
                }
            }
            for id in &c.endpoint_ids {
                let ep = self.endpoint(id)?;
                if ep.receiver_app != c.recipient.identity.app_id
                    || !ep.permitted_peer_apps.contains(&c.sender.identity.app_id)
                {
                    return Err(Error::InvalidConfiguration);
                }
            }
        }
        let mut overrides = BTreeSet::new();
        for o in &cfg.endpoint_overrides {
            let c = self.connection(&o.connection_id)?;
            if !overrides.insert((&o.connection_id, &o.endpoint_id))
                || !c.endpoint_ids.contains(&o.endpoint_id)
                || (!o.extra_encryption && !self.endpoint(&o.endpoint_id)?.off_supported)
            {
                return Err(Error::InvalidConfiguration);
            }
        }
        let hash = &cfg.credential_snapshot_sha256;
        if (self.requires_keys()? || !hash.is_empty())
            && (hash.len() != 64
                || !hash
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
        {
            return Err(Error::InvalidConfiguration);
        }
        Ok(())
    }
    pub fn connection_requires_keys(&self, c: &Connection) -> Result<bool> {
        for endpoint in &c.endpoint_ids {
            if self.encrypted(&c.connection_id, endpoint)? {
                return Ok(true);
            }
        }
        Ok(false)
    }
    pub fn requires_keys(&self) -> Result<bool> {
        for connection in &self.config.connections {
            if self.connection_requires_keys(connection)? {
                return Ok(true);
            }
        }
        Ok(false)
    }
    pub fn connection(&self, id: &str) -> Result<&Connection> {
        self.config
            .connections
            .iter()
            .find(|c| c.connection_id == id)
            .ok_or(Error::Denied)
    }
    pub fn endpoint(&self, id: &str) -> Result<&Endpoint> {
        self.catalog
            .iter()
            .find(|e| e.endpoint_id == id)
            .ok_or(Error::Denied)
    }
    pub fn resolve(&self, app: &str, method: &str, path: &str) -> Result<&Endpoint> {
        if !valid_path(path) {
            return Err(Error::Denied);
        }
        let matches = || {
            self.catalog.iter().filter(|ep| {
                ep.receiver_app == app && ep.method == method && path_matches(&ep.path, path)
            })
        };
        if let Some(exact) = matches().find(|ep| ep.path == path) {
            return Ok(exact);
        }
        let mut choices = matches();
        let first = choices.next().ok_or(Error::Denied)?;
        if choices.next().is_some() {
            return Err(Error::Denied);
        }
        Ok(first)
    }
    pub fn encrypted(&self, connection: &str, endpoint: &str) -> Result<bool> {
        let c = self.connection(connection)?;
        if !c.endpoint_ids.iter().any(|e| e == endpoint) {
            return Err(Error::Denied);
        }
        Ok(self
            .config
            .endpoint_overrides
            .iter()
            .find(|o| o.connection_id == connection && o.endpoint_id == endpoint)
            .is_none_or(|o| o.extra_encryption))
    }
    pub fn digest(&self, id: &str) -> Result<String> {
        let c = self.connection(id)?;
        let mut endpoints = c
            .endpoint_ids
            .iter()
            .map(|id| Ok((self.endpoint(id)?, self.encrypted(&c.connection_id, id)?)))
            .collect::<Result<Vec<_>>>()?;
        endpoints.sort_by(|a, b| a.0.endpoint_id.cmp(&b.0.endpoint_id));
        // Local credential hash and unrelated app configuration are excluded.
        Ok(sha256(&encoding::canonical(&(
            1u64,
            &c.connection_id,
            c.policy_revision,
            &c.sender,
            &c.recipient,
            endpoints,
        ))?))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn path_admission_rejects_alternate_interpretations() {
        for p in [
            "/../a", "/a/./b", "/a//b", "/a%2fb", "/a%252fb", "/a\\b", "/a?b", "/a\0", "/a b",
        ] {
            assert!(!valid_path(p));
        }
        assert!(valid_path("/transaction/btc/sign"));
        assert!(valid_path("/transaction/btc/sign/"));
    }
    #[test]
    fn safe_segment_templates_are_finite() {
        for path in ["/items/{id}", "/{first}/values/{second}"] {
            assert!(valid_template(path));
        }
        for path in [
            "/x/{bad-name}",
            "/{id}/{id}",
            "/x/{0bad}",
            "/x/a{id}",
            "/x/{id?}",
            "/x/{id}/..",
            "/x/{id}/%2f",
        ] {
            assert!(!valid_template(path));
        }
        assert!(path_matches("/items/{id}", "/items/123"));
        for path in ["/items/", "/items/123/extra", "/items/%2f", "/items/.."] {
            assert!(!path_matches("/items/{id}", path));
        }
        assert!(templates_overlap("/{a}/fixed", "/fixed/{b}"));
        assert!(!templates_overlap("/a/{id}", "/b/{id}"));
    }
    #[test]
    fn reject_transport_and_spoofing_headers() {
        for h in [
            "X-Cell-ID",
            "host",
            "x-forwarded-for",
            "x-app-security-mode",
            "content-encoding",
            "x-proxy-hop",
        ] {
            assert!(!valid_header(h));
        }
        assert!(valid_header("x-cell-id"));
        assert!(valid_header("x-signatory-trace-id"));
    }
}
