"""Exact published synthetic wire vectors from the finalized Auth owner commit."""
import hashlib,json,unittest
from pathlib import Path
from shadify_api.nomination_contract import parse_operation,SelectionProjection,ProjectionBase,Contacts

class AuthNominationVectorTests(unittest.TestCase):
    def test_four_exact_owner_bodies_and_projection_shapes(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/auth_nomination_92a4e07b.json').read_text())
        self.assertEqual(fixture['auth_source_commit'],'92a4e07b681aaf9964084ebf0741331acf3cfcab')
        for vector in fixture['lifecycle_vectors']:
            raw=vector['exact_utf8_body'].encode('utf-8');operation=parse_operation(raw)
            self.assertEqual(operation.operation,vector['operation'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(),vector['body_sha256'])
            model=SelectionProjection if operation.operation in ('register','replace') else ProjectionBase
            proof=model.model_validate(vector['admin_nomination_v1'])
            self.assertEqual(proof.body_sha256,vector['body_sha256']);self.assertEqual(proof.scope,operation.scope)
        Contacts.model_validate(fixture['verified_contacts_v1'])
