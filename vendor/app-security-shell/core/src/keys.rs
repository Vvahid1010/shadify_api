//! Protected file admission and Node Agent key-format interoperability.
use crate::{encoding, Error, Result};
use aws_lc_rs::{
    digest, kem,
    signature::{self, KeyPair},
};
use rustix::fs::{openat, Mode, OFlags, CWD};
use serde::Deserialize;
use std::{
    fs::File,
    io::Read,
    path::{Component, Path},
};
use zeroize::{Zeroize, ZeroizeOnDrop, Zeroizing};

#[derive(Deserialize, Zeroize, ZeroizeOnDrop)]
#[serde(deny_unknown_fields)]
pub struct KeyMaterial {
    pub kid: String,
    pub private_key_pem: String,
    pub public_key_pem: String,
}
#[derive(Deserialize, Zeroize, ZeroizeOnDrop)]
#[serde(deny_unknown_fields)]
pub struct CredentialFile {
    pub schema_version: u64,
    pub node_id: String,
    pub app_id: String,
    pub app_instance_id: String,
    pub credential_owner_id: String,
    pub owner_app_instance_id: String,
    pub generation: u64,
    pub ed25519: KeyMaterial,
    pub ml_dsa_65: KeyMaterial,
    pub ml_kem_768: KeyMaterial,
}

pub struct Keys {
    pub(crate) ed: signature::Ed25519KeyPair,
    pub(crate) dsa: signature::PqdsaKeyPair,
    pub(crate) kem: kem::DecapsulationKey,
    pub(crate) kem_public: Vec<u8>,
}

pub fn is_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        })
        && value != "00000000-0000-0000-0000-000000000000"
}
pub fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-.:".contains(&b))
}

pub fn pem(value: &str, label: &str, max: usize) -> Result<Zeroizing<Vec<u8>>> {
    if value.len() > max.div_ceil(3) * 4 + 1024 {
        return Err(Error::InvalidKey);
    }
    let mut lines = value.lines();
    if lines.next() != Some(format!("-----BEGIN {label}-----").as_str()) {
        return Err(Error::InvalidKey);
    }
    let end = format!("-----END {label}-----");
    let mut encoded = Zeroizing::new(String::new());
    let mut ended = false;
    for line in lines {
        if ended {
            return Err(Error::InvalidKey);
        }
        if line == end {
            ended = true;
            continue;
        }
        if line.is_empty() || line.len() > 64 || !line.is_ascii() {
            return Err(Error::InvalidKey);
        }
        encoded.push_str(line);
    }
    if !ended {
        return Err(Error::InvalidKey);
    }
    encoding::decode(&encoded, max)
        .map(Zeroizing::new)
        .map_err(|_| Error::InvalidKey)
}

// RFC 8410 canonical Ed25519 SubjectPublicKeyInfo: OID 1.3.101.112, absent params.
const ED_SPKI_PREFIX: &[u8] = &[
    0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00,
];
pub fn ed_public(pem_text: &str) -> Result<Vec<u8>> {
    let bytes = pem(pem_text, "PUBLIC KEY", 44)?;
    if bytes.len() != 44 || !bytes.starts_with(ED_SPKI_PREFIX) {
        return Err(Error::InvalidKey);
    }
    Ok(bytes[12..].to_vec())
}

fn valid_coefficients(bytes: &[u8]) -> bool {
    bytes.len().is_multiple_of(3)
        && bytes.chunks_exact(3).all(|b| {
            let a = u16::from(b[0]) | ((u16::from(b[1]) & 15) << 8);
            let c = (u16::from(b[1]) >> 4) | (u16::from(b[2]) << 4);
            a < 3329 && c < 3329
        })
}

impl Keys {
    pub fn import(file: &CredentialFile) -> Result<Self> {
        if file.schema_version != 1
            || file.generation == 0
            || !is_uuid(&file.node_id)
            || !is_uuid(&file.app_instance_id)
            || !is_uuid(&file.credential_owner_id)
            || !identifier(&file.app_id)
            || file.owner_app_instance_id != file.app_instance_id
        {
            return Err(Error::InvalidKey);
        }
        let ids = [&file.ed25519.kid, &file.ml_dsa_65.kid, &file.ml_kem_768.kid];
        if ids.iter().any(|s| !identifier(s))
            || ids[0] == ids[1]
            || ids[0] == ids[2]
            || ids[1] == ids[2]
        {
            return Err(Error::InvalidKey);
        }
        let ed_private = pem(&file.ed25519.private_key_pem, "PRIVATE KEY", 256)?;
        let ed_public = ed_public(&file.ed25519.public_key_pem)?;
        let ed =
            signature::Ed25519KeyPair::from_pkcs8(&ed_private).map_err(|_| Error::InvalidKey)?;
        if ed.public_key().as_ref() != ed_public {
            return Err(Error::InvalidKey);
        }
        let dsa_private = pem(
            &file.ml_dsa_65.private_key_pem,
            "ML-DSA-65 PRIVATE KEY",
            4032,
        )?;
        let dsa_public = pem(&file.ml_dsa_65.public_key_pem, "ML-DSA-65 PUBLIC KEY", 1952)?;
        if dsa_private.len() != 4032 || dsa_public.len() != 1952 {
            return Err(Error::InvalidKey);
        }
        let dsa = signature::PqdsaKeyPair::from_raw_private_key(
            &signature::ML_DSA_65_SIGNING,
            &dsa_private,
        )
        .map_err(|_| Error::InvalidKey)?;
        if dsa.public_key().as_ref() != dsa_public.as_slice() {
            return Err(Error::InvalidKey);
        }
        let kem_private = pem(
            &file.ml_kem_768.private_key_pem,
            "ML-KEM-768 PRIVATE KEY",
            2400,
        )?;
        let kem_public = pem(
            &file.ml_kem_768.public_key_pem,
            "ML-KEM-768 PUBLIC KEY",
            1184,
        )?;
        if kem_private.len() != 2400
            || kem_public.len() != 1184
            || !valid_coefficients(&kem_private[..1152])
            || !valid_coefficients(&kem_public[..1152])
            || kem_private[1152..2336] != *kem_public
            || kem_private[2336..2368] != *digest::digest(&digest::SHA3_256, &kem_public).as_ref()
        {
            return Err(Error::InvalidKey);
        }
        let kem = kem::DecapsulationKey::new(&kem::ML_KEM_768, &kem_private)
            .map_err(|_| Error::InvalidKey)?;
        let public = kem::EncapsulationKey::new(&kem::ML_KEM_768, &kem_public)
            .map_err(|_| Error::InvalidKey)?;
        let (cipher, secret) = public.encapsulate().map_err(|_| Error::InvalidKey)?;
        let opened = kem.decapsulate(cipher).map_err(|_| Error::InvalidKey)?;
        if secret.as_ref() != opened.as_ref() {
            return Err(Error::InvalidKey);
        }
        // Pair checks include actual signatures, not just key-size acceptance.
        let challenge = b"app-security-v1/key-admission\0";
        let ed_sig = ed.try_sign(challenge).map_err(|_| Error::InvalidKey)?;
        signature::UnparsedPublicKey::new(&signature::ED25519, &ed_public)
            .verify(challenge, ed_sig.as_ref())
            .map_err(|_| Error::InvalidKey)?;
        let mut sig = vec![0; signature::ML_DSA_65_SIGNING.signature_len()];
        let n = dsa
            .sign(challenge, &mut sig)
            .map_err(|_| Error::InvalidKey)?;
        signature::UnparsedPublicKey::new(&signature::ML_DSA_65, dsa_public.as_slice())
            .verify(challenge, &sig[..n])
            .map_err(|_| Error::InvalidKey)?;
        Ok(Self {
            ed,
            dsa,
            kem,
            kem_public: kem_public.to_vec(),
        })
    }

    pub fn public_sizes(&self) -> (usize, usize, usize) {
        (
            self.ed.public_key().as_ref().len(),
            self.dsa.public_key().as_ref().len(),
            self.kem_public.len(),
        )
    }
    pub fn kem_algorithm(&self) -> &'static str {
        let _ = self.kem.algorithm();
        "ML-KEM-768"
    }
}

// systemd LoadCredential uses root-owned, read-only RAM files with one named
// service UID in their ACL. Keep app-owned file admission unchanged.
fn service_acl(uid: u32, permissions: u16) -> Vec<u8> {
    let mut value = 2_u32.to_le_bytes().to_vec();
    for (tag, rights, id) in [
        (1_u16, permissions, u32::MAX),
        (2, permissions, uid),
        (4, 0, u32::MAX),
        (16, permissions, u32::MAX),
        (32, 0, u32::MAX),
    ] {
        value.extend(tag.to_le_bytes());
        value.extend(rights.to_le_bytes());
        value.extend(id.to_le_bytes());
    }
    value
}

fn protected_service_file(fd: impl std::os::fd::AsFd, uid: u32, directory: bool) -> bool {
    let Ok(info) = rustix::fs::fstat(&fd) else {
        return false;
    };
    let Ok(fs) = rustix::fs::fstatfs(&fd) else {
        return false;
    };
    let Ok(mount) = rustix::fs::fstatvfs(&fd) else {
        return false;
    };
    let permissions = if directory { 5 } else { 4 };
    let mode = if directory { 0o550 } else { 0o440 };
    let mut acl = [0_u8; 44];
    uid != 0
        && info.st_uid == 0
        && info.st_gid == 0
        && info.st_mode & 0o7777 == mode
        && matches!(fs.f_type as u64, 0x01021994 | 0x858458f6)
        && mount.f_flag.contains(rustix::fs::StatVfsMountFlags::RDONLY)
        && rustix::fs::fgetxattr(&fd, "system.posix_acl_access", &mut acl[..]) == Ok(acl.len())
        && acl.as_slice() == service_acl(uid, permissions)
}

pub fn read_protected(directory: &str, expected_sha256: &str) -> Result<CredentialFile> {
    let path = Path::new(directory);
    if !path.is_absolute()
        || expected_sha256.len() != 64
        || !expected_sha256
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(Error::InvalidConfiguration);
    }
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let mut dir = openat(CWD, "/", flags, Mode::empty()).map_err(|_| Error::InvalidKey)?;
    for component in path.components() {
        match component {
            Component::RootDir => (),
            Component::Normal(part) => {
                dir = openat(&dir, part, flags, Mode::empty()).map_err(|_| Error::InvalidKey)?;
            }
            _ => return Err(Error::InvalidKey),
        }
    }
    let stat = rustix::fs::fstat(&dir).map_err(|_| Error::InvalidKey)?;
    let uid = rustix::process::geteuid().as_raw();
    let service_credentials = path.parent() == Some(Path::new("/run/credentials"))
        && protected_service_file(&dir, uid, true);
    if !service_credentials && (stat.st_uid != uid || stat.st_mode & 0o022 != 0) {
        return Err(Error::InvalidKey);
    }
    let fd = openat(
        &dir,
        "app_security.credentials.json",
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK,
        Mode::empty(),
    )
    .map_err(|_| Error::InvalidKey)?;
    let mut file = File::from(fd);
    let before = file.metadata().map_err(|_| Error::InvalidKey)?;
    use std::os::unix::fs::MetadataExt;
    if !before.is_file()
        || if service_credentials {
            !protected_service_file(&file, uid, false)
        } else {
            before.uid() != uid || before.mode() & 0o077 != 0
        }
        || before.nlink() != 1
        || before.len() > encoding::MAX_CREDENTIALS as u64
    {
        return Err(Error::InvalidKey);
    }
    let mut bytes = Zeroizing::new(Vec::new());
    file.by_ref()
        .take((encoding::MAX_CREDENTIALS + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| Error::InvalidKey)?;
    let after = file.metadata().map_err(|_| Error::InvalidKey)?;
    if bytes.len() > encoding::MAX_CREDENTIALS
        || before.len() != after.len()
        || before.mtime() != after.mtime()
        || before.mtime_nsec() != after.mtime_nsec()
        || before.ctime() != after.ctime()
        || before.ctime_nsec() != after.ctime_nsec()
    {
        return Err(Error::InvalidKey);
    }
    let actual = digest::digest(&digest::SHA256, &bytes)
        .as_ref()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    if actual != expected_sha256 {
        return Err(Error::InvalidKey);
    }
    // Typed structs reject duplicate/unknown fields without a generic Value tree
    // that would create unguarded copies of secret strings.
    serde_json::from_slice(&bytes).map_err(|_| Error::InvalidKey)
}
