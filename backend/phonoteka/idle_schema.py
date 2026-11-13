"""Additive v2 indexes: small job status and durable policy-cleanup invalidation."""


def _json_value(source, path):
    # Preserve JSON booleans (authorization_version deliberately rejects bools).
    value = f"json_extract({source},'{path}')"
    return f"json(CASE json_type({source},'{path}') WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' ELSE json_quote({value}) END)"


def _status(source):
    source = f"CASE WHEN json_valid({source}) THEN {source} ELSE '{{}}' END"
    keys = ('trigger', 'retry_at', 'pause_requested', 'authorization_wait', 'authorization_version', 'export_progress')
    pairs = [f"'{key}',{_json_value(source, '$.' + key)}" for key in keys]
    pairs.append("'params',json_object('reconcile_root'," + _json_value(source, '$.params.reconcile_root') + ")")
    return 'json_object(' + ','.join(pairs) + ')'


def _track_values(prefix):
    raw = prefix + 'metadata'
    data = f"CASE WHEN json_valid({raw}) THEN {raw} ELSE '{{}}' END"
    def value(path): return f"json_extract({data},'$.{path}')"
    return ','.join((prefix+'id', prefix+'sha256', prefix+'is_current', value('format'), prefix+'status',
                     value('youtube.state'), value('has_art'), f"length(COALESCE({value('lrc')},''))",
                     f"json_type({data},'$.input_error')", value('write_supported'),
                     f"COALESCE({value('cue_album_image')},0)", f'json_valid({raw})'))


def install(conn):
    """Called only inside initialize's atomic v2 extension/write-barrier transaction."""
    conn.execute('CREATE TABLE IF NOT EXISTS job_status (job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE, public_payload TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS analysis_status (track_id TEXT PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE, input_sha256 TEXT NOT NULL, sources TEXT NOT NULL)')
    # JSON-derived values have no affinity, preserving SQLite JSON scalar types.
    conn.execute('''CREATE TABLE IF NOT EXISTS track_status (id TEXT PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
        sha256 TEXT NOT NULL,is_current INTEGER NOT NULL,format,status TEXT NOT NULL,youtube,has_art,
        lrc_length INTEGER NOT NULL,input_error,write_supported,cue_album_image,valid_json INTEGER NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS track_status_current ON track_status(is_current,id,sha256)')
    conn.execute('CREATE TABLE IF NOT EXISTS policy_cleanup (entity TEXT NOT NULL, identity TEXT NOT NULL, due REAL, PRIMARY KEY(entity,identity))')
    conn.execute('CREATE INDEX IF NOT EXISTS policy_cleanup_due ON policy_cleanup(due,entity,identity)')
    conn.execute('CREATE INDEX IF NOT EXISTS provider_cache_expiry ON provider_cache(expires)')
    conn.execute('CREATE INDEX IF NOT EXISTS provider_cache_provider ON provider_cache(provider)')
    conn.execute('CREATE INDEX IF NOT EXISTS provider_cache_stats ON provider_cache(provider,expires,length(payload))')
    conn.execute('CREATE INDEX IF NOT EXISTS job_items_status ON job_items(job_id,state)')
    # Scalar columns after payload can still traverse its SQLite overflow pages.
    # Keep the complete list-page projection in a small covering index instead.
    conn.execute('CREATE INDEX IF NOT EXISTS jobs_status_cover ON jobs(id,kind,state,stage,total,completed,failed,cancel_requested,created_at,updated_at,error)')
    # Activity summaries already filter valid JSON and job_id; avoid revisiting
    # the entire event history on each small status-page poll.
    conn.execute("CREATE INDEX IF NOT EXISTS events_job_activity ON events(json_extract(payload,'$.job_id'),id) WHERE json_valid(payload)")
    status = _status('NEW.payload')
    for event in ('INSERT', 'UPDATE OF payload'):
        name = 'insert' if event == 'INSERT' else 'update'
        condition = '' if name == 'insert' else ' WHEN NEW.payload IS NOT OLD.payload'
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS job_status_{name} AFTER {event} ON jobs{condition} BEGIN
            INSERT INTO job_status VALUES(NEW.id,{status}) ON CONFLICT(job_id) DO UPDATE SET public_payload=excluded.public_payload;
        END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS job_status_delete AFTER DELETE ON jobs BEGIN
        DELETE FROM job_status WHERE job_id=OLD.id; END''')
    # Check for missing entries without reading a single existing payload on later starts.
    conn.execute('INSERT INTO job_status SELECT id,' + _status('payload') + ' FROM jobs WHERE NOT EXISTS (SELECT 1 FROM job_status s WHERE s.job_id=jobs.id)')
    def sources(value):
        return f"CASE WHEN json_valid({value}) AND json_type({value},'$.sources') IS NOT NULL THEN json_quote(json_extract({value},'$.sources')) ELSE '[]' END"
    for event in ('INSERT', 'UPDATE OF payload,input_sha256'):
        name = 'insert' if event == 'INSERT' else 'update'
        condition = '' if name == 'insert' else ' WHEN NEW.payload IS NOT OLD.payload OR NEW.input_sha256 IS NOT OLD.input_sha256'
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS analysis_status_{name} AFTER {event} ON analyses{condition} BEGIN
            INSERT INTO analysis_status VALUES(NEW.track_id,NEW.input_sha256,{sources('NEW.payload')})
            ON CONFLICT(track_id) DO UPDATE SET input_sha256=excluded.input_sha256,sources=excluded.sources; END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS analysis_status_delete AFTER DELETE ON analyses BEGIN
        DELETE FROM analysis_status WHERE track_id=OLD.track_id; END''')
    conn.execute('INSERT INTO analysis_status SELECT track_id,input_sha256,' + sources('payload') + ' FROM analyses WHERE NOT EXISTS (SELECT 1 FROM analysis_status s WHERE s.track_id=analyses.track_id)')
    for event in ('INSERT', 'UPDATE OF metadata,status,sha256,is_current'):
        name = 'insert' if event == 'INSERT' else 'update'
        condition = '' if name == 'insert' else ' WHEN ' + ' OR '.join(f'NEW.{k} IS NOT OLD.{k}' for k in ('metadata','status','sha256','is_current'))
        fields = ('sha256','is_current','format','status','youtube','has_art','lrc_length','input_error','write_supported','cue_album_image','valid_json')
        assignments = ','.join(f'{key}=excluded.{key}' for key in fields)
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS track_status_{name} AFTER {event} ON tracks{condition} BEGIN
            INSERT INTO track_status VALUES({_track_values('NEW.')}) ON CONFLICT(id) DO UPDATE SET {assignments}; END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS track_status_delete AFTER DELETE ON tracks BEGIN
        DELETE FROM track_status WHERE id=OLD.id; END''')
    conn.execute('INSERT INTO track_status SELECT '+_track_values('t.')+' FROM tracks t WHERE NOT EXISTS (SELECT 1 FROM track_status s WHERE s.id=t.id)')
    for table, key, column in (('analyses', 'track_id', 'payload'), ('proposals', 'id', 'payload'), ('jobs', 'id', 'payload'), ('settings', 'key', 'value')):
        restriction = "NEW.key GLOB 'release_review:*'" if table == 'settings' else '1'
        for event in ('INSERT', f'UPDATE OF {column}'):
            name = 'insert' if event == 'INSERT' else 'update'
            condition = restriction + (f' AND NEW.{column} IS NOT OLD.{column}' if name == 'update' else '')
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS policy_cleanup_{table}_{name} AFTER {event} ON {table} WHEN {condition} BEGIN
                INSERT INTO policy_cleanup VALUES('{table}',NEW.{key},0) ON CONFLICT(entity,identity) DO UPDATE SET due=0; END''')
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS policy_cleanup_{table}_delete AFTER DELETE ON {table} BEGIN
            DELETE FROM policy_cleanup WHERE entity='{table}' AND identity=OLD.{key}; END''')
        where = " WHERE key GLOB 'release_review:*'" if table == 'settings' else ''
        conn.execute(f"INSERT OR IGNORE INTO policy_cleanup SELECT '{table}',{key},0 FROM {table}{where}")
    conn.execute('''CREATE TRIGGER IF NOT EXISTS policy_cleanup_job_finished AFTER UPDATE OF state ON jobs
        WHEN OLD.state='running' AND NEW.state!='running' BEGIN
        INSERT INTO policy_cleanup VALUES('jobs',NEW.id,0) ON CONFLICT(entity,identity) DO UPDATE SET due=0; END''')
    for event, ref in (('INSERT', 'NEW'), ('UPDATE OF value', 'NEW'), ('DELETE', 'OLD')):
        name = event.split()[0].lower()
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS policy_cleanup_rules_{name} AFTER {event} ON settings
            WHEN {ref}.key GLOB 'provider_usage:*' BEGIN UPDATE policy_cleanup SET due=0; END''')
