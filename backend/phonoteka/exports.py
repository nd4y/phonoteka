"""Verified Ready snapshots, portable playlists and revision-specific delivery receipts."""
import json
import os
from pathlib import Path
import tempfile
import uuid

from fastapi import Request
from fastapi.responses import FileResponse
from pydantic import BaseModel,ConfigDict,Field
from . import db,media,security
from .providers import scalar


def manifest(settings,identity):
    try:identity=str(uuid.UUID(identity))
    except ValueError:raise security.error(404,'export_missing','Экспорт не найден')
    path=settings.state_dir/'exports'/identity/'manifest.json'
    try:media._no_links(path)
    except media.MediaError:raise security.error(404,'export_missing','Экспорт не найден')
    if not path.is_file() or path.stat().st_size>10*1024**2:raise security.error(404,'export_missing','Экспорт не найден')
    return json.loads(path.read_text(encoding='utf-8'))


def generate(settings,authorize=lambda:None):
    entries=[];seen=set()
    for row in db.fetchall(settings.db_path,"SELECT id,relative_path,sha256,metadata FROM tracks WHERE is_current=1 AND json_extract(metadata,'$.publication.sha256') IS NOT NULL ORDER BY relative_path"):
        authorize();meta=json.loads(row['metadata']);pub=meta['publication']
        if pub['sha256']!=row['sha256']:continue
        relative=pub['relative_path']
        if '\n' in relative or '\r' in relative or '\\' in relative or relative in seen:
            raise media.MediaError('playlist_path_conflict','Путь нельзя однозначно включить в плейлист')
        path=media._validate(settings.publication_root/'Ready'/relative,settings.roots)
        media._expect(path,pub['sha256']);seen.add(relative)
        entries.append({'track_id':row['id'],'path':relative,'sha256':pub['sha256'],'size':path.stat().st_size,'duration':meta.get('duration',0),
                        'title':scalar(meta.get('tags',{}),'title'),'artist':scalar(meta.get('tags',{}),'artist'),
                        'youtube':meta.get('youtube',{})})
    if not entries:raise media.MediaError('empty_publication','Нет опубликованных актуальных версий для экспорта')
    identity=str(uuid.uuid4());base=settings.state_dir/'exports';media._make_directory(base)
    document={'id':identity,'created_at':db.utcnow(),'files':entries,'total_bytes':sum(x['size'] for x in entries),'verified_on':'server_ready','delivery_verified':False}
    playlist=['#EXTM3U']
    for entry in entries:
        label=(entry['artist']+' - '+entry['title']).replace('\n',' ').replace('\r',' ')
        playlist += [f"#EXTINF:{int(entry['duration'] or 0)},{label}",'./'+entry['path']]
    with tempfile.TemporaryDirectory(prefix='.stage-',dir=base) as tmp:
        stage=Path(tmp)/'snapshot';media._make_directory(stage)
        media._journal(stage/'manifest.json',document)
        with (stage/'playlist.m3u8').open('x',encoding='utf-8',newline='\n') as output:
            output.write('\n'.join(playlist)+'\n');output.flush();os.fsync(output.fileno())
        # Publication and export share the ordinary scheduler lane. Recheck the
        # catalog so a changed revision cannot silently acquire this snapshot.
        for entry in entries:
            current=db.fetchone(settings.db_path,'SELECT sha256 FROM tracks WHERE id=? AND is_current=1',(entry['track_id'],))
            if not current or current['sha256']!=entry['sha256']:raise media.MediaError('conflict','Коллекция изменилась во время экспорта')
        authorize();media._sync_directory(stage);media._replace(stage,base/identity,no_replace=True);media._sync_directory(base)
    with db.transaction(settings.db_path) as conn:
        conn.execute("INSERT INTO settings VALUES('latest_export',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(identity,))
        db.event(conn,'ready_exported',{'id':identity,'files':len(entries),'bytes':document['total_bytes']})
    return {'export_id':identity,'files':len(entries),'bytes':document['total_bytes']}


class ReceiptInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    export_id:str=Field(max_length=36)
    device:str=Field(min_length=1,max_length=80)
    checksums:dict[str,str]=Field(default_factory=dict,max_length=10000)
    poweramp_checked:bool=False


def install(app,settings):
    @app.get('/api/exports/latest')
    def latest(request:Request):
        security.require_session(request,settings)
        identity=db.setting(settings.db_path,'latest_export')
        if not identity:return {'export':None,'receipts':[]}
        doc=manifest(settings,identity)
        receipts=db.fetchall(settings.db_path,'SELECT id,payload,created_at FROM delivery_receipts WHERE export_id=? ORDER BY created_at DESC',(identity,))
        current={r['id']:r['sha256'] for r in db.fetchall(settings.db_path,'SELECT id,sha256 FROM tracks WHERE is_current=1')}
        stale=sum(current.get(x['track_id'])!=x['sha256'] for x in doc['files'])
        return {'export':doc,'stale_files':stale,'receipts':[{'id':r['id'],'created_at':r['created_at'],**json.loads(r['payload'])} for r in receipts]}

    @app.get('/api/exports/{identity}/{kind}')
    def download(request:Request,identity:str,kind:str):
        security.require_session(request,settings)
        if kind not in {'manifest.json','playlist.m3u8'}:raise security.error(404,'export_missing','Файл экспорта не найден')
        doc=manifest(settings,identity);path=settings.state_dir/'exports'/doc['id']/kind;media._no_links(path)
        return FileResponse(path,filename=kind,media_type='application/json' if kind.endswith('json') else 'audio/x-mpegurl')

    @app.post('/api/deliveries',status_code=201)
    def receipt(request:Request,body:ReceiptInput):
        active=security.require_session(request,settings,True)
        if active.get('api_token_id') and ('jobs:write' not in active['scopes'] or body.poweramp_checked):
            raise security.error(403,'owner_session_required','Poweramp подтверждает владелец в браузере')
        doc=manifest(settings,body.export_id)
        expected={x['path']:x['sha256'] for x in doc['files']}
        if body.checksums and body.checksums!=expected:raise security.error(409,'delivery_mismatch','Не все контрольные суммы клиента совпадают с экспортом')
        if body.poweramp_checked and not body.checksums:raise security.error(422,'checksums_required','Сначала требуется отчет контрольных сумм доставленных файлов')
        data={'device':body.device,'state':'poweramp_owner_verified' if body.poweramp_checked else 'client_hashes_match' if body.checksums else 'copy_reported',
              'verification_origin':'owner_report' if not active.get('api_token_id') else 'api_client_report',
              'verified_files':len(body.checksums),'poweramp_checked':body.poweramp_checked,'audio_revision_bound':True}
        identity=str(uuid.uuid4())
        with db.transaction(settings.db_path) as conn:
            conn.execute('INSERT INTO delivery_receipts VALUES(?,?,?,?)',(identity,doc['id'],db.encode(data),db.utcnow()))
            db.event(conn,'delivery_reported',{'id':identity,'export_id':doc['id'],'state':data['state']})
        return {'id':identity,**data}
