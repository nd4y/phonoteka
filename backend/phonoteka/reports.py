"""Read-only collection reports; similarity never deletes or merges files."""
from collections import defaultdict
import csv
import io
import json
import re
from . import db,provider_policy
from .providers import scalar, normalized


def rows(settings, conn=None):
    if conn is None:
        with db.connect(settings.db_path) as active:
            yield from rows(settings,active)
        return
    for row in conn.execute('SELECT * FROM tracks WHERE is_current=1 ORDER BY relative_path'):
        meta=json.loads(row['metadata'])
        yield row,meta


def summary(settings, conn=None):
    if conn is None:
        with db.connect(settings.db_path) as active:
            active.execute('BEGIN')
            return summary(settings,active)
    exact=defaultdict(list);audio=defaultdict(list);albums=defaultdict(list);candidates=defaultdict(list);by_id={}
    counts={'youtube_recorded':0,'youtube_confirmed':0,'youtube_candidates':0,'youtube_conflicts':0,'files':0,'cue_sheets':0,'input_errors':0,'writable_profiles':0,'with_lyrics':0,'with_cover':0,'analyzed':0,'with_candidates':0,'no_match':0,'asr_drafts':0,'fingerprint_matches':0,'fingerprint_no_match':0,'fingerprint_unavailable':0}
    for row,meta in rows(settings,conn):
        counts['files']+=1
        state=meta.get('youtube',{}).get('state')
        for key,value in [('youtube_recorded','recorded'),('youtube_confirmed','owner_confirmed'),('youtube_candidates','candidates'),('youtube_conflicts','conflict')]:counts[key]+=state==value
        if meta.get('format')=='cue':counts['cue_sheets']+=1;continue
        counts['input_errors']+=bool(meta.get('input_error'))
        counts['writable_profiles']+=bool(meta.get('write_supported')) and not meta.get('cue_album_image',False)
        counts['with_lyrics']+=bool(meta.get('lrc'));counts['with_cover']+=bool(meta.get('has_art'))
        entry={'id':row['id'],'path':row['relative_path'],'format':meta.get('format'),'duration':meta.get('duration'),'status':row['status']}
        by_id[row['id']]=entry
        exact[row['sha256']].append(entry)
        identity=meta.get('audit',{}).get('audio_identity') or meta.get('audio_identity')
        if identity:audio[identity].append(entry)
        tags=meta.get('tags',{});album=scalar(tags,'album');artist=scalar(tags,'albumartist') or scalar(tags,'artist')
        recording=scalar(tags,'musicbrainz_trackid')
        if recording:candidates[('musicbrainz_recording',recording)].append(entry)
        if album:
            albums[(normalized(artist),normalized(album))].append({**entry,'album':album,'artist':artist,'track':scalar(tags,'tracknumber'),'disc':scalar(tags,'discnumber')})
    counts['metadata_pending_retry']=0
    for row in conn.execute('SELECT a.track_id,a.payload FROM analyses a JOIN tracks t ON t.id=a.track_id AND t.is_current=1 AND t.sha256=a.input_sha256'):
        data=json.loads(row['payload']);counts['analyzed']+=1;counts['with_candidates']+=bool(data.get('candidates'));counts['no_match']+=data.get('state')=='no_match';counts['asr_drafts']+=bool(data.get('asr'))
        if row['track_id'] in by_id:
            for candidate in data.get('candidates',[]):
                if candidate.get('provider')=='shazam' and candidate.get('identification')=='audio_fingerprint' and candidate.get('id'):
                    candidates[('fingerprint_candidate',candidate['id'])].append(by_id[row['track_id']])
        for source in data.get('sources',[]):
            if source['provider']=='shazam':
                counts['fingerprint_matches' if source['state']=='fingerprint_match' else 'fingerprint_no_match' if source['state']=='no_match' else 'fingerprint_unavailable']+=1
        counts['metadata_pending_retry']+=any(s.get('provider') in {'musicbrainz','lrclib'} and s.get('state') not in {'candidates','no_match'} for s in data.get('sources',[]))
    album_report=[]
    for items in albums.values():
        observed=defaultdict(list);declared={}
        for item in items:
            disc=item['disc'].split('/')[0] or '1'
            match=re.fullmatch(r'(\d+)(?:/(\d+))?',item['track'])
            if match:
                observed[disc].append(int(match[1]))
                if match[2]: declared.setdefault(disc,set()).add(int(match[2]))
        missing={disc:[i for i in range(1,next(iter(totals))+1) if i not in observed[disc]] for disc,totals in declared.items() if len(totals)==1 and next(iter(totals))<=1000}
        duplicate_numbers={disc:sorted({n for n in numbers if numbers.count(n)>1}) for disc,numbers in observed.items()}
        album_report.append({'album':items[0]['album'],'artist':items[0]['artist'],'files':len(items),'missing_by_tags':missing,'duplicate_numbers':duplicate_numbers,'release_completeness':'unknown','ids':[x['id'] for x in items]})
    candidate_groups=[{'evidence':key[0],'verified_duplicate':False,'warning':'Совпадение идентификатора не подтверждает исполнение, мастеринг или издание','files':list({x['id']:x for x in values}.values())} for key,values in candidates.items() if len({x['id'] for x in values})>1]
    return {'counts':counts,'exact_duplicates':[x for x in exact.values() if len(x)>1][:100],'same_audio_packets':[x for x in audio.values() if len(x)>1][:100],'recording_candidates':candidate_groups[:100],'albums':album_report[:500],'limits':{'duplicate_groups':100,'albums':500},'deletion_available':False}


def safe_code(value):
    return value if isinstance(value,str) and re.fullmatch(r'[a-z][a-z0-9_:-]{0,100}',value) else ''


def export(settings, kind):
    from . import lyrics_review
    report=[]
    # Every exported result and policy decision belongs to one DB read snapshot.
    with db.connect(settings.db_path) as conn:
        conn.execute('BEGIN')
        records=list(rows(settings,conn))
        analyses={row['track_id']:row for row in conn.execute('SELECT * FROM analyses')}
        outcomes=defaultdict(list)
        for row in conn.execute("""WITH ranked AS (
            SELECT ji.item_key,j.id AS job_id,j.kind,j.state AS job_state,ji.state,ji.result,j.created_at,
              ROW_NUMBER() OVER(PARTITION BY ji.item_key,j.kind ORDER BY j.created_at DESC,j.rowid DESC) AS rank
            FROM job_items ji JOIN jobs j ON j.id=ji.job_id
        ) SELECT * FROM ranked WHERE rank=1 ORDER BY created_at DESC,job_id DESC"""):
            result=json.loads(row['result'])
            outcomes[row['item_key']].append({'job_id':row['job_id'],'kind':row['kind'],
                'state':row['state'],'job_state':row['job_state'],'code':safe_code(result.get('code')),
                'skipped':result.get('skipped') is True,'reason':safe_code(result.get('reason')),
                'job_created_at':row['created_at']})
        for row,meta in records:
            entry={'id':row['id'],'path':row['relative_path'],'sha256':row['sha256'],'format':meta.get('format'),
                   'status':row['status'],'title':scalar(meta.get('tags',{}),'title'),
                   'artist':scalar(meta.get('tags',{}),'artist'),'album':scalar(meta.get('tags',{}),'album'),
                   'duration':meta.get('duration'),'has_art':bool(meta.get('has_art')),'has_lrc':bool(meta.get('lrc')),
                   'error':safe_code((meta.get('input_error') or {}).get('code')),
                   'write_supported':bool(meta.get('write_supported')) and not meta.get('cue_album_image',False)}
            provenance=meta.get('usage_provenance',{})
            excluded=[]
            for field in ('title','artist','album'):
                usages=provider_policy.references({'field_sources':{field:provenance.get(field,{})}})
                if any(not provider_policy._allowed(conn,usage,'export') for usage in usages):
                    entry[field]='';excluded.append(field)
            entry['provenance']=json.dumps(provenance or {'origin':'preexisting','rights_status':'unknown','license_verified':False},ensure_ascii=False)
            entry['policy_excluded_fields']=';'.join(excluded)
            previous=analyses.get(row['id'])
            analysis=json.loads(previous['payload']) if previous and previous['input_sha256']==row['sha256'] else {}
            entry['analysis_state']=safe_code(analysis.get('state')) or ('stale' if previous else 'not_started')
            entry['analysis_sources']=json.dumps([{'provider':safe_code(source.get('provider')),
                'state':safe_code(source.get('state'))} for source in analysis.get('sources',[])],ensure_ascii=False)
            entry['latest_outcomes']=json.dumps(outcomes.get(row['id'],[]),ensure_ascii=False)
            audit=meta.get('audit') or {}
            entry['audit_state']=safe_code(audit.get('state')) if audit.get('sha256')==row['sha256'] else 'not_current'
            gate=lyrics_review.publication_gate(settings,row,conn=conn)
            entry['publication_gate']=gate['state']
            entry['publication_reasons']=';'.join(gate['reasons'])
            entry['publication_profile']=gate['profile']
            entry['publication_exceptions']=';'.join(gate['exceptions'])
            publication=meta.get('publication') or {}
            entry['published_revision']=publication.get('sha256','')
            entry['published_current_revision']=publication.get('sha256')==row['sha256']
            entry['youtube_url']='https://www.youtube.com/watch?v='+meta['youtube']['linked_id'] if meta.get('youtube',{}).get('linked_id') else ''
            entry['youtube_state']=meta.get('youtube',{}).get('state','unknown')
            report.append(entry)
        source={row['id']:meta.get('youtube',{}) for row,meta in records}
        snapshot_summary=summary(settings,conn) if kind=='json' else None
    if kind=='json':return json.dumps({'schema':2,'files':report,'youtube':source,'summary':snapshot_summary},ensure_ascii=False,indent=2)
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(report[0]) if report else ['id','path','status']);writer.writeheader()
    for row in report:
        writer.writerow({k:("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v) for k,v in row.items()})
    return stream.getvalue()
