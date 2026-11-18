import re
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


def normalize_email(value):
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', value):
        raise ValueError('identity_lookup_invalid')
    return value


def full_phone(value):
    value = value.strip()
    if not re.fullmatch(r'\+[0-9]{8,15}', value):
        raise ValueError('identity_lookup_invalid')
    return value


class IdentitySyncInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1]
    account_id: str
    domain_app: Literal['shadify']
    factor_scope_id: str
    proxy_app: Literal['shadify_api']
    version: StrictInt = Field(gt=0, le=9223372036854775807)
    identity_status: Literal['active', 'pending', 'disabled', 'blocked', 'deleted']
    verified_email: str | None = Field(max_length=254)
    verified_phone: str | None = Field(max_length=32)

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_schema(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError('identity_sync_invalid')
        return value

    @field_validator('account_id', 'factor_scope_id')
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError('identity_sync_invalid')
        return value

    @field_validator('verified_email', 'verified_phone')
    @classmethod
    def canonical_contact(cls, value, info):
        if value is not None and (normalize_email(value) if info.field_name == 'verified_email' else full_phone(value)) != value:
            raise ValueError('identity_sync_invalid')
        return value
