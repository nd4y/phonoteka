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
