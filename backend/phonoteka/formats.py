"""Metadata profiles for MP4 and Ogg audio; untouched fields are verified by caller."""
import base64
import hashlib
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.flac import Picture

PROFILES = (MP4, OggVorbis, OggOpus)
MP4_FIELDS = {'title':'©nam','artist':'©ART','album':'©alb','albumartist':'aART','date':'©day','genre':'©gen','comment':'©cmt','composer':'©wrt','tracknumber':'trkn','discnumber':'disk'}


def key(audio, field):
    if isinstance(audio, MP4):
        return MP4_FIELDS.get(field, '----:com.apple.iTunes:'+field)
    return field


def front(audio):
    if isinstance(audio, MP4):
        return bytes(audio['covr'][0]) if audio.get('covr') else None
    for raw in audio.get('metadata_block_picture', []):
        try:
            picture = Picture(base64.b64decode(raw, validate=True))
            if picture.type == 3:
                return picture.data
        except (ValueError, TypeError):
            continue
    return None


def inspect(audio, fields):
    tags = {}
    for field in fields:
        name=key(audio,field)
        variants=[k for k in audio if k.casefold()==name.casefold()]
        values = audio.get(variants[0], []) if variants else []
        if not values:
            continue
        if isinstance(audio, MP4) and field in {'tracknumber','discnumber'}:
            tags[field] = [str(values[0][0]) + ('/'+str(values[0][1]) if values[0][1] else '')]
        else:
            tags[field] = [v.decode('utf-8', errors='replace') if isinstance(v, bytes) else str(v) for v in values]
    raw = audio.get('©lyr' if isinstance(audio, MP4) else 'lyrics', [''])[0]
    return tags, raw, bool(front(audio))


def edit(audio, tags, lyrics, cover, cover_info, values):
    if audio.tags is None:
        audio.add_tags()
    for field, val in tags.items():
        name = key(audio, field)
        data = values(val)
        if isinstance(audio, MP4):
            if name.startswith('----:'):
                for existing in list(audio):
                    if existing.casefold()==name.casefold():del audio[existing]
            if field in {'tracknumber','discnumber'} and data:
                parts = data[0].split('/')
                data = [(int(parts[0]), int(parts[1]) if len(parts)>1 else 0)]
            elif name.startswith('----:'):
                data = [v.encode('utf-8') for v in data]
        if data: audio[name] = data
        elif name in audio: del audio[name]
    if lyrics is not None:
        name = '©lyr' if isinstance(audio, MP4) else 'lyrics'
        if lyrics: audio[name] = [lyrics]
        elif name in audio: del audio[name]
    if cover is not None:
        if isinstance(audio, MP4):
            # Preserve additional artwork; MP4 has no front/back picture types.
            others = list(audio.get('covr', []))[1:]
            audio['covr'] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG if cover_info[0]=='image/jpeg' else MP4Cover.FORMAT_PNG), *others]
        else:
            pictures = [Picture(base64.b64decode(v, validate=True)) for v in audio.get('metadata_block_picture', [])]
            if sum(p.type==3 for p in pictures)>1:
                raise ValueError('Multiple front covers require manual selection')
            p=Picture();p.type=3;p.mime,p.width,p.height=cover_info;p.depth=24;p.data=cover
            audio['metadata_block_picture']=[base64.b64encode(p.write()).decode(), *[base64.b64encode(x.write()).decode() for x in pictures if x.type!=3]]
    audio.save()


def untouched(audio, tags, lyrics, cover, cover_index=0):
    excluded = {key(audio, f) for f in tags}
    if lyrics is not None: excluded.add('©lyr' if isinstance(audio, MP4) else 'lyrics')
    if cover is not None: excluded.add('covr' if isinstance(audio, MP4) else 'metadata_block_picture')
    result = {'fields': {k:repr(v) for k,v in (audio.tags or {}).items() if k.casefold() not in {x.casefold() for x in excluded}}}
    if cover is not None:
        if isinstance(audio, MP4): other=[bytes(x) for index,x in enumerate(audio.get('covr', [])) if index != cover_index]
        else: other=[p.write() for p in [Picture(base64.b64decode(v,validate=True)) for v in audio.get('metadata_block_picture', [])] if p.type!=3]
        result['other_art']=[hashlib.sha256(x).hexdigest() for x in other]
    return result
