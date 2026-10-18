"""Data usage controls and durable provenance; API access is not a content license.

Unknown provider capabilities fail closed. Local/owner data remains usable in the
personal collection profile, with an explicitly unverified rights status.
"""
import base64
from copy import deepcopy
import hashlib
import json
import re
import time

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from . import db, media, security
from .telemetry import policy_denied

OPERATIONS = ("fetch", "cache", "embed", "export", "derived_analysis")
LOCAL = {"local", "local_cover", "local_replaygain", "independent_asr", "owner", "preexisting"}
DATA_TYPES = {"metadata", "lyrics", "cover", "provenance"}
CHECKED_AT = "2026-10-18"
TERMS = {
    "musicbrainz": ("https://musicbrainz.org/doc/About/Data_License", "CC0 core metadata; supplementary data is a separate license"),
    "lrclib": ("https://lrclib.net/docs", "API access documented; a license for individual lyrics is not established"),
    "coverartarchive": ("https://coverartarchive.org/", "Images remain copyrighted by their respective owners"),
    "shazam": ("https://www.apple.com/legal/internet-services/", "ShazamIO is unofficial; API access does not establish content reuse rights"),
    "theaudiodb": ("https://www.theaudiodb.com/docs_terms_of_use.php", "Attribution and third-party content restrictions apply"),
    "discogs": ("https://support.discogs.com/hc/en-us/articles/360009334593-API-Terms-of-Use", "CC0 catalogue data differs from restricted images/user/marketplace data; cache freshness applies"),
    "youtube": ("https://www.youtube.com/static?template=terms", "Store factual video provenance only; no audio/video downloading"),
}
SUPPORTED = {'musicbrainz':{'metadata'},'lrclib':{'lyrics'},'coverartarchive':{'metadata','cover'},'shazam':{'metadata'},
             'theaudiodb':{'metadata','cover'},'discogs':{'metadata'},'youtube':{'provenance'}}


def loads(value, default=None):
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else (default or {})
    except (TypeError, ValueError):
        return default or {}


def digest(value):
    return hashlib.sha256(db.encode(value).encode()).hexdigest()


def data_type(candidate):
    return {"lyrics": "lyrics", "cover": "cover"}.get(candidate.get("kind"), "metadata")


def defaults(provider, kind):
    local = provider in LOCAL
    core = provider == "musicbrainz" and kind == "metadata"
    discogs = provider == "discogs" and kind == "metadata"
    provenance = provider == "youtube" and kind == "provenance"
    granted = local or core or discogs or provenance
    known = kind in SUPPORTED.get(provider,set()) or local
    ttl = 6 * 3600 if provider == "discogs" else 7 * 86400 if granted else 0
    rule = {"allow_" + operation: granted for operation in OPERATIONS}
    # Existing deployment opt-in still controls transmission. Fetch is a separate
    # capability, never an assertion that the returned lyrics/art can be embedded.
    rule["allow_fetch"] = known
    url, note = TERMS.get(provider, ("", "Owner supplied or preexisting data; rights are not asserted"))
    return {
        **rule, "cache_ttl": ttl, "provider": provider, "data_type": kind,
        "rights_status": "cc0_core_metadata" if core or discogs else "owner_or_preexisting_unverified" if local else "factual_provenance" if provenance else "unknown",
        "basis": "Core catalogue metadata only" if core or discogs else "Personal collection profile" if local else "Factual source link" if provenance else "",
        "terms_url": url, "terms_note": note, "checked_at": CHECKED_AT,
        "review_after": "2027-04-18", "attribution": provider if provider in {"discogs", "theaudiodb"} else "",
    }


def rule(conn, provider, kind):
    result = defaults(provider, kind)
    saved = conn.execute("SELECT value FROM settings WHERE key=?", (f"provider_usage:{provider}:{kind}",)).fetchone()
    if saved:
        result.update(loads(saved["value"]))
    if provider=='discogs':result['cache_ttl']=min(result.get('cache_ttl',0),6*3600)
    result["policy_version"] = digest({key: value for key, value in result.items() if key != "policy_version"})
    return result


def get_rule(settings, provider, kind):
    with db.connect(settings.db_path) as conn:
        return rule(conn, provider, kind)


def check(settings, provider, kind, operation):
    permission = get_rule(settings, provider, kind)
    if not permission.get("allow_" + operation):
        policy_denied(provider,operation)
        raise media.MediaError("provider_policy_denied", f"Политика {provider}/{kind}: операция {operation} не разрешена")
    return permission


def field_values(payload):
    result = {str(key): [str(item) for item in value] if isinstance(value, list) else [str(value)]
              for key, value in payload.get("tags", {}).items()}
    if payload.get("lrc") is not None or payload.get('plain_lyrics'):
        # Time-only edits retain the same content fingerprint and restrictions.
        text = re.sub(r"\[\d+:\d+(?:[.,]\d+)?\]", "", str(payload.get('lrc') or payload.get('plain_lyrics') or ''))
        result["lrc"] = " ".join(text.split())
    if payload.get("cover"):
        try:
            result["cover"] = hashlib.sha256(base64.b64decode(payload["cover"].split(",", 1)[1], validate=True)).hexdigest()
        except (ValueError, IndexError):
            result["cover"] = digest(payload["cover"])
    return result


def candidate_usage(conn, candidate, now=None):
    provider = candidate.get("provider", "unknown")
    rights = rule(conn, provider, data_type(candidate))
    return {**rights, "origin": "owner_supplied" if provider in LOCAL else "provider_acquired",
            "acquired_at": now if now is not None else time.time(), "source_url": candidate.get("source_url", ""),
            "candidate_id": candidate.get("id", ""), "license_verified": False}


def register_values(conn, candidate, usage):
    for name, value in field_values(candidate).items():
        value_hash = digest(value)
        key = "usage_value:" + name + ":" + value_hash
        previous = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        entries = loads(previous["value"]).get("entries", []) if previous else []
        reference = {**usage, "value_sha256": value_hash}
        entries = [entry for entry in entries if (entry.get("provider"), entry.get("policy_version")) != (usage["provider"], usage["policy_version"])]
        entries.append(reference)
        conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, db.encode({"entries": entries})))


def _allowed(conn, usage, operation):
    if any(not _allowed(conn,parent,operation) for parent in usage.get('inherited_restrictions',[]) if isinstance(parent,dict)):
        return False
    if usage.get("origin") in {"preexisting", "owner_supplied"}:
        return True
    provider, kind = usage.get("provider", "unknown"), usage.get("data_type", "metadata")
    current = rule(conn, provider, kind)
    # An acquired restriction cannot disappear by copying the value into a
    # manual form or by editing the current provider defaults.
    acquired = usage.get("allow_" + operation, current.get("allow_" + operation, False))
    if operation == 'cache' and not usage.get('embedded_media'):
        ttl = min(usage.get('cache_ttl', current.get('cache_ttl', 0)), current.get('cache_ttl', 0))
        if ttl <= 0 or time.time() > usage.get('acquired_at', time.time()) + ttl:
            return False
    return bool(acquired and current.get("allow_" + operation))


def require_sources(conn, sources, operation):
    for usage in sources:
        if not isinstance(usage, dict):
            continue
        if not _allowed(conn, usage, operation):
            policy_denied(usage.get('provider','unknown'),operation)
            raise media.MediaError("provider_policy_denied", f"{usage.get('provider', 'unknown')}/{usage.get('data_type', 'metadata')}: операция {operation} запрещена политикой источника")


def references(payload):
    values = []
    for source in payload.get("field_sources", {}).values():
        if isinstance(source, dict):
            if source.get("usage"):
                values.extend(source["usage"] if isinstance(source["usage"], list) else [source["usage"]])
            elif source.get("origin") or source.get("provider"):
                values.append(source)
    for candidate in payload.get("sources", []):
        if isinstance(candidate, dict):
            usage = candidate.get("usage")
            if isinstance(usage, dict):
                values.append(usage)
            elif candidate.get("provider"):
                values.append({"origin": "provider_acquired", "provider": candidate["provider"], "data_type": data_type(candidate)})
    return values


def decorate_manual(conn, row, payload):
    """Keep known restrictions when values are copied or owner-edited."""
    payload = deepcopy(payload)
    payload['sources']=[{key:value for key,value in source.items() if key not in CONTENT_FIELDS} for source in payload.get('sources',[]) if isinstance(source,dict)]
    fields = payload.setdefault("field_sources", {})
    meta = loads(row["metadata"])
    previous = conn.execute("SELECT payload FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (row["id"],)).fetchone()
    known = loads(previous["payload"]).get("field_sources", {}) if previous else {}
    known = {**meta.get("usage_provenance", {}), **known}
    embedded = field_values({"tags": meta.get("tags", {}), "lrc": meta.get("lrc", "")})
    for name, value in field_values(payload).items():
        if fields.get(name, {}).get("usage"):
            continue
        previous_field = known.get(name, {})
        if previous_field.get("usage"):
            fields[name] = {**previous_field, "owner_edited": True}
            continue
        lookup = conn.execute("SELECT value FROM settings WHERE key=?", ("usage_value:" + name + ":" + digest(value),)).fetchone()
        usages = loads(lookup["value"]).get("entries", []) if lookup else []
        if usages:
            fields[name] = {**fields.get(name, {}), "origin": "provider_acquired", "usage": usages}
        elif fields.get(name, {}).get("provider") and fields[name]["provider"] not in LOCAL:
            provider = fields[name]["provider"]
            fields[name]["usage"] = {**rule(conn, provider, "lyrics" if name == "lrc" else "cover" if name == "cover" else "metadata"), "origin": "provider_acquired", "license_verified": False}
        else:
            fields[name] = {**fields.get(name, {}), "origin": "preexisting" if embedded.get(name) == value else "owner_supplied",
                            "license_verified": False, "rights_status": "unknown", "value_sha256": digest(value)}
    return payload


def require_payload(conn, row, payload, operation):
    decorated = decorate_manual(conn, row, payload)
    require_sources(conn, references(decorated), operation)
    if operation in {"embed", "derived_analysis"}:
        # A stored proposal is itself a cache of provider content.
        require_sources(conn, references(decorated), "cache")
    return decorated


def api_payload(conn, row, payload, operation='cache'):
    try:
        return require_payload(conn, row, payload, operation)
    except media.MediaError as exc:
        raise security.error(409, exc.code, exc.message) from exc


def require_track(settings, row, operation, *, conn=None):
    if conn is None:
        with db.connect(settings.db_path) as active:
            return require_track(settings,row,operation,conn=active)
    meta = loads(row["metadata"])
    require_sources(conn, media_sources(meta.get("usage_provenance", {})), operation)
    for source in meta.get('cue', {}).get('files', []):
        if source.get('relative_path'):
            image=conn.execute('SELECT metadata FROM tracks WHERE relative_path=? AND is_current=1',(source['relative_path'],)).fetchone()
            if image:
                require_sources(conn,media_sources(loads(image['metadata']).get('usage_provenance',{})),operation)


def media_sources(provenance):
    """Response-cache TTL does not expire an already permitted embedded file.

    Revoked cache/embed/export flags still apply to new copies/operations. This
    distinction avoids making an old, legitimately embedded CC0 tag uneditable.
    """
    def mark(usage):
        return {**usage,'embedded_media':True,'inherited_restrictions':[mark(parent) for parent in usage.get('inherited_restrictions',[])]}
    return [mark(usage) for usage in references({'field_sources':provenance})]


def require_export_entry(settings, entry):
    with db.connect(settings.db_path) as conn:
        require_sources(conn,references({'field_sources':entry.get('usage_provenance',{})}),'export')


def require_backup(settings):
    """Existing restricted artifacts are retained, but not copied to a new backup."""
    with db.connect(settings.db_path) as conn:
        require_backup_connection(conn)


def require_backup_connection(conn):
    for row in conn.execute('SELECT metadata FROM tracks'):
        require_sources(conn,media_sources(loads(row['metadata']).get('usage_provenance',{})),'cache')
    for row in conn.execute('SELECT details FROM history'):
        details=loads(row['details'])
        for name in ('usage_before','usage_after'):
            require_sources(conn,media_sources(details.get(name,{})),'cache')


def redact_payload(conn, original):
    payload=deepcopy(original);denied=[]
    payload['sources']=[{key:value for key,value in source.items() if key not in CONTENT_FIELDS} for source in payload.get('sources',[]) if isinstance(source,dict)]
    for field,source in payload.get('field_sources',{}).items():
        if any(not _allowed(conn,usage,'cache') for usage in references({'field_sources':{field:source}})):
            denied.append(field)
    if not payload.get('field_sources'):
        try:require_sources(conn,references(payload),'cache')
        except media.MediaError:denied=list(payload.get('tags',{}))+['lrc','cover']
    for field in denied:
        if field in {'lrc','cover'}:payload.pop(field,None)
        else:payload.get('tags',{}).pop(field,None)
    if 'lrc' in denied:payload.pop('lyrics_document',None)
    if denied:payload['policy_omitted_fields']=denied
    return payload


def sanitize_backup(conn):
    """Work only on the backup database copy, before its VACUUM."""
    for row in conn.execute('SELECT track_id,payload FROM analyses').fetchall():
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?',(db.encode(prepare_analysis(conn,loads(row['payload']),register=False)),row['track_id']))
    for row in conn.execute('SELECT id,payload FROM proposals').fetchall():
        conn.execute('UPDATE proposals SET payload=? WHERE id=?',(db.encode(redact_payload(conn,loads(row['payload']))),row['id']))
    for row in conn.execute('SELECT id,payload FROM jobs').fetchall():
        payload=loads(row['payload'])
        for item in payload.get('items',[]):
            if item.get('proposal'):item['proposal']=redact_payload(conn,item['proposal'])
            if item.get('reference_text'):
                reference=redact_payload(conn,{'lrc':item['reference_text'],'field_sources':item.get('reference_sources',{})})
                if 'lrc' not in reference:item.pop('reference_text',None)
        conn.execute('UPDATE jobs SET payload=? WHERE id=?',(db.encode(payload),row['id']))
    _clean_release_reviews(conn)


def _clean_release_reviews(conn):
    from . import release_review
    for row in conn.execute("SELECT key,value FROM settings WHERE key LIKE 'release_review:%'").fetchall():
        original=loads(row['value']);cleaned=release_review.redact(conn,original)
        if original!=cleaned:conn.execute('UPDATE settings SET value=? WHERE key=?',(db.encode(cleaned),row['key']))


CONTENT_FIELDS = {"tags", "lrc", "plain_lyrics", "cover", "title", "artist", "album", "release", "releases"}


def prepare_analysis(conn, data, *, register=True, now=None):
    data = deepcopy(data)
    now = time.time() if now is None else now
    candidates = []
    for candidate in data.get("candidates", []):
        usage = candidate.get("usage") or candidate_usage(conn, candidate, now)
        candidate["usage"] = usage
        if register:
            register_values(conn, candidate, usage)
        ttl = min(usage.get("cache_ttl", 0), rule(conn, usage["provider"], usage["data_type"]).get("cache_ttl", 0))
        cache_allowed = _allowed(conn, usage, "cache") and ttl > 0 and now <= usage.get("acquired_at", now) + ttl
        if not cache_allowed and usage.get("origin") == "provider_acquired":
            candidate = {key: value for key, value in candidate.items() if key not in CONTENT_FIELDS}
            candidate["transient_only"] = True
            candidate["policy_review_required"] = True
        candidates.append(candidate)
    data["candidates"] = candidates
    if any(not _allowed(conn,usage,'cache') for usage in data.get('alignment_usage',[]) if isinstance(usage,dict)):
        data.pop('alignment',None)
    # A fingerprint-derived query also contains provider content. It is not an
    # alternate persistent store for a result whose cache permission is absent.
    if data.get("query_source") == "audio_fingerprint" and any(c.get("provider") == "shazam" and c.get("transient_only") for c in candidates):
        data.pop("query", None)
    return data


def public_analysis(settings, data):
    with db.connect(settings.db_path) as conn:
        return prepare_analysis(conn, data, register=False)


def cleanup_cache(settings):
    """Forget expired/transient provider responses; never delete music or backups."""
    changed = 0
    with db.transaction(settings.db_path) as conn:
        conn.execute("DELETE FROM provider_cache WHERE expires<=?", (time.time(),))
        for provider in TERMS:
            if not any(rule(conn, provider, kind).get("allow_cache") for kind in DATA_TYPES):
                conn.execute("DELETE FROM provider_cache WHERE provider=?", (provider,))
        for row in conn.execute("SELECT track_id,payload FROM analyses").fetchall():
            original = loads(row["payload"])
            cleaned = prepare_analysis(conn, original, register=True)
            if cleaned != original:
                conn.execute("UPDATE analyses SET payload=? WHERE track_id=?", (db.encode(cleaned), row["track_id"]))
                changed += 1
        for row in conn.execute('SELECT id,payload FROM proposals').fetchall():
            original=loads(row['payload']);cleaned=redact_payload(conn,original)
            if original!=cleaned:conn.execute('UPDATE proposals SET payload=? WHERE id=?',(db.encode(cleaned),row['id']))
        for row in conn.execute("SELECT id,payload,state,completed FROM jobs WHERE state!='running'").fetchall():
            original=loads(row['payload']);cleaned=deepcopy(original);omitted=False
            for item in cleaned.get('items',[]):
                if item.get('proposal'):
                    item['proposal']=redact_payload(conn,item['proposal']);omitted=omitted or bool(item['proposal'].get('policy_omitted_fields'))
                if item.get('reference_text'):
                    reference=redact_payload(conn,{'lrc':item['reference_text'],'field_sources':item.get('reference_sources',{})})
                    if 'lrc' not in reference:item.pop('reference_text',None);omitted=True
            if original!=cleaned:
                conn.execute('UPDATE jobs SET payload=? WHERE id=?',(db.encode(cleaned),row['id']))
                if omitted and row['state'] in {'queued','paused'}:
                    conn.execute("UPDATE jobs SET state=?,stage='cancelled',cancel_requested=1,error='provider_policy_expired',updated_at=? WHERE id=?",('cancelled_partial' if row['completed'] else 'cancelled',db.utcnow(),row['id']))
                    db.event(conn,'job_cancel_requested',{'job_id':row['id'],'code':'provider_policy_expired'})
        _clean_release_reviews(conn)
    return {"analyses_cleaned": changed, "music_deleted": False, "rollback_versions_deleted": False}


def provenance_report(settings):
    result = []
    with db.connect(settings.db_path) as conn:
        for row in conn.execute("SELECT id,relative_path,metadata FROM tracks WHERE is_current=1 ORDER BY relative_path LIMIT 10000"):
            meta = loads(row["metadata"])
            provenance = deepcopy(meta.get("usage_provenance", {}))
            for field in [*meta.get('tags',{}),*(['lrc'] if meta.get('lrc') or meta.get('lyrics') else []),*(['cover'] if meta.get('has_art') else [])]:
                provenance.setdefault(field,{'origin':'preexisting','rights_status':'unknown','license_verified':False})
            findings = []
            for field, source in provenance.items():
                for usage in references({"field_sources": {field: source}}):
                    denied = [operation for operation in ("embed", "export", "derived_analysis", "cache") if not _allowed(conn, usage, operation)]
                    if denied:
                        findings.append({"field": field, "provider": usage.get("provider"), "denied_operations": denied})
            pending=conn.execute('SELECT payload FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(row['id'],)).fetchone()
            pending_rights=references(loads(pending['payload'])) if pending else []
            pending_findings=[{'provider':usage.get('provider'),'data_type':usage.get('data_type','metadata'),'denied_operations':[operation for operation in OPERATIONS if not _allowed(conn,usage,operation)]} for usage in pending_rights]
            result.append({"track_id": row["id"], "relative_path": row["relative_path"], "field_sources": provenance,
                           "fallback_origin": "preexisting", "fallback_rights_status": "unknown", "license_verified": False, "findings": findings})
            result[-1]['pending_proposal_findings']=pending_findings
    return {"generated_at": db.utcnow(), "profile": "personal_collection", "files": result,
            "music_deleted": False, "rollback_versions_deleted": False, "limit": 10000}


class UsageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_type: str = Field(pattern="^(metadata|lyrics|cover|provenance)$")
    allow_fetch: bool
    allow_cache: bool
    allow_embed: bool
    allow_export: bool
    allow_derived_analysis: bool
    cache_ttl: int = Field(ge=0, le=30 * 86400)
    basis: str = Field(min_length=5, max_length=2000)
    terms_url: str = Field(default="", max_length=500)
    attribution: str = Field(default="", max_length=500)
    base_version: str = Field(min_length=64,max_length=64)


def install(app, settings):
    @app.get("/api/provider-policy")
    def status(request: Request):
        security.require_session(request, settings)
        with db.connect(settings.db_path) as conn:
            providers = [{"id": provider, "rules": [rule(conn, provider, kind) for kind in sorted(DATA_TYPES)]} for provider in TERMS]
        return {"profile": "personal_collection", "providers": providers, "operations": OPERATIONS,
                "notice": "Доступ к API и согласие на передачу данных не подтверждают лицензию на текст или обложку. Новые неизвестные операции запрещены; существующие файлы сохраняются."}

    @app.put("/api/provider-policy/{provider}")
    def update(request: Request, provider: str, body: UsageInput):
        active = security.require_session(request, settings, True)
        if active.get("api_token_id"):
            raise security.error(403, "owner_session_required", "Права использования подтверждает владелец в браузере")
        if provider not in TERMS:
            raise security.error(422, "unknown_provider", "Неизвестный музыкальный источник")
        if body.terms_url and (not body.terms_url.startswith("https://") or "@" in body.terms_url):
            raise security.error(422, "invalid_policy_url", "Укажите HTTPS-ссылку на условия без учетных данных")
        if body.allow_cache and not body.cache_ttl:
            raise security.error(422, "cache_ttl_required", "Для кеша укажите положительный срок хранения")
        payload = body.model_dump()
        payload.pop('base_version')
        payload.update(rights_status="owner_asserted", checked_at=db.utcnow(), license_verified=False)
        with db.transaction(settings.db_path) as conn:
            if body.base_version!=rule(conn,provider,body.data_type)['policy_version']:
                raise security.error(412,'policy_conflict','Политика изменилась в другой сессии. Обновите форму')
            conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (f"provider_usage:{provider}:{body.data_type}", db.encode(payload)))
            # Existing cached responses belong to a previous permission version.
            conn.execute("DELETE FROM provider_cache WHERE provider=?", (provider,))
            db.event(conn, "provider_policy_changed", {"provider": provider, "data_type": body.data_type})
        cleanup_cache(settings)
        return {"rule": get_rule(settings, provider, body.data_type), "report": provenance_report(settings)}

    @app.get("/api/reports/provenance")
    def report(request: Request):
        security.require_session(request, settings)
        return provenance_report(settings)

    @app.post("/api/tracks/{track_id}/provider-preview")
    async def preview(request: Request, track_id: str):
        security.require_session(request, settings, True)
        try:body = await request.json()
        except ValueError:raise security.error(422,'invalid_preview','Ожидается JSON с candidate_id')
        identity = body.get("candidate_id") if isinstance(body, dict) else None
        row = db.fetchone(settings.db_path, "SELECT * FROM tracks WHERE id=? AND is_current=1", (track_id,))
        analysis = db.fetchone(settings.db_path, "SELECT * FROM analyses WHERE track_id=?", (track_id,))
        if not row or not analysis or analysis["input_sha256"] != row["sha256"]:
            raise security.error(409, "analysis_stale", "Сначала обновите поиск для текущей версии файла")
        candidate = next((candidate for candidate in loads(analysis["payload"]).get("candidates", []) if candidate.get("id") == identity), None)
        if not candidate or candidate.get("provider") not in {"lrclib", "coverartarchive"} or not settings.external_metadata:
            raise security.error(409, "preview_unavailable", "Предпросмотр этого источника недоступен")
        from .providers import Providers,ProviderError
        providers = Providers(settings)
        providers.transient=True
        try:
            if candidate["provider"] == "lrclib" and re.fullmatch(r"lrclib:\d{1,20}", identity or ""):
                doc = await providers.get("lrclib", "https://lrclib.net/api/get/" + identity.split(":", 1)[1])
                result = {"lrc": (doc or {}).get("syncedLyrics", ""), "plain_lyrics": (doc or {}).get("plainLyrics", "")}
            elif candidate["provider"] == "coverartarchive" and re.fullmatch(r"caa:[0-9a-fA-F-]{36}", identity or ""):
                result = await providers.cover(identity.split(":", 1)[1]) or {}
            else:
                raise security.error(409, "preview_unavailable", "Некорректный идентификатор источника")
            return {**result, "transient_only": True, "notice": "Предпросмотр в памяти. Для сохранения, вшивания и экспорта нужны отдельные разрешения источника."}
        except ProviderError as exc:
            raise security.error(409,exc.code,str(exc)) from exc
        finally:
            await providers.close()
