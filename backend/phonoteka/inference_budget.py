"""One supervised resident model process; its failures never poison the API process."""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import stat
import tempfile
import threading
import time

REMOTE_BACKEND=os.environ.get('ASR_BACKEND','cpu')=='pc2'
PROFILE = 'pc2-large-v3-https-supervised-v1' if REMOTE_BACKEND else 'cpu-small-int8-unpacked-supervised-v3'
MAX_DURATION = 1800
MAX_SAMPLES = MAX_DURATION * 16000
MAX_PCM_BYTES = 60 * 1024 ** 2
MAX_RESULT_BYTES = 8 * 1024 ** 2
MAX_INPUT_BYTES = 1024 ** 2
MAX_REFERENCE_BYTES = 2048  # A single message stays below Linux PIPE_BUF.
MAX_CONTROL_BYTES = 4096
MAX_INPUT_CHANNELS = 8
MAX_INPUT_RATE = 384000
MAX_RSS_BYTES = 512 * 1024 ** 2 if REMOTE_BACKEND else 2 * 1024 ** 3
MAX_JOB_SECONDS = 6600
CPU_CONTAINER_BYTES=3*1024**3
SUPERVISOR_RESERVE_BYTES=256*1024**2


def container_memory_limit():
    """Read both cgroup versions; enormous v1 sentinels are not real limits."""
    for name in ('/sys/fs/cgroup/memory.max','/sys/fs/cgroup/memory/memory.limit_in_bytes','/sys/fs/cgroup/memory.limit_in_bytes'):
        try:
            value=int(Path(name).read_text().strip())
            if 0<value<2**60:return value
        except (OSError,ValueError):pass
    return None


def require_cpu_capacity():
    limit=container_memory_limit()
    if limit is not None and limit<CPU_CONTAINER_BYTES:
        raise BudgetError('inference_container_budget','CPU ASR requires its declared 3 GiB container profile')


class BudgetError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def rss_bytes(pid):
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def process_tree_rss(pid):
    total = rss_bytes(pid)
    try:
        children = Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
        for child in children[:32]:
            total += rss_bytes(int(child))
    except (OSError, ValueError):
        pass
    return total


def read_model_input(reference):
    if not 0 < len(reference) <= MAX_REFERENCE_BYTES:
        raise BudgetError('inference_input_budget', 'Invalid model input reference')
    descriptor = os.open(os.fsdecode(reference), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_INPUT_BYTES:
            raise BudgetError('inference_input_budget', 'Model input exceeds the resource profile')
        return json.loads(stream.read(MAX_INPUT_BYTES + 1))


def write_model_result(scratch, result):
    encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
    if not 0 < len(encoded) <= MAX_RESULT_BYTES:
        raise BudgetError('inference_result_budget', 'Model result exceeds the resource profile')
    descriptor = os.open(Path(scratch)/'output.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(encoded)
        stream.flush()


def read_model_result(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_RESULT_BYTES:
            raise BudgetError('inference_result_budget', 'Invalid model result artifact')
        encoded = stream.read(MAX_RESULT_BYTES + 1)
        if len(encoded) > MAX_RESULT_BYTES:
            raise BudgetError('inference_result_budget', 'Model result exceeds the resource profile')
        result = json.loads(encoded)
        if not isinstance(result, dict):
            raise BudgetError('inference_worker_protocol', 'Invalid model result document')
        return result


def _model_main(connection):
    # Prefer this disposable child over the API process if the cgroup kills a
    # process during an allocation spike. The explicit RSS budget is lower than
    # the supported container's 3 GiB limit and is checked by the supervisor.
    if os.name == 'posix':
        os.setsid()
    try:
        Path('/proc/self/oom_score_adj').write_text('500')
    except OSError:
        pass
    from .inference import execute
    from .telemetry import configure_logging
    configure_logging('inference')
    from . import tracing
    tracing.configure('inference')
    while True:
        try:
            body = read_model_input(connection.recv_bytes(MAX_REFERENCE_BYTES))
        except (EOFError, OSError):
            return
        try:
            with tracing.span('asr.execute', body.get('_trace_attributes',{}), parent=tracing.extracted(body.get('_trace',{}))):
                result = execute(body, on_model_loaded=lambda: connection.send_bytes(b'{"event":"loaded"}'))
            write_model_result(body['_scratch_root'], result)
            document = {'event':'result'}
        except BaseException as exc:
            oom = isinstance(exc, MemoryError) or 'out of memory' in str(exc).lower() or 'cannot allocate memory' in str(exc).lower()
            code = 'inference_model_oom' if oom else getattr(exc, 'code', 'inference_failed')
            # Never send a traceback, model prompt, path, or arbitrary exception
            # text to logs or the supervisor protocol.
            document = {'event':'error', 'code':code, 'reset':oom}
        encoded = json.dumps(document, ensure_ascii=False).encode('utf-8')
        if len(encoded) > MAX_CONTROL_BYTES:
            encoded = b'{"event":"error","code":"inference_worker_protocol","reset":true}'
        try:
            connection.send_bytes(encoded)
        except (BrokenPipeError, EOFError, OSError):
            return
        if document.get('reset'):
            return


class ModelProcess:
    def __init__(self, *, target=_model_main, max_rss=MAX_RSS_BYTES, deadline=MAX_JOB_SECONDS, on_residency=None, on_rss=None, on_start=None, scratch_root=None):
        self.target, self.max_rss, self.deadline = target, max_rss, deadline
        limit=container_memory_limit()
        if limit is not None:self.max_rss=min(self.max_rss,max(1,limit-SUPERVISOR_RESERVE_BYTES))
        self.on_residency = on_residency or (lambda loaded: None)
        self.on_rss = on_rss or (lambda current, peak: None)
        self.on_start = on_start or (lambda: None)
        self.process = None
        self.connection = None
        self.peak_rss = 0
        self.starts = 0
        self.scratch_root = scratch_root
        self.group_pid = None
        self._owner = threading.Lock()
        self._stopping = threading.Event()

    def request_stop(self):
        self._stopping.set()

    def _check_stop(self,cancel_event=None):
        if self._stopping.is_set():
            raise BudgetError('inference_stopping', 'Model service is stopping; no new execution is allowed')
        if cancel_event is not None and cancel_event.is_set():
            raise BudgetError('inference_cancelled','Inference request cancelled; result discarded')

    def close(self):
        self.request_stop()
        with self._owner:
            self._unload()

    def _unload(self):
        """Only the serial lifecycle owner may touch Process/Connection handles."""
        process, connection = self.process, self.connection
        group_pid = self.group_pid
        self.group_pid = None
        self.process = self.connection = None
        if process is not None and process.pid is not None:
            pid = process.pid
            if process.is_alive():
                try:
                    if os.name == 'posix' and os.getpgid(pid) == pid:
                        group_pid = pid
                        os.killpg(pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except ProcessLookupError:
                    pass
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            # A decoder may survive a killed model. The child creates its own
            # process group, so this cannot target another service or build.
            if os.name == 'posix' and group_pid == pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.close()
        elif process is not None:
            process.close()  # Spawn never completed; joining would be invalid.
        if connection is not None:
            connection.close()
        self.on_residency(False)
        self.on_rss(0, self.peak_rss)

    def run(self, body, *, cancel_event=None):
        with self._owner:
            self._check_stop(cancel_event)
            with tempfile.TemporaryDirectory(prefix='model-', dir=self.scratch_root) as scratch:
                encoded = json.dumps({**body, '_scratch_root':scratch}, ensure_ascii=False).encode('utf-8')
                if len(encoded) > MAX_INPUT_BYTES:
                    raise BudgetError('inference_input_budget', 'Model input exceeds the resource profile')
                path = Path(scratch) / 'input.json'
                reference = os.fsencode(path)
                if len(reference) > MAX_REFERENCE_BYTES:
                    raise BudgetError('inference_input_budget', 'Model input path exceeds the resource profile')
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(encoded)
                    stream.flush()
                return self._run(reference,cancel_event)

    def _run(self, reference,cancel_event=None):
        # The inference scheduler has exactly one consumer; no second resident
        # weights process is started for alignment or a queued request.
        started = time.monotonic()
        try:
            self._check_stop(cancel_event)
            if self.process is None:
                context = multiprocessing.get_context('spawn')
                parent, child = context.Pipe()
                self.process = context.Process(target=self.target, args=(child,), daemon=True)
                self.connection = parent
                try:
                    self._check_stop(cancel_event)
                    self.process.start()
                finally:
                    child.close()
                self.starts += 1
                self.on_start()
            self._check_stop(cancel_event)
            process, connection = self.process, self.connection
            # One small reference, one in-flight job. Large JSON never blocks the
            # pipe before the deadline/stop supervisor gets control.
            connection.send_bytes(reference)
            while True:
                self._check_stop(cancel_event)
                if os.name == 'posix':
                    try:
                        if os.getpgid(process.pid) == process.pid:
                            self.group_pid = process.pid
                    except ProcessLookupError:
                        pass
                rss = process_tree_rss(process.pid)
                self.peak_rss = max(self.peak_rss, rss)
                self.on_rss(rss, self.peak_rss)
                if rss > self.max_rss:
                    raise BudgetError('inference_memory_budget', 'Model worker exceeded the CPU memory profile; explicit retry required')
                if time.monotonic() - started > self.deadline:
                    raise BudgetError('inference_execution_timeout', 'Model execution deadline reached; explicit retry required')
                if connection.poll(.1):
                    try:
                        message = json.loads(connection.recv_bytes(MAX_CONTROL_BYTES))
                    except OSError as exc:
                        raise BudgetError('inference_worker_protocol', 'Oversized model control frame') from exc
                    if message.get('event') == 'loaded':
                        self.on_residency(True)
                    elif message.get('event') == 'result':
                        if process_tree_rss(process.pid) > self.max_rss:
                            raise BudgetError('inference_memory_budget', 'Model worker exceeded the CPU memory profile; explicit retry required')
                        # The child cannot nominate a path. Read only this job's
                        # expected private artifact, after its ready notification.
                        result = read_model_result(Path(os.fsdecode(reference)).parent/'output.json')
                        return {**result, 'resource_profile':PROFILE}
                    elif message.get('event') == 'error':
                        raise BudgetError(message.get('code','inference_failed'), 'Model execution failed; explicit retry required')
                    else:
                        raise BudgetError('inference_worker_protocol', 'Invalid model worker result')
                elif not process.is_alive():
                    # SIGKILL alone is not proof of an OOM. Preserve an honest
                    # worker-termination code and never retry the same job.
                    raise BudgetError('inference_worker_terminated', 'Model worker terminated; explicit retry required')
        except (EOFError, OSError, ValueError) as exc:
            self._unload()
            raise BudgetError('inference_worker_terminated', 'Model worker result unavailable; explicit retry required') from exc
        except BaseException:
            self._unload()
            raise


def profile():
    return {'id':PROFILE, 'models':['pc2:faster-whisper-large-v3:float16'] if REMOTE_BACKEND else ['faster-whisper-small:int8'], 'resident_models':0 if REMOTE_BACKEND else 1,
            'max_duration_seconds':MAX_DURATION, 'channels':1, 'sample_rate':16000,
            'max_input_channels':MAX_INPUT_CHANNELS, 'max_input_sample_rate':MAX_INPUT_RATE,
            'max_pcm_samples':MAX_SAMPLES, 'max_pcm_bytes':MAX_PCM_BYTES,
            'model_process_rss_bytes':MAX_RSS_BYTES, 'container_memory_bytes':3 * 1024 ** 3,
            'max_result_bytes':MAX_RESULT_BYTES, 'deadline_seconds':MAX_JOB_SECONDS,
            'max_ipc_control_bytes':MAX_CONTROL_BYTES, 'ipc_payload_policy':'private_json_files_short_control_only',
            'decoder_spool_bytes':33 * 1024 ** 2, 'resident_weights_policy':'one_pinned_model_unload_on_failure_or_shutdown',
            'oom_policy':'fail_and_unload_no_automatic_retry', 'gpu':REMOTE_BACKEND,'execution_device':'pc2_cuda' if REMOTE_BACKEND else 'local_cpu'}
