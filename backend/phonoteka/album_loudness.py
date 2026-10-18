"""Bounded PCM streaming into libebur128; album loudness is jointly gated."""
import ctypes as C
import ctypes.util
import hashlib
import json
import math
import os
import selectors
import subprocess
import sys
import tempfile
import time
from . import media

CHANNELS={'mono':[3],'stereo':[1,2],'2.1':[1,2,0],'3.0':[1,2,3],
          'quad':[1,2,4,5],'4.0':[1,2,3,4],'5.0':[1,2,3,4,5],
          '5.0(side)':[1,2,3,4,5],'5.1':[1,2,3,0,4,5],
          '5.1(side)':[1,2,3,0,4,5],'7.1':[1,2,3,0,4,5,4,5]}


def library():
    name=ctypes.util.find_library('ebur128')
    if not name:raise media.MediaError('tool_unavailable','libebur128 не установлена')
    lib=C.CDLL(name)
    signatures={
        'init':([C.c_uint,C.c_ulong,C.c_int],C.c_void_p),
        'destroy':([C.POINTER(C.c_void_p)],None),
        'set_channel':([C.c_void_p,C.c_uint,C.c_int],C.c_int),
        'add_frames_float':([C.c_void_p,C.POINTER(C.c_float),C.c_size_t],C.c_int),
        'loudness_global':([C.c_void_p,C.POINTER(C.c_double)],C.c_int),
        'loudness_global_multiple':([C.POINTER(C.c_void_p),C.c_size_t,C.POINTER(C.c_double)],C.c_int),
        'true_peak':([C.c_void_p,C.c_uint,C.POINTER(C.c_double)],C.c_int)}
    for name,(args,result) in signatures.items():
        fn=getattr(lib,'ebur128_'+name);fn.argtypes=args;fn.restype=result
    return lib


def checked(code):
    if code:raise media.MediaError('loudness_unavailable','Ошибка измерения libebur128')


def measure(lib,path,roots,sha,authorize):
    path=media._validate(path,roots);media._expect(path,sha);authorize()
    probe=json.loads(media._process(path,'ffprobe',['-select_streams','a:0','-show_entries','stream=sample_rate,channels,channel_layout','-of','json']).stdout)['streams'][0]
    channels=int(probe['channels']);rate=int(probe['sample_rate'])
    layout=probe.get('channel_layout') or {1:'mono',2:'stereo'}.get(channels)
    mapping=CHANNELS.get(layout)
    if sys.platform!='linux' or not mapping or len(mapping)!=channels or not 8000<=rate<=192000:
        raise media.MediaError('loudness_profile_unsupported','Измерение требует Linux и известную схему каналов, 8-192 кГц')
    # I + TRUE_PEAK + HISTOGRAM: bounded state, common gated album histogram.
    state=C.c_void_p(lib.ebur128_init(channels,rate,117))
    if not state:raise media.MediaError('loudness_unavailable','Не удалось создать измеритель громкости')
    try:
        for index,channel in enumerate(mapping):checked(lib.ebur128_set_channel(state,index,channel))
        with media._file(path) as source, tempfile.TemporaryFile() as errors:
            command=['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe',
                     '-format_whitelist','mp3,flac,mov,aac,ogg,wav,aiff,ape,wv,asf,matroska,dsf,iff,ac3',
                     '-i',f'/proc/self/fd/{source.fileno()}','-xerror','-map','0:a:0','-vn','-sn','-dn',
                     '-c:a','pcm_f32le','-f','f32le','pipe:1']
            with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=errors,pass_fds=(source.fileno(),)) as process:
                try:
                    end=time.monotonic()+1800;pending=b'';frames=0
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout,selectors.EVENT_READ)
                        while True:
                            authorize()
                            if time.monotonic()>end:raise media.MediaError('media_timeout','Превышено время измерения громкости')
                            if os.fstat(errors.fileno()).st_size>1024**2:raise media.MediaError('verification_too_large','Слишком большой журнал декодера')
                            if not selector.select(.25):continue
                            block=os.read(process.stdout.fileno(),65536)
                            if not block:break
                            pending+=block;size=len(pending)//(4*channels)*(4*channels)
                            if size:
                                samples=(C.c_float*(size//4)).from_buffer_copy(pending[:size])
                                checked(lib.ebur128_add_frames_float(state,samples,size//(4*channels)))
                                frames+=size//(4*channels);pending=pending[size:]
                            if frames>rate*21600:raise media.MediaError('media_too_long','Не более 6 часов на файл')
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:raise media.MediaError('media_timeout','Декодер не завершился')
                    if process.returncode or pending or frames<rate*.4:raise media.MediaError('invalid_audio','Аудио не прошло декодирование для измерения')
                finally:
                    if process.poll() is None:process.kill();process.wait()
        media._expect(path,sha);authorize()
        value=C.c_double();checked(lib.ebur128_loudness_global(state,C.byref(value)));lufs=value.value;peak=0.
        for index in range(channels):
            checked(lib.ebur128_true_peak(state,index,C.byref(value)));peak=max(peak,value.value)
        if not math.isfinite(peak) or peak<0 or peak>64:raise media.MediaError('loudness_unavailable','Некорректный пик сигнала')
        return state,lufs,peak
    except BaseException:
        lib.ebur128_destroy(C.byref(state));raise


def analyze(items,roots,authorize=lambda:None):
    if not 1<=len(items)<=200:raise media.MediaError('invalid_album','В группе должно быть 1-200 файлов')
    lib=library();measured=[]
    try:
        for item in items:measured.append(measure(lib,item['path'],roots,item['sha256'],authorize))
        states=(C.c_void_p*len(measured))(*(s.value for s,_,_ in measured));value=C.c_double()
        checked(lib.ebur128_loudness_global_multiple(states,len(measured),C.byref(value)))
        album_lufs=value.value;album_peak=max(p for _,_,p in measured)
        if not math.isfinite(album_lufs):raise media.MediaError('loudness_unavailable','В группе нет измеримого звука')
        album_gain=-18-album_lufs
        if not -60<=album_gain<=60:raise media.MediaError('loudness_unavailable','Громкость вне диапазона')
        identity=hashlib.sha256(json.dumps([(str(i['path']),i['sha256']) for i in items]).encode()).hexdigest()
        result=[]
        for item,(_,lufs,peak) in zip(items,measured):
            gain=-18-lufs if math.isfinite(lufs) else 0
            if not -60<=gain<=60:raise media.MediaError('loudness_unavailable','Громкость трека вне диапазона')
            result.append({'id':'album-replaygain:'+identity+':'+item['sha256'],'provider':'local_replaygain','kind':'loudness',
                           'title':f'ReplayGain альбома: {album_gain:+.2f} dB',
                           'tags':{'replaygain_track_gain':f'{gain:+.2f} dB','replaygain_track_peak':f'{peak:.8f}',
                                   'replaygain_album_gain':f'{album_gain:+.2f} dB','replaygain_album_peak':f'{album_peak:.8f}'},
                           'match':{'state':'needs_review','reasons':['replaygain_preview','confirm_album_group']},
                           'method':'libebur128_joint_gating','reference_lufs':-18,'group_identity':identity,'group_files':len(items),
                           'integrated_lufs':lufs if math.isfinite(lufs) else None,'album_lufs':album_lufs,
                           'input_sha256':item['sha256'],'audio_unchanged':True})
        for item in items:media._expect(item['path'],item['sha256'])
        authorize();return result
    finally:
        for state,_,_ in measured:lib.ebur128_destroy(C.byref(state))
