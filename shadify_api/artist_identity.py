"""Bounded artist identity policy. Uses existing authority transactions and UUIDs."""
import re
import unicodedata
from datetime import timedelta
from uuid import uuid4
from psycopg import IntegrityError
from psycopg.types.json import Jsonb
from .media import MediaError

GENRE_IDS = ('pop','rock','electronic','hip-hop','r-and-b','folk','jazz','classical','country','metal','blues','soul','world','alternative','indie','ambient','dance','experimental','traditional')
RESERVED_SLUGS = frozenset(('admin','api','auth','studio','search','media','account','login','logout','signup','register','settings','support','help','privacy','terms','discover','artists','artist','track','release','playlist','tv','tvl','www','official','shadify'))
LANGUAGE = re.compile(r'^(?:[a-z]{2,3}(?:-[a-z]{3}){0,3}|[a-z]{4,8})(?:-[a-z]{4})?(?:-(?:[a-z]{2}|[0-9]{3}))?(?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*(?:-[0-9a-wy-z](?:-[a-z0-9]{2,8})+)*(?:-x(?:-[a-z0-9]{1,8})+)?$')
def language_tag(value):
    value=value.strip().lower()
    if len(value)>64 or not LANGUAGE.fullmatch(value):raise ValueError('Use a well-formed BCP 47 language tag')
    parts=value.split('-');singletons=[p for p in parts if len(p)==1 and p!='x']
    if len(singletons)!=len(set(singletons)):raise ValueError('Duplicate language extension')
    return value

def normalize_slug(value):
    value=value.strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,62}[a-z0-9]',value) or value in RESERVED_SLUGS:
        raise ValueError('Slug is reserved or invalid')
    return value

def search_text(value):
    value=unicodedata.normalize('NFKD',value).casefold().translate(str.maketrans('يىك٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹','ییک01234567890123456789'))
    return ' '.join(''.join(' ' if c in ('\u200c','\u200d') else c for c in value if not unicodedata.combining(c)).split())

class ArtistIdentityOperations:
    def identity_available(self,c):
        return c.execute("SELECT to_regclass('shadify_artist_slugs') AS table_name").fetchone()['table_name'] is not None

    def change_slug(self,actor,artist,body):
        try:
            with self.authorized(actor,artist,operation='edit_profile') as (c,auth,page,_):
                if not self.identity_available(c):raise MediaError(503,'Artist identity schema integration unavailable')
                if page['slug']!=body.expected_slug:raise MediaError(409,'Slug changed; reload before saving')
                if page['slug']==body.slug:return {**auth.dto(),'page':self._page(page)}
                now=c.execute('SELECT clock_timestamp() AS now').fetchone()['now']
                if page['slug_changed_at'] and not auth.primary and now<page['slug_changed_at']+timedelta(days=30):
                    raise MediaError(409,'Slug can be changed once every 30 days')
                previous=page['slug']
                row=c.execute('UPDATE shadify_artists SET slug=%s,slug_changed_at=%s WHERE id=%s RETURNING *',(body.slug,now,artist)).fetchone()
                c.execute('INSERT INTO shadify_artist_slug_audit(id,artist_id,actor_user_id,old_slug,new_slug,primary_override) VALUES(%s,%s,%s,%s,%s,%s)',
                          (uuid4(),artist,auth.user_id,previous,body.slug,bool(auth.primary and page['slug_changed_at'] and now<page['slug_changed_at']+timedelta(days=30))))
                return {**auth.dto(),'page':self._page(row)}
        except IntegrityError:raise MediaError(409,'Slug unavailable') from None

    def identity_profile(self,c,page,patch,model):
        if not self.identity_available(c):
            if set(patch)&{'localized','primary_language','genre_ids'}:raise MediaError(503,'Artist identity schema integration unavailable')
            return model
        # Old clients update the declared primary entry; no guessed translation or splitting.
        if not set(patch)&{'localized','primary_language'}:
            for entry in model.localized:
                if entry.language==model.primary_language:
                    entry.name=model.name;entry.bio=model.bio
        primary=next((entry for entry in model.localized if entry.language==model.primary_language),None)
        if primary is None:raise MediaError(422,'Primary language entry required')
        model.name=primary.name;model.bio=primary.bio
        legacy=page.get('genres',[])
        if any(value not in legacy for value in model.genres):raise MediaError(422,'Select canonical genres; existing legacy genres may be retained')
        return model
