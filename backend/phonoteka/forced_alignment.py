"""Known reference tokens aligned with Whisper attention/DTW and independent ASR anchors."""
from .alignment import align_lines
from .media import serialize_lrc
from .providers import normalized


def align_reference(model,samples,text,language,words,duration):
    from faster_whisper.tokenizer import Tokenizer
    from faster_whisper.audio import pad_or_trim
    tokenizer=Tokenizer(model.hf_tokenizer,model.model.is_multilingual,task='transcribe',language=language)
    def provider(clip,reference):
        tokens=tokenizer.encode(' '+reference)
        if not 0<len(tokens)<=400:return None
        features=model.feature_extractor(clip)
        encoded=model.encode(pad_or_trim(features,length=3000,axis=-1))
        return model.find_alignment(tokenizer,[tokens],encoded,len(clip)//160)[0]
    return align_with_provider(samples,text,words,duration,provider)


def align_with_provider(samples,text,words,duration,provider):
    """All client-side independent anchors and timing gates apply to every engine."""
    preliminary=align_lines(text,words,duration)
    if len(preliminary['lines'])>500:raise ValueError('Alignment supports up to 500 lines')
    lines=[{**line,'start_ms':None,'end_ms':None,'state':'unaligned','reason':'independent_anchor_missing'} for line in preliminary['lines']]
    groups=[];group=[]
    for index,line in enumerate(preliminary['lines']):
        if line['state']!='anchored':
            if group:groups.append(group);group=[]
            continue
        if group and line['end_ms']-preliminary['lines'][group[0]]['start_ms']>28000:
            groups.append(group);group=[]
        group.append(index)
    if group:groups.append(group)
    previous=-1
    for group in groups:
        first=preliminary['lines'][group[0]];last=preliminary['lines'][group[-1]]
        start=max(0,first['start_ms']/1000-.25);end=min(duration,last['end_ms']/1000+.25)
        reference=' '.join(preliminary['lines'][index]['text'].strip() for index in group)
        for index in group:lines[index]['reason']='window_or_token_limit'
        if not 0<end-start<=29.5:continue
        clip=samples[round(start*16000):round(end*16000)]
        aligned=provider(clip,reference)
        if aligned is None:continue
        spoken=[];positions=[]
        for word in aligned:
            for token in normalized(word['word']).split():spoken.append(token);positions.append(word)
        for index in group:lines[index]['reason']='alignment_low_confidence'
        if spoken!=normalized(reference).split():continue
        cursor=0
        for index in group:
            line=preliminary['lines'][index];count=len(normalized(line['text']).split())
            found=positions[cursor:cursor+count];cursor+=count
            if not found:continue
            probabilities=[float(w['probability']) for w in found]
            first_time=start+float(found[0]['start']);last_time=start+float(found[-1]['end'])
            ordered=all(0<=float(w['start'])<=float(w['end'])<=end-start+.1 for w in found)
            # Independent recognition validates the words; alignment additionally
            # needs bounded, monotonic timing and useful mean likelihood. Individual
            # token likelihood is sensitive to punctuation/context, so retain
            # low values for review instead of treating them as audio absence.
            if ordered and first_time>=previous and last_time>first_time and sum(probabilities)/len(probabilities)>=.5 and abs(first_time-line['start_ms']/1000)<=1.5:
                lines[index].update(start_ms=round(first_time*1000),end_ms=round(last_time*1000),state='aligned',reason='requires_listening',probability=round(sum(probabilities)/len(probabilities),4),minimum_token_probability=round(min(probabilities),4),low_probability_words=sum(p<.15 for p in probabilities))
                previous=first_time
    complete=bool(lines) and all(x['state']=='aligned' for x in lines)
    return {**preliminary,'method':'whisper_attention_dtw_with_independent_asr_anchors_v1','lines':lines,'coverage':sum(x['state']=='aligned' for x in lines)/max(1,len(lines)),'complete':complete,'state':'needs_review' if complete else 'partially_aligned','automatic_acceptance':False,'music_quality_gate':'not_validated','lrc':serialize_lrc(lines) if complete else None}
