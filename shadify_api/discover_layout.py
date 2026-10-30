"""Versioned Discover layout inside the existing editorial JSON snapshots."""
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from .artist_models import Input

REGISTRY_VERSION = 2
TEMPLATES = {
 'featured-hero': ('Featured / Hero', ['track','release','artist','playlist'], 'hero', None),
 'featured-picks': ('Featured Picks', ['track','release','artist','playlist'], 'rail', None),
 'mood-playlists': ('Mood Playlists', ['playlist'], 'rail', None),
 'singles-tracks': ('Singles & Tracks', ['track'], 'rail', None),
 'new-releases': ('New Releases', ['release'], 'rail', None),
 'new-from-artists': ('New from Artists', ['track','release'], 'rail', None),
 'music-videos': ('Music Videos', ['video'], 'rail', None),
 'video-playlists': ('Video Playlists', [], 'rail', 'No video-playlist source is connected.'),
 'podcasts': ('Podcasts & Episodes', [], 'list', 'No podcast or episode source is connected.'),
 'dj-sets': ('DJ Sets', [], 'rail', 'No DJ-set source is connected.'),
 'artist-spotlight': ('Artist Spotlight', ['artist'], 'rail', None),
 'personal-listening': ('Continue Listening / Recommended', [], 'rail', 'A real listener-history source is not connected. Listener data stays personal.'),
 'moments': ('Moments', [], 'rail', 'The current Moments provider is demo-only.'),
 'custom-special': ('Custom / Special', ['track','release','playlist','artist','video'], 'grid', None),
}
LEGACY = {'calm':'mood-playlists','romantic':'mood-playlists','happy':'mood-playlists','party':'mood-playlists','releases':'new-releases','tracks':'singles-tracks','videos':'music-videos','video-playlists':'video-playlists','podcasts':'podcasts','dj-sets':'dj-sets','artists':'artist-spotlight'}
LEGACY_TITLES = {'calm':'Calm','romantic':'Romantic','happy':'Happy','party':'Party'}

class LayoutEntry(Input):
 id: UUID
 kind: Literal['playlist','track','release','video','artist']
 entity_id: UUID | None = None
 title: str = Field(default='',max_length=200)
 track_ids: list[UUID] = Field(default_factory=list,max_length=100)
 @model_validator(mode='after')
 def references(self):
  if len(set(self.track_ids))!=len(self.track_ids):raise ValueError('Distinct ordered tracks required')
  if self.kind=='playlist':
   if self.entity_id is not None or not self.title.strip() or not self.track_ids:raise ValueError('Playlist title and selected tracks required')
  elif self.entity_id is None or (self.kind!='release' and self.track_ids):raise ValueError('Explicit content reference required')
  return self

class LayoutSettings(Input):
 show_empty: bool = False
 spacing: Literal['comfortable','compact'] = 'comfortable'

class Section(Input):
 id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
 template_id: str
 title: str = Field(max_length=200)
 subtitle: str = Field(default='',max_length=300)
 enabled: bool = True
 show_title: bool = True
 view_all: bool = True
 display: Literal['hero','rail','grid','list'] = 'rail'
 card_size: Literal['small','medium','large'] = 'medium'
 aspect: Literal['square','landscape','portrait'] = 'square'
 density: Literal['comfortable','compact'] = 'comfortable'
 item_limit: int = Field(default=12,strict=True,ge=1,le=20)
 mode: Literal['manual'] = 'manual'
 mood: Literal['calm','happy','party'] | None = None
 cta: Literal['play','open'] = 'open'
 cta_label: str = Field(default='Explore',max_length=40)
 entries: list[LayoutEntry] = Field(default_factory=list,max_length=20)
 @model_validator(mode='after')
 def matching(self):
  template=TEMPLATES.get(self.template_id)
  if template is None:raise ValueError('Unknown Discover template')
  if template[3] and (self.enabled or self.entries):raise ValueError(template[3])
  if any(entry.kind not in template[1] for entry in self.entries):raise ValueError('Content type does not match this template')
  keys=[(entry.kind,entry.entity_id or entry.id) for entry in self.entries]
  if len(set(keys))!=len(keys):raise ValueError('Distinct section selections required')
  if self.template_id=='featured-hero' and len(self.entries)>5:raise ValueError('Hero supports at most five selections')
  if self.mood and self.template_id!='mood-playlists':raise ValueError('Mood belongs to a Mood Playlists section')
  return self

class LayoutSnapshot(Input):
 schema_version: Literal[2] = 2
 registry_version: Literal[2] = 2
 settings: LayoutSettings = Field(default_factory=LayoutSettings)
 sections: list[Section] = Field(default_factory=list,max_length=40)
 @model_validator(mode='before')
 @classmethod
 def legacy(cls,value):
  if isinstance(value,dict) and set(value)=={'rails'}:
   if not isinstance(value['rails'],list):raise ValueError('Legacy rails must be a list')
   sections=[]
   for rail in value['rails']:
    if not isinstance(rail,dict) or set(rail)!={'id','entries'}:raise ValueError('Invalid legacy rail')
    template_id=LEGACY.get(rail['id'])
    if template_id is None:raise ValueError('Unknown legacy rail')
    template=TEMPLATES[template_id]
    sections.append(dict(id=rail['id'],template_id=template_id,title=LEGACY_TITLES.get(rail['id'],template[0]),enabled=template[3] is None,display=template[2],aspect='landscape' if template_id=='music-videos' else 'square',mood=rail['id'] if rail['id'] in ('calm','happy','party') else None,entries=rail['entries']))
   return dict(sections=sections)
  return value
 @property
 def rails(self):return self.sections
 @model_validator(mode='after')
 def bounded(self):
  ids=[s.id for s in self.sections];entries=[e for s in self.sections for e in s.entries]
  if len(ids)!=len(set(ids)) or len(entries)>100 or len({e.id for e in entries})!=len(entries) or sum(len(e.track_ids) for e in entries)>1000:raise ValueError('Distinct bounded sections and placements required')
  return self

def registry():
 return [dict(id=id,title=row[0],kinds=row[1],display=row[2],available=row[3] is None,reason=row[3],modes=['manual'] if row[3] is None else []) for id,row in TEMPLATES.items()]
