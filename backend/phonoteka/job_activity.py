"""Owner-facing job activity derived from durable events, without worker side effects.

Only an explicit projection of the event log is exposed. Provider responses, job
payloads, arbitrary error messages and paths of backups never enter this API.
"""

from datetime import datetime
import json
import math
import re
import time

from . import db, processing_activity


EVENT_KINDS = {
    "job_action", "job_stage",
    "job_created", "job_started", "job_progress", "job_finished", "job_pause",
    "job_paused", "job_resume", "job_waiting", "job_cancel_requested", "job_authorization_required", "job_reauthorized",
    "album_loudness_measured",
}
TERMINAL_ITEMS = {"succeeded", "failed", "conflict", "needs_review"}
TERMINAL_JOBS = {
    "succeeded", "failed", "completed_with_errors", "cancelled", "cancelled_partial", "interrupted", "conflict", "needs_review",
}
ITEM_STATES = TERMINAL_ITEMS | {"running", "deferred", "interrupted"}
ACTIONS = {
    "scan": "Сканирование файлов", "audit": "Проверка аудио",
    "analyze": "Поиск тегов, обложки и текста", "recognize": "Распознавание музыки",
    "transcribe": "Распознавание речи", "align": "Синхронизация текста с аудио",
    "apply": "Запись тегов и обложки", "rollback": "Восстановление сохраненной версии",
    "publish": "Публикация готовых файлов", "extract_cue": "Разделение CUE на треки",
    "rollback_ready": "Откат опубликованной версии",
    "replaygain": "Измерение громкости", "replaygain_album": "Измерение громкости альбома",
    "export_ready": "Подготовка экспорта", "normalize_audio": "Создание редактируемой копии",
    "youtube": "Поиск исходного видео YouTube",
    "release_audit": "Сверка выбранного релиза MusicBrainz",
    "publish_sidecars": "Публикация LRC и обложек рядом с аудио",
}
PHASES = {
    'preflight':'Проверка версии файла и разрешений', 'fingerprint_sample':'Подготовка аудиоотпечатка',
    'fingerprint_lookup':'Поиск совпадения Shazam', 'metadata_lookup':'Запрос тегов, обложки и текста',
    'decode':'Проверка декодирования и сигнала', 'write_tags':'Подготовка резервной версии и запись тегов',
    'commit':'Проверка и атомарная фиксация файла', 'rollback':'Восстановление сохраненной версии',
    'publication':'Подготовка и проверка Ready', 'cue_extract':'Извлечение композиций CUE',
    'asr':'Ожидание локального ASR', 'align':'Сопоставление строк с аудио',
    'normalize':'Создание редактируемой аудиокопии',
}
SAFE_CODES = {
    "conflict", "revision_conflict", "needs_review", "invalid_lyrics", "unsupported_format",
    "authorization_revoked", "provider_unavailable", "rate_limited", "budget_exhausted",
    "cancelled", "storage_unavailable", "storage_error", "decode_failed", "internal_error",
    "partial_scan", "recovery_required", "read_only", "input_changed", "invalid_audio",
    "inference_unavailable", "inference_timeout", "file_unavailable", "root_identity_changed",
    "insufficient_space",
    "provider_policy_expired", "provider_policy_denied",
    "recognition_attempt_budget_exhausted", "recognition_identity_required",
}


def _object(value):
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _number(value, default=0):
    return max(0, value) if type(value) is int else default


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError):
        return None


def _filename(value):
    # Windows separators can occur in scan keys even when the server is Linux.
    value = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[\x00-\x1f\x7f]", "", value)[:240]


def event_rows(conn, job_ids):
    """The durable log is already capped at 10,000 events / one day by db.event."""
    if not job_ids:
        return []
    placeholders = ",".join("?" for _ in job_ids)
    kinds = ",".join("?" for _ in EVENT_KINDS)
    rows = conn.execute(
        f"SELECT id,kind,payload,created_at FROM events WHERE kind IN ({kinds}) "
        f"AND json_valid(payload) AND json_extract(payload,'$.job_id') IN ({placeholders}) "
        "ORDER BY id DESC LIMIT 10000", (*sorted(EVENT_KINDS), *job_ids),
    ).fetchall()
    return [{**dict(row), "data": _object(row["payload"])} for row in reversed(rows)]


def estimate(row, events, now=None):
    """Estimate the remaining attempts from this uninterrupted execution segment.

    Starting/resuming a worker segment discards the old sample. Queue/provider
    waits and pause requests never contribute to its duration. Missing/pruned
    segment boundaries deliberately produce an unknown ETA.
    """
    now = time.time() if now is None else now
    total = _number(row["total"])
    processed = min(total, _number(row["completed"]) + _number(row["failed"])) if total else 0
    remaining = max(0, total - processed)
    payload = _object(row["payload"])
    result = {
        "processed": processed, "remaining": remaining, "progress_basis": "attempted_items",
        "progress": round(100 * processed / total) if total else 0,
        "eta_seconds": None, "eta_reason": "insufficient_samples", "recent_rate": None,
    }
    if row["state"] in TERMINAL_JOBS:
        result["eta_reason"] = "finished"
        if row["state"] == "succeeded" and total and not remaining:
            result["eta_seconds"] = 0
        return result
    if row["cancel_requested"]:
        result["eta_reason"] = "cancelling"
        return result
    if row['state']=='waiting_authorization' or row['stage']=='waiting_authorization':
        result['eta_reason']='waiting_authorization'
        return result
    if payload.get("pause_requested") or row["state"] == "paused":
        result["eta_reason"] = "paused"
        return result
    if row["stage"] in {"waiting_provider","waiting_storage"}:
        result["eta_reason"] = row["stage"]
        return result
    if row["state"] != "running":
        result["eta_reason"] = "queued"
        return result
    if not total:
        result["eta_reason"] = "unknown_total"
        return result
    if not remaining:
        result["eta_reason"] = "finalizing"
        return result
    start = None
    baseline = 0
    # A retained start can follow pruned progress from an earlier segment. Do
    # not assume that the first observed completion began at count zero.
    last_count = None
    samples = []
    for event in events:
        data = event["data"]
        stamp = _timestamp(event["created_at"])
        kind = event["kind"]
        if kind == "job_created":
            last_count = 0
        elif kind == "job_started":
            start, baseline, samples = stamp, last_count, []
        elif kind in {"job_paused", "job_waiting", "job_resume", "job_finished", "job_authorization_required", "job_reauthorized"}:
            start, samples = None, []
        elif kind == "job_progress" and isinstance(data.get("state"), str) and data["state"] in TERMINAL_ITEMS:
            count = _number(data.get("completed")) + _number(data.get("failed"))
            if start is not None and stamp is not None and stamp >= start:
                if baseline is None:
                    start, baseline = stamp, count
                    samples = [(stamp, count)]
                elif last_count is None or count > last_count:
                    samples.append((stamp, count))
            last_count = count
    if start is None or len(samples) < 2:
        return result
    # A bounded recent sample follows changes in codec/provider/file duration.
    if len(samples) > 20:
        start, baseline = samples[-21]
        samples = samples[-20:]
    end, count = samples[-1]
    elapsed, attempts = end - start, count - baseline
    if elapsed < 2 or attempts < 2 or now < end:
        return result
    seconds_per_item = elapsed / attempts
    # A stuck request is not a promise that a previously observed rate continues.
    if now - end > max(60, seconds_per_item * 5):
        result["eta_reason"] = "no_recent_progress"
        return result
    result.update(eta_seconds=math.ceil(remaining * seconds_per_item), eta_reason="estimated",
                  recent_rate=round(attempts / elapsed, 6))
    return result


def action_for(row):
    payload = _object(row["payload"])
    if row["state"] in TERMINAL_JOBS:
        return {
            "succeeded": "Обработка завершена", "failed": "Обработка завершилась ошибкой",
            "completed_with_errors": "Обработка завершена с ошибками",
            "cancelled": "Обработка отменена", "cancelled_partial": "Обработка частично выполнена и отменена",
            "interrupted": "Обработка прервана, требуется проверка",
            "conflict": "Конфликт версий, требуется решение владельца",
            "needs_review": "Ожидается проверка владельцем",
        }[row["state"]]
    if row["cancel_requested"]:
        return "Завершение текущих операций перед отменой"
    if row['state']=='waiting_authorization':
        return 'Ожидание явного подтверждения прав владельцем'
    if row['stage']=='waiting_authorization':
        return 'Завершение начатых операций перед ожиданием прав'
    if row["state"] == "paused":
        return "Обработка приостановлена"
    if payload.get("pause_requested"):
        return "Завершение текущих файлов перед паузой"
    if row["stage"] == "waiting_storage":
        return "Ожидание свободного места в хранилище"
    if row["stage"] == "waiting_provider":
        return "Ожидание доступности музыкального сервиса"
    if row["state"] != "running":
        return "Ожидание запуска в очереди"
    return ACTIONS.get(row["kind"], "Обработка файлов")


def summaries(settings, rows):
    """One bounded event read for a page of jobs, rather than a query per job."""
    ids = [row["id"] for row in rows]
    if not ids:
        return {}
    with db.connect(settings.db_path) as conn:
        events = event_rows(conn, ids)
        placeholders = ",".join("?" for _ in ids)
        active = {r["job_id"]: r["n"] for r in conn.execute(
            f"SELECT job_id,COUNT(*) AS n FROM job_items WHERE state='running' AND job_id IN ({placeholders}) GROUP BY job_id", ids,
        )}
    grouped = {identity: [] for identity in ids}
    for event in events:
        grouped[event["data"]["job_id"]].append(event)
    now = time.time()
    return {row["id"]: {
        **estimate(row, grouped[row["id"]], now), "current_action": action_for(row),
        "active_count": active.get(row["id"], 0) if row["state"] == "running" else 0,
    } for row in rows}


def safe_event(event, row, tracks, results):
    data = event["data"]
    result = {"id": event["id"], "kind": event["kind"], "created_at": event["created_at"]}
    key = data.get("track_id")
    track = tracks.get(key) if isinstance(key, str) else None
    if track:
        result.update(track_id=key, filename=_filename(track["relative_path"]))
    elif row["kind"] == "scan" and isinstance(key, str):
        result["filename"] = _filename(key)
    state = data.get("state")
    if isinstance(state, str) and state in ITEM_STATES | TERMINAL_JOBS:
        result["state"] = state
    else:
        state = None
    for field in ("completed", "failed"):
        if type(data.get(field)) is int:
            result[field] = _number(data[field])
    retry_at = data.get("retry_at")
    if type(retry_at) in {int, float} and math.isfinite(retry_at) and retry_at > 0:
        result["retry_at"] = retry_at
    if event['kind']=='job_stage':
        phase=data.get('stage') if isinstance(data.get('stage'),str) else ''
        result['message']=PHASES.get(phase, {'provider_http':'Запрос внешнего сервиса', 'provider_lookup':'Обращение к источнику', 'provider_admission':'Ожидание допуска к сервису', 'file':'Попытка обработки файла'}.get(phase,'Этап обработки'))
        if data.get('provider') in processing_activity.PROVIDERS: result['provider']=data['provider']
        if type(data.get('duration_ms')) in (int,float) and 0 <= data['duration_ms'] < 1e12: result['duration_ms']=data['duration_ms']
        if type(data.get('http_status')) is int and 100 <= data['http_status'] <= 599: result['http_status']=data['http_status']
    elif event['kind']=='job_action':
        phase=data.get('action')
        result['message']=PHASES.get(phase,'Действие обработки') if isinstance(phase,str) else 'Действие обработки'
    elif event["kind"] == "job_progress":
        result["message"] = {
            "running": ACTIONS.get(row["kind"], "Обработка файла"), "succeeded": "Файл обработан",
            "failed": "Ошибка обработки файла", "conflict": "Файл изменился, требуется проверка",
            "needs_review": "Требуется проверка файла", "deferred": "Файл ожидает повторной попытки",
            "interrupted": "Обработка файла прервана",
        }.get(state, "Обновление прогресса")
        detail = results.get(key, {}) if isinstance(key, str) else {}
        code = detail.get("code")
        if state in {"failed", "conflict", "needs_review", "interrupted"} and code:
            result["code"] = code if isinstance(code, str) and code in SAFE_CODES else "processing_error"
    else:
        result["message"] = {
            "job_created": "Задача добавлена в очередь", "job_started": "Задача запущена",
            "job_finished": "Задача завершена", "job_pause": "Запрошена пауза",
            "job_paused": "Задача приостановлена", "job_resume": "Задача возвращена в очередь",
            "job_waiting": "Ожидание свободного места" if data.get("reason")=="storage" else "Ожидание сервиса перед повторной попыткой",
            "job_cancel_requested": "Запрошена отмена", "album_loudness_measured": "Громкость альбома измерена",
            "job_authorization_required": "Ожидается подтверждение прав владельцем",
            "job_reauthorized": "Владелец подтвердил новые ограниченные права задания",
        }.get(event["kind"], "Состояние задачи изменилось")
        if event['kind']=='job_cancel_requested' and data.get('code')=='provider_policy_expired':
            result['message']='Задача отменена: срок хранения или разрешение источника истекли'
            result['code']='provider_policy_expired'
    return result


def snapshot(settings, row, limit=80, after=0):
    with db.connect(settings.db_path) as conn:
        conn.execute("BEGIN")
        boundary = conn.execute("SELECT MIN(id) AS first,MAX(id) AS last FROM events").fetchone()
        last = boundary["last"] or 0
        reset = bool(after and ((boundary["first"] and after < boundary["first"] - 1) or after > last))
        events = event_rows(conn, [row["id"]])
        rows = conn.execute(
            "SELECT ji.item_key,ji.state,ji.result,t.id,t.relative_path FROM job_items ji "
            "LEFT JOIN tracks t ON t.id=ji.item_key WHERE ji.job_id=? ORDER BY ji.item_key LIMIT 10000", (row["id"],),
        ).fetchall()
        keys = {event["data"].get("track_id") for event in events if isinstance(event["data"].get("track_id"), str)}
        tracks = {item["id"]: dict(item) for item in rows if item["id"]}
        # Scanning uses a relative path as item key, normal jobs use a track UUID.
        # Track names still resolve for an item whose running event precedes its result.
        missing = keys - tracks.keys()
        if missing:
            for offset in range(0, len(missing), 500):
                subset = list(missing)[offset:offset + 500]
                placeholders = ",".join("?" for _ in subset)
                tracks.update({item["id"]: dict(item) for item in conn.execute(
                    f"SELECT id,relative_path FROM tracks WHERE id IN ({placeholders})", subset,
                )})
    selected = [event for event in events if not after or reset or event["id"] > after]
    if not after or reset:
        selected = selected[-limit:]
        has_more = False
    else:
        has_more = len(selected) > limit
        selected = selected[:limit]
    cursor = selected[-1]["id"] if has_more else last
    results = {item["item_key"]: _object(item["result"]) for item in rows}
    starts = {event["data"]["track_id"]: event["created_at"] for event in events
              if event["kind"] == "job_progress" and event["data"].get("state") == "running"
              and isinstance(event["data"].get("track_id"), str)}
    phases={}
    for event in events:
        key=event['data'].get('track_id')
        if not isinstance(key,str):continue
        if event['kind']=='job_progress' and event['data'].get('state')=='running':
            phases.pop(key,None)
        phase=event['data'].get('action')
        if event['kind']=='job_action' and isinstance(phase,str) and phase in PHASES:
            phases[key]=PHASES[phase]
    active = []
    if row["state"] == "running":
        for item in rows:
            if item["state"] == "running":
                active.append({
                    "track_id": item["id"], "filename": _filename(item["relative_path"] or item["item_key"]),
                    "action": phases.get(item['item_key'],ACTIONS.get(row["kind"], "Обработка файла")),
                    "started_at": starts.get(item["item_key"]),
                })
    heartbeat = _object(db.setting(settings.db_path, 'worker_heartbeat', '{}'))
    measurements = processing_activity.project(events, rows, heartbeat, time.time(), job_running=row['state']=='running')
    for measured in measurements['stage_history']:
        track = tracks.get(measured['track_id'])
        measured['filename'] = _filename(track['relative_path']) if track else None
    waits = []
    for item in rows:
        if item['state'] != 'deferred': continue
        detail = results.get(item['item_key'], {})
        for source in detail.get('sources', []) if isinstance(detail.get('sources', []), list) else []:
            if not isinstance(source, dict) or source.get('provider') not in processing_activity.PROVIDERS: continue
            if not isinstance(source.get('state'),str) or source['state'] not in {'rate_limited','provider_cooldown','provider_unavailable','budget_exhausted'}: continue
            retry = source.get('retry_at', detail.get('retry_at'))
            retry = retry if type(retry) in (int,float) and math.isfinite(retry) and retry > 0 else None
            waits.append({'track_id': item['id'], 'filename': _filename(item['relative_path']),
                'provider': source['provider'], 'reason_code': source['state'], 'retry_at': retry,
                'overdue_wait': bool(retry is not None and time.time() > retry+30)})
    return {
        **measurements, 'provider_waits': waits[:100], 'provider_waits_truncated': len(waits)>100,
        "summary": {**estimate(row, events), "current_action": action_for(row), "active_count": len(active)},
        "active_items": active[:100], "active_items_truncated": len(active) > 100,
        "events": [safe_event(event, row, tracks, results) for event in selected],
        "cursor": cursor, "resync_required": reset, "has_more": has_more,
    }
