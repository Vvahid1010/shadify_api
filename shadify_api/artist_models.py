"""Bounded plain-text inputs; authority/lifecycle fields have dedicated endpoints."""
from datetime import datetime
from typing import Literal
from uuid import UUID
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')

    @field_validator('*', mode='before')
    @classmethod
    def plain_text(cls, value):
        if isinstance(value, str):
            if any(ord(c)<32 and c!='\n' for c in value) or '\x7f' in value:
                raise ValueError('Control characters forbidden')
            return value.strip()
        return value


def https(value):
    p = urlsplit(value)
    if (len(value)>2048 or p.scheme!='https' or not p.hostname or p.username is not None
            or p.password is not None or any(c.isspace() for c in value)):
        raise ValueError('HTTPS URL required')
    return value


class Profile(Input):
    name: str = Field(min_length=1, max_length=120)
    bio: str = Field(default='', max_length=5000)
    genres: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('name')
    @classmethod
    def name_line(cls, value):
        if '\n' in value: raise ValueError('Single-line name required')
        return value

    @field_validator('genres')
    @classmethod
    def genre_values(cls, values):
        values=[v.strip() for v in values]
        if len(set(values))!=len(values) or any(not 1<=len(v.strip())<=64 or any(ord(c)<32 for c in v) for v in values):
            raise ValueError('Distinct short genres required')
        return [v.strip() for v in values]


class ArtistProfile(Profile):
    socials: list[str] = Field(default_factory=list, max_length=10)
    avatar_asset_id: UUID | None = None
    header_asset_id: UUID | None = None

    @field_validator('socials')
    @classmethod
    def safe_socials(cls, values):
        return [https(v) for v in values]


class ArtistCreate(ArtistProfile):
    slug: str = Field(min_length=3, max_length=64, pattern=r'^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$')


class PublicUserProfile(Profile):
    slug: str | None = Field(default=None, min_length=3, max_length=64, pattern=r'^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$')


class AssignOwner(Input):
    account_id: str = Field(min_length=1, max_length=36)


class TransferOwner(AssignOwner):
    expected_owner_account_id: str = Field(min_length=1, max_length=36)


class RevokeOwner(Input):
    expected_owner_account_id: str = Field(min_length=1, max_length=36)


class Publication(Input):
    state: Literal['draft','published']


class Suspension(Input):
    state: Literal['suspended','draft','published']


class Verification(Input):
    verified: bool = Field(strict=True)


class Work(Input):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=5000)

    @field_validator('title')
    @classmethod
    def title_line(cls, value):
        if '\n' in value: raise ValueError('Single-line title required')
        return value


class Track(Work):
    genre: str = Field(default='', max_length=64)
    access_class: Literal['free','paid'] = 'free'
    original_asset_id: UUID | None = None
    delivery_asset_id: UUID | None = None
    preview_asset_id: UUID | None = None
    artwork_asset_id: UUID | None = None


class Release(Work):
    kind: Literal['Single','EP','Album'] = 'Single'
    genre: str = Field(default='', max_length=64)
    cover_asset_id: UUID | None = None
    track_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @field_validator('track_ids')
    @classmethod
    def distinct_tracks(cls, values):
        if len(values)!=len(set(values)): raise ValueError('Distinct ordered tracks required')
        return values


class Video(Work):
    kind: Literal['music_video','live_performance'] = 'music_video'
    track_id: UUID | None = None
    delivery_asset_id: UUID | None = None


class Moment(Input):
    text: str = Field(min_length=1, max_length=5000)
    track_id: UUID | None = None
    image_asset_id: UUID | None = None


class Event(Work):
    starts_at: datetime
    timezone: str = Field(min_length=1,max_length=100)
    city: str = Field(min_length=1,max_length=200)
    venue: str = Field(min_length=1,max_length=200)
    ticket_url: str | None = None
    cancelled: bool = Field(default=False,strict=True)

    @field_validator('starts_at')
    @classmethod
    def aware_time(cls, value):
        if value.utcoffset() is None: raise ValueError('Offset-aware instant required')
        return value

    @field_validator('timezone')
    @classmethod
    def known_timezone(cls, value):
        try: ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError): raise ValueError('IANA timezone required')
        return value

    @field_validator('ticket_url')
    @classmethod
    def safe_ticket(cls,value):
        return https(value) if value is not None else None


class FeaturedWork(Input):
    kind: Literal['track','release']
    id: UUID


class Section(Input):
    id: Literal['featured','songs','video','moments','bio','tour']
    position: int = Field(ge=0,le=5,strict=True)
    visible: bool = Field(strict=True)


class Layout(Input):
    featured_work: FeaturedWork | None = None
    featured_video_id: UUID | None = None
    pinned_moment_id: UUID | None = None
    sections: list[Section] = Field(default_factory=list,max_length=6)

    @model_validator(mode='after')
    def distinct_sections(self):
        if (len({v.id for v in self.sections})!=len(self.sections)
                or len({v.position for v in self.sections})!=len(self.sections)):
            raise ValueError('Distinct sections and positions required')
        return self


RESOURCE_MODELS = {'tracks': Track, 'releases': Release, 'videos': Video, 'moments': Moment, 'events': Event}
