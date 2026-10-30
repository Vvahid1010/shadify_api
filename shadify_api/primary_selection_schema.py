"""Private v2 contact selection. No transport or authority issuer."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from .identity_sync_schema import normalize_email, full_phone

class ContactSelection(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    contract: Literal['primary-contact-v2'] = 'primary-contact-v2'
    operation_id: UUID
    node_id: UUID
    instance_id: UUID
    domain_app: Literal['shadify']
    factor_scope_id: UUID
    email: str | None = Field(default=None, max_length=254, repr=False)
    mobile: str | None = Field(default=None, max_length=32, repr=False)
    expected_primary_id: UUID | None
    expected_primary_revision: StrictInt = Field(ge=0, lt=2147483647)
    expected_selection_revision: StrictInt = Field(ge=0, lt=2147483647)
    expires_at: StrictInt = Field(gt=0)

    @field_validator('email', 'mobile')
    @classmethod
    def normalize(cls, value, info):
        return None if value is None else (normalize_email(value) if info.field_name == 'email' else full_phone(value))

    @model_validator(mode='after')
    def one_contact(self):
        if self.email is None and self.mobile is None:
            raise ValueError('primary_contact_required')
        return self


class SelectionRead(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    contract: Literal['primary-contact-v2'] = 'primary-contact-v2'
    operation_id: UUID
    node_id: UUID
    instance_id: UUID
    domain_app: Literal['shadify']
    factor_scope_id: UUID
    expires_at: StrictInt = Field(gt=0)
