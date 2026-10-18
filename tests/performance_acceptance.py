"""Opt-in API load harness; never collected by pytest and never uses real music.

Run inside a resource-limited, isolated build-01 container, for example:
  python tests/performance_acceptance.py --duration-seconds 900 --samples 1200 \
      --output /reports/performance-15min.json
Use 1800 seconds for the separate sustained-resource observation. Short runs
explicitly remain smoke tests. Temporary state is removed on every exit.
Optional --ready-file signals the first actual resource sample after measurement
starts, for coordinating another isolated harness. It does not change the clock,
sample cadence, load duration or acceptance thresholds, or prove product health.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime, timezone
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import resource
import secrets
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import hashlib
import shutil
import time

import httpx


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def distribution(values):
    return {"samples": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95),
            "p99": percentile(values, .99), "max": max(values) if values else None}


def settings_for(base, port):
    from phonoteka.config import Settings
    return Settings(state_dir=base / "state", work_root=base / "work", source_root=base / "original",
                    publication_root=base / "publication", storage_guard_dir=base / "guard",
                    owner_password_file=base / "owner_password", app_url=f"http://127.0.0.1:{port}",
                    allow_insecure_localhost=True, file_workers=4, io_threads=4,
                    read_only=True, external_metadata=False, external_fingerprints=False,
                    youtube_enabled=False, static_dir=None)


def seed(base, records, port, real_audio=False):
    from phonoteka import db
    settings = settings_for(base, port)
    for name in ("state", "work", "original", "publication", "guard"):
        (base / name).mkdir()
    (base / "owner_password").write_text(secrets.token_urlsafe(32), encoding="utf-8")
    (base / "owner_password").chmod(0o600)
    db.initialize(settings.db_path)
    now = db.utcnow()
    with db.transaction(settings.db_path) as connection:
        connection.executemany(
            "INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ((f"synthetic-{index:06}", f"Album {index // 20:04}/Track {index:06}.flac", f"{index:064x}",
              db.encode({"format": "flac", "duration": 180 + index % 120, "size": 20000000,
                         "write_supported": True, "has_art": index % 3 != 0,
                         "tags": {"title": [f"Песня {index:06}"], "artist": [f"Исполнитель {index % 100:03}"],
                                  "album": [f"Альбом {index // 20:04}"]},
                         "lrc": "[00:01.000]Синтетическая строка" if index % 4 else ""}), now, now)
             for index in range(records)))
        if not real_audio:
            connection.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                               ("synthetic-background", "audit", "running", "processing", "{}", records, now, now))
            db.event(connection, "job_started", {"job_id": "synthetic-background"})
    if real_audio:
        fixture=base/'fixture.flac'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=16000:duration=2','-c:a','flac',str(fixture)],check=True)
        revision=hashlib.sha256(fixture.read_bytes()).hexdigest()
        with db.transaction(settings.db_path) as connection:
            for index in range(16):
                relative=f'Album {index // 20:04}/Track {index:06}.flac'
                path=settings.work_root/relative;path.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(fixture,path)
                row=connection.execute('SELECT metadata FROM tracks WHERE id=?',(f'synthetic-{index:06}',)).fetchone()
                meta=json.loads(row['metadata']);meta.update(sha256=revision,duration=2,size=path.stat().st_size)
                connection.execute('UPDATE tracks SET sha256=?,metadata=? WHERE id=?',(revision,db.encode(meta),f'synthetic-{index:06}'))
        fixture.unlink()
    return settings


def process_sample():
    """Linux /proc observations, including the server's observed high watermark."""
    values = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:", "Threads:")):
            key, value, *_ = line.replace(":", " ").split()
            values[key] = int(value) * (1024 if key != "Threads" else 1)
    descendants = set()
    pending = [os.getpid()]
    child_rss = child_fd = 0
    while pending and len(descendants) < 256:
        parent = pending.pop()
        for child_file in Path(f"/proc/{parent}/task").glob("*/children"):
            with suppress(OSError, ValueError):
                for raw in child_file.read_text().split():
                    identity = int(raw)
                    if identity not in descendants:
                        descendants.add(identity)
                        pending.append(identity)
                        child_fd += len(list(Path(f"/proc/{identity}/fd").iterdir()))
                        for line in Path(f"/proc/{identity}/status").read_text().splitlines():
                            if line.startswith("VmRSS:"):
                                child_rss += int(line.split()[1]) * 1024
    parent_fd = len(list(Path("/proc/self/fd").iterdir()))
    def cgroup_integer(name):
        with suppress(OSError, ValueError):
            return int(Path('/sys/fs/cgroup', name).read_text().strip())
        return None
    return {"at": time.monotonic(), "rss_bytes": values.get("VmRSS"), "rss_high_water_bytes": values.get("VmHWM"),
            "threads": values.get("Threads"), "fd": parent_fd,
            "child_process_count": len(descendants), "child_rss_bytes": child_rss, "child_fd": child_fd,
            "combined_rss_bytes": (values.get("VmRSS") or 0) + child_rss, "combined_fd": parent_fd + child_fd,
            "cgroup_memory_current_bytes": cgroup_integer('memory.current'),
            "cgroup_memory_peak_bytes": cgroup_integer('memory.peak')}


def serve(base, port, fd, interval, real_audio=False):
    """Only this isolated child receives extra instrumentation routes/tasks."""
    import uvicorn
    from phonoteka import db
    from phonoteka.app import create_app
    from phonoteka.telemetry import configure_logging
    configure_logging("performance-acceptance")
    settings = settings_for(base, port)
    app = create_app(settings)
    readings = {"lag_ms": [], "resources": [], "task_counts": [], "background_errors": [], "updates": 0,
                "real_audio": real_audio, "spans": [], "active": 0, "batches": 0, "progress": [], "failed_jobs": []}
    stop = threading.Event()
    allocator = threading.Lock()
    next_item = 0
    records = db.fetchone(settings.db_path, "SELECT COUNT(*) AS count FROM tracks")["count"]
    # Instrument only this disposable server. Durable event timestamps allow
    # actual Worker SSE latency to be measured, without changing product events.
    original_event = db.event
    def observed_event(connection, kind, payload):
        if str(payload.get('job_id', '')).startswith('performance-audit-'):
            payload = {**payload, 'benchmark_emitted_at': time.monotonic()}
        return original_event(connection, kind, payload)
    db.event = observed_event

    def writer(worker):
        nonlocal next_item
        while not stop.wait(interval):
            with allocator:
                if next_item >= records:
                    return
                index = next_item
                next_item += 1
            identity = f"synthetic-{index:06}"
            try:
                with db.transaction(settings.db_path) as connection:
                    connection.execute("INSERT INTO job_items(job_id,item_key,state,result) VALUES(?,?,?,?)",
                                       ("synthetic-background", identity, "done", db.encode({"synthetic": True, "worker": worker})))
                    connection.execute("UPDATE jobs SET completed=completed+1,updated_at=?,heartbeat=? WHERE id=?",
                                       (db.utcnow(), time.time(), "synthetic-background"))
                    count = connection.execute("SELECT completed FROM jobs WHERE id=?", ("synthetic-background",)).fetchone()[0]
                    db.event(connection, "job_progress", {"job_id": "synthetic-background", "track_id": identity,
                             "completed": count, "failed": 0, "state": "done", "benchmark_emitted_at": time.monotonic()})
                with allocator:
                    readings["updates"] += 1
                    readings["progress"].append({'at': time.monotonic(), 'updates': readings['updates']})
            except Exception as error:
                readings["background_errors"].append(type(error).__name__)
                return

    async def monitor():
        resource_at = 0
        while True:
            deadline = time.monotonic() + .05
            await asyncio.sleep(.05)
            now = time.monotonic()
            readings["lag_ms"].append(max(0, now - deadline) * 1000)
            readings["task_counts"].append(len(asyncio.all_tasks()))
            if now >= resource_at:
                readings["resources"].append(await asyncio.to_thread(process_sample))
                resource_at = now + 1

    audio_worker = None
    audio_task = producer_task = None

    async def real_producer():
        nonlocal audio_worker, audio_task
        from phonoteka.worker import Worker
        audio_worker=Worker(settings)
        original=audio_worker.process_item
        def observed(job,item):
            started=time.monotonic();thread_id=threading.get_native_id()
            with allocator:
                readings['active']+=1
                active=readings['active']
            try:return original(job,item)
            finally:
                with allocator:
                    readings['active']-=1
                    readings['spans'].append({'start':started,'end':time.monotonic(),'thread_id':thread_id,'concurrent':active})
        audio_worker.process_item=observed
        audio_task=asyncio.create_task(audio_worker.run(),name='real-four-file-workers')
        try:
            while not stop.is_set():
                if audio_task.done():
                    audio_task.result()
                    raise RuntimeError('Real worker stopped during load')
                pending=await asyncio.to_thread(db.fetchone,settings.db_path,"SELECT COUNT(*) AS count FROM jobs WHERE state IN ('queued','running')")
                if pending['count']==0:
                    def enqueue():
                        with db.transaction(settings.db_path) as connection:
                            rows=connection.execute("SELECT id,relative_path,sha256 FROM tracks ORDER BY relative_path LIMIT 16").fetchall()
                            items=[{'track_id':row['id'],'relative_path':row['relative_path'],'sha256':row['sha256']} for row in rows]
                            identity='performance-audit-'+str(readings['batches'])
                            now=db.utcnow();epoch=int(db.setting(settings.db_path,'auth_epoch','0'))
                            connection.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,'audit','queued','pending',?,?,?,?,?)",
                                (identity,db.encode({'items':items,'params':{}}),len(items),now,now,epoch))
                            db.event(connection,'job_created',{'job_id':identity,'kind':'audit'})
                    await asyncio.to_thread(enqueue)
                    readings['batches']+=1
                stats=await asyncio.to_thread(db.fetchone,settings.db_path,"SELECT COALESCE(SUM(completed),0) AS n,COALESCE(SUM(failed),0) AS failed FROM jobs")
                if readings['updates'] != stats['n']:
                    readings['updates']=stats['n']
                    readings['progress'].append({'at':time.monotonic(),'updates':stats['n']})
                if stats['failed']:
                    def failed_summary():
                        with db.connect(settings.db_path) as connection:
                            rows=connection.execute("SELECT job_id,item_key,state,result FROM job_items WHERE state='failed' LIMIT 16").fetchall()
                            return [dict(row) for row in rows]
                    readings['failed_jobs']=await asyncio.to_thread(failed_summary)
                    raise RuntimeError('Real audio audit failed during load')
                await asyncio.sleep(max(1,interval*4))
        except asyncio.CancelledError:raise
        except Exception as error:readings['background_errors'].append({'type':type(error).__name__,'reason':str(error)[:300]})

    @app.get("/__acceptance/observations", include_in_schema=False)
    async def observations():
        return readings

    class Instrumented:
        async def __call__(self, scope, receive, send):
            if scope["type"] != "lifespan":
                return await app(scope, receive, send)
            monitor_task = None
            pool = None

            async def lifecycle_send(message):
                nonlocal monitor_task, pool, producer_task
                if message["type"] == "lifespan.startup.complete":
                    monitor_task = asyncio.create_task(monitor(), name="acceptance-event-loop-probe")
                    if real_audio:
                        producer_task=asyncio.create_task(real_producer(),name='real-audio-batch-producer')
                    else:
                        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="synthetic-worker")
                        for worker in range(4):pool.submit(writer, worker)
                elif message["type"] in {"lifespan.shutdown.complete", "lifespan.shutdown.failed"}:
                    stop.set()
                    if producer_task:
                        producer_task.cancel()
                        with suppress(asyncio.CancelledError):await producer_task
                    if audio_worker:audio_worker.stopping=True
                    if audio_task:
                        with suppress(asyncio.CancelledError):await audio_task
                    if pool:
                        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
                    if monitor_task:
                        monitor_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await monitor_task
                await send(message)

            return await app(scope, receive, lifecycle_send)

    uvicorn.run(Instrumented(), fd=fd, access_log=False, log_config=None, timeout_graceful_shutdown=10)


def environment(settings):
    def read(path):
        with suppress(OSError):
            return Path(path).read_text().strip()
        return None
    with sqlite3.connect(settings.db_path) as connection:
        indexes = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")]
    return {"platform": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "versions": {name: importlib.metadata.version(name) for name in ("phonoteka", "fastapi", "uvicorn", "httpx")},
            "logical_cpu_count": os.cpu_count(), "cgroup_cpu_max": read("/sys/fs/cgroup/cpu.max"),
            "cgroup_memory_max": read("/sys/fs/cgroup/memory.max"), "fd_limit": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
            "storage": {"path": str(settings.state_dir), "filesystem": "temporary directory; caller should provide tmpfs",
                        "db_bytes_at_start": settings.db_path.stat().st_size, "indexes": indexes}}


async def exercise(base_url, password, args, process):
    clients = [httpx.AsyncClient(base_url=base_url, headers={'Origin': base_url}, timeout=20, trust_env=False,
                              limits=httpx.Limits(max_connections=args.concurrency + 3, max_keepalive_connections=args.concurrency + 3)) for _ in range(2)]
    csrf = []
    sse_tasks = []
    sse = [{"events": 0, "last_event_id": 0, "errors": [], "delivery_ms": []} for _ in range(2)]
    latencies = {"list": [], "search": [], "status": []}
    failures = []
    checks = {"pagination_bounded": True, "catalog_total_correct": True}
    started = None
    stopping = asyncio.Event()
    try:
        for _ in range(300):
            if process.poll() is not None:
                raise RuntimeError("isolated uvicorn exited during startup")
            try:
                if (await clients[0].get("/health")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.1)
        else:
            raise RuntimeError("isolated uvicorn startup timeout")
        for client in clients:
            response = await client.post("/api/auth/login", json={"username": "owner", "password": password})
            response.raise_for_status()
            csrf.append(response.json()["csrf_token"])

        async def stream(index):
            try:
                async with clients[index].stream("GET", "/api/events", timeout=None) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("id:"):
                            sse[index]["last_event_id"] = int(line[3:].strip())
                        elif line.startswith("data:"):
                            payload = json.loads(line[5:])
                            if payload.get("job_id") == "synthetic-background" or str(payload.get('job_id','')).startswith('performance-audit-'):
                                if started is not None and payload.get('benchmark_emitted_at', started) < started:
                                    continue
                                sse[index]["events"] += 1
                                if "benchmark_emitted_at" in payload:
                                    sse[index]["delivery_ms"].append(max(0, time.monotonic() - payload["benchmark_emitted_at"]) * 1000)
                        if stopping.is_set():
                            break
                    if not stopping.is_set():
                        sse[index]['errors'].append('stream_ended_early')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                sse[index]["errors"].append(type(error).__name__)

        sse_tasks = [asyncio.create_task(stream(index), name=f"acceptance-ui-{index}-sse") for index in range(2)]
        paths = [("list", "/api/tracks?page=1&page_size=30"),
                 ("search", "/api/tracks?page=1&page_size=30&search=" + httpx.QueryParams({"q": "Исполнитель 042"})["q"]),
                 ("status", "/api/summary")]
        for index in range(args.warmup_requests):
            response = await clients[index % 2].get(paths[index % 3][1])
            response.raise_for_status()
        baseline = (await clients[0].get("/__acceptance/observations")).json()
        baseline_lag = len(baseline["lag_ms"])
        baseline_resources = len(baseline["resources"])
        baseline_tasks = len(baseline['task_counts'])
        baseline_updates = baseline['updates']
        for session in sse:
            session['warmup_events'] = session['events']
            session['events'] = 0
            session['delivery_ms'] = []
        started = time.monotonic()
        ticket = 0

        async def load(worker):
            nonlocal ticket
            while ticket < args.samples:
                if time.monotonic() > started + args.duration_seconds + min(60, max(10, args.duration_seconds * .1)):
                    failures.append({'category': 'load_profile', 'error': 'request_budget_exceeded', 'unfinished_requests': args.samples-ticket})
                    break
                current = ticket
                ticket += 1
                await asyncio.sleep(max(0, started + current * args.duration_seconds / args.samples - time.monotonic()))
                name, path = paths[current % 3]
                # List pages vary over the whole catalog. Search/status are
                # measured separately; neither audio nor external API is timed.
                if name == "list":
                    page = current % max(1, math.ceil(args.records / 30)) + 1
                    path = f"/api/tracks?page={page}&page_size=30"
                before = time.monotonic()
                response = None
                try:
                    response = await clients[worker % 2].get(path)
                    elapsed = (time.monotonic() - before) * 1000
                    response.raise_for_status()
                    payload = response.json()
                    latencies[name].append(elapsed)
                    if name in {"list", "search"}:
                        checks["pagination_bounded"] &= len(payload["items"]) <= 30
                    if name == "list":
                        checks["catalog_total_correct"] &= payload["total"] == args.records
                    if name == "status":
                        checks["catalog_total_correct"] &= payload["total"] == args.records
                except Exception as error:
                    failures.append({"category": name, "error": type(error).__name__, "status": response.status_code if response is not None else None})

        readiness = None
        if args.ready_file:
            async def publish_readiness():
                # Use the authenticated observation route, never a synthetic
                # sample or a fixed startup delay. Keep ordinary load running.
                async with asyncio.timeout(10):
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError("isolated uvicorn exited before measurement readiness")
                        response = await clients[0].get("/__acceptance/observations")
                        response.raise_for_status()
                        sample = next((item for item in response.json()["resources"][baseline_resources:]
                                       if item["at"] >= started), None)
                        if sample is not None:
                            signal = {"state": "measurement_sample_observed", "pid": os.getpid(),
                                      "measurement_started_at_monotonic": started,
                                      "resource_sample_at_monotonic": sample["at"],
                                      "ready_at_monotonic": time.monotonic()}
                            target = args.ready_file
                            target.parent.mkdir(parents=True, exist_ok=True)
                            if target.exists() or target.is_symlink():
                                raise RuntimeError("measurement readiness path already exists")
                            temporary = target.with_name(target.name + f".{os.getpid()}.writing")
                            with temporary.open("x", encoding="utf-8") as stream:
                                os.fchmod(stream.fileno(), 0o600)
                                json.dump(signal, stream)
                                stream.flush()
                                os.fsync(stream.fileno())
                            temporary.replace(target)
                            return signal
                        await asyncio.sleep(.05)
            load_tasks = [asyncio.create_task(load(index)) for index in range(args.concurrency)]
            try:
                readiness = await publish_readiness()
                await asyncio.gather(*load_tasks)
            finally:
                for task in load_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*load_tasks, return_exceptions=True)
        else:
            await asyncio.gather(*(load(index) for index in range(args.concurrency)))
        await asyncio.sleep(max(0, started + args.duration_seconds - time.monotonic()))
        elapsed = time.monotonic() - started
        for index, task in enumerate(sse_tasks):
            sse[index]['alive_at_measurement_end'] = not task.done()
        observations = (await clients[0].get("/__acceptance/observations")).json()
        observations["lag_ms"] = observations["lag_ms"][baseline_lag:]
        observations["resources"] = observations["resources"][baseline_resources:]
        observations['task_counts'] = observations['task_counts'][baseline_tasks:]
        observations['updates'] -= baseline_updates
        observations['progress'] = [point for point in observations['progress'] if started <= point['at'] <= started + elapsed]
        observations['spans'] = [span for span in observations['spans'] if started <= span['start'] <= started + elapsed]
        observations['measurement_started_at_monotonic'] = started
        result = {"elapsed_seconds": elapsed, "requests": {name: distribution(values) for name, values in latencies.items()},
                  "failures": failures, "checks": checks, "sse": sse, "observations": observations}
        if readiness is not None:
            result["measurement_readiness"] = readiness
        return result
    finally:
        stopping.set()
        for task in sse_tasks:
            task.cancel()
        await asyncio.gather(*sse_tasks, return_exceptions=True)
        for index, client in enumerate(clients):
            if index < len(csrf):
                with suppress(Exception):
                    await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf[index]})
            await client.aclose()


def summarize(result, args):
    observations = result.pop("observations")
    samples = observations["resources"]
    window = max(1, min(30, len(samples) // 4))
    growth = {}
    for name in ("rss_bytes", "fd", 'combined_rss_bytes', 'combined_fd', 'cgroup_memory_current_bytes'):
        values = [sample[name] for sample in samples if sample.get(name) is not None]
        growth[name] = {"first_window_mean": statistics.mean(values[:window]) if values else None,
                        "last_window_mean": statistics.mean(values[-window:]) if values else None,
                        "delta": statistics.mean(values[-window:]) - statistics.mean(values[:window]) if values else None,
                        "observed_peak": max(values) if values else None}
    result["resources"] = {"sample_interval_seconds": 1, "samples": samples, "growth": growth,
                           "rss_high_water_bytes": max((sample.get("rss_high_water_bytes") or 0 for sample in samples), default=None),
                           "async_tasks": distribution(observations["task_counts"]),
                           "server_child_processes_peak": max((sample["child_process_count"] for sample in samples), default=None),
                           "server_children_rss_peak_bytes": max((sample["child_rss_bytes"] for sample in samples), default=None),
                           "server_children_fd_peak": max((sample["child_fd"] for sample in samples), default=None),
                           "cgroup_memory_peak_bytes": max((sample.get('cgroup_memory_peak_bytes') or 0 for sample in samples), default=None)}
    result["event_loop_lag_ms"] = distribution(observations["lag_ms"])
    measured_start = observations['measurement_started_at_monotonic']
    measured_end = measured_start + result['elapsed_seconds']
    progress_times = [point['at'] for point in observations['progress']]
    progress_gaps = [b-a for a,b in zip([measured_start, *progress_times], [*progress_times, measured_end])]
    maximum_gap = max(progress_gaps, default=result['elapsed_seconds'])
    allowed_gap = min(60, max(20, args.duration_seconds * .1))
    max_concurrent = max((span['concurrent'] for span in observations['spans']), default=0)
    result["background"] = {"kind": "real Worker and FFmpeg audit of 16 generated FLAC fixtures, repeated bounded batches" if observations['real_audio'] else "synthetic short SQLite transactions and durable SSE events", "workers": 4,
                            "updates": observations["updates"], "errors": observations["background_errors"],
                            "batches":observations['batches'],"max_concurrent_files":max_concurrent,
                            "processing_spans":observations['spans'], 'progress':observations['progress'],
                            'max_measured_progress_gap_seconds':maximum_gap, 'allowed_progress_gap_seconds':allowed_gap,
                            'failed_jobs': observations['failed_jobs']}
    for session in result["sse"]:
        session["delivery_ms"] = distribution(session["delivery_ms"])
    request_count = sum(item["samples"] for item in result["requests"].values())
    result["profile"] = {"records": args.records, "ui_sessions": 2, "sse_connections": 2,
                         "request_concurrency": args.concurrency, "file_workers":4, "actual_audio_workers":args.real_audio_workers, "requested_samples": args.samples,
                         "requested_duration_seconds": args.duration_seconds, "warmup_requests_excluded": args.warmup_requests,
                         "local_api_profile_15min_reached": result["elapsed_seconds"] >= 900 and request_count >= 1000 and args.records >= 10000,
                         "resource_observation_30min_reached": result["elapsed_seconds"] >= 1800,
                         "resource_growth_regression_limits": {"rss_bytes": args.max_rss_growth_mib * 1024 ** 2, "fd": args.max_fd_growth}}
    result["criteria"] = {
        "all_http_succeeded": not result["failures"] and request_count == args.samples,
        "list_search_status_each_p95_under_1000ms": all(item["p95"] is not None and item["p95"] <= 1000 for item in result["requests"].values()),
        "event_loop_lag_p95_under_100ms": result["event_loop_lag_ms"]["p95"] is not None and result["event_loop_lag_ms"]["p95"] <= 100,
        "both_sse_sessions_received_updates": all(session["events"] > 0 and not session["errors"] and session['alive_at_measurement_end'] for session in result["sse"]),
        "both_sse_sessions_delivery_p95_under_1000ms": all(session['delivery_ms']['p95'] is not None and session['delivery_ms']['p95'] <= 1000 for session in result['sse']),
        "background_progressed_throughout_measurement": observations["updates"] > 0 and not observations["background_errors"] and maximum_gap <= allowed_gap,
        "rss_growth_within_configured_regression_limit": growth["rss_bytes"]["delta"] is not None and growth["rss_bytes"]["delta"] <= args.max_rss_growth_mib * 1024 ** 2,
        "fd_growth_within_configured_regression_limit": growth["fd"]["delta"] is not None and growth["fd"]["delta"] <= args.max_fd_growth,
        "combined_rss_growth_within_configured_regression_limit": growth['combined_rss_bytes']['delta'] is not None and growth['combined_rss_bytes']['delta'] <= args.max_rss_growth_mib * 1024**2,
        "combined_fd_growth_within_configured_regression_limit": growth['combined_fd']['delta'] is not None and growth['combined_fd']['delta'] <= args.max_fd_growth,
        "cgroup_memory_growth_within_configured_regression_limit": growth['cgroup_memory_current_bytes']['delta'] is not None and growth['cgroup_memory_current_bytes']['delta'] <= args.max_rss_growth_mib * 1024**2,
        "real_audio_concurrency_observed": not args.real_audio_workers or max_concurrent >= 4 and len({span['thread_id'] for span in observations['spans']})>=4,
        **result.pop("checks"),
    }
    result["passed_measured_checks"] = all(result["criteria"].values())
    result["limitations"] = ["Real audio background uses generated 2-second FLAC fixtures with the actual Worker/FFmpeg; external providers and inference are not part of this local API profile." if args.real_audio_workers else "Background workers are synthetic DB/event writers, not the real audio/provider pipeline; this does not close all AC-25/27.",
                              "Observed API latencies exclude audio/external APIs; percentile units are milliseconds.",
                              "Finite RAM/fd observations and explicit regression limits do not prove the absence of every long-term leak.",
                              "Short runs do not satisfy the 15-minute API or 30-minute sustained-resource acceptance profile.",
                              "Server /proc high watermark includes startup; periodic RSS/fd readings can miss short spikes."]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-audio-workers",action='store_true',help='Exercise actual four-file Worker/FFmpeg alongside HTTP')
    parser.add_argument("--duration-seconds", type=float, default=60)
    parser.add_argument("--samples", type=int, default=1200)
    parser.add_argument("--records", type=int, default=10000)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--warmup-requests", type=int, default=30)
    parser.add_argument("--max-rss-growth-mib", type=float, default=64)
    parser.add_argument("--max-fd-growth", type=int, default=32)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ready-file", type=Path,
                        help="Atomic signal after the first measured resource sample; coordination only, not product health")
    parser.add_argument("--serve", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--background-interval", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        return serve(args.serve, args.port, args.fd, args.background_interval, args.real_audio_workers)
    if not (2 <= args.duration_seconds <= 7200 and 6 <= args.samples <= 100000 and 30 <= args.records <= 100000
            and 2 <= args.concurrency <= 16 and 0 <= args.warmup_requests <= 300):
        parser.error("bounded profile required: duration 2..7200, samples 6..100000, records 30..100000, concurrency 2..16")
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "harness": "isolated-real-http-v3"}
    with tempfile.TemporaryDirectory(prefix="phonoteka-performance-") as directory:
        base = Path(directory)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        settings = seed(base, args.records, port, args.real_audio_workers)
        result["environment"] = environment(settings)
        interval = max(.2, (args.duration_seconds + 10) * 4 / (args.records * .75))
        result["background_interval_seconds_per_worker"] = interval
        with (base / "server.log").open("wb") as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", str(base), "--port", str(port),
                                      "--fd", str(listener.fileno()), "--background-interval", str(interval), *(['--real-audio-workers'] if args.real_audio_workers else [])],
                                     pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT)
            listener.close()
            try:
                result.update(asyncio.run(exercise(f"http://127.0.0.1:{port}", (base / "owner_password").read_text(), args, child)))
                result = summarize(result, args)
            except Exception as error:
                result["fatal_error"] = {"type": type(error).__name__, "message": str(error)}
                result["passed_measured_checks"] = False
                result["server_log_tail"] = (base / "server.log").read_text(errors="replace")[-12000:]
            finally:
                child.send_signal(signal.SIGTERM) if child.poll() is None else None
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                result["server_exit_code"] = child.returncode
                stopped_log = (base / "server.log").read_text(errors="replace")[-12000:]
                # Uvicorn re-raises a handled SIGTERM after lifespan cleanup.
                clean = child.returncode in (0, -signal.SIGTERM) and 'service_stopping' in stopped_log
                result.setdefault("criteria", {})["server_shutdown_clean"] = clean
                result["passed_measured_checks"] = result["passed_measured_checks"] and clean
                if not result['passed_measured_checks']:
                    result['server_log_tail'] = stopped_log
        result["temporary_state_removed_after_report"] = True
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload, flush=True)
    return 0 if result.get("passed_measured_checks") else 1


if __name__ == "__main__":
    raise SystemExit(main())
