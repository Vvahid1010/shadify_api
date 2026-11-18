"""Release ordering and publication; called inside the existing artist authority transaction."""
from .media import MediaError
from . import track_media


class ReleaseOperations:
    def _release_revision(self, row, expected):
        if type(expected) is not int or expected != row['revision']:
            raise MediaError(409, 'Release changed; reload before saving (release_revision_conflict)')

    def _release_tracks(self, c, artist, release, *, lock=False):
        return c.execute('SELECT t.* FROM shadify_release_tracks r JOIN shadify_tracks t '
                         'ON t.id=r.track_id AND t.artist_id=r.artist_id '
                         'WHERE r.release_id=%s AND r.artist_id=%s ORDER BY '+
                         ('t.id FOR UPDATE OF t' if lock else 'r.position'), (release,artist)).fetchall()

    def _track_memberships(self, c, artist, track_ids):
        result={str(track):[] for track in track_ids}
        if not track_ids: return result
        rows=c.execute("SELECT rt.track_id,rt.position,r.id,r.data,r.publish_state,r.cover_asset_id,"
                       "(SELECT count(*) FROM shadify_release_tracks n WHERE n.release_id=r.id) AS track_count "
                       "FROM shadify_release_tracks rt JOIN shadify_releases r ON r.id=rt.release_id "
                       "AND r.artist_id=rt.artist_id WHERE rt.artist_id=%s AND rt.track_id=ANY(%s) ORDER BY r.id",
                       (artist,track_ids)).fetchall()
        for row in rows:
            result[str(row['track_id'])].append(dict(id=str(row['id']),title=row['data']['title'],
                kind=row['data'].get('kind','Single'),position=row['position'],track_count=row['track_count'],
                publish_state=row['publish_state'],cover_asset_id=str(row['cover_asset_id']) if row['cover_asset_id'] else None))
        return result

    def _published_release_dependency(self,c,artist,track):
        return c.execute("SELECT 1 FROM shadify_release_tracks rt JOIN shadify_releases r ON r.id=rt.release_id "
                         "AND r.artist_id=rt.artist_id WHERE rt.track_id=%s AND rt.artist_id=%s "
                         "AND r.publish_state='published' LIMIT 1",(track,artist)).fetchone() is not None

    def _release_publication(self,c,auth,artist,row,state,expected,include_tracks):
        self._release_revision(row,expected)
        tracks=self._release_tracks(c,artist,row['id'],lock=True)
        if state=='published':
            if include_tracks is not True:
                raise MediaError(422,'Confirm publication of the release and its included songs')
            if (auth.publish_state=='suspended' or not row['data']['title'].strip() or not tracks
                    or row['data'].get('kind','Single')=='Single' and len(tracks)!=1):
                raise MediaError(409,'Release needs eligible songs and an active artist page')
            self._asset(c,artist,row['cover_asset_id'],'artwork_delivery')
            for track in tracks:
                if (not track_media.readiness(self,c,track,lock=True)['ready']
                        or not self._publishable(c,artist,'tracks',track)):
                    raise MediaError(409,'Every song needs both current input scans and outputs ready and attached')
            # All checks precede every write; no track is exposed before the same commit.
            for track in tracks:
                c.execute("UPDATE shadify_tracks SET publish_state='published' WHERE id=%s AND artist_id=%s",
                          (track['id'],artist))
        if row['publish_state']!=state:
            row=c.execute("UPDATE shadify_releases SET publish_state=%s,revision=revision+1, "
                          "data=CASE WHEN %s='published' THEN data||jsonb_build_object('published_at',"
                          "to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')) ELSE data END "
                          "WHERE id=%s AND artist_id=%s RETURNING *",(state,state,row['id'],artist)).fetchone()
        if state=='draft':
            for track in tracks:
                exposed=track['independently_published'] or self._published_release_dependency(c,artist,track['id'])
                c.execute('UPDATE shadify_tracks SET publish_state=%s WHERE id=%s AND artist_id=%s',
                          ('published' if exposed else 'draft',track['id'],artist))
        updated=self._release_tracks(c,artist,row['id'])
        memberships=self._track_memberships(c,artist,[t['id'] for t in updated])
        return {**auth.dto(),'content':self._content_dto(c,'releases',row),
                'tracks':[self._content_dto(c,'tracks',t,memberships=memberships) for t in updated]}
