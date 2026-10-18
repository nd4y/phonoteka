"""Verified Ready snapshots, portable playlists and revision-specific delivery receipts."""
import json
import os
from pathlib import Path
import tempfile
import uuid

from fastapi import Request
from pydantic import BaseModel,ConfigDict,Field
from . import db,media,security,provider_policy,export_leases,publication_paths,publication_sidecars
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
    from . import comparison
    entries=[];seen=publication_paths.Index();aliases={}
    for row in db.fetchall(settings.db_path,"SELECT id,relative_path,sha256,metadata FROM tracks WHERE is_current=1 AND json_extract(metadata,'$.publication.sha256') IS NOT NULL ORDER BY relative_path"):
        authorize();meta=json.loads(row['metadata']);pub=meta['publication']
        if pub['sha256']!=row['sha256']:continue
        chosen=comparison.publication_choice(settings,row['id'])
        if chosen!=row['id']:
            aliases[row['id']]=chosen
            continue
        provider_policy.require_track(settings,row,'export')
        relative=pub['relative_path']
        publication_paths.add(seen,relative)
        path=media._validate(settings.publication_root/'Ready'/relative,settings.roots)
        media._expect(path,pub['sha256'])
        entries.append({'track_id':row['id'],'kind':'audio','owners':[row['id']],'path':relative,'sha256':pub['sha256'],'size':path.stat().st_size,'duration':meta.get('duration',0),
                        'title':scalar(meta.get('tags',{}),'title'),'artist':scalar(meta.get('tags',{}),'artist'),
                        'youtube':meta.get('youtube',{}),'usage_provenance':meta.get('usage_provenance',{}),
                        'publication_profile':pub.get('profile','legacy_unknown'), 'lyrics_review_version':pub.get('review_version'),
                        'lyrics_gate':pub.get('lyrics_gate'), 'publication_exceptions':pub.get('exceptions',[]),
                        'publication_exception_reason':pub.get('exception','')})
    if not entries:raise media.MediaError('empty_publication','Нет опубликованных актуальных версий для экспорта')
    if any(chosen not in {entry['track_id'] for entry in entries} for chosen in aliases.values()):
        raise media.MediaError('preferred_not_published','Сначала опубликуйте выбранную предпочтительную копию')
    audio_entries=list(entries)
    sidecars=publication_sidecars.export_objects(settings,audio_entries)
    for entry in sidecars:publication_paths.add(seen,entry['path'])
    entries.extend(sidecars)
    identity=str(uuid.uuid4());base=settings.state_dir/'exports';media._make_directory(base)
    document={'id':identity,'created_at':db.utcnow(),'files':entries,'audio_files':len(audio_entries),'sidecar_files':len(sidecars),'multi_file_atomic':False,'total_bytes':sum(x['size'] for x in entries),'verified_on':'server_ready','delivery_verified':False,'snapshot_available':True,'resolved_track_ids':aliases}
    playlist=['#EXTM3U']
    for entry in audio_entries:
        label=(entry['artist']+' - '+entry['title']).replace('\n',' ').replace('\r',' ')
        playlist += [f"#EXTINF:{int(entry['duration'] or 0)},{label}",'./files/'+entry['path']]
    with media._reserve_filesystem_space([(base,document['total_bytes']+2*1024**2)]), tempfile.TemporaryDirectory(prefix='.stage-',dir=base) as tmp:
        stage=Path(tmp)/'snapshot';media._make_directory(stage)
        # Copies belong to this export generation. Never use hard links: later
        # external edits of Ready must not change a delivery already in progress.
        for entry in entries:
            authorize()
            source=media._validate(settings.publication_root/'Ready'/entry['path'],settings.roots)
            media._expect(source,entry['sha256'])
            target=stage/'files'/entry['path']
            media._make_directory(target.parent)
            media._copy(source,target)
            media._expect(target,entry['sha256'])
            media._expect(source,entry['sha256'])
        for directory in sorted((p for p in stage.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):
            media._sync_directory(directory)
        media._journal(stage/'manifest.json',document)
        with (stage/'playlist.m3u8').open('x',encoding='utf-8',newline='\n') as output:
            output.write('\n'.join(playlist)+'\n');output.flush();os.fsync(output.fileno())
        # Publication and export share the ordinary scheduler lane. Recheck the
        # catalog so a changed revision cannot silently acquire this snapshot.
        with db.transaction(settings.db_path) as conn:
            # Policy/duplicate decisions cannot be changed between final
            # revalidation and the filesystem commit of this generation.
            for entry in audio_entries:
                current=conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1',(entry['track_id'],)).fetchone()
                if not current or current['sha256']!=entry['sha256']:raise media.MediaError('conflict','Коллекция изменилась во время экспорта')
                provider_policy.require_track(settings,current,'export')
                comparison.assert_publish_allowed(settings,entry['track_id'])
            if publication_sidecars.export_objects(settings,audio_entries)!=sidecars:
                raise media.MediaError('sidecar_registry_conflict','Состав sidecar изменился во время создания снимка')
            for entry in sidecars:
                media._expect(media._validate(settings.publication_root/'Ready'/entry['path'],settings.roots),entry['sha256'])
            for original,chosen in aliases.items():
                if comparison.publication_choice(settings,original)!=chosen:
                    raise media.MediaError('conflict','Решение по дубликатам изменилось во время экспорта')
            authorize();media._sync_directory(stage);media._replace(stage,base/identity,no_replace=True);media._sync_directory(base)
            conn.execute("INSERT INTO settings VALUES('latest_export',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(identity,))
            db.event(conn,'ready_exported',{'id':identity,'files':len(entries),'bytes':document['total_bytes']})
    return {'export_id':identity,'files':len(entries),'bytes':document['total_bytes']}


class ReceiptInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    export_id:str=Field(max_length=36)
    device:str=Field(min_length=1,max_length=80)
    checksums:dict[str,str]=Field(default_factory=dict,max_length=10000)
    poweramp_checked:bool=False
    lease_id:str|None=Field(default=None,min_length=36,max_length=36)


def install(app,settings):
    @app.get('/api/exports/latest')
    def latest(request:Request):
        security.require_session(request,settings)
        identity=db.setting(settings.db_path,'latest_export')
        if not identity:return {'export':None,'receipts':[]}
        doc=manifest(settings,identity)
        receipts=db.fetchall(settings.db_path,'SELECT id,payload,created_at FROM delivery_receipts WHERE export_id=? ORDER BY created_at DESC',(identity,))
        current={r['id']:r['sha256'] for r in db.fetchall(settings.db_path,'SELECT id,sha256 FROM tracks WHERE is_current=1')}
        stale=sum(current.get(x['track_id'])!=x['sha256'] for x in doc['files'] if x.get('kind','audio')=='audio')
        return {'export':doc,'stale_files':stale,'receipts':[{'id':r['id'],'created_at':r['created_at'],**json.loads(r['payload'])} for r in receipts]}

    @app.get('/api/exports/{identity}/{kind}')
    def download(request:Request,identity:str,kind:str):
        active=security.require_session(request,settings)
        if kind not in {'manifest.json','playlist.m3u8'}:raise security.error(404,'export_missing','Файл экспорта не найден')
        doc=manifest(settings,identity);path=settings.state_dir/'exports'/doc['id']/kind;media._no_links(path)
        try:
            for entry in doc['files']:provider_policy.require_export_entry(settings,entry)
        except media.MediaError as exc:raise security.error(409,exc.code,exc.message) from exc
        return export_leases.LeasedFileResponse(path,filename=kind,media_type='application/json' if kind.endswith('json') else 'audio/x-mpegurl',
            settings=settings,export_id=doc['id'],lease_actor=export_leases.actor(active))

    @app.get('/api/exports/{identity}/files/{relative:path}')
    def download_audio(request:Request,identity:str,relative:str):
        active=security.require_session(request,settings)
        doc=manifest(settings,identity)
        entry=next((x for x in doc['files'] if x['path']==relative),None)
        if not doc.get('snapshot_available') or not entry:
            raise security.error(404,'export_missing','Файл снимка не найден')
        try:provider_policy.require_export_entry(settings,entry)
        except media.MediaError as exc:raise security.error(409,exc.code,exc.message) from exc
        if Path(relative).is_absolute() or '..' in Path(relative).parts or '\\' in relative:
            raise security.error(404,'export_missing','Файл снимка не найден')
        path=settings.state_dir/'exports'/doc['id']/'files'/relative
        media._no_links(path);media._expect(path,entry['sha256'])
        return export_leases.LeasedFileResponse(path,filename=Path(relative).name,media_type='application/octet-stream',headers={'Cache-Control':'no-store'},
            settings=settings,export_id=doc['id'],lease_actor=export_leases.actor(active))

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
            if body.lease_id:
                row=conn.execute('SELECT value FROM settings WHERE key=?',('export_lease:'+body.lease_id,)).fetchone()
                lease=json.loads(row['value']) if row else None
                if not lease or lease['actor']!=export_leases.actor(active) or lease['export_id']!=doc['id']:
                    raise security.error(409,'delivery_lease_mismatch','Аренда не относится к этой доставке')
                conn.execute('DELETE FROM settings WHERE key=?',('export_lease:'+body.lease_id,))
            conn.execute('INSERT INTO delivery_receipts VALUES(?,?,?,?)',(identity,doc['id'],db.encode(data),db.utcnow()))
            db.event(conn,'delivery_reported',{'id':identity,'export_id':doc['id'],'state':data['state']})
        return {'id':identity,**data}
