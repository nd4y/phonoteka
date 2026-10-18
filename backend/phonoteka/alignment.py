"""Conservative word anchors from independent ASR; never interpolate missing lines."""
from difflib import SequenceMatcher
import hashlib
import re
from .media import parse_lrc, serialize_lrc
from .providers import normalized


def align_lines(text, words, duration):
    if re.search(r'\[\d+:\d+',text):
        original=parse_lrc(text)['lines']
    else: original=[{'text':x,'start_ms':None} for x in text.splitlines() if x.strip()]
    tokens=[];owners=[]
    for index,line in enumerate(original):
        for token in normalized(line['text']).split(): tokens.append(token);owners.append(index)
    spoken=[];positions=[]
    for word in words:
        if word.get('probability',0)<.5 or word.get('start',-1)<0 or word.get('end',duration+1)>duration+.1: continue
        for token in normalized(word.get('word','')).split(): spoken.append(token);positions.append(word)
    if len(tokens)>20000 or len(spoken)>20000: raise ValueError('Alignment text exceeds limit')
    mapped={}
    for block in SequenceMatcher(None,tokens,spoken,autojunk=False).get_matching_blocks():
        # An isolated common word is not a reliable musical anchor.
        if block.size<2: continue
        for offset in range(block.size): mapped[block.a+offset]=positions[block.b+offset]
    result=[];previous=-1
    for index,line in enumerate(original):
        indexes=[i for i,owner in enumerate(owners) if owner==index]
        found=[i for i in indexes if i in mapped]
        state='unaligned';stamp=None
        if indexes and len(found)==len(indexes):
            stamps=[mapped[i]['start'] for i in indexes]
            ends=[mapped[i]['end'] for i in indexes]
            if stamps==sorted(stamps) and stamps[0]>=previous and ends[-1]-stamps[0]<=max(15,len(indexes)*2):
                stamp=round(stamps[0]*1000);state='anchored';previous=stamps[-1]
        result.append({'text':line['text'],'original_ms':line.get('start_ms'),'start_ms':stamp,'state':state,'matched_words':len(found),'words':len(indexes)})
    complete=bool(result) and all(x['state']=='anchored' for x in result)
    return {'method':'independent_asr_word_anchors_v1','text_sha256':hashlib.sha256(text.encode()).hexdigest(),'lines':result,'coverage':sum(x['state']=='anchored' for x in result)/max(1,len(result)),'complete':complete,'automatic_acceptance':False,'state':'needs_review' if complete else 'partially_aligned','lrc':serialize_lrc(result) if complete else None}
