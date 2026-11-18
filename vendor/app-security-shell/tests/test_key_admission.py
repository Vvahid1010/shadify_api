"""Real compiled native admission using disposable Node Agent-shaped material."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pqcrypto.kem import ml_kem_768
from pqcrypto.sign import ml_dsa_65

from app_security_shell._native import validate_credentials


def raw_pem(label, value):
    encoded = base64.b64encode(value).decode('ascii')
    return f'-----BEGIN {label}-----\n' + '\n'.join(encoded[i:i+64] for i in range(0, len(encoded), 64)) + f'\n-----END {label}-----\n'


@pytest.fixture(scope='module')
def material():
    ed = Ed25519PrivateKey.generate()
    dsa_public, dsa_private = ml_dsa_65.generate_keypair()
    kem_public, kem_private = ml_kem_768.generate_keypair()
    return {
        'schema_version': 1,
        'node_id': '11111111-1111-4111-8111-111111111111',
        'app_id': 'wallet_manager',
        'app_instance_id': '22222222-2222-4222-8222-222222222222',
        'credential_owner_id': '33333333-3333-4333-8333-333333333333',
        'owner_app_instance_id': '22222222-2222-4222-8222-222222222222',
        'generation': 1,
        'ed25519': {
            'kid': 'ed-1',
            'private_key_pem': ed.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
            'public_key_pem': ed.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        },
        'ml_dsa_65': {'kid': 'dsa-1', 'private_key_pem': raw_pem('ML-DSA-65 PRIVATE KEY', dsa_private), 'public_key_pem': raw_pem('ML-DSA-65 PUBLIC KEY', dsa_public)},
        'ml_kem_768': {'kid': 'kem-1', 'private_key_pem': raw_pem('ML-KEM-768 PRIVATE KEY', kem_private), 'public_key_pem': raw_pem('ML-KEM-768 PUBLIC KEY', kem_public)},
    }


def write_material(directory, material):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    content = json.dumps(material).encode()
    target = directory / 'app_security.credentials.json'
    target.write_bytes(content)
    target.chmod(0o600)
    return target, hashlib.sha256(content).hexdigest()


def test_node_agent_provider_inputs_are_admitted(tmp_path, material):
    _, digest = write_material(tmp_path, material)
    assert validate_credentials(str(tmp_path), digest) == (1, 'wallet_manager', material['app_instance_id'], 1)


@pytest.mark.parametrize('field,value', [('schema_version', True), ('generation', 0), ('generation', True), ('node_id', 'node_01'), ('owner_app_instance_id', '44444444-4444-4444-8444-444444444444')])
def test_identity_and_schema_rejection(tmp_path, material, field, value):
    changed = copy.deepcopy(material)
    changed[field] = value
    _, digest = write_material(tmp_path, changed)
    with pytest.raises(ValueError, match='^app_security_invalid_credentials$'):
        validate_credentials(str(tmp_path), digest)


@pytest.mark.parametrize('kind', ['ed25519', 'ml_dsa_65', 'ml_kem_768'])
def test_mismatched_public_key_rejected(tmp_path, material, kind):
    changed = copy.deepcopy(material)
    if kind == 'ed25519':
        public = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    elif kind == 'ml_dsa_65':
        public = raw_pem('ML-DSA-65 PUBLIC KEY', ml_dsa_65.generate_keypair()[0])
    else:
        public = raw_pem('ML-KEM-768 PUBLIC KEY', ml_kem_768.generate_keypair()[0])
    changed[kind]['public_key_pem'] = public
    _, digest = write_material(tmp_path, changed)
    with pytest.raises(ValueError, match='^app_security_invalid_credentials$'):
        validate_credentials(str(tmp_path), digest)


def test_same_size_key_corruption_rejected_by_snapshot_digest(tmp_path, material):
    target, digest = write_material(tmp_path, material)
    changed = copy.deepcopy(material)
    text = changed['ml_kem_768']['private_key_pem']
    lines = text.splitlines()
    decoded = bytearray(base64.b64decode(''.join(lines[1:-1])))
    decoded[-1] ^= 1
    changed['ml_kem_768']['private_key_pem'] = raw_pem('ML-KEM-768 PRIVATE KEY', decoded)
    target.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='^app_security_invalid_credentials$'):
        validate_credentials(str(tmp_path), digest)


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o440, 0o666])
def test_file_permissions_rejected(tmp_path, material, mode):
    target, digest = write_material(tmp_path, material)
    target.chmod(mode)
    with pytest.raises(ValueError):
        validate_credentials(str(tmp_path), digest)


def test_symlink_file_and_directory_rejected(tmp_path, material):
    actual = tmp_path / 'actual'
    target, digest = write_material(actual, material)
    linked = tmp_path / 'linked'
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError):
        validate_credentials(str(linked), digest)
    destination = actual / 'renamed'
    target.rename(destination)
    target.symlink_to(destination)
    with pytest.raises(ValueError):
        validate_credentials(str(actual), digest)


def test_duplicate_fields_and_sanitized_failure(tmp_path, material, capfd):
    target, _ = write_material(tmp_path, material)
    secret = 'SYNTHETIC-PRIVATE-CANARY'
    data = target.read_text()[:-1] + f', "schema_version": 1, "{secret}": true}}'
    target.write_text(data)
    with pytest.raises(ValueError) as error:
        validate_credentials(str(tmp_path), hashlib.sha256(data.encode()).hexdigest())
    assert str(error.value) == 'app_security_invalid_credentials'
    assert secret not in ''.join(capfd.readouterr())


def test_special_file_rejected_without_blocking(tmp_path):
    tmp_path.chmod(0o700)
    os.mkfifo(tmp_path / 'app_security.credentials.json', 0o600)
    with pytest.raises(ValueError):
        validate_credentials(str(tmp_path), '0' * 64)


@pytest.mark.skipif(os.environ.get('APP_SECURITY_SYSTEMD_TEST') != '1',
                    reason='requires explicit disposable systemd composition')
def test_actual_systemd_loadcredential_admission(tmp_path, material):
    import subprocess
    import sys
    import uuid
    import app_security_shell

    target, digest = write_material(tmp_path, material)
    package = str(Path(app_security_shell.__file__).resolve().parent.parent)
    code = """import os, sys
sys.path.insert(0,sys.argv[1])
from app_security_shell._native import validate_credentials
location=os.environ['CREDENTIALS_DIRECTORY']
assert validate_credentials(location,sys.argv[2]) == (1,'wallet_manager',sys.argv[3],1)
try: validate_credentials(location,'0'*64)
except ValueError: pass
else: raise AssertionError('wrong_digest_accepted')
print('protected_systemd_credentials_admitted')
"""
    result = subprocess.run([
        'sudo','-n','systemd-run','--quiet','--wait','--pipe','--collect',
        '--unit=app-security-credentials-test-'+uuid.uuid4().hex,
        '--property=User=nobody','--property=MemorySwapMax=0',
        '--property=LimitCORE=0','--property=RuntimeMaxSec=30s',
        '--property=LoadCredential=app_security.credentials.json:'+str(target),
        sys.executable,'-I','-B','-c',code,package,digest,material['app_instance_id'],
    ],capture_output=True,text=True,timeout=40)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'protected_systemd_credentials_admitted'
