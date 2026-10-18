"""Offline, checkpointed schema transitions. Never restores or rewrites music.

Production schema 1 is not upgraded by API/worker startup. Stop both components
and explicitly run this operator CLI with their exact storage configuration.
"""
import argparse
from contextlib import ExitStack,closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from . import db,media,provider_policy
from .config import Settings


def marker_path(settings):return settings.state_dir/'.schema-migration.json'


def write_marker(settings,value):
    media._no_links(settings.state_dir)
    media._journal(marker_path(settings),value)


def dependency_manifest(settings,conn):
    files=[]
    for name in ('backups','exports','retention'):
        root=settings.state_dir/name
        if not root.exists():continue
        media._no_links(root)
        for path in sorted(root.rglob('*')):
            media._no_links(path)
            if path.is_dir():continue
            if not path.is_file() or path.stat().st_nlink!=1:raise ValueError('Unsafe checkpoint dependency')
            if len(files)>=100000:raise ValueError('Checkpoint dependency limit exceeded')
            files.append({'path':path.relative_to(settings.state_dir).as_posix(),'size':path.stat().st_size,'sha256':media.sha256_file(path)})
    artifacts=['history:'+row['id'] for row in conn.execute('SELECT id FROM history')]
    root=settings.state_dir/'exports'
    if root.exists():
        for directory in root.iterdir():
            try:
                if directory.is_dir() and str(uuid.UUID(directory.name))==directory.name:artifacts.append('export:'+directory.name)
            except ValueError:continue
    return {'root':str(settings.state_dir.resolve()),'files':files,'artifact_ids':sorted(set(artifacts))}


def verify_dependencies(document):
    root=Path(document['root']);media._no_links(root)
    for entry in document['files']:
        relative=Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0] not in {'backups','exports','retention'}:
            raise ValueError('Invalid checkpoint dependency path')
        path=root/relative;media._no_links(path)
        if not path.is_file() or path.stat().st_size!=entry['size'] or media.sha256_file(path)!=entry['sha256']:
            raise ValueError('Checkpoint dependency changed or disappeared')


def verify_checkpoint(directory):
    directory=Path(directory).absolute();media._no_links(directory/'manifest.json')
    document=json.loads((directory/'manifest.json').read_text())
    if document.get('contract')!='phonoteka-schema-checkpoint-v1' or document.get('source_schema')!=1 or document.get('target_schema')!=2:
        raise ValueError('Unsupported migration checkpoint')
    path=directory/'checkpoint.sqlite3';media._no_links(path)
    if path.stat().st_size!=document['database']['size'] or media.sha256_file(path)!=document['database']['sha256']:
        raise ValueError('Checkpoint database hash mismatch')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        if conn.execute('PRAGMA user_version').fetchone()[0]!=1 or conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('Checkpoint schema/integrity check failed')
    verify_dependencies(document['dependencies'])
    return document


def checkpoint(settings,conn,identity):
    provider_policy.require_backup_connection(conn)
    dependencies=dependency_manifest(settings,conn)
    root=settings.state_dir/'migrations';media._make_directory(root)
    directory=root/identity
    if directory.exists():raise ValueError('Checkpoint identity already exists')
    directory.mkdir(mode=0o700)
    reference={'checkpoint_id':identity,'artifact_ids':dependencies['artifact_ids'],'dependency_root':dependencies['root'],
               'checkpoint_path':str(directory),'released':False}
    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('maintenance_artifact_refs:'+identity,db.encode(reference)));conn.commit()
    baseline=conn.execute('PRAGMA data_version').fetchone()[0]
    target=directory/'checkpoint.sqlite3'
    with closing(sqlite3.connect(target)) as copy:
        conn.backup(copy);db.configure_connection(copy)
        copy.execute('DELETE FROM provider_cache');copy.execute('DELETE FROM sessions');copy.execute('DELETE FROM api_tokens')
        provider_policy.sanitize_backup(copy);copy.commit()
        copy.execute('PRAGMA journal_mode=DELETE');copy.execute('VACUUM')
    os.chmod(target,0o600);media._sync_file(target)
    document={'contract':'phonoteka-schema-checkpoint-v1','checkpoint_id':identity,'created_at':db.utcnow(),
              'source_schema':1,'target_schema':2,'database':{'size':target.stat().st_size,'sha256':media.sha256_file(target)},
              'dependencies':dependencies,'music_included':False,'blobs_copied':False,'credentials_excluded':True}
    media._journal(directory/'manifest.json',document);media._sync_directory(directory);media._sync_directory(root)
    verify_checkpoint(directory)
    return directory,document,baseline


def migrate(settings,*,confirm_stopped=False,retry=False,fault=lambda phase:None):
    if not confirm_stopped:raise ValueError('Stop app and worker first, then use --confirm-stopped')
    # Import lazily: worker imports db during startup.
    from .worker import scheduler_guard,verify_storage
    settings.validate()
    with ExitStack() as stack:
        stack.enter_context(scheduler_guard(settings.state_dir))
        stack.enter_context(media.writer_guard(settings.storage_guard_dir,settings.instance_id))
        verify_storage(settings)
        media._no_links(settings.db_path)
        previous=json.loads(marker_path(settings).read_text()) if marker_path(settings).exists() else None
        if previous and previous.get('state')!='committed' and not retry:
            raise ValueError('Previous migration requires status inspection and explicit --retry')
        with db.connect(settings.db_path,maintenance=True) as conn:
            version=conn.execute('PRAGMA user_version').fetchone()[0]
            if version==2:
                db.verify_schema(conn)
                if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Database integrity failed')
                if previous:
                    verify_checkpoint(previous['checkpoint_path'])
                    write_marker(settings,{**previous,'state':'committed','verified_at':db.utcnow()})
                return {'schema':2,'changed':False,'verified':True}
            if version!=1:raise ValueError('Only the reviewed schema 1 to 2 transition is supported')
            if conn.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone():
                raise ValueError('Running jobs require worker journal reconciliation before migration')
            identity=str(uuid.uuid4())
            journal={'schema':1,'id':identity,'source':1,'target':2,'state':'preparing','created_at':db.utcnow()}
            write_marker(settings,journal)
            try:
                directory,document,baseline=checkpoint(settings,conn,identity)
                journal.update(state='checkpoint_verified',checkpoint_path=str(directory),database_sha256=document['database']['sha256'])
                write_marker(settings,journal);fault('after_checkpoint')
                conn.execute('BEGIN IMMEDIATE')
                if conn.execute('PRAGMA data_version').fetchone()[0]!=baseline:
                    raise ValueError('State changed after checkpoint; app must remain stopped')
                db.create_base_schema(conn)
                fault('after_ddl')
                db.install_schema_guard(conn,1,identity)
                reference=json.loads(conn.execute('SELECT value FROM settings WHERE key=?',('maintenance_artifact_refs:'+identity,)).fetchone()[0])
                reference['manifest_sha256']=media.sha256_file(directory/'manifest.json')
                conn.execute('UPDATE settings SET value=? WHERE key=?',(db.encode(reference),'maintenance_artifact_refs:'+identity))
                fault('before_version')
                conn.execute('PRAGMA user_version=2')
                db.verify_schema(conn)
                if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Migrated database integrity failed')
                fault('before_commit');conn.commit();fault('after_commit')
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                media._sync_file(settings.db_path);media._sync_directory(settings.state_dir)
                verify_dependencies(document['dependencies'])
                write_marker(settings,{**journal,'state':'committed','verified_at':db.utcnow()})
                return {'schema':2,'changed':True,'verified':True,'checkpoint_id':identity,'dependencies':len(document['dependencies']['files']),'music_changed':False}
            except BaseException as exc:
                conn.rollback()
                write_marker(settings,{**journal,'state':'failed','error_type':type(exc).__name__})
                raise


def restore_checkpoint(settings,directory,destination,*,confirm_stopped=False):
    if not confirm_stopped:raise ValueError('Stop app and worker first, then use --confirm-stopped')
    from .worker import scheduler_guard
    document=verify_checkpoint(directory)
    destination=Path(destination).absolute()
    from .maintenance import safe_destination
    destination=safe_destination(destination,[settings.state_dir,*settings.roots.values(),Path(directory)])
    media._no_links(destination.parent)
    with scheduler_guard(settings.state_dir),media.writer_guard(settings.storage_guard_dir,settings.instance_id):
        verify_checkpoint(directory)
        destination.mkdir(mode=0o700)
        try:
            target=destination/'phonoteka.sqlite3'
            shutil.copyfile(Path(directory)/'checkpoint.sqlite3',target);os.chmod(target,0o600)
            if media.sha256_file(target)!=document['database']['sha256']:raise ValueError('Checkpoint changed while restoring')
            with db.connect(target,maintenance=True) as conn:
                conn.execute('DELETE FROM sessions');conn.execute('DELETE FROM api_tokens')
                conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='auth_epoch'")
                for key,value in [('recovery_required','true'),('webhook_paused','true'),('schedule',db.encode({'enabled':False,'remaining':[]})),
                                  ('webhook_event_epoch',str(uuid.uuid4())),
                                  ('restored_migration_checkpoint',db.encode({'checkpoint_id':document['checkpoint_id'],'dependency_root':document['dependencies']['root']}))]:
                    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,value))
                cancelled=[row['id'] for row in conn.execute("SELECT id FROM jobs WHERE state IN ('queued','running','paused','interrupted')")]
                conn.execute("UPDATE jobs SET cancel_requested=1,state='cancelled',stage='cancelled',error='state_restored' WHERE state IN ('queued','running','paused','interrupted')")
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='webhook_reservations'").fetchone():
                    for identity in cancelled:db.event(conn,'job_finished',{'job_id':identity,'state':'cancelled','reason':'state_restored'})
                conn.commit()
            media._sync_file(target);media._sync_directory(destination);media._sync_directory(destination.parent)
        except BaseException:
            # Keep the new directory for explicit inspection. Existing state and
            # music have never been overwritten; no automatic destructive rollback.
            raise
    return {'schema':1,'recovery_required':True,'credentials_revoked':True,'music_changed':False,'blobs_copied':False,'requires_compatible_image_or_migration':True}


def status(settings):
    marker=marker_path(settings)
    document=json.loads(marker.read_text()) if marker.exists() else None
    with db.connect(settings.db_path,maintenance=True) as conn:
        version=conn.execute('PRAGMA user_version').fetchone()[0]
    return {'schema':version,'supported_schema':db.SCHEMA_VERSION,'migration':document,'automatic_production_upgrade':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('status')
    task=commands.add_parser('migrate');task.add_argument('--confirm-stopped',action='store_true');task.add_argument('--retry',action='store_true')
    task=commands.add_parser('verify-checkpoint');task.add_argument('directory')
    task=commands.add_parser('restore-checkpoint');task.add_argument('directory');task.add_argument('destination');task.add_argument('--confirm-stopped',action='store_true')
    args=parser.parse_args();settings=Settings.from_env()
    if args.command=='status':result=status(settings)
    elif args.command=='migrate':result=migrate(settings,confirm_stopped=args.confirm_stopped,retry=args.retry)
    elif args.command=='verify-checkpoint':result={'verified':True,'checkpoint_id':verify_checkpoint(args.directory)['checkpoint_id']}
    else:result=restore_checkpoint(settings,args.directory,args.destination,confirm_stopped=args.confirm_stopped)
    print(json.dumps(result))


if __name__=='__main__':main()
