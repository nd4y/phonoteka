"""Generate a private-data-free, embedded-only Android acceptance pack on Linux.

No personal music is read. A real phone test is still required after generation.
"""
import argparse
from datetime import datetime,timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile

from PIL import Image,ImageDraw
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import TXXX
from phonoteka import media

LRC='[00:00.000]Первый сигнал\n[00:05.000]Второй сигнал\n[00:10.000]Третий сигнал'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--revision',required=True)
    args=parser.parse_args()
    if not __import__('re').fullmatch('[a-f0-9]{40}',args.revision):parser.error('A source revision is required')
    destination=args.output.resolve()
    if destination.exists():raise SystemExit('Use a new output directory; existing artifacts are never overwritten')
    destination.mkdir(parents=True,mode=0o700)
    manifest={'contract':'phonoteka-poweramp-generated-fixtures-v1','source_revision':args.revision,
              'created_at':datetime.now(timezone.utc).isoformat(),'private_audio':False,
              'device_test_performed':False,'poweramp_verified':False,'files':[]}
    with tempfile.TemporaryDirectory(prefix='phonoteka-device-fixtures-') as temporary:
        base=Path(temporary)
        roots={name:base/name for name in ('source','work','publication')}
        for root in roots.values():root.mkdir()
        for extension in ('mp3','flac'):
            for index in range(1,11):
                path=roots['work']/('fixture.'+extension)
                expression=f'if(lt(mod(t,5),0.25),0.15*sin(2*PI*{440+index*20}*t),0)'
                command=['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',f"aevalsrc='{expression}':s=44100:d=15",
                         '-ac','2','-c:a','libmp3lame' if extension=='mp3' else 'flac']
                if extension=='mp3':command+=['-q:a','2']
                command+=['-y',str(path)]
                subprocess.run(command,check=True,timeout=60)
                if extension=='mp3':
                    original=MP3(path)
                    if original.tags is None:original.add_tags()
                    original.tags.add(TXXX(encoding=3,desc='FIXTURE_MARKER',text=['generated-only']))
                    original.save(v2_version=3 if index<=5 else 4)
                else:
                    original=FLAC(path);original['FIXTURE_MARKER']=['generated-only'];original.save()
                identity=media.audio_identity(path)
                picture=Image.new('RGB',(600,600),(25+index*18,40 if extension=='mp3' else 130,160-index*10))
                ImageDraw.Draw(picture).text((35,35),f'PHONOTEKA\n{extension.upper()} {index:02}',fill='white')
                buffer=io.BytesIO();picture.save(buffer,format='PNG');cover=buffer.getvalue()
                title=f'Проверка {extension.upper()} {index:02}'
                tags={'title':title,'artist':'Фонотека - проверка устройства','album':f'Проверка {extension.upper()}',
                      'albumartist':'Фонотека','tracknumber':str(index),'date':'2026','genre':'Test',
                      'replaygain_track_gain':'-3.00 dB','replaygain_album_gain':'-8.00 dB',
                      'replaygain_track_peak':'0.150000','replaygain_album_peak':'0.150000'}
                result=media.apply_changes(path,tags,LRC,media.sha256_file(path),base/'backups',roots,cover=cover)
                current=media.inspect_audio(path,roots)
                assert current['lrc']==LRC and current['has_art'] and media.audio_identity(path)==identity
                assert current['tags']['title']==[title] and not current.get('poweramp_verified')
                audit=media.audit_audio(path,roots)
                assert audit['state']=='passed' and audit['sha256']==result['sha256']
                relative=f'{extension.upper()}/{index:02} - Сигналы.{extension}'
                target=destination/relative;target.parent.mkdir(exist_ok=True)
                target.write_bytes(path.read_bytes())
                manifest['files'].append({'path':relative,'sha256':media.sha256_file(target),
                    'bytes':target.stat().st_size,'audio_identity':identity,'format':extension,
                    'expected_tags':tags,'expected_lrc':LRC,'cover_sha256':__import__('hashlib').sha256(cover).hexdigest(),
                    'id3_version':current.get('id3_version'),'verified_on_device':False})
                path.unlink()
    (destination/'Фонотека.m3u8').write_text('#EXTM3U\n'+'\n'.join(item['path'] for item in manifest['files'])+'\n',encoding='utf-8')
    (destination/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (destination/'result.csv').write_text('path,tags,cover,embedded_lines,seek_5s,seek_10s,offline,replaygain,notes\n'+
        '\n'.join('"'+item['path']+'",,,,,,,,' for item in manifest['files'])+'\n',encoding='utf-8-sig')
    instructions='''Проверка Poweramp: 10 MP3 и 10 FLAC

Это сгенерированные сигналы, личной музыки нет. Файлы содержат встроенные
обложки и строки в 0/5/10 секунд. Рядом нет LRC или cover-файла: проверяется
именно чтение встроенного содержимого. На каждом начале строки звучит
короткий тихий сигнал. MP3 01..05 сохраняют ID3v2.3, 06..10 - ID3v2.4.

1. Скопируйте всю папку на телефон, сохраните подпапки MP3/FLAC.
2. Добавьте эту папку в библиотеку Poweramp и импортируйте Фонотека.m3u8.
3. Включите авиарежим. Для каждого из 20 файлов проверьте название,
   исполнителя, цветную обложку, встроенные строки и их смену в 0/5/10 с.
4. Перейдите до/после 5 и 10 секунд. Подсветка должна соответствовать позиции.
5. Проверьте отображение Track Gain -3.00 dB / Album Gain -8.00 dB.
   Это специально заданные контрольные теги, не результат измерения loudness.
6. В result.csv отметьте каждую фактически выполненную проверку и ошибки.
   Отдельно запишите модель телефона, Android, версию Poweramp и настройки
   чтения/показа синхронного текста. Генератор ничего не отмечает пройденным.

manifest.json содержит ожидаемые данные и SHA-256 каждого файла. Эта проверка
не заменяет закрытый музыкальный пилот точности ASR/alignment, gapless тест
на настоящем альбоме и проверку доставки обновления прежнего имени файла.
'''
    (destination/'README.txt').write_text(instructions,encoding='utf-8')
    archive=destination.with_suffix('.zip')
    if archive.exists():raise SystemExit('Archive already exists; generated directory retained')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(destination.rglob('*')):
            if path.is_file():output.write(path,path.relative_to(destination).as_posix())
    print(json.dumps({'archive':str(archive),'sha256':media.sha256_file(archive),'files':len(manifest['files']),
                      'device_test_performed':False,'source_revision':args.revision}))


if __name__=='__main__':main()
