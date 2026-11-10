"""Read-only source comparison, bound to the decisions it was made against."""
import json

from . import field_policy, lyrics_document


def revision(conn, row, analysis, payload):
    document = conn.execute('SELECT value FROM settings WHERE key=?', (lyrics_document.key(row['id']),)).fetchone()
    proposal = conn.execute('SELECT id FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (row['id'],)).fetchone()
    return field_policy.value_hash({
        'audio': row['sha256'], 'analysis': analysis['payload'],
        'payload': payload, 'document': document['value'] if document else None,
        'proposal': proposal['id'] if proposal else None,
        'field_policy': field_policy.load(conn, row['id']),
        'source_priorities': field_policy.sources(conn),
    })


def compare(settings, conn, row, payload, token):
    # The caller rolls back the entire validation/proposal path, including ensure().
    proposed = lyrics_document.propose(conn, row, payload)
    meta = json.loads(row['metadata'])
    fields = []
    for name, after in field_policy.payload_values(proposed).items():
        before = field_policy.current_value(settings, row, name)
        unchanged = before == field_policy.normalized_value(name, after)
        if name == 'cover':
            before = bool(meta.get('has_art'))
            after = bool(after)
        elif name != 'lrc':
            before = meta.get('tags', {}).get(name)
        fields.append({'field': name, 'before': before, 'after': after,
                       'changed': not unchanged,
                       'source': proposed.get('field_sources', {}).get(name, {})})
    previous = conn.execute('SELECT id FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (row['id'],)).fetchone()
    return {'preview_revision': token, 'fields': fields,
            'blocked_fields': proposed.get('field_policy', {}).get('blocked_fields', []),
            'omitted_fields': proposed.get('policy_omitted_fields', []),
            'lyric_error': proposed.get('candidate_lyric_error'),
            'lyric_adjustment': proposed.get('candidate_lyric_adjustment'),
            'replaces_proposal': bool(previous)}
