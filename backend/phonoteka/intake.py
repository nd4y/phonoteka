"""Content hints for discovery; successful parsing, not a suffix, establishes format."""
PROFILES={
 'mp3':('mp3',{'mp3'}),'flac':('flac',{'flac'}),'wave':('wav',{'wav','wave'}),
 'aiff':('aiff',{'aif','aiff'}),'mp4':('m4a',{'m4a','m4b','mp4'}),
 'oggvorbis':('ogg',{'ogg','oga'}),'oggopus':('opus',{'opus','ogg','oga'}),
 'aac':('aac',{'aac'}),'asf':('wma',{'wma','asf'}),'monkeysaudio':('ape',{'ape'}),
 'wavpack':('wv',{'wv'}),'dsf':('dsf',{'dsf'}),'dsdiff':('dff',{'dff'}),
}
MIMES={'mp3':'audio/mpeg','flac':'audio/flac','wav':'audio/wav','wave':'audio/wav','aiff':'audio/aiff','aif':'audio/aiff','m4a':'audio/mp4','m4b':'audio/mp4','mp4':'audio/mp4','ogg':'audio/ogg','oga':'audio/ogg','opus':'audio/ogg','webm':'audio/webm','aac':'audio/aac'}


def header_is_audio(header):
    if header.startswith((b'ID3',b'fLaC',b'OggS',b'MAC ',b'wvpk',b'DSD ',bytes.fromhex('3026b2758e66cf11a6d900aa0062ce6c'),bytes.fromhex('1a45dfa3'))):return True
    if header[:4]==b'RIFF' and header[8:12]==b'WAVE':return True
    if header[:4]==b'FORM' and header[8:12] in (b'AIFF',b'AIFC'):return True
    if header[:4]==b'FRM8' and header[12:16]==b'DSD ':return True
    if header[4:8]==b'ftyp':return True
    return len(header)>=4 and header[0]==255 and header[1]&224==224


def describe(path,audio):
    suffix=path.suffix.lower().lstrip('.')
    kind,aliases=PROFILES.get(type(audio).__name__.lower(),(suffix or type(audio).__name__.lower(),{suffix}))
    actual=suffix if suffix in aliases else kind
    return {'format':actual,'original_extension':suffix,'extension_missing':not suffix,'extension_mismatch':bool(suffix and suffix not in aliases)}


def publication_relative(relative,meta):
    if not meta.get('extension_missing') and not meta.get('extension_mismatch'):return relative
    kind=meta.get('format','')
    if kind in {p[0] for p in PROFILES.values()}:return relative+'.'+kind
    return relative
