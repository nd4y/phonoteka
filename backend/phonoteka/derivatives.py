"""Writable local derivatives for readable formats without a native tag writer."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from . import cue,media


def export(source,metadata,settings,authorize=lambda:None):
    media._mutation_supported();source=media._validate(source,settings.roots)
    media._expect(source,metadata['sha256'])
    if metadata.get('input_error') or metadata.get('cue_album_image') or metadata.get('format')=='cue':
        raise media.MediaError('unsupported_input','Нужен отдельный декодируемый аудиофайл')
    relative=source.relative_to(settings.work_root).as_posix()
    identity=hashlib.sha256(json.dumps(['writable-pcm-v1',relative,metadata['sha256']]).encode()).hexdigest()
    destination=settings.work_root/'.phonoteka-derived'/identity
    media._validate(destination/'manifest.json',settings.roots,write=True,missing=True)
    if destination.exists():
        manifest=destination/'manifest.json';media._no_links(manifest)
        if manifest.stat().st_size>1024**2:raise media.MediaError('conflict','Манифест слишком большой')
        result=json.loads(manifest.read_text(encoding='utf-8'))
        if result.get('identity')!=identity:raise media.MediaError('conflict','Каталог занят')
        for entry in result['files']:
            path=cue.safe_path(settings.work_root,entry['relative_path'])
            if path.parent!=destination:raise media.MediaError('conflict','Некорректный путь производного файла')
            media._expect(path,entry['sha256'])
        return {**result,'reused':True}
    probe=json.loads(media._process(source,'ffprobe',['-select_streams','a:0','-show_entries','stream=sample_fmt,sample_rate,channels,bits_per_raw_sample,bits_per_sample','-of','json']).stdout)['streams'][0]
    rate=int(probe['sample_rate']);channels=int(probe['channels']);fmt=probe['sample_fmt'].rstrip('p')
    duration=float(metadata.get('duration',0))
    if not 8000<=rate<=384000 or not 1<=channels<=8 or not math.isfinite(duration) or not 0<duration<=21600:
        raise media.MediaError('unsupported_input','Профиль: 8-384 кГц, 1-8 каналов, до 6 часов')
    bits=int(probe.get('bits_per_raw_sample') or probe.get('bits_per_sample') or (16 if fmt=='s16' else 32))
    lossless=fmt in {'s16','s32'} and bits<=24
    pcm='pcm_f32le' if fmt=='flt' else 'pcm_f64le' if fmt=='dbl' else 'pcm_s16le' if fmt=='s16' else 'pcm_s32le'
    codec='flac' if lossless else pcm;ext='flac' if lossless else 'wav'
    estimate=math.ceil(duration)*rate*channels*8
    if estimate>32*1024**3:raise media.MediaError('derivative_too_large','Производный файл превышает 32 ГиБ')
    staging=settings.work_root/'.phonoteka-staging';media._make_directory(staging);media._make_directory(destination.parent)
    with media._reserve_filesystem_space([(settings.work_root,estimate+64*1024**2)]),tempfile.TemporaryDirectory(prefix='normalize-',dir=staging) as tmp:
        stage=Path(tmp)/'audio';media._make_directory(stage);target=stage/('track.'+ext)
        authorize();args=['-nostdin','-threads','1','-map','0:a:0','-map_metadata','-1','-vn','-sn','-dn','-c:a',codec]
        if lossless:args+=['-bits_per_raw_sample',str(bits)]
        media._process(source,'ffmpeg',args+['-n',str(target)],timeout=1800)
        def pcm_hash(path):return media._process(path,'ffmpeg',['-nostdin','-map','0:a:0','-c:a',pcm,'-f','hash','-hash','sha256','-'],timeout=1800).stdout.strip()
        expected=pcm_hash(source)
        if pcm_hash(target)!=expected:raise media.MediaError('audio_changed','PCM производной копии отличается')
        tags={k:v for k,v in metadata.get('tags',{}).items() if k in media._TEXT_FRAMES or k in media._CUSTOM_FIELDS}
        artwork=media.read_art(source,settings.roots) if metadata.get('has_art') else None
        art=artwork[0] if artwork else None
        # Preserve only complete timed text; plain text remains in the source and catalog.
        media._edit(target,tags,metadata.get('lrc') or None,art)
        if pcm_hash(target)!=expected:raise media.MediaError('audio_changed','Запись тегов изменила PCM')
        entry={'relative_path':(destination/target.name).relative_to(settings.work_root).as_posix(),'sha256':media.sha256_file(target),
               'source':relative,'source_sha256':metadata['sha256'],'pcm_sha256':expected.decode().split('=')[-1],
               'profile':{'extension':ext,'codec':codec,'pcm_codec':pcm,'rate':rate,'channels':channels},'kind':'writable_derivative'}
        result={'identity':identity,'policy':'writable-pcm-v1','files':[entry],'state':'committed','source_metadata':{'plain_lyrics_preserved_in_source':bool(metadata.get('lyrics'))}}
        media._expect(source,metadata['sha256']);authorize()
        with media._file(target) as handle:os.fsync(handle.fileno())
        media._journal(stage/'manifest.json',result);media._sync_directory(stage)
        media._replace(stage,destination,no_replace=True);media._sync_directory(destination.parent)
    return result
