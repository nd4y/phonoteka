import { describe, expect, it } from 'vitest';
import { activeGuideJob, backGuide, moveGuide, parseGuideState, recommendedGuideStep } from './guideState';
import type { Job } from './types';

describe('guide navigation and existing tasks', () => {
  it('round trips a bounded step history, rather than treating visits as collection readiness', () => {
    const first = parseGuideState(null), next = moveGuide(moveGuide(first, 'scan'), 'metadata');
    expect(backGuide(next).step).toBe('scan'); expect(parseGuideState(JSON.stringify(next))).toEqual(next);
    expect(moveGuide(next,'metadata')).toBe(next);
    expect(parseGuideState('{"step":"delete","history":["scan","bad"]}')).toEqual({step:'folder',history:['scan']});
    expect(parseGuideState('not json')).toEqual(first);
    expect(parseGuideState(JSON.stringify({step:'ready',history:Array(100).fill('scan')})).history).toHaveLength(20);
  });
  it.each(['queued','running','paused','waiting_provider','waiting_authorization','deferred'])('reuses an existing %s task instead of creating a duplicate', state => {
    const job = {id:'existing',kind:'recognize',state} as Job;
    expect(activeGuideJob([job],'recognize')).toBe(job);
    expect(activeGuideJob([job],'metadata')).toBeUndefined();
  });
  it('allows the next explicit action after a terminal task', () => {
    expect(activeGuideJob([{kind:'recognize',state:'completed'} as Job],'recognize')).toBeUndefined();
  });
  it('suggests existing work instead of replaying the whole pipeline', () => {
    expect(recommendedGuideStep({audio_files:10,current_proposals:3,metadata_pending:10})).toBe('review');
    expect(recommendedGuideStep({audio_files:0})).toBe('scan');
    expect(recommendedGuideStep({audio_files:10,with_core_tags:5,metadata_pending:4})).toBe('metadata');
    expect(recommendedGuideStep({audio_files:10,recognition_pending:3})).toBe('recognize');
    expect(recommendedGuideStep({audio_files:10,published_current:0})).toBe('review');
    expect(recommendedGuideStep({audio_files:10,published_current:10})).toBe('ready');
  });
});
