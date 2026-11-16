"""A bounded album lookup hint, never an edition choice or acceptance proof."""
import re

from .providers import normalized

RELEASE_ID = re.compile(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}')


def _tag(tags, key):
    value = tags.get(key, '')
    if isinstance(value, list):
        if not 1 <= len(value) <= 16 or any(not isinstance(v, str) or len(v) > 4096 for v in value):
            raise ValueError('invalid tag')
        value = value[0]
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('invalid tag')
    return value.strip()


def album_hint(metadata, analysis, matched_recordings):
    """Only current, independently matched recordings may inform this lookup.

    Parent verifies revision, authority, conflicts, locks and failed attempts.
    Any previous artwork response needs review instead of an automatic retry.
    """
    if not isinstance(metadata, dict) or not isinstance(analysis, dict):
        return None
    if metadata.get('has_art', False) is not False or any(
            analysis.get(key) for key in ('possible_mix', 'critical_conflicts', 'unresolved_conflicts')):
        return None
    tags = metadata.get('tags')
    candidates, sources = analysis.get('candidates', []), analysis.get('sources', [])
    if (not isinstance(tags, dict) or not isinstance(candidates, list) or len(candidates) > 128
            or not isinstance(sources, list) or len(sources) > 128
            or not isinstance(matched_recordings, list) or not 1 <= len(matched_recordings) <= 128):
        return None
    try:
        if not _tag(tags, 'title') or not _tag(tags, 'artist'):
            return None
        embedded = _tag(tags, 'album')
    except ValueError:
        return None
    if any(not isinstance(s, dict) or s.get('provider') == 'coverartarchive' for s in sources):
        return None
    if any(not isinstance(c, dict) or c.get('cover') or c.get('kind') == 'cover'
           or c.get('provider') == 'coverartarchive' for c in candidates):
        return None
    names, releases = {}, {}
    for recording in matched_recordings:
        if (not isinstance(recording, dict) or recording.get('provider') != 'musicbrainz'
                or recording.get('kind') != 'metadata'
                or not isinstance(recording.get('releases_truncated', False), bool)
                or recording.get('releases_truncated')):
            return None
        options = recording.get('releases')
        if not isinstance(options, list) or not 1 <= len(options) < 15:
            return None
        official = False
        for release in options:
            if not isinstance(release, dict):
                return None
            if release.get('status') != 'Official':
                continue
            rid, title = release.get('id'), release.get('title')
            if (not isinstance(rid, str) or not RELEASE_ID.fullmatch(rid)
                    or not isinstance(title, str) or not 0 < len(title.strip()) <= 1024):
                return None
            title = title.strip()
            key = normalized(title)
            if not key or rid.lower() in releases and releases[rid.lower()] != key:
                return None
            releases[rid.lower()] = key
            names.setdefault(key, title)
            official = True
        if not official:
            return None
    if embedded:
        return embedded if normalized(embedded) in names else None
    return next(iter(names.values())) if len(names) == 1 else None
