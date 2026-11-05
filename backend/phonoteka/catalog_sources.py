"""Retained inputs are not additional deliverables once their outputs exist.

This is catalog accounting, not publication acceptance. Missing outputs or stale
source revisions keep the input visible in outstanding work.
"""
import json


def completed_sources(conn):
    parents={row['id']:dict(row) for row in conn.execute("""SELECT id,relative_path,sha256,status,metadata
        FROM tracks WHERE is_current=1 AND (
            json_type(metadata,'$.audio_export')='object' OR
            json_type(metadata,'$.cue_export')='object' OR
            json_extract(metadata,'$.cue_album_image')=1)""")}
    metadata={tid:json.loads(row['metadata']) for tid,row in parents.items()}
    exports={}
    for tid,meta in metadata.items():
        value=meta.get('cue_export') if meta.get('format')=='cue' else meta.get('audio_export')
        if not isinstance(value,dict):continue
        ids=value.get('track_ids')
        if (not isinstance(ids,list) or not ids or not all(isinstance(x,str) for x in ids)
                or len(set(ids))!=len(ids) or value.get('count')!=len(ids)
                or not isinstance(value.get('identity'),str) or not value['identity']):
            continue
        exports[tid]=value
    wanted=sorted({tid for value in exports.values() for tid in value['track_ids']})
    if not wanted:return {}
    children={row['id']:dict(row) for row in conn.execute("""SELECT id,sha256,status,metadata FROM tracks
        WHERE is_current=1 AND id IN (SELECT value FROM json_each(?))""",(json.dumps(wanted),))}
    origins={tid:json.loads(row['metadata']).get('derived_from',{}) for tid,row in children.items()}
    def parent_matches(origin,parent):
        if not isinstance(origin,dict):return False
        if (metadata[parent['id']].get('format')=='cue'
                and 'parent_track_id' not in origin and 'parent_sha256' not in origin):
            # Earlier CUE exports stored the same identity under sheet_*.
            return origin.get('sheet_id')==parent['id'] and origin.get('sheet_sha256')==parent['sha256']
        return origin.get('parent_track_id')==parent['id'] and origin.get('parent_sha256')==parent['sha256']
    result={}
    for tid,value in exports.items():
        parent=parents[tid]
        if all(child in children and children[child]['status']!='missing'
               and parent_matches(origins[child],parent)
               and origins[child].get('export_identity')==value['identity']
               for child in value['track_ids']):
            result[tid]=value['track_ids']
    # A CUE image may be referenced by several sheets or share a sheet with
    # other audio images. Every related sheet and this image's audio revision
    # must be represented before excluding the retained image.
    for tid,meta in metadata.items():
        links=meta.get('cue_sheets')
        if not meta.get('cue_album_image') or not isinstance(links,list) or not links:continue
        outputs=[]
        for link in links:
            if (not isinstance(link,dict) or link.get('id') not in result
                    or metadata.get(link['id'],{}).get('format')!='cue'):break
            matching=[child for child in result[link['id']]
                      if origins[child].get('source')==parents[tid]['relative_path']]
            count=link.get('track_count')
            if (type(count) is not int or count<1 or len(matching)<count
                    or any(origins[child].get('source_sha256')!=parents[tid]['sha256'] for child in matching)):
                break
            outputs.extend(matching)
        else:
            result[tid]=list(dict.fromkeys(outputs))
    return result
