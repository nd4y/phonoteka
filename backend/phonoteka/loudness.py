"""ReplayGain analysis only. Applying its proposal uses normal backup/rollback."""
import math
import re
from . import media


def analyze(path, roots, expected_sha):
    path=media._validate(path,roots);media._expect(path,expected_sha)
    result=media._process(path,'ffmpeg',['-nostdin','-v','info','-map','0:a:0','-vn','-sn','-dn','-af','replaygain','-f','null','-'],timeout=1800)
    log=result.stderr.decode('utf-8',errors='replace')
    gains=re.findall(r'track_gain\s*=\s*([+-]?[0-9.]+)\s*dB',log)
    peaks=re.findall(r'track_peak\s*=\s*([0-9.]+)',log)
    if not gains or not peaks:raise media.MediaError('loudness_unavailable','Не удалось измерить ReplayGain')
    gain,peak=float(gains[-1]),float(peaks[-1])
    if not math.isfinite(gain) or not math.isfinite(peak) or not -60<=gain<=60 or not 0<peak<=64:
        raise media.MediaError('loudness_unavailable','Тишина или измерение вне диапазона')
    media._expect(path,expected_sha)
    return {'id':'replaygain:'+expected_sha,'provider':'local_replaygain','kind':'loudness','title':f'ReplayGain: {gain:+.2f} dB',
            'tags':{'replaygain_track_gain':f'{gain:+.2f} dB','replaygain_track_peak':f'{peak:.8f}'},
            'match':{'state':'needs_review','reasons':['replaygain_preview']},'method':'ffmpeg_replaygain','input_sha256':expected_sha,'audio_unchanged':True}
