"""Current-revision audio audit policy shared by queries, API and exports.

CUE sheets are documents, so their state is not applicable. Physical album
images remain eligible. Missing files and current scanner input errors take
precedence over an old audit. Failed/unsupported current audits are errors;
stale or malformed results are unchecked. Warnings are a subset of passed.
These are audit outcomes, never a conclusion that an error means damaged audio.
"""
import json
import re


FILTERS = ('audit_errors', 'audit_warnings', 'audit_unchecked', 'audit_passed')
STAGES = frozenset({'header', 'packets', 'decode'})


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_:-]{0,100}', value) else ''


def describe(metadata, revision, status):
    """Only expose bounded codes; retain no diagnostic message or private path."""
    result = {'audit_status': 'unchecked', 'audit_error': '', 'audit_stage': ''}
    if not isinstance(metadata, dict):
        metadata = {}
    if metadata.get('format') == 'cue':
        return {**result, 'audit_status': 'not_applicable'}
    if status == 'missing':
        return {**result, 'audit_status': 'error', 'audit_error': 'missing_file'}
    input_error = metadata.get('input_error')
    if input_error:
        code = safe_code(input_error.get('code')) if isinstance(input_error, dict) else ''
        return {**result, 'audit_status': 'error', 'audit_error': code or 'input_error'}
    audit = metadata.get('audit')
    if (not isinstance(audit, dict) or not isinstance(revision, str)
            or not re.fullmatch(r'[0-9a-f]{64}', revision) or audit.get('sha256') != revision):
        return result
    state = audit.get('state')
    error = safe_code(audit.get('error'))
    stage = audit.get('failure_stage')
    stage = stage if isinstance(stage, str) and stage in STAGES else ''
    if state in ('failed', 'unsupported') or (state == 'passed' and error):
        return {'audit_status': 'error', 'audit_error': error or 'audit_failed', 'audit_stage': stage}
    if state != 'passed' or audit.get('error'):
        return result
    warnings = audit.get('warnings', [])
    if (not isinstance(warnings, list) or any(not isinstance(item, dict) or not safe_code(item.get('code'))
            or ('message' in item and not isinstance(item['message'], str)) for item in warnings)):
        return result
    return {**result, 'audit_status': 'warning' if warnings else 'passed'}


def matches(metadata_json, revision, status, selected):
    """SQLite scalar used only by the four opt-in audit filters, once per row."""
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, ValueError):
        metadata = {}
    state = describe(metadata, revision, status)['audit_status']
    accepted = {'audit_errors': ('error',), 'audit_warnings': ('warning',),
                'audit_unchecked': ('unchecked',), 'audit_passed': ('passed', 'warning')}
    return int(state in accepted.get(selected, ()))
