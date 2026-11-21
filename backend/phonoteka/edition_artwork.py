"""Select front artwork only after binding the current track to a release."""
import json

from . import confirmed_fields as fields, release_review
from .providers import embedded_recording_id, normalized, scalar


def edition_id(conn, row, metadata, analysis):
    tags = metadata.get('tags') or {}
    if (not scalar(tags, 'album').strip() or any(isinstance(tags.get(k), list) and len(tags[k]) != 1
            for k in ('musicbrainz_albumid', 'musicbrainz_trackid', 'album', 'artist', 'title'))
            or any(analysis.get(k) for k in ('possible_mix', 'multiple_recordings', 'critical_conflicts', 'unresolved_conflicts'))):
        return None
    album = normalized(scalar(tags, 'album'))
    embedded = scalar(tags, 'musicbrainz_albumid').lower()
    if embedded and not fields.UUID.fullmatch(embedded):
        return None
    recordings, _, error = fields.recording(conn, analysis, metadata)
    if error not in (None, 'recording_unconfirmed'):
        return None
    ref_recording = embedded_recording_id(metadata)
    if ref_recording and any(c.get('recording_id') != ref_recording for c in recordings):
        return None
    if not recordings:
        recordings = fields.tagged_artwork_recordings(conn, metadata, analysis)
    official = {release.get('id', '').lower() for recording in recordings for release in recording.get('releases', [])
                if release.get('status') == 'Official' and normalized(release.get('title', '')) == album}
    verified = {embedded} if embedded and embedded in official else set()
    proof = analysis.get('verified_catalog_edition') or {}
    if (proof.get('file_sha256') == row['sha256'] and proof.get('slot') and normalized(proof.get('album','')) == album
            and fields.UUID.fullmatch(str(proof.get('release_id',''))) and fields.permitted(conn, proof, ('cache','derived_analysis'))):
        from .collection_evidence import bound_recording
        if bound_recording(conn,metadata,analysis) == proof.get('recording_id'):
            verified.add(proof['release_id'])
    # Previously audited release membership is scoped to the actual file revision.
    for hit in conn.execute("""SELECT value FROM settings WHERE key GLOB 'release_review:*'
            AND EXISTS (SELECT 1 FROM json_each(value,'$.members') m WHERE
            json_extract(m.value,'$.track_id')=? AND json_extract(m.value,'$.sha256')=?)""", (row['id'], row['sha256'])):
        review = release_review.report(conn, json.loads(hit[0]))
        if (review.get('stale') or review.get('state') not in ('complete', 'subset_complete')
                or row['id'] not in review.get('matched_track_ids', [])):
            continue
        ref = review.get('reference') or {}
        if ref.get('status') != 'Official' or normalized(ref.get('title', '')) != album:
            continue
        for track in review.get('tracks', []):
            if (track.get('expected') and any(f.get('track_id') == row['id'] and not f.get('reasons')
                    and f.get('evidence') in ('release_track_id', 'recording_id_and_position') for f in track['files'])
                    and (not embedded_recording_id(metadata) or embedded_recording_id(metadata) == track.get('recording_id'))):
                rid = str(review.get('release_id', '')).lower()
                if fields.UUID.fullmatch(rid):
                    verified.add(rid)
    if embedded and verified != {embedded}:
        return None
    return next(iter(verified)) if len(verified) == 1 else None

def library_front(settings, identity):
    """Recover a native committed CAA front by release, content digest and current file revision."""
    import base64
    from . import db, field_policy, media, provider_policy

    with db.connect(settings.db_path) as conn:
        donors = conn.execute("""SELECT relative_path,sha256,json_extract(metadata,'$.usage_provenance.cover') AS origin
            FROM tracks WHERE is_current=1 AND status NOT IN ('error','conflict','missing')
            AND json_extract(metadata,'$.has_art')=1 AND json_extract(metadata,'$.usage_provenance.cover.candidate_id')=?
            AND json_extract(metadata,'$.usage_provenance.cover.provider')='coverartarchive' ORDER BY id""",
            ('caa:'+identity,)).fetchall()
        if not donors:
            return None
        origins = [json.loads(d['origin']) for d in donors]
        hashes = {o.get('value_sha256') for o in origins}
        if len(hashes) != 1 or not fields.HASH.fullmatch(str(next(iter(hashes)))):
            raise media.MediaError('cover_conflict','Для одного издания сохранены разные обложки.')
        for donor, origin in zip(donors, origins):
            usage = origin.get('usage') or {}
            if (usage.get('provider') != 'coverartarchive' or usage.get('candidate_id') != 'caa:'+identity
                    or not all(provider_policy._allowed(conn,usage,k) for k in ('cache','derived_analysis','embed'))):
                continue
            try:
                path = media._validate(settings.work_root / donor['relative_path'], settings.roots)
                media._expect(path, donor['sha256'])
                image = media.read_art(path, settings.roots)
                if not image:
                    continue
                data, mime = image
                cover = 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')
                if field_policy.value_hash(field_policy.normalized_value('cover', cover)) != origin['value_sha256']:
                    continue
                return {'id':'caa:'+identity, 'release_id':identity,'provider':'coverartarchive','kind':'cover',
                        'source_url':'https://coverartarchive.org/release/'+identity, 'usage':usage,'cover':cover,
                        'match':{'state':'needs_review','reasons':['confirm_release']}}
            except (OSError, media.MediaError):
                continue
    return None

def unique_edition(page, album, embedded):
    if not isinstance(page, dict) or page.get('release-offset') != 0:
        return None
    releases = page.get('releases')
    if not isinstance(releases, list) or page.get('release-count') != len(releases):
        return None
    if any(not isinstance(v, dict) or not fields.UUID.fullmatch(str(v.get('id',''))) or not v.get('title') for v in releases):
        return None
    options = [v for v in releases if normalized(v['title']) == normalized(album)]
    if embedded:
        options = [v for v in options if v['id'] == embedded]
    if len(options) == 1 and options[0].get('status') == 'Official':
        return options[0]['id']
    return None


def release_slot(ref, recording, meta):
    tags = meta['tags']
    if ref.get('status') != 'Official' or normalized(ref['title']) != normalized(scalar(tags,'album')):
        return None
    options = [t for t in ref['tracks'] if t['recording_id'] == recording]
    if len(options) != 1:
        return None
    slot = options[0]
    for field, key in [('tracknumber','position'), ('discnumber','disc')]:
        if scalar(tags,field) and release_review.position(scalar(tags,field)) != slot[key]:
            return None
    if scalar(tags,'musicbrainz_releasetrackid') and scalar(tags,'musicbrainz_releasetrackid') != slot['release_track_id']:
        return None
    if (not fields.number(slot['duration']) or abs(slot['duration'] - meta['duration']) > 2
            or normalized(slot['title']) != normalized(scalar(tags,'title'))
            or normalized(slot['artist']) != normalized(scalar(tags,'artist'))):
        return None
    return slot['slot']


async def discover(providers, recording, meta, authorize):
    from .providers import musicbrainz_candidate
    base = 'https://' + providers.settings.musicbrainz_host + '/ws/2/'
    authorize()
    item = await providers.get('musicbrainz',base+'recording/'+recording,{'fmt':'json','inc':'artist-credits'})
    if not isinstance(item,dict) or item.get('id') != recording:
        return None
    query = {k:scalar(meta['tags'],k) for k in ('title','artist','album')}
    if musicbrainz_candidate(item,query,meta['duration'],False)['match']['state'] != 'matching_metadata':
        return None
    embedded = scalar(meta['tags'],'musicbrainz_albumid')
    if embedded and not fields.UUID.fullmatch(embedded):
        return None
    authorize()
    page = await providers.get('musicbrainz',base+'release',{'recording':recording,'fmt':'json','limit':100})
    rid = unique_edition(page,query['album'],embedded)
    if not rid:
        return None
    authorize()
    document = await providers.get('musicbrainz',base+'release/'+rid,{'fmt':'json','inc':'recordings+artist-credits'})
    if not document:
        return None
    ref = release_review.reference(document,rid)
    slot = release_slot(ref,recording,meta)
    return {'recording_id':recording,'release_id':rid,'slot':slot,'album':query['album']} if slot else None
