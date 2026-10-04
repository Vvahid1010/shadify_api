"""Finite provider/status contract on real isolated SQL; verifier synthetic."""
from uuid import uuid4
from unittest.mock import patch
from shadify_api.media import MediaError
from shadify_api.nomination_control import NominationControl
from shadify_api.nomination_contract import Witness
from test_nomination_control import NominationControlCase

class NominationProviderTests(NominationControlCase):
    def snapshot(self):
        with self.connect() as c:
            return tuple(c.execute(query).fetchall() for query in (
                'SELECT primary_user_id,revision FROM shadify_app_admin',
                'SELECT id,revision,state FROM shadify_admin_nomination_v1 ORDER BY id',
                'SELECT operation_id FROM shadify_nomination_operations ORDER BY operation_id',
                'SELECT id FROM shadify_app_admin_audit ORDER BY id'))
    def test_no_provider_never_treats_witness_file_or_contacts_as_authority(self):
        owner=NominationControl(self.connect,lambda:self.scope)
        before=self.snapshot()
        with self.assertRaises(MediaError) as error:owner.read_verified_witness(self.scope,str(uuid4()))
        self.assertEqual(error.exception.status,503)
        with self.assertRaises(MediaError):owner.finalize(self.target,self.contacts())
        with self.assertRaises(MediaError):owner.read_operation_result(self.scope,str(uuid4()))
        self.assertEqual(self.snapshot(),before)
    def test_verified_read_and_provider_current_checks_are_exact(self):
        cmd=self.command();self.apply_command(cmd)
        with patch.object(self.reader,'require_current',wraps=self.reader.require_current) as current:
            witness=self.nom.read_verified_witness(self.scope,cmd.operation_id)
            self.assertIs(witness,self.reader.witness)
            self.assertEqual(current.call_count,2)
        with self.assertRaises(MediaError):self.nom.read_verified_witness(self.scope,str(uuid4()))
    def test_status_known_unknown_and_claimed_are_read_only(self):
        cmd=self.command();self.apply_command(cmd);before=self.snapshot()
        result=self.nom.read_operation_result(self.scope,cmd.operation_id)
        self.assertEqual(set(result),{'operation_id','operation','nomination_id','revision','state','intent','expires_at'})
        self.assertEqual(result['state'],'pending');self.assertEqual(result['revision'],1)
        self.assertIsNone(self.nom.read_operation_result(self.scope,str(uuid4())))
        self.assertEqual(self.snapshot(),before)
        self.nom.finalize(self.target,self.contacts());before=self.snapshot()
        result=self.nom.read_operation_result(self.scope,cmd.operation_id)
        self.assertEqual(result['state'],'claimed');self.assertEqual(result['revision'],2)
        self.assertEqual(self.snapshot(),before)
    def test_other_scope_and_noncanonical_id_fail_before_lookup(self):
        wrong=self.scope.model_copy(update={'deployment_id':'wrong'})
        before=self.snapshot()
        with patch.object(self.nom,'connect',side_effect=AssertionError('must not query')):
            with self.assertRaises(MediaError):self.nom.read_operation_result(wrong,str(uuid4()))
            for operation in (str(uuid4()).replace('-',''),uuid4(),'not-a-uuid'):
                with self.assertRaises(MediaError):self.nom.read_operation_result(self.scope,operation)
        self.assertEqual(self.snapshot(),before)
    def test_binding_drift_after_lookup_never_returns_result(self):
        cmd=self.command();self.apply_command(cmd);before=self.snapshot();calls=[0]
        def current(scope,witness=None):
            calls[0]+=1
            if calls[0]==2:raise MediaError(409,'Synthetic installed binding changed')
        with patch.object(self.reader,'require_current',side_effect=current):
            with self.assertRaises(MediaError):self.nom.read_operation_result(self.scope,cmd.operation_id)
        self.assertEqual(calls[0],2);self.assertEqual(self.snapshot(),before)
    def test_bare_dict_and_permissive_verifier_return_fail_closed(self):
        cmd=self.command();self.apply_command(cmd);before=self.snapshot()
        with patch.object(self.reader,'read_verified_witness',return_value=self.reader.witness.model_dump()):
            with self.assertRaises(MediaError):self.nom.read_verified_witness(self.scope,cmd.operation_id)
        with patch.object(self.reader,'require_current',return_value=True):
            with self.assertRaises(MediaError):self.nom.read_operation_result(self.scope,cmd.operation_id)
        self.assertEqual(self.snapshot(),before)
    def test_witness_version_is_strict_integer(self):
        cmd=self.command();self.apply_command(cmd)
        body=self.reader.witness.model_dump(mode='json')
        for version in (True,'1',1.0):
            with self.assertRaises(ValueError):Witness.model_validate({**body,'schema_version':version})

    def test_immutable_register_receipt_survives_renew_then_claim(self):
        cmd=self.command();self.apply_command(cmd)
        first=self.nom.read_committed_operation(self.scope,cmd.operation_id)
        self.assertEqual(first.outcome,'committed');self.assertEqual(first.receipt.status,'pending');self.assertEqual(first.receipt.revision,1)
        renew=self.follow(cmd,'renew',expires_at=cmd.expires_at+60);self.apply_command(renew)
        self.assertEqual(self.nom.read_committed_operation(self.scope,cmd.operation_id),first)
        renewed=self.nom.read_committed_operation(self.scope,renew.operation_id)
        self.assertEqual(renewed.receipt.revision,2);self.assertEqual(renewed.receipt.status,'pending')
        self.nom.finalize(self.target,self.contacts())
        self.assertEqual(self.nom.read_committed_operation(self.scope,cmd.operation_id),first)
        self.assertEqual(self.nom.read_committed_operation(self.scope,renew.operation_id),renewed)
        self.assertEqual(self.nom.read_operation_result(self.scope,cmd.operation_id)['state'],'claimed')
    def test_immutable_revoke_and_replacement_receipts(self):
        cmd=self.command();self.apply_command(cmd)
        registered=self.nom.read_committed_operation(self.scope,cmd.operation_id)
        revoke=self.follow(cmd,'revoke');self.apply_command(revoke)
        revoked=self.nom.read_committed_operation(self.scope,revoke.operation_id)
        self.assertEqual(revoked.operation,'revoke');self.assertEqual(revoked.receipt.status,'revoked');self.assertEqual(revoked.receipt.revision,2)
        self.assertEqual(self.nom.read_committed_operation(self.scope,cmd.operation_id),registered)
        fresh=self.command();self.apply_command(fresh)
        replacement=self.command(operation='replace',nomination_id=fresh.nomination_id,expected_nomination_revision=1,replacement_nomination_id=str(uuid4()))
        self.apply_command(replacement)
        replaced=self.nom.read_committed_operation(self.scope,replacement.operation_id)
        self.assertEqual(replaced.receipt.nomination_id,replacement.replacement_nomination_id)
        self.assertEqual(replaced.receipt.status,'pending');self.assertEqual(replaced.receipt.revision,1)
        self.assertEqual(self.nom.read_committed_operation(self.scope,revoke.operation_id),revoked)
    def test_committed_lookup_not_found_unknown_and_drift(self):
        operation_id=str(uuid4());before=self.snapshot()
        absent=self.nom.read_committed_operation(self.scope,operation_id)
        self.assertEqual(absent.outcome,'not_found');self.assertIsNone(absent.operation);self.assertIsNone(absent.receipt)
        with patch.object(self.nom,'connect',side_effect=RuntimeError('synthetic DB failure')):
            failed=self.nom.read_committed_operation(self.scope,operation_id)
        self.assertEqual(failed.outcome,'unknown');self.assertIsNone(failed.operation);self.assertIsNone(failed.receipt)
        self.assertEqual(self.snapshot(),before)
        calls=[0]
        def current(scope,witness=None):
            calls[0]+=1
            if calls[0]==2:raise MediaError(409,'Synthetic binding drift')
        with patch.object(self.reader,'require_current',side_effect=current):
            with self.assertRaises(MediaError):self.nom.read_committed_operation(self.scope,operation_id)
        self.assertEqual(calls[0],2)
    def test_missing_or_conflicting_audit_is_unknown_not_committed(self):
        from psycopg.types.json import Jsonb
        cmd=self.command();self.apply_command(cmd)
        with self.connect() as c:
            actor=c.execute("SELECT id FROM shadify_users WHERE auth_account_id='admin'").fetchone()[0]
            c.execute("INSERT INTO shadify_app_admin_audit(id,actor_user_id,actor_account_id,action,details) VALUES(%s,%s,'admin','nomination_v1_register',%s)",
                      (uuid4(),actor,Jsonb({'operation_id':cmd.operation_id,'nomination_id':cmd.nomination_id})))
        result=self.nom.read_committed_operation(self.scope,cmd.operation_id)
        self.assertEqual(result.outcome,'unknown');self.assertIsNone(result.receipt)
        # A synthetic ledger row without finalized matching audit cannot be reported.
        fake=str(uuid4())
        with self.connect() as c:
            c.execute("INSERT INTO shadify_nomination_operations(operation_id,nomination_id,scope,operation,actor_account_id,body_sha256,approval_fingerprint,result) SELECT %s,nomination_id,scope,operation,actor_account_id,body_sha256,%s,result FROM shadify_nomination_operations WHERE operation_id=%s",(fake,'c'*64,cmd.operation_id))
        self.assertEqual(self.nom.read_committed_operation(self.scope,fake).outcome,'unknown')
    def test_committed_envelope_is_frozen_exact_and_read_only(self):
        from pydantic import ValidationError
        cmd=self.command();self.apply_command(cmd);before=self.snapshot()
        result=self.nom.read_committed_operation(self.scope,cmd.operation_id)
        self.assertEqual(set(result.model_dump()),{'schema_version','scope','operation_id','outcome','operation','receipt'})
        self.assertEqual(set(result.receipt.model_dump()),{'operation_id','nomination_id','status','revision'})
        with self.assertRaises(ValidationError):result.receipt.revision=99
        self.assertEqual(self.snapshot(),before)
