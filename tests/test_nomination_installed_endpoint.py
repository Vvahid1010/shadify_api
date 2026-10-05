"""Actual Node installed record/seal composition, remapped OS source fixture."""
import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from test_nomination_endpoint import descriptor
from test_nomination_endpoint_installation import installation
from node_agent_package_helper import nomination_endpoint_installation as node
from node_agent_package_helper.nomination_endpoint import ProtectedReaderEndpointGuard
from node_agent_package_helper.nomination_reader import Denied
from shadify_api.nomination_transport import ProtectedNominationComposition


class InstalledEndpointTests(unittest.TestCase):
    def fixture(self, directory, patches):
        installed=installation.__wrapped__(Path(directory),patches)
        # Reuse Node's actual setup receipt, payload manifest, full official
        # verifier child and loaded-unit parser. Only OS manager/files/group
        # snapshot is virtual: no protected paths/root activation are accessed.
        expected=descriptor();observations=[]
        def os_snapshot(*,read_units):
            self.assertIs(type(read_units),node.ProtectedLoadedUnitObserver)
            units=read_units()  # Actual production observer, not fake ReaderUnits.
            observations.append(units)
            canonical_release=Path('/opt/node-agent/releases')/units.release_source_commit
            canonical_units=replace(expected.units,
                socket_fragment_sha256=units.socket_fragment_sha256,
                service_fragment_sha256=units.service_fragment_sha256,
                launcher_sha256=units.launcher_sha256,
                release_path=canonical_release,release_digest=units.release_digest,
                release_verified=units.release_verified,release_source_commit=units.release_source_commit,
                release_manifest_sha256=units.release_manifest_sha256,
                protected_installation_revision=units.protected_installation_revision)
            files=tuple(replace(entry,path=canonical_release) if entry.path==expected.units.release_path
                        else entry for entry in expected.files)
            return replace(expected,units=canonical_units,files=files)
        patches.setattr(node,'observe_endpoint',os_snapshot)
        patches.setattr(node.grp,'getgrnam',lambda name:SimpleNamespace(gr_gid=1002))
        return installed,observations

    def compose(self):
        # No observer/expected/group injected into the API construction.
        return ProtectedNominationComposition.from_installed_endpoint(
            read_installed=lambda _:None,management=None,require_control=lambda _:None)

    def test_actual_installed_factory_uses_official_record_seal_and_observer(self):
        with TemporaryDirectory() as directory,pytest.MonkeyPatch.context() as patches:
            installed,observations=self.fixture(directory,patches)
            composition=self.compose()
            self.assertIs(type(composition._endpoint_guard),ProtectedReaderEndpointGuard)
            self.assertEqual(observations[0].release_digest,installed.digest)
            self.assertTrue(observations[0].release_verified)
            self.assertEqual(observations[0].protected_installation_revision,
                hashlib.sha256(installed.setup.read_bytes()).hexdigest())
            self.assertTrue(any(command[:3]==['/usr/bin/python3','-I','-c'] for command in installed.calls))
            queried=[command[3] for command in installed.calls if command[0]=='/usr/bin/systemctl']
            self.assertIn(node.SERVICE_QUERY,queried)
            self.assertNotIn(node.TEMPLATE_UNIT,queried)
            composition._endpoint_guard.current()
            self.assertEqual(len(observations),2)
            self.assertIsNone(composition.adapter)  # No app mutation/provider activation.

    def test_real_loaded_unit_and_full_seal_drift_deny_without_fallback(self):
        for kind in ('payload_tamper','loaded_module_drift','missing_authoritative_receipt',
                     'wrong_instance_id','instance_fragment'):
            with self.subTest(kind=kind),TemporaryDirectory() as directory,pytest.MonkeyPatch.context() as patches:
                installed,observations=self.fixture(directory,patches)
                composition=self.compose()
                if kind=='payload_tamper':
                    (installed.release/'backend/helper.py').write_text('tampered')
                elif kind=='loaded_module_drift':
                    installed.service_info['Environment']=installed.service_info['Environment'].replace(node.MODULE,'other')
                elif kind=='wrong_instance_id':
                    installed.service_info['Id']=node.TEMPLATE_UNIT
                elif kind=='instance_fragment':
                    installed.service_info['FragmentPath']=str(installed.units/node.SERVICE_QUERY)
                else:installed.setup.unlink()
                with self.assertRaises(Denied):composition._endpoint_guard.current()
                with self.assertRaises(Denied):self.compose()
                self.assertEqual(len(observations),1)
                self.assertIsNone(composition.adapter)

    def test_absent_existing_group_denies_before_protected_observation(self):
        with pytest.MonkeyPatch.context() as patches:
            def absent(_):raise KeyError('fixture group absent')
            def forbidden(**_):raise AssertionError('must not observe')
            patches.setattr(node.grp,'getgrnam',absent)
            patches.setattr(node,'observe_endpoint',forbidden)
            with self.assertRaises(Denied):self.compose()
