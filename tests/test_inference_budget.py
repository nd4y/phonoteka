import json
import os
from pathlib import Path
import time
import subprocess
import multiprocessing
import signal
import threading
import struct

import pytest
from phonoteka.inference_budget import ModelProcess, BudgetError, PROFILE, profile, read_model_input, write_model_result


def echo_model(connection):
    while True:
        try:
            body = read_model_input(connection.recv_bytes())
        except EOFError:
            return
        (Path(body['_scratch_root'])/'decoded.wav').write_bytes(b'synthetic-pcm')
        connection.send_bytes(b'{"event":"loaded"}')
        write_model_result(body['_scratch_root'], {'generation':body['generation']})
        connection.send_bytes(b'{"event":"result"}')


def slow_model(connection):
    connection.recv_bytes()
    time.sleep(30)


def oom_model(connection):
    connection.recv_bytes()
    connection.send_bytes(b'{"event":"error","code":"inference_model_oom","reset":true}')


def dying_model(connection):
    connection.recv_bytes()
    os._exit(19)


def never_receiving_model(connection):
    time.sleep(30)


def stalled_oversize_sender(connection):
    connection.recv_bytes()
    # A real transport header followed by a stalled body used to block the
    # supervisor inside recv_bytes(8 MiB), beyond its execution deadline.
    os.write(connection.fileno(), struct.pack('!i', 8 * 1024**2))
    time.sleep(30)


def loaded_slow_model(connection):
    read_model_input(connection.recv_bytes())
    connection.send_bytes(b'{"event":"loaded"}')
    time.sleep(30)


def deadline_case(directory, result):
    if os.name == 'posix': os.setsid()
    worker = ModelProcess(target=never_receiving_model, deadline=.3, scratch_root=directory)
    started = time.monotonic()
    try:
        worker.run({'generation': 1, 'reference_text': 'x' * (128 * 1024)})
        result.send({'error': 'unexpected_success'})
    except BudgetError as error:
        result.send({'code': error.code, 'seconds': time.monotonic() - started, 'starts': worker.starts,
                     'unloaded': worker.process is None, 'scratch_empty': not list(Path(directory).iterdir())})
    finally:
        worker.close(); result.close()


def oversized_control_case(directory, result):
    if os.name == 'posix': os.setsid()
    worker = ModelProcess(target=stalled_oversize_sender, deadline=.5, scratch_root=directory)
    try:
        worker.run({'generation':1})
        result.send({'error':'unexpected_success'})
    except BudgetError as error:
        result.send({'code':error.code,'unloaded':worker.process is None,'starts':worker.starts,
                     'scratch_empty':not list(Path(directory).iterdir())})
    finally:
        worker.close(); result.close()


def lifecycle_case(directory, result):
    if os.name == 'posix': os.setsid()
    from multiprocessing.process import BaseProcess
    original_start = BaseProcess.start
    records = []
    try:
        for phase in ('startup', 'startup', 'active'):
            entered, release, loaded = threading.Event(), threading.Event(), threading.Event()
            worker = ModelProcess(target=loaded_slow_model, deadline=5, scratch_root=directory, on_residency=lambda value: loaded.set() if value else None)
            errors = []
            def delayed_start(process):
                entered.set()
                if not release.wait(3): raise TimeoutError('startup barrier timed out')
                return original_start(process)
            def run():
                try: worker.run({'generation': 1})
                except BudgetError as error: errors.append(error.code)
                except BaseException as error: errors.append(type(error).__name__)
            def close():
                try: worker.close()
                except BaseException as error: errors.append(type(error).__name__)
            if phase == 'startup': BaseProcess.start = delayed_start
            runner = threading.Thread(target=run); runner.start()
            if not (entered.wait(3) if phase == 'startup' else loaded.wait(3)):
                raise TimeoutError('model never reached lifecycle barrier')
            worker.request_stop()
            closers = [threading.Thread(target=close) for _ in range(3)]
            for closer in closers: closer.start()
            release.set()
            runner.join(3)
            for closer in closers: closer.join(3)
            BaseProcess.start = original_start
            assert not runner.is_alive() and not any(closer.is_alive() for closer in closers)
            assert worker.process is None and errors == ['inference_stopping'], errors
            with pytest.raises(BudgetError) as stopped: worker.run({'generation': 2})
            assert stopped.value.code == 'inference_stopping' and worker.starts <= 1
            records.append({'phase': phase, 'starts': worker.starts, 'unloaded': worker.process is None})
        result.send({'records': records, 'scratch_empty': not list(Path(directory).iterdir())})
    except BaseException as error:
        result.send({'error': type(error).__name__, 'message': str(error)})
    finally:
        BaseProcess.start = original_start
        result.close()


def isolated_case(target, directory):
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(str(directory), send))
    process.start(); send.close()
    try:
        process.join(timeout=10)
        if process.is_alive():
            if os.name == 'posix' and os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.join(timeout=3)
            pytest.fail('External watchdog stopped a hung inference lifecycle test')
        assert process.exitcode == 0
        assert receive.poll(.2), 'scenario returned no result'
        return receive.recv()
    finally:
        receive.close(); process.close()


def test_nonreading_child_large_input_still_obeys_deadline_with_external_watchdog(tmp_path):
    outcome = isolated_case(deadline_case, tmp_path)
    assert outcome.get('code') == 'inference_execution_timeout', outcome
    assert outcome['seconds'] < 5 and outcome['starts'] == 1 and outcome['unloaded'] and outcome['scratch_empty']


def test_shutdown_races_start_and_execution_without_unstarted_join_or_orphan(tmp_path):
    outcome = isolated_case(lifecycle_case, tmp_path)
    assert 'error' not in outcome, outcome
    assert len(outcome['records']) == 3 and all(item['unloaded'] for item in outcome['records'])
    assert outcome['scratch_empty']


def test_stalled_oversized_result_header_aborts_without_blocking_deadline(tmp_path):
    outcome = isolated_case(oversized_control_case, tmp_path)
    assert outcome.get('code') == 'inference_worker_protocol', outcome
    assert outcome['unloaded'] and outcome['starts'] == 1 and outcome['scratch_empty']


def test_single_resident_process_reused_and_scratch_removed(tmp_path):
    loaded = []
    worker = ModelProcess(target=echo_model, scratch_root=tmp_path, on_residency=loaded.append)
    try:
        first = worker.run({'generation':1})
        pid = worker.process.pid
        assert worker.run({'generation':2})['generation'] == 2
        assert worker.process.pid == pid and worker.starts == 1
        assert first['resource_profile'] == PROFILE and loaded == [True, True]
        assert list(tmp_path.iterdir()) == []
    finally:
        worker.close()
    assert loaded[-1] is False


@pytest.mark.parametrize('target,limit,deadline,code', [
    (slow_model,2*1024**3,.5,'inference_execution_timeout'),
    (slow_model,1,10,'inference_memory_budget'),
    (oom_model,2*1024**3,10,'inference_model_oom'),
    (dying_model,2*1024**3,10,'inference_worker_terminated'),
])
def test_failed_model_is_unloaded_without_automatic_retry(tmp_path,target,limit,deadline,code):
    worker = ModelProcess(target=target, max_rss=limit, deadline=deadline, scratch_root=tmp_path)
    try:
        with pytest.raises(BudgetError) as error:
            worker.run({'generation':1})
        assert error.value.code == code
        assert worker.process is None and worker.starts == 1
        assert list(tmp_path.iterdir()) == []
    finally:
        worker.close()


def test_cpu_profile_bounds_decoded_audio_and_shared_residency():
    limits = profile()
    assert limits['max_pcm_samples'] == 1800*16000
    assert limits['max_pcm_bytes'] >= limits['max_pcm_samples']*2
    assert limits['resident_models'] == 1 and limits['model_process_rss_bytes'] < limits['container_memory_bytes']
    assert limits['oom_policy'] == 'fail_and_unload_no_automatic_retry'
    assert limits['gpu'] is False


def test_tiny_compressed_long_input_is_refused_before_loading_weights(tmp_path,monkeypatch):
    from phonoteka import inference, media
    path=tmp_path/'compressed.flac'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','anullsrc=r=16000:cl=mono','-t','1801','-c:a','flac',str(path)],check=True)
    assert path.stat().st_size < 1024**2
    monkeypatch.setattr(inference,'ROOT',tmp_path)
    assert inference.MODEL_INSTANCE is None
    with pytest.raises(BudgetError) as error:
        inference.execute({'relative_path':path.name,'sha256':media.sha256_file(path)})
    assert error.value.code=='inference_duration_budget' and inference.MODEL_INSTANCE is None


@pytest.mark.parametrize('channels,rate', [(9,48000), (2,384001), (0,44100), (2,0)])
def test_source_channel_and_rate_budget_refused_before_model_load(tmp_path,monkeypatch,channels,rate):
    from phonoteka import inference, media
    source = tmp_path/'header.flac'; source.write_bytes(b'controlled header fixture')
    monkeypatch.setattr(inference,'ROOT',tmp_path)
    monkeypatch.setattr(media,'probe_audio',lambda _: {'duration':1,'channels':channels,'sample_rate':rate})
    assert inference.MODEL_INSTANCE is None
    with pytest.raises(BudgetError) as error:
        inference.execute({'relative_path':source.name,'sha256':media.sha256_file(source)})
    assert error.value.code == 'inference_input_audio_budget' and inference.MODEL_INSTANCE is None
