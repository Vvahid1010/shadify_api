"""Two artist-scoped image purposes; same upload/scan/processing pipeline."""
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

PROFILE_ORIGINALS = frozenset(('avatar_original', 'header_original'))
PROFILE_DELIVERIES = frozenset(('avatar_delivery', 'header_delivery'))
PROFILE_KINDS = PROFILE_ORIGINALS | PROFILE_DELIVERIES
MAX_PROFILE_IMAGE_BYTES = 10 * 1024 * 1024

class ProfileUploadIntent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    purpose: Literal['avatar', 'header']
    mime: Literal['image/jpeg', 'image/png']
    size: int = Field(gt=0, le=MAX_PROFILE_IMAGE_BYTES)

def profile_asset(asset):
    return asset.asset_type in PROFILE_KINDS

def profile_operation(asset):
    return 'edit_profile' if profile_asset(asset) else 'manage_content'

def delivery_kind(original):
    return {'avatar_original':'avatar_delivery','header_original':'header_delivery'}[original]
