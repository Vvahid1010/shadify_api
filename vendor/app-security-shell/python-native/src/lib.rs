use app_security_core::{
    encoding, policy,
    protocol::{Context, HttpPayload, Meta, Opened, Wire},
    Error,
};
use pyo3::{
    exceptions::PyValueError,
    prelude::*,
    types::{PyBytes, PyString},
};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use zeroize::Zeroizing;

fn error(_: Error) -> PyErr {
    PyValueError::new_err("app_security_rejected")
}
#[pyfunction]
fn protocol_version() -> &'static str {
    app_security_core::protocol_version()
}
#[pyfunction]
fn validate_credentials(
    py: Python<'_>,
    directory: String,
    expected_sha256: String,
) -> PyResult<(u64, String, String, u64)> {
    py.detach(move || {
        let file = app_security_core::keys::read_protected(&directory, &expected_sha256)?;
        let keys = app_security_core::keys::Keys::import(&file)?;
        let _ = (keys.public_sizes(), keys.kem_algorithm());
        Ok((
            file.schema_version,
            file.app_id.clone(),
            file.app_instance_id.clone(),
            file.generation,
        ))
    })
    .map_err(|_: Error| PyValueError::new_err("app_security_invalid_credentials"))
}
// Selection metadata is untrusted. It only indexes a single admitted context;
// receive() still performs the full native protocol and signature verification.
#[pyfunction]
fn inbound_selector(wire: Vec<u8>) -> PyResult<(String, String)> {
    let parsed: Wire = encoding::parse(&wire, encoding::MAX_WIRE).map_err(error)?;
    let m = parsed.meta;
    if m.protocol != "app-security"
        || m.version != 1
        || m.suite != app_security_core::protocol::SUITE
        || m.direction != "request"
    {
        return Err(error(Error::Denied));
    }
    let selector = encoding::canonical(&(
        &m.connection_id,
        m.policy_revision,
        &m.policy_digest,
        &m.sender,
        &m.recipient,
        &m.key_ids,
    ))
    .map_err(error)?;
    Ok((
        m.connection_id,
        String::from_utf8(selector).map_err(|_| error(Error::Denied))?,
    ))
}
#[pyclass(frozen)]
struct NativeContext {
    context: Arc<Context>,
}
#[pymethods]
impl NativeContext {
    #[new]
    fn new(py: Python<'_>, directory: String, config: Vec<u8>, catalog: Vec<u8>) -> PyResult<Self> {
        py.detach(move || {
            Context::load(&directory, &config, &catalog).map(|c| Self {
                context: Arc::new(c),
            })
        })
        .map_err(error)
    }
    fn snapshot_digest(&self) -> PyResult<String> {
        let p = &self.context.policy;
        let c = &p.config;
        let encoded = encoding::canonical(&(
            "app-security-context/1",
            c.contract_version,
            c.extra_encryption_default,
            &c.credentials_file,
            &c.credential_snapshot_sha256,
            &c.local,
            &c.connections,
            &c.endpoint_overrides,
            &p.catalog,
        ))
        .map_err(error)?;
        Ok(policy::sha256(&encoded))
    }
    fn status(&self) -> (String, String, u64) {
        let local = &self.context.policy.config.local;
        (
            local.app_id.clone(),
            local.app_instance_id.clone(),
            local.generation,
        )
    }
    fn begin(
        &self,
        py: Python<'_>,
        connection: String,
        endpoint: String,
        payload: Vec<u8>,
        now: u64,
    ) -> PyResult<PendingRequest> {
        let context = self.context.clone();
        py.detach(move || {
            let payload = Zeroizing::new(payload);
            let mut payload: HttpPayload = encoding::parse(&payload, encoding::MAX_WIRE)?;
            let wire = context.seal_request(&connection, &endpoint, payload.clone(), now)?;
            let parsed: Wire = encoding::parse(&wire, encoding::MAX_WIRE)?;
            payload.retain_correlation_only();
            Ok(PendingRequest {
                commitment: policy::sha256(&wire),
                wire,
                meta: parsed.meta,
                payload,
                context,
                completed: AtomicBool::new(false),
            })
        })
        .map_err(error)
    }
    fn receive(&self, py: Python<'_>, wire: Vec<u8>, now: u64) -> PyResult<InboundRequest> {
        let context = self.context.clone();
        py.detach(move || {
            let opened = context.open(&wire, "request", now)?;
            Ok(InboundRequest {
                opened,
                context,
                responded: AtomicBool::new(false),
            })
        })
        .map_err(error)
    }
    fn inbound_selectors(&self) -> PyResult<Vec<(String, String)>> {
        let p = &self.context.policy;
        let mut result = Vec::new();
        for c in &p.config.connections {
            if c.recipient.identity != p.config.local {
                continue;
            }
            let ids = if p.connection_requires_keys(c).map_err(error)? {
                let sender = c.sender.envelope_keys().map_err(error)?;
                let recipient = c.recipient.envelope_keys().map_err(error)?;
                Some(app_security_core::protocol::MessageKeyIds {
                    ed25519: sender.key_ids.ed25519.clone(),
                    ml_dsa_65: sender.key_ids.ml_dsa_65.clone(),
                    ml_kem_768: recipient.key_ids.ml_kem_768.clone(),
                })
            } else {
                None
            };
            let value = encoding::canonical(&(
                &c.connection_id,
                c.policy_revision,
                p.digest(&c.connection_id).map_err(error)?,
                &c.sender.identity,
                &c.recipient.identity,
                ids,
            ))
            .map_err(error)?;
            result.push((
                c.connection_id.clone(),
                String::from_utf8(value).map_err(|_| error(Error::Denied))?,
            ));
        }
        Ok(result)
    }
    // Exact authorization comparison, excluding only rotation material and
    // its revision. Endpoint declarations and effective ON/OFF stay bound.
    fn inbound_plain_matches(&self, connection: String, metadata: String) -> PyResult<bool> {
        if metadata.len() > 4096 {
            return Ok(false);
        }
        let p = &self.context.policy;
        let c = p.connection(&connection).map_err(error)?;
        if c.recipient.identity != p.config.local {
            return Ok(false);
        }
        for endpoint in &c.endpoint_ids {
            if !p.encrypted(&connection, endpoint).map_err(error)?
                && self.plain_policy(connection.clone(), endpoint.clone())? == metadata
            {
                return Ok(true);
            }
        }
        Ok(false)
    }
    fn connection_generations(&self, connection: String) -> PyResult<(u64, u64)> {
        let c = self.context.policy.connection(&connection).map_err(error)?;
        Ok((
            c.sender.identity.generation,
            c.recipient.identity.generation,
        ))
    }
    fn rotation_authorization(&self, connection: String) -> PyResult<String> {
        let p = &self.context.policy;
        let c = p.connection(&connection).map_err(error)?;
        let mut sender = c.sender.identity.clone();
        let mut recipient = c.recipient.identity.clone();
        sender.generation = 0;
        recipient.generation = 0;
        let mut endpoints = c
            .endpoint_ids
            .iter()
            .map(|id| Ok((p.endpoint(id)?, p.encrypted(&connection, id)?)))
            .collect::<Result<Vec<_>, Error>>()
            .map_err(error)?;
        endpoints.sort_by(|a, b| a.0.endpoint_id.cmp(&b.0.endpoint_id));
        let value =
            encoding::canonical(&(connection, sender, recipient, endpoints)).map_err(error)?;
        String::from_utf8(value).map_err(|_| error(Error::Denied))
    }
    fn revisions(&self) -> PyResult<Vec<(String, u64, String)>> {
        self.context
            .policy
            .config
            .connections
            .iter()
            .map(|c| {
                Ok((
                    c.connection_id.clone(),
                    c.policy_revision,
                    self.context
                        .policy
                        .digest(&c.connection_id)
                        .map_err(error)?,
                ))
            })
            .collect()
    }
    fn generations(&self) -> Vec<(String, u64)> {
        self.context
            .policy
            .config
            .connections
            .iter()
            .flat_map(|c| [&c.sender, &c.recipient])
            .map(|p| (p.identity.app_instance_id.clone(), p.identity.generation))
            .collect()
    }
    fn receiver_routes(&self) -> Vec<(String, String)> {
        let policy = &self.context.policy;
        policy
            .catalog
            .iter()
            .filter(|e| e.receiver_app == policy.config.local.app_id)
            .map(|e| (e.method.clone(), e.path.clone()))
            .collect()
    }
    fn enrolled(&self, method: String, path: String) -> bool {
        let policy = &self.context.policy;
        policy
            .config
            .connections
            .iter()
            .filter(|c| c.recipient.identity == policy.config.local)
            .any(|c| {
                c.endpoint_ids.iter().any(|id| {
                    policy.endpoint(id).is_ok_and(|ep| {
                        ep.method == method && policy::path_matches(&ep.path, &path)
                    })
                })
            })
    }
    fn transport_binding(
        &self,
        connection: String,
    ) -> PyResult<(String, String, String, String, u64, String)> {
        let policy = &self.context.policy;
        let c = policy.connection(&connection).map_err(error)?;
        if c.sender.identity != policy.config.local {
            return Err(error(Error::Denied));
        }
        Ok((
            c.sender.identity.node_id.clone(),
            c.sender.identity.app_instance_id.clone(),
            c.recipient.identity.node_id.clone(),
            c.recipient.identity.app_instance_id.clone(),
            c.policy_revision,
            policy.digest(&connection).map_err(error)?,
        ))
    }
    fn plain_policy(&self, connection: String, endpoint: String) -> PyResult<String> {
        let policy = &self.context.policy;
        let c = policy.connection(&connection).map_err(error)?;
        if policy.encrypted(&connection, &endpoint).map_err(error)? {
            return Err(error(Error::Denied));
        }
        serde_json::to_string(&(
            1u64,
            &connection,
            &endpoint,
            c.policy_revision,
            policy.digest(&connection).map_err(error)?,
        ))
        .map_err(|_| error(Error::InvalidMessage))
    }
    fn max_response_bytes(&self, endpoint: String) -> PyResult<usize> {
        Ok(self
            .context
            .policy
            .endpoint(&endpoint)
            .map_err(error)?
            .max_response_bytes)
    }
    fn validate_response_payload(&self, endpoint: String, payload: Vec<u8>) -> PyResult<()> {
        let payload = Zeroizing::new(payload);
        let payload: HttpPayload = encoding::parse(
            &payload,
            self.context
                .policy
                .endpoint(&endpoint)
                .map_err(error)?
                .response_wire_limit(),
        )
        .map_err(error)?;
        self.context
            .validate_payload(&endpoint, &payload, true)
            .map_err(error)
    }
    fn validate_plain_response(
        &self,
        connection: String,
        endpoint: String,
        metadata: String,
        payload: Vec<u8>,
    ) -> PyResult<()> {
        let policy = &self.context.policy;
        if policy
            .connection(&connection)
            .map_err(error)?
            .sender
            .identity
            != policy.config.local
            || metadata != self.plain_policy(connection, endpoint.clone())?
        {
            return Err(error(Error::Denied));
        }
        let payload = Zeroizing::new(payload);
        let payload: HttpPayload = encoding::parse(
            &payload,
            self.context
                .policy
                .endpoint(&endpoint)
                .map_err(error)?
                .response_wire_limit(),
        )
        .map_err(error)?;
        if payload.response_to.is_some() {
            return Err(error(Error::Denied));
        }
        self.context
            .validate_payload(&endpoint, &payload, true)
            .map_err(error)
    }
    fn admit_plain(
        &self,
        connection: String,
        node_id: String,
        instance_id: String,
        metadata: String,
        payload: Vec<u8>,
    ) -> PyResult<String> {
        let policy = &self.context.policy;
        let conn = policy.connection(&connection).map_err(error)?;
        if conn.recipient.identity != policy.config.local
            || conn.sender.identity.node_id != node_id
            || conn.sender.identity.app_instance_id != instance_id
        {
            return Err(error(Error::Denied));
        }
        let payload = Zeroizing::new(payload);
        let payload: HttpPayload = encoding::parse(&payload, encoding::MAX_WIRE).map_err(error)?;
        let ep = policy
            .resolve(
                &conn.recipient.identity.app_id,
                &payload.method,
                &payload.path,
            )
            .map_err(error)?;
        let id = &ep.endpoint_id;
        if !conn.endpoint_ids.contains(id)
            || policy.encrypted(&connection, id).map_err(error)?
            || metadata != self.plain_policy(connection.clone(), id.clone())?
        {
            return Err(error(Error::Denied));
        }
        self.context
            .validate_payload(id, &payload, false)
            .map_err(error)?;
        Ok(id.clone())
    }

    fn validate_outbound_plain(
        &self,
        connection: String,
        endpoint: String,
        payload: Vec<u8>,
    ) -> PyResult<()> {
        let policy = &self.context.policy;
        let conn = policy.connection(&connection).map_err(error)?;
        if conn.sender.identity != policy.config.local
            || policy.encrypted(&connection, &endpoint).map_err(error)?
        {
            return Err(error(Error::Denied));
        }
        let payload = Zeroizing::new(payload);
        let payload: HttpPayload = encoding::parse(&payload, encoding::MAX_WIRE).map_err(error)?;
        self.context
            .validate_payload(&endpoint, &payload, false)
            .map_err(error)
    }
    fn endpoint_metadata(&self, endpoint: String) -> PyResult<String> {
        serde_json::to_string(self.context.policy.endpoint(&endpoint).map_err(error)?)
            .map_err(|_| error(Error::InvalidConfiguration))
    }
    fn response_stream(&self, endpoint: String) -> PyResult<bool> {
        Ok(self
            .context
            .policy
            .endpoint(&endpoint)
            .map_err(error)?
            .response_stream)
    }
    fn route(&self, connection: String, method: String, path: String) -> PyResult<(String, bool)> {
        let policy = &self.context.policy;
        let conn = policy.connection(&connection).map_err(error)?;
        if conn.sender.identity != policy.config.local {
            return Err(error(Error::Denied));
        }
        let ep = policy
            .resolve(&conn.recipient.identity.app_id, &method, &path)
            .map_err(error)?;
        let id = &ep.endpoint_id;
        if ep.response_stream && policy.encrypted(&connection, id).map_err(error)? {
            return Err(error(Error::Denied));
        }
        Ok((
            id.clone(),
            policy.encrypted(&connection, id).map_err(error)?,
        ))
    }
}
#[pyclass(frozen)]
struct PendingRequest {
    context: Arc<Context>,
    wire: Vec<u8>,
    commitment: String,
    meta: Meta,
    payload: HttpPayload,
    completed: AtomicBool,
}
#[pymethods]
impl PendingRequest {
    fn wire<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.wire)
    }
    fn complete<'py>(
        &self,
        py: Python<'py>,
        wire: Vec<u8>,
        now: u64,
    ) -> PyResult<Bound<'py, PyString>> {
        if self.completed.swap(true, Ordering::AcqRel) {
            return Err(error(Error::Denied));
        }
        let result = py
            .detach(|| {
                let limit = self
                    .context
                    .policy
                    .endpoint(&self.meta.endpoint_id)?
                    .response_wire_limit();
                if wire.len() > limit {
                    return Err(Error::InvalidMessage);
                }
                let opened = self.context.open(&wire, "response", now)?;
                if opened.meta.response_to.as_deref() != Some(&self.commitment)
                    || opened.meta.connection_id != self.meta.connection_id
                    || opened.meta.endpoint_id != self.meta.endpoint_id
                    || opened.meta.request_id != self.meta.request_id
                    || opened.payload.method != self.payload.method
                    || opened.payload.path != self.payload.path
                    || opened.payload.query_b64 != self.payload.query_b64
                {
                    return Err(Error::InvalidMessage);
                }
                serde_json::to_string(&opened.payload)
                    .map(Zeroizing::new)
                    .map_err(|_| Error::InvalidMessage)
            })
            .map_err(error)?;
        Ok(PyString::new(py, &result))
    }
}
#[pyclass(frozen)]
struct InboundRequest {
    context: Arc<Context>,
    opened: Opened,
    responded: AtomicBool,
}
#[pymethods]
impl InboundRequest {
    fn max_response_bytes(&self) -> PyResult<usize> {
        Ok(self
            .context
            .policy
            .endpoint(&self.opened.meta.endpoint_id)
            .map_err(error)?
            .max_response_bytes)
    }
    /// Match independent channel evidence against authenticated envelope metadata.
    /// Exposes no key material and does not reserve replay or consume the response.
    fn admit_transport(
        &self,
        connection_id: String,
        node_id: String,
        app_instance_id: String,
    ) -> PyResult<()> {
        let meta = &self.opened.meta;
        if connection_id != meta.connection_id
            || node_id != meta.sender.node_id
            || app_instance_id != meta.sender.app_instance_id
        {
            return Err(error(Error::Denied));
        }
        Ok(())
    }
    fn payload<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyString>> {
        let result = Zeroizing::new(
            serde_json::to_string(&self.opened.payload)
                .map_err(|_| error(Error::InvalidMessage))?,
        );
        Ok(PyString::new(py, &result))
    }
    fn replay(&self) -> PyResult<(String, u64)> {
        let m = &self.opened.meta;
        // No generation in the replay namespace: rotation cannot clear admission.
        let key = encoding::canonical(&(
            &m.connection_id,
            &m.direction,
            &m.sender.node_id,
            &m.sender.app_instance_id,
            &m.recipient.node_id,
            &m.recipient.app_instance_id,
            &m.request_id,
        ))
        .map_err(error)?;
        Ok((policy::sha256(&key), m.expires_at))
    }
    fn respond<'py>(
        &self,
        py: Python<'py>,
        payload: Vec<u8>,
        now: u64,
    ) -> PyResult<Bound<'py, PyBytes>> {
        if self.responded.swap(true, Ordering::AcqRel) {
            return Err(error(Error::Denied));
        }
        let result = py
            .detach(|| {
                let payload = Zeroizing::new(payload);
                let payload: HttpPayload = encoding::parse(
                    &payload,
                    self.context
                        .policy
                        .endpoint(&self.opened.meta.endpoint_id)?
                        .response_wire_limit(),
                )?;
                self.context.seal_response(&self.opened, payload, now)
            })
            .map_err(error)?;
        Ok(PyBytes::new(py, &result))
    }
}
#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(protocol_version, module)?)?;
    module.add_function(wrap_pyfunction!(inbound_selector, module)?)?;
    module.add_function(wrap_pyfunction!(validate_credentials, module)?)?;
    module.add_class::<NativeContext>()?;
    module.add_class::<PendingRequest>()?;
    module.add_class::<InboundRequest>()?;
    Ok(())
}
