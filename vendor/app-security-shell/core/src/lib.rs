#![forbid(unsafe_code)]

pub mod encoding;
pub mod keys;
pub mod policy;
pub mod protocol;

/// Public stable error codes; no secret-bearing source or provider errors.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    InvalidMessage,
    InvalidConfiguration,
    InvalidKey,
    Denied,
    Unavailable,
}
pub type Result<T> = std::result::Result<T, Error>;

pub fn protocol_version() -> &'static str {
    "app-security/1"
}
