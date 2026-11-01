"""Persisted intake admission for unattended work, never a replacement for SHA CAS.

Only independently completed scan passes count. A single inspection, an aborted
scan, or an old catalog row cannot assert that a downloader has finished.
Explicit owner jobs deliberately do not inherit this unattended admission rule.
"""
import math
import re
import time


CONTRACT = 'two-scan-stability-v1'


def signature(metadata):
    size, mtime = metadata.get('size'), metadata.get('mtime_ns')
    revision = metadata.get('sha256')
    if (not isinstance(size, int) or isinstance(size, bool) or size < 0 or
            not isinstance(mtime, int) or isinstance(mtime, bool) or mtime < 0 or
            not isinstance(revision, str) or re.fullmatch('[a-f0-9]{64}', revision) is None):
        return None
    return {'size': size, 'mtime_ns': mtime, 'sha256': revision}


def observed(metadata, previous, scan_id, *, now=None):
    """Keep a candidate observation separate from confirmed scan evidence."""
    now = time.time() if now is None else now
    current = signature(metadata)
    old = previous.get('input_readiness', {}) if isinstance(previous, dict) else {}
    valid = (isinstance(old, dict) and old.get('contract') == CONTRACT and
             current is not None and old.get('signature') == current)
    result = dict(old) if valid else {'contract': CONTRACT, 'signature': current,
                                    'confirmations': 0, 'first_observed_at': now}
    result['state'] = 'waiting_stability'
    # A derivative/manual scan_one(None) is not a completed library scan.
    result['candidate_scan_id'] = scan_id
    result['candidate_observed_at'] = now
    return result


def confirm(metadata, scan_id, *, complete, grace_seconds, now=None):
    now = time.time() if now is None else now
    value = metadata.get('input_readiness', {})
    if not isinstance(scan_id,str) or not scan_id or not isinstance(value, dict) or value.get('contract') != CONTRACT or value.get('candidate_scan_id') != scan_id:
        return None
    value = dict(value)
    if not complete or value.get('signature') != signature(metadata):
        # Do not let a partial/cancelled scan become the second proof later.
        value.update(confirmations=0, first_observed_at=now, last_scan_id=None,
                     state='waiting_stability')
        return value
    if value.get('last_scan_id') != scan_id:
        count=value.get('confirmations',0)
        value['confirmations'] = min(2, max(0,count if type(count) is int else 0) + 1)
        value['last_scan_id'] = scan_id
        value['last_observed_at'] = value['candidate_observed_at']
    value.pop('candidate_scan_id', None)
    value.pop('candidate_observed_at', None)
    value['state'] = describe({'input_readiness': value, **{key: metadata.get(key)
                               for key in ('size', 'mtime_ns', 'sha256')}},
                              grace_seconds=grace_seconds, now=now)['state']
    return value


def describe(metadata, *, grace_seconds, now=None):
    now = time.time() if now is None else now
    value = metadata.get('input_readiness', {})
    first = value.get('first_observed_at') if isinstance(value, dict) else None
    valid = (isinstance(value, dict) and value.get('contract') == CONTRACT and
             value.get('signature') == signature(metadata) and signature(metadata) is not None and
             isinstance(first, (int, float)) and not isinstance(first, bool) and
             math.isfinite(first) and first <= now and type(value.get('confirmations')) is int and value['confirmations'] == 2)
    remaining = max(0., first + grace_seconds - now) if valid else None
    return {'state': 'ready' if valid and remaining == 0 else 'waiting_stability',
            'reason': None if valid and remaining == 0 else 'input_stabilizing',
            'retry_after': max(1, math.ceil(remaining)) if remaining else None}
