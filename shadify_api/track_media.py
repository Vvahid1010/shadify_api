"""Current-input readiness. Read projections never attach or publish media."""
from uuid import UUID
from .media import Asset, MediaError


def source_job(c, artist, track, reference, kind):
    if reference is None:
        return None
    if c.execute("SELECT to_regclass('shadify_ingest_variants') AS name").fetchone()['name'] is None:
        return None
    return c.execute(
        "SELECT j.* FROM shadify_ingest_jobs j "
        "JOIN shadify_media_assets source ON source.id=j.asset_id "
        "WHERE source.artist_id=%s AND source.track_id=%s AND source.asset_type=%s "
        "AND (j.asset_id=%s OR EXISTS(SELECT 1 FROM shadify_ingest_variants v "
        "WHERE v.job_id=j.id AND v.asset_id=%s))",
        (artist, track, kind, reference, reference)).fetchone()


def current_source(c, row, kind):
    field = 'original_asset_id' if kind == 'audio' else 'artwork_asset_id'
    job = source_job(c, row['artist_id'], row['id'], row[field], kind+'_original')
    # Unknown legacy references remain distinct; never resolve by upload order.
    return job['asset_id'] if job else row[field]


def readiness(owner, c, row, *, lock=False):
    checks, sources, outputs = {}, {}, {}
    delivery_eligible=False
    for kind in ('cover', 'audio'):
        asset_kind = 'artwork' if kind == 'cover' else 'audio'
        field = 'artwork_asset_id' if kind == 'cover' else 'original_asset_id'
        job = source_job(c, row['artist_id'], row['id'], row[field], asset_kind+'_original')
        source = dict(asset_id=str(job['asset_id']), job_id=str(job['id']),
                      input_sha256=job['input_sha256']) if job else None
        sources[kind] = source
        scan_state = output_state = 'absent' if row[field] is None else 'unavailable'
        error = None
        approved = {}
        if job:
            error = job['last_error']
            scan = c.execute("SELECT verdict,full_coverage FROM shadify_ingest_scans "
                             "WHERE job_id=%s AND input_sha256=%s ORDER BY scanned_at DESC,id DESC LIMIT 1",
                             (job['id'], job['input_sha256'])).fetchone()
            scan_state = ('passed' if scan and scan['verdict']=='clean' and scan['full_coverage']
                          else 'rejected' if scan and scan['verdict']=='infected'
                          else 'running' if job['state']=='scanning'
                          else 'pending' if job['state']=='uploaded' else 'unavailable')
            if error=='bytes_changed':
                scan_state='unavailable'
            output_state = ('rejected' if job['state']=='rejected' else
                            'unavailable' if error else 'running' if job['state'] in
                            ('validating','matching','processing') else 'pending')
            if job['state']=='ready' and scan_state=='passed':
                variants = c.execute("SELECT v.role,a.* FROM shadify_ingest_variants v "
                                     "JOIN shadify_media_assets a ON a.id=v.asset_id WHERE v.job_id=%s",
                                     (job['id'],)).fetchall()
                required = {'master','cover'} if kind=='cover' else {'master','high','low'}
                try:
                    for variant in variants:
                        if variant['role'] not in required:
                            continue
                        expected_kind = ('audio_original' if kind=='audio' else 'artwork_original') if variant['role']=='master' else ('audio_delivery' if kind=='audio' else 'artwork_delivery')
                        if (variant['state']!='ready' or variant['asset_type']!=expected_kind
                                or variant['artist_id']!=row['artist_id'] or variant['track_id']!=row['id']):
                            raise MediaError(409, 'Output not ready')
                        asset = Asset(**{key:variant[key] for key in Asset.__dataclass_fields__})
                        owner.require_ingest_approval(asset, cursor=c, lock=lock)
                        approved[variant['role']] = str(asset.id)
                        if kind=='audio' and variant['role']=='high':
                            delivery_eligible=asset.access_class==row['access_class']
                    output_state = 'passed' if set(approved)==required else 'unavailable'
                except MediaError:
                    output_state = 'unavailable'
                    approved = {}
        checks[kind+'_scan'] = dict(status=scan_state, source=source, error=error)
        checks[kind+'_output'] = dict(status=output_state, source=source, error=error)
        outputs[kind] = approved
    audio, cover = outputs['audio'], outputs['cover']
    attached = bool(audio and cover and str(row['original_asset_id'])==audio.get('master')
                    and str(row['delivery_asset_id'])==audio.get('high')
                    and str(row['artwork_asset_id'])==cover.get('cover'))
    return dict(checks=checks, sources=sources, outputs=outputs, attached=attached,
                ready=attached and delivery_eligible and bool(row['title'].strip())
                and all(check['status']=='passed' for check in checks.values()))


def check_expected(c, row, expected):
    if type(expected) is not dict or not set(expected)<= {'audio','cover'} or not expected:
        raise MediaError(422, 'Expected current inputs required')
    for kind, value in expected.items():
        try:
            supplied = UUID(value) if value is not None else None
        except (ValueError, TypeError, AttributeError):
            raise MediaError(422, 'Invalid expected input') from None
        actual = current_source(c, row, 'artwork' if kind=='cover' else 'audio')
        if supplied != actual:
            raise MediaError(409, 'Selected input changed; refresh song')


def prepare(owner, c, row, expected):
    check_expected(c, row, expected)
    state = readiness(owner, c, row, lock=True)
    audio, cover = state['outputs']['audio'], state['outputs']['cover']
    changes={}
    for kind in expected:
        if not all(state['checks'][kind+'_'+stage]['status']=='passed' for stage in ('scan','output')):
            raise MediaError(409, 'Current input checks are incomplete')
        if kind=='audio':
            if row['preview_asset_id'] is not None:
                preview_job=source_job(c,row['artist_id'],row['id'],row['preview_asset_id'],'audio_original')
                if not preview_job or str(preview_job['asset_id'])!=state['sources']['audio']['asset_id']:
                    raise MediaError(409,'Existing preview is not bound to current audio')
                owner._asset(c,row['artist_id'],row['preview_asset_id'],'audio_preview',track_id=row['id'])
            changes.update(original_asset_id=audio['master'],delivery_asset_id=audio['high'])
        else:
            changes.update(artwork_asset_id=cover['cover'])
    if all(str(row[key])==str(value) for key,value in changes.items()):
        return row
    if row['publish_state']!='draft':
        raise MediaError(409, 'Unpublish before replacing media')
    # Existing manage_content transaction owns attachment, never the worker.
    return c.execute('UPDATE shadify_tracks SET '+','.join(key+'=%s' for key in changes)+
                     ' WHERE id=%s AND artist_id=%s RETURNING *',
                     (*changes.values(),row['id'],row['artist_id'])).fetchone()
