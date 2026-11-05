from phonoteka.alignment import align_lines


def test_missing_words_stay_unaligned():
    result=align_lines('hello world\nmissing line',[{'word':'hello','start':1,'end':1.5,'probability':.9},{'word':'world','start':2,'end':2.5,'probability':.9}],10)
    assert result['coverage']==.5 and result['lrc'] is None
    assert result['lines'][1]['start_ms'] is None
    assert result['automatic_acceptance'] is False


def test_existing_words_unchanged_and_times_in_original_timeline():
    result=align_lines('[00:01.00]Hello world',[{'word':'Hello','start':4,'end':4.5,'probability':.9},{'word':'world','start':5,'end':5.5,'probability':.9}],10)
    assert result['complete'] and result['lines'][0]['start_ms']==4000
    assert result['lines'][0]['original_ms']==1000
    assert result['lines'][0]['text']=='Hello world'
    assert result['automatic_acceptance'] is False


def test_hallucination_low_confidence_and_invalid_bounds_not_accepted():
    result=align_lines('hello world',[{'word':'hello','start':1,'end':1.5,'probability':.1},{'word':'world','start':11,'end':12,'probability':.9}],10)
    assert result['coverage']==0


def test_empty_lrc_markers_are_not_missing_lyrics_or_fabricated_anchors():
    words=[{'word':'hello','start':1,'end':1.5,'probability':.9},
           {'word':'world','start':2,'end':2.5,'probability':.9}]
    result=align_lines('[00:00.00]\n[00:01.00]hello world\n[00:05.00]',words,10)
    assert result['complete'] and result['coverage']==1
    assert len(result['lines'])==3 and result['lines'][2]['original_ms']==5000
    assert result['lines'][2]['start_ms'] is None and result['lines'][2]['reason']=='empty_line'
    assert result['lrc'].count('\n')==0 and 'hello world' in result['lrc']
    assert not result['automatic_acceptance']
    empty=align_lines('[00:05.00]',words,10)
    assert not empty['complete'] and empty['coverage']==0 and empty['lrc'] is None
    missing=align_lines('[00:01.00]unheard lyrics\n[00:05.00]',words,10)
    assert missing['coverage']==0 and missing['lrc'] is None
