"""Read-only collection reports; similarity never deletes or merges files."""
from collections import defaultdict
import csv
import io
import json
import re
from . import db
from .providers import scalar, normalized


def rows(settings):
    for row in db.fetchall(settings.db_path,'SELECT * FROM tracks WHERE is_current=1 ORDER BY relative_path'):
        meta=json.loads(row['metadata'])
        yield row,meta


def summary(settings):
    exact=defaultdict(list);audio=defaultdict(list);albums=defaultdict(list)
    counts={'files':0,'cue_sheets':0,'input_errors':0,'writable_profiles':0,'with_lyrics':0,'with_cover':0,'analyzed':0,'with_candidates':0,'no_match':0,'asr_drafts':0}
    for row,meta in rows(settings):
        counts['files']+=1
        if meta.get('format')=='cue':counts['cue_sheets']+=1;continue
        counts['input_errors']+=bool(meta.get('input_error'))
        counts['writable_profiles']+=bool(meta.get('write_supported')) and not meta.get('cue_album_image',False)
        counts['with_lyrics']+=bool(meta.get('lrc'));counts['with_cover']+=bool(meta.get('has_art'))
        entry={'id':row['id'],'path':row['relative_path'],'format':meta.get('format'),'duration':meta.get('duration'),'status':row['status']}
        exact[row['sha256']].append(entry)
        identity=meta.get('audit',{}).get('audio_identity') or meta.get('audio_identity')
        if identity:audio[identity].append(entry)
        tags=meta.get('tags',{});album=scalar(tags,'album');artist=scalar(tags,'albumartist') or scalar(tags,'artist')
        if album:
            albums[(normalized(artist),normalized(album))].append({**entry,'album':album,'artist':artist,'track':scalar(tags,'tracknumber'),'disc':scalar(tags,'discnumber')})
    for row in db.fetchall(settings.db_path,'SELECT a.payload FROM analyses a JOIN tracks t ON t.id=a.track_id AND t.is_current=1 AND t.sha256=a.input_sha256'):
        data=json.loads(row['payload']);counts['analyzed']+=1;counts['with_candidates']+=bool(data.get('candidates'));counts['no_match']+=data.get('state')=='no_match';counts['asr_drafts']+=bool(data.get('asr'))
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
    return {'counts':counts,'exact_duplicates':[x for x in exact.values() if len(x)>1][:100],'same_audio_packets':[x for x in audio.values() if len(x)>1][:100],'albums':album_report[:500],'limits':{'duplicate_groups':100,'albums':500},'deletion_available':False}


def export(settings, kind):
    report=[]
    for row,meta in rows(settings):
        report.append({'id':row['id'],'path':row['relative_path'],'sha256':row['sha256'],'format':meta.get('format'),'status':row['status'],'title':scalar(meta.get('tags',{}),'title'),'artist':scalar(meta.get('tags',{}),'artist'),'album':scalar(meta.get('tags',{}),'album'),'duration':meta.get('duration'),'has_art':bool(meta.get('has_art')),'has_lrc':bool(meta.get('lrc')),'error':meta.get('input_error',{}).get('code',''),'write_supported':bool(meta.get('write_supported')) and not meta.get('cue_album_image',False)})
    if kind=='json':return json.dumps({'files':report,'summary':summary(settings)},ensure_ascii=False,indent=2)
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(report[0]) if report else ['id','path','status']);writer.writeheader()
    for row in report:
        # CSV formula injection protection applies to paths and provider/user tag text.
        writer.writerow({k:("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v) for k,v in row.items()})
    return stream.getvalue()
