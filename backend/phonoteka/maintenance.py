"""Offline state backup/restore. Never restores or deletes music trees."""
import argparse
from contextlib import closing
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid
from . import __version__,db,media,provider_policy
from .config import Settings
from .worker import scheduler_guard


def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def safe_destination(path,forbidden):
    path=Path(path).absolute();media._no_links(path,missing=True)
    if path.exists():raise ValueError('Destination must not exist')
    for root in forbidden:
        root=Path(root).resolve()
        if path==root or root in path.parents or path in root.parents:raise ValueError('Destination overlaps protected storage')
    return path


def verify(directory):
    root=Path(directory).absolute();media._no_links(root/'manifest.json')
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('contract')!='phonoteka-state-v1' or manifest.get('schema')!=db.SCHEMA_VERSION:raise ValueError('Unsupported backup contract')
    seen=set()
    for item in manifest['files']:
        relative=Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts or str(relative) in seen:raise ValueError('Invalid backup path')
        if relative.parts[0] not in {'phonoteka.sqlite3','backups','exports','retention'}:raise ValueError('Unexpected backup artifact')
        seen.add(str(relative));path=root/relative;media._no_links(path)
        if path.stat().st_size!=item['size'] or digest(path)!=item['sha256']:raise ValueError('Backup artifact hash mismatch')
    if 'phonoteka.sqlite3' not in seen:raise ValueError('Database missing')
    with closing(sqlite3.connect('file:'+str(root/'phonoteka.sqlite3')+'?mode=ro',uri=True)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Database integrity failed')
    return manifest


def backup(settings,destination):
    dest=safe_destination(destination,[settings.state_dir,*settings.roots.values()]);media._no_links(dest.parent)
    with scheduler_guard(settings.state_dir),media.writer_guard(settings.storage_guard_dir,settings.instance_id):
        active=db.fetchone(settings.db_path,"SELECT COUNT(*) AS n FROM jobs WHERE state IN ('running','interrupted')")['n']
        if active:raise ValueError('Reconcile running jobs before backup')
        provider_policy.require_backup(settings)
        with tempfile.TemporaryDirectory(prefix='.phonoteka-backup-',dir=dest.parent) as folder:
            stage=Path(folder)/'snapshot';stage.mkdir(mode=0o700)
            with db.connect(settings.db_path) as src,closing(sqlite3.connect(stage/'phonoteka.sqlite3')) as dst:
                src.backup(dst)
                db.configure_connection(dst)
                # Caches and active credentials are not portable backup artifacts.
                dst.execute('DELETE FROM provider_cache');dst.execute('DELETE FROM sessions');dst.execute('DELETE FROM api_tokens');dst.commit()
                dst.row_factory=sqlite3.Row
                provider_policy.sanitize_backup(dst);dst.commit()
                dst.execute('PRAGMA wal_checkpoint(TRUNCATE)');dst.execute('PRAGMA journal_mode=DELETE')
                # DELETE alone leaves cached/private rows in SQLite free pages.
                # Compact the copy so excluded rows are absent from backup bytes.
                dst.execute('VACUUM')
            for name in ('backups','exports','retention'):
                source=settings.state_dir/name
                if not source.exists():continue
                for path in source.rglob('*'):
                    media._no_links(path)
                    if not path.is_file() and not path.is_dir():raise ValueError('Special backup artifact rejected')
                shutil.copytree(source,stage/name)
            manifest={'contract':'phonoteka-state-v1','schema':db.SCHEMA_VERSION,'version':__version__,'created_at':db.utcnow(),
                      'music_included':False,'secrets_included':False,'authentication_hash_included':True,'files':[]}
            for path in sorted(stage.rglob('*')):
                if path.is_file():
                    os.chmod(path,0o600)
                    manifest['files'].append({'path':path.relative_to(stage).as_posix(),'size':path.stat().st_size,'sha256':digest(path)})
                    with path.open('rb') as stream:os.fsync(stream.fileno())
            media._journal(stage/'manifest.json',manifest);verify(stage);media._sync_directory(stage)
            media._replace(stage,dest,no_replace=True);media._sync_directory(dest.parent)
        db.set_setting(settings.db_path,'last_state_backup',db.encode({'created_at':manifest['created_at'],'files':len(manifest['files']),'verified':True,'music_included':False}))
    return {'path':str(dest),'files':len(manifest['files']),'verified':True,'music_included':False}


def restore(directory,destination):
    source=Path(directory).absolute();manifest=verify(source)
    dest=safe_destination(destination,[source]);media._no_links(dest.parent)
    with tempfile.TemporaryDirectory(prefix='.phonoteka-restore-',dir=dest.parent) as folder:
        stage=Path(folder)/'state';stage.mkdir(mode=0o700)
        for entry in manifest['files']:
            target=stage/entry['path'];target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source/entry['path'],target);os.chmod(target,0o600)
            if digest(target)!=entry['sha256']:raise ValueError('Backup changed during restore')
        with db.transaction(stage/'phonoteka.sqlite3') as conn:
            conn.execute('DELETE FROM sessions');conn.execute('DELETE FROM api_tokens')
            conn.execute("DELETE FROM settings WHERE key LIKE 'export_lease:%'")
            conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='auth_epoch'")
            conn.execute("INSERT OR REPLACE INTO settings VALUES('recovery_required','true')")
            conn.execute("INSERT OR REPLACE INTO settings VALUES('schedule',?)",(db.encode({'enabled':False,'remaining':[]}),))
            conn.execute("INSERT OR REPLACE INTO settings VALUES('webhook_paused','true')")
            conn.execute("INSERT OR REPLACE INTO settings VALUES('webhook_event_epoch',?)",(str(uuid.uuid4()),))
            cancelled=[row['id'] for row in conn.execute("SELECT id FROM jobs WHERE state IN ('running','queued','paused','interrupted')")]
            conn.execute("UPDATE jobs SET state='cancelled',stage='cancelled',cancel_requested=1,error='state_restored' WHERE state IN ('running','queued','paused','interrupted')")
            for identity in cancelled:db.event(conn,'job_finished',{'job_id':identity,'state':'cancelled','reason':'state_restored'})
            db.event(conn,'state_restored',{'version':manifest['version'],'music_restored':False,'recovery_required':True})
        media._sync_directory(stage);media._replace(stage,dest,no_replace=True);media._sync_directory(dest.parent)
    return {'path':str(dest),'recovery_required':True,'music_restored':False,'credentials_revoked':True}


def acknowledge(settings):
    with scheduler_guard(settings.state_dir),media.writer_guard(settings.storage_guard_dir,settings.instance_id):
        if db.fetchone(settings.db_path,"SELECT 1 FROM jobs WHERE state IN ('running','queued') LIMIT 1"):raise ValueError('Wait for all verification jobs')
        for row in db.fetchall(settings.db_path,'SELECT relative_path,sha256,metadata,status FROM tracks WHERE is_current=1'):
            if row['status']=='missing':raise ValueError('Missing files require reconciliation')
            path=media._validate(settings.work_root/row['relative_path'],settings.roots);media._expect(path,row['sha256'])
            pub=json.loads(row['metadata']).get('publication')
            if pub:media._expect(media._validate(settings.publication_root/'Ready'/pub['relative_path'],settings.roots),pub['sha256'])
        with db.transaction(settings.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO settings VALUES('recovery_required','false')")
            db.event(conn,'recovery_acknowledged',{'work_ready_revisions_checked':True})
    return {'recovery_required':False,'new_jobs_require_new_authorization':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('backup');p.add_argument('destination')
    p=sub.add_parser('verify');p.add_argument('backup')
    p=sub.add_parser('restore');p.add_argument('backup');p.add_argument('destination')
    sub.add_parser('acknowledge-recovery')
    p=sub.add_parser('prune-staging');p.add_argument('--older-than-days',type=int,default=7);p.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if args.command=='backup':result=backup(Settings.from_env(),args.destination)
    elif args.command=='verify':result={'verified':True,'files':len(verify(args.backup)['files'])}
    elif args.command=='restore':result=restore(args.backup,args.destination)
    elif args.command=='acknowledge-recovery':result=acknowledge(Settings.from_env())
    else:result=cleanup_staging(Settings.from_env(),args.older_than_days,args.apply)
    print(json.dumps(result))


def cleanup_staging(settings,older_days=7,apply=False):
    if older_days<1:raise ValueError('Grace period must be at least one day')
    root=settings.work_root/'.phonoteka-staging';media._no_links(root,missing=True)
    if not root.exists():return {'candidates':[],'removed':0,'rollback_versions_retained':True}
    candidates=[]
    with scheduler_guard(settings.state_dir),media.writer_guard(settings.storage_guard_dir,settings.instance_id):
        if db.fetchone(settings.db_path,"SELECT 1 FROM jobs WHERE state IN ('running','interrupted') LIMIT 1"):raise ValueError('Reconcile interrupted operations first')
        for child in root.iterdir():
            if not re.fullmatch(r'(cue|normalize)-[a-zA-Z0-9_-]+',child.name):continue
            media._no_links(child)
            if not child.is_dir() or child.stat().st_mtime>time.time()-older_days*86400:continue
            for path in child.rglob('*'):media._no_links(path)
            # Only uncommitted scratch under the explicit staging root. Immutable
            # derivatives, backups, exports and all music files stay intact.
            if child.resolve().parent!=root.resolve():raise ValueError('Staging path escaped root')
            candidates.append(child.name)
            if apply:shutil.rmtree(child)
        if apply:
            with db.transaction(settings.db_path) as conn:db.event(conn,'staging_pruned',{'directories':len(candidates),'grace_days':older_days})
    return {'candidates':candidates,'removed':len(candidates) if apply else 0,'rollback_versions_retained':True}


if __name__=='__main__':main()
