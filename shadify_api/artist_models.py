"""Bounded plain-text inputs; authority/lifecycle fields have dedicated endpoints."""
from datetime import datetime
from typing import Literal
from uuid import UUID
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .artist_identity import GENRE_IDS, language_tag, normalize_slug
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


class LocalizedArtist(Input):
    language: str = Field(min_length=2,max_length=64)
    name: str = Field(min_length=1,max_length=120)
    bio: str = Field(default='',max_length=5000)
    aliases: list[str] = Field(default_factory=list,max_length=20)
    _language=field_validator('language')(language_tag)
    @field_validator('name')
    @classmethod
    def single_line(cls,value):
        if '\n' in value:raise ValueError('Single-line name required')
        return value
    @field_validator('aliases')
    @classmethod
    def alias_values(cls,values):
        values=[v.strip() for v in values]
        if any(not 1<=len(v)<=120 or any(ord(c)<32 for c in v) for v in values) or len(set(v.casefold() for v in values))!=len(values):raise ValueError('Distinct short single-line aliases required')
        return values

class SlugChange(Input):
    slug: str
    expected_slug: str = Field(min_length=3,max_length=64)
    _slug=field_validator('slug')(normalize_slug)

class ArtistProfile(Profile):
    primary_language: str = 'und'
    localized: list[LocalizedArtist] = Field(default_factory=list,max_length=20)
    genre_ids: list[str] = Field(default_factory=list,max_length=20)
    _language=field_validator('primary_language')(language_tag)
    @field_validator('genre_ids')
    @classmethod
    def canonical_genres(cls,values):
        if len(set(values))!=len(values) or any(v not in GENRE_IDS for v in values):raise ValueError('Distinct canonical genres required')
        return values
    @model_validator(mode='after')
    def distinct_languages(self):
        if len({v.language for v in self.localized})!=len(self.localized):raise ValueError('Distinct language entries required')
        if self.localized and self.primary_language not in {v.language for v in self.localized}:raise ValueError('Primary language entry required')
        return self

    socials: list[str] = Field(default_factory=list, max_length=10)
    avatar_asset_id: UUID | None = None
    header_asset_id: UUID | None = None

    @field_validator('socials')
    @classmethod
    def safe_socials(cls, values):
        values=[https(v) for v in values]
        keys=[]
        for value in values:
            url=urlsplit(value)
            try:port=url.port
            except ValueError:raise ValueError('Invalid URL port') from None
            keys.append((url.hostname.lower(),port or 443,url.path.rstrip('/'),url.query))
        if len(set(keys))!=len(keys):raise ValueError('Distinct links required')
        return values


class ArtistCreate(ArtistProfile):
    slug: str = Field(min_length=3, max_length=64, pattern=r'^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$')


    _slug=field_validator('slug')(normalize_slug)


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


class ReleasePublication(Publication):
    expected_revision: int = Field(ge=0,strict=True)
    publish_included_tracks: bool = Field(default=False,strict=True)


class ReleaseRevision(Input):
    expected_revision: int = Field(ge=0,strict=True)


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


MOOD_IDS = ('calm', 'romantic', 'happy', 'party')
MoodId = Literal['calm', 'romantic', 'happy', 'party']


class Track(Work):
    mood_ids: list[MoodId] = Field(default_factory=list, max_length=4)

    @field_validator('mood_ids')
    @classmethod
    def distinct_moods(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('Distinct mood identifiers required')
        return [mood for mood in MOOD_IDS if mood in values]

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


class DelegateRevision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(strict=True, ge=0, le=9223372036854775807)


class DelegatePermissions(DelegateRevision):
    permissions: list[Literal['create_artist','edit_profile','manage_content','publish_content',
        'change_owner','suspend','verify','edit_user_profile']] = Field(max_length=8)


class PrimaryTransfer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_id: str = Field(min_length=1,max_length=36)
    expected_primary_account_id: str = Field(min_length=1,max_length=36)
    delegates: Literal['retain','revoke']
