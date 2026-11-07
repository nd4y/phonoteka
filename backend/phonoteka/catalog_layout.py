"""Portable paths for independent exports; never rename Work or Ready files."""
from collections import defaultdict
import hashlib
from pathlib import PurePosixPath
import re
import unicodedata
import uuid

from . import media, publication_paths


def _identity(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _shorten(value, limit):
    return value.encode('utf-8')[:limit].decode('utf-8', errors='ignore').rstrip(' .')


def _component(value, limit=55):
    value = unicodedata.normalize('NFKC', value)
    value = ''.join('_' if char in '/\\<>:"|?*' or unicodedata.category(char).startswith('C') else char for char in value)
    value = _shorten(' '.join(value.split()).strip(' .'), limit) or 'Unknown'
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value):
        value = '_' + value
    return _shorten(value, limit)


def _suffix(value, identity, limit):
    suffix = ' [' + hashlib.sha256(identity.encode()).hexdigest()[:12] + ']'
    return _shorten(value, limit - len(suffix)) + suffix


def _directories(labels):
    # Canonical spelling for equivalent names is independent of traversal order.
    names = {identity: _component(min(values)) for identity, values in labels.items()}
    buckets = defaultdict(list)
    for identity, name in names.items():
        buckets[publication_paths.key(name)].append(identity)
    for identities in buckets.values():
        if len(identities) > 1:
            for identity in identities:
                names[identity] = _suffix(names[identity], identity, 55)
    if len({publication_paths.key(name) for name in names.values()}) != len(names):
        raise media.MediaError('catalog_path_conflict', 'Catalog directory names collide')
    return names


def _number(value):
    match = re.fullmatch(r'\s*(\d{1,4})(?:/\d{1,4})?\s*', value)
    return int(match[1]) if match and int(match[1]) > 0 else None


def audio_paths(entries):
    """Map UUIDs to deterministic Artist/Album/Track paths, bounded to 220 bytes."""
    artists = defaultdict(set); albums = defaultdict(lambda: defaultdict(set))
    records = []; ids = set()
    for entry in entries:
        identity = entry.get('track_id')
        try:
            valid = isinstance(identity, str) and str(uuid.UUID(identity)) == identity
        except (ValueError, AttributeError):
            valid = False
        if not valid or identity in ids:
            raise media.MediaError('invalid_catalog_identity', 'Export requires unique canonical track UUIDs')
        ids.add(identity)
        source = entry.get('path')
        if (not isinstance(source, str) or not source or '\\' in source or source.startswith('/') or
                re.match(r'^[A-Za-z]:', source) or any(part in {'', '.', '..'} for part in source.split('/'))):
            raise media.MediaError('invalid_catalog_source', 'Export source must be a safe relative path')
        path = PurePosixPath(source)
        extension = path.suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,10}', extension):
            raise media.MediaError('invalid_catalog_source', 'Export source requires an audio extension')
        values = {name: entry.get(name, '') for name in ('title','artist','albumartist','album','discnumber','tracknumber')}
        if any(not isinstance(value, str) for value in values.values()):
            raise media.MediaError('invalid_catalog_tags', 'Catalog tags must be strings')
        artist = values['albumartist'].strip() or values['artist'].strip()
        album = values['album'].strip()
        artist_id = 'tag:' + _identity(artist) if artist else 'missing:'
        album_id = 'tag:' + _identity(album) if album else 'missing:'
        artists[artist_id].add(artist or 'Unknown Artist')
        albums[artist_id][album_id].add(album or 'Unknown Album')
        track, disc = _number(values['tracknumber']), _number(values['discnumber'])
        prefix = ((str(disc) + '-') if disc else '') + f'{track:02d} - ' if track else ''
        title = values['title'].strip() or path.stem
        records.append((identity,artist_id,album_id,_component(prefix + title, 80),extension))
    artist_names = _directories(artists)
    album_names = {identity: _directories(labels) for identity, labels in albums.items()}
    bases = {}; collisions = defaultdict(list)
    for identity, artist, album, stem, extension in records:
        parent = artist_names[artist] + '/' + album_names[artist][album]
        bases[identity] = (parent,stem,extension)
        # Reserve the stem across extensions, including future sibling LRC files.
        collisions[publication_paths.key(parent + '/' + stem)].append(identity)
    for identities in collisions.values():
        if len(identities) > 1:
            for identity in identities:
                parent, stem, extension = bases[identity]
                bases[identity] = (parent,_suffix(stem,identity,80),extension)
    result = {}; paths = publication_paths.Index()
    for identity in sorted(bases):
        parent, stem, extension = bases[identity]
        relative = parent + '/' + stem + extension
        if len(relative.encode('utf-8')) > 220:
            raise media.MediaError('catalog_path_too_long', 'Catalog path exceeds the portable limit')
        publication_paths.add(paths, relative)
        result[identity] = relative
    return result
