"""Sample-accurate CUE derivatives, committed as one new immutable directory.

INDEX 00 belongs to the preceding track (INDEX 01 boundaries). Audio before the
first INDEX 01 is retained as a separate lead-in. Synthetic PREGAP is not audio.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from . import cue, media

POLICY='cue-pcm-v1'


def export(sheet, metadata, settings, authorize=lambda: None):
    media._mutation_supported()
    root=settings.work_root
    sheet=cue.safe_path(root,Path(sheet).relative_to(root))
    media._expect(sheet,metadata['sha256'])
    document=metadata.get('cue',{})
    if document.get('errors') or not document.get('tracks') or not all(t.get('available') for t in document['tracks']):
        raise media.MediaError('invalid_cue','CUE содержит недоступные композиции')
    dependencies={}; profiles={}; groups={}
    for track in document['tracks']:
        source=cue.playback_path(root,sheet.relative_to(root),metadata,track['number'])
        relative=source.relative_to(root).as_posix()
        groups.setdefault(relative,[]).append(track)
        if relative in dependencies:continue
        dependencies[relative]=media.sha256_file(source)
        probe=json.loads(media._process(source,'ffprobe',['-select_streams','a:0','-show_entries','stream=codec_name,sample_fmt,sample_rate,channels,bits_per_raw_sample,bits_per_sample','-of','json']).stdout)['streams'][0]
        rate=int(probe['sample_rate']);channels=int(probe['channels'])
        if not 8000<=rate<=384000 or not 1<=channels<=8:
            raise media.MediaError('unsupported_format','CUE: частота или число каналов вне профиля')
        fmt=probe['sample_fmt'].rstrip('p')
        bits=int(probe.get('bits_per_raw_sample') or probe.get('bits_per_sample') or (16 if fmt=='s16' else 32))
        if fmt in {'s16','s32'} and bits<=24:
            profile={'extension':'flac','codec':'flac','pcm':'s16le' if bits<=16 else 's32le','pcm_codec':'pcm_s16le' if bits<=16 else 'pcm_s32le','format':'s16' if bits<=16 else 's32','bits':bits}
        elif fmt in {'flt','dbl'}:
            profile={'extension':'wav','codec':'pcm_f32le' if fmt=='flt' else 'pcm_f64le','pcm':'f32le' if fmt=='flt' else 'f64le','pcm_codec':'pcm_f32le' if fmt=='flt' else 'pcm_f64le','format':fmt,'bits':32 if fmt=='flt' else 64}
        else:
            profile={'extension':'wav','codec':'pcm_s32le','pcm':'s32le','pcm_codec':'pcm_s32le','format':'s32','bits':32}
        profiles[relative]={**profile,'rate':rate,'channels':channels,'source_codec':probe['codec_name']}
    identity=hashlib.sha256(json.dumps([POLICY,str(sheet.relative_to(root)),metadata['sha256'],dependencies],sort_keys=True).encode()).hexdigest()
    destination=root/'.phonoteka-derived'/identity
    media._validate(destination/'manifest.json',settings.roots,write=True,missing=True)
    if destination.exists():
        manifest_path=destination/'manifest.json';media._no_links(manifest_path)
        if manifest_path.stat().st_size>1024**2:raise media.MediaError('conflict','Неверный манифест CUE')
        existing=json.loads(manifest_path.read_text(encoding='utf-8'))
        if existing.get('identity')!=identity:raise media.MediaError('conflict','Каталог извлечения занят')
        for entry in existing['files']:
            target=cue.safe_path(root,entry['relative_path'])
            if target.parent!=destination:raise media.MediaError('conflict','Неверный путь производного файла')
            media._expect(target,entry['sha256'])
        return {**existing,'reused':True}
    estimate=sum(math.ceil(document['files'][t['file_index']]['duration'])*profiles[r]['rate']*profiles[r]['channels']*8 for r,tracks in groups.items() for t in tracks[:1])
    if estimate>32*1024**3:raise media.MediaError('cue_export_too_large','Экспорт CUE превышает 32 ГиБ')
    staging=root/'.phonoteka-staging';media._make_directory(staging);media._make_directory(destination.parent)
    manifest={'identity':identity,'policy':POLICY,'sheet':sheet.relative_to(root).as_posix(),'sheet_sha256':metadata['sha256'],
              'dependencies':dependencies,'gap_policy':'INDEX01; INDEX00 appended to previous; lead-in retained separately; no synthetic silence',
              'files':[],'state':'committed'}
    with media._reserve_filesystem_space([(root,estimate+64*1024**2)]), tempfile.TemporaryDirectory(prefix='cue-',dir=staging) as folder:
        stage=Path(folder)/'album';media._make_directory(stage)
        for relative,tracks in groups.items():
            profile=profiles[relative];rate=profile['rate'];source=root/relative
            parts=[]
            if tracks[0]['start_frames']>0:
                parts.append((None,0,(tracks[0]['start_frames']*rate+37)//75))
            for i,track in enumerate(tracks):
                start=(track['start_frames']*rate+37)//75
                end=(tracks[i+1]['start_frames']*rate+37)//75 if i+1<len(tracks) else None
                parts.append((track,start,end))
            for track,start,end in parts:
                authorize()
                number=track['number'] if track else 0
                name=(f'{number:02}' if track else '00-lead-in-'+hashlib.sha256(relative.encode()).hexdigest()[:8])+'.'+profile['extension']
                target=stage/name
                trim=f'atrim=start_sample={start}'+(f':end_sample={end}' if end is not None else '')+',asetpts=PTS-STARTPTS'
                args=['-nostdin','-threads','1','-map','0:a:0','-map_metadata','-1','-vn','-sn','-dn','-af',trim,
                      '-c:a',profile['codec'],'-sample_fmt',profile['format']]
                if profile['codec']=='flac':args+=['-bits_per_raw_sample',str(profile['bits'])]
                media._process(source,'ffmpeg',args+['-n',str(target)],timeout=1800)
                expected=media._process(source,'ffmpeg',['-nostdin','-map','0:a:0','-af',trim,'-c:a',profile['pcm_codec'],'-f','hash','-hash','sha256','-'],timeout=1800).stdout.strip()
                actual=media._process(target,'ffmpeg',['-nostdin','-map','0:a:0','-c:a',profile['pcm_codec'],'-f','hash','-hash','sha256','-'],timeout=1800).stdout.strip()
                if expected!=actual:raise media.MediaError('audio_changed','Декодированное аудио не совпало с границами CUE')
                tags={k:v for k,v in {'title':track['title'] if track else 'Lead-in','artist':(track.get('performer') if track else '') or document.get('performer'),
                                     'albumartist':document.get('performer'),'album':document.get('title'),'date':document.get('date'),
                                     'genre':document.get('genre'),'tracknumber':str(number)+'/'+str(len(document['tracks']))}.items() if v}
                media._edit(target,tags,None,None)
                # Metadata writing must preserve the just-verified PCM too.
                after=media._process(target,'ffmpeg',['-nostdin','-map','0:a:0','-c:a',profile['pcm_codec'],'-f','hash','-hash','sha256','-'],timeout=1800).stdout.strip()
                if after!=expected:raise media.MediaError('audio_changed','Запись тегов изменила аудио')
                with media._file(target) as handle:os.fsync(handle.fileno())
                manifest['files'].append({'relative_path':(destination/name).relative_to(root).as_posix(),'sha256':media.sha256_file(target),
                                          'source':relative,'source_sha256':dependencies[relative],'track_number':number,'start_sample':start,'end_sample':end,
                                          'sample_rate':rate,'pcm_sha256':actual.decode().split('=')[-1],'profile':profile,'lead_in':track is None})
        media._expect(sheet,metadata['sha256'])
        for relative,digest in dependencies.items():media._expect(root/relative,digest)
        authorize()
        media._journal(stage/'manifest.json',manifest)
        media._sync_directory(stage)
        media._replace(stage,destination,no_replace=True)
        media._sync_directory(destination.parent);media._sync_directory(staging)
    return manifest
