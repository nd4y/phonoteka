"""AUD-01/05: exact historic identities with bounded, fully consumed input."""
import hashlib
import contextlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tracemalloc

import pytest

from phonoteka import media, packetstream
from test_media import make_audio, roots, TOOLS


HASH = 'SHA256:'+'a'*64
STREAM = {'index': 7, 'codec_name': 'pcm_s16le', 'sample_rate': '8000', 'channels': 1}


def legacy(payload):
    # Independent reference is the exact previously deployed canonical object,
    # not the new incremental encoder or parser's selected stream representation.
    streams = payload['streams']
    value = {'packets': [{'hash': item['data_hash'], 'side_data': item.get('side_data_list', [])}
                         for item in payload['packets']],
             'streams': [{key: item[key] for key in ('codec_name', 'sample_rate', 'channels', 'extradata_hash')
                          if key in item} for item in streams]}
    if len(streams) > 1:
        value['scheme'] = 'encoded_audio_packets_v2_all_streams'
        ordinals = {item['index']: ordinal for ordinal, item in enumerate(streams)}
        for packet, source in zip(value['packets'], payload['packets']):
            packet['stream'] = ordinals[source['stream_index']]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def header(streams):
    return packetstream.stream_header(json.dumps({'streams': streams}).encode())


def pieces(raw, size=11):
    return (raw[index:index+size] for index in range(0, len(raw), size))


@TOOLS
@pytest.mark.parametrize('multiple', [False, True])
def test_real_generated_audio_matches_the_previously_deployed_ffprobe_hash(make_audio, roots, multiple):
    path = make_audio() if not multiple else roots['work']/'two-streams.mka'
    if multiple:
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
            'sine=frequency=440:sample_rate=8000:duration=0.25', '-f', 'lavfi', '-i',
            'sine=frequency=880:sample_rate=8000:duration=0.25', '-map', '0:a', '-map', '1:a',
            '-c:a', 'flac', str(path)], check=True)
    raw = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_packets',
        '-show_streams', '-show_entries',
        'packet=stream_index,data_hash:stream=index,codec_name,sample_rate,channels,extradata_hash',
        '-show_data_hash', 'sha256', '-of', 'json', str(path)])
    assert len(raw) < 1024**2, 'Legacy reference is bounded to generated sub-second audio'
    assert media.audio_identity(path) == legacy(json.loads(raw))


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux pinned input descriptor contract')
def test_packet_command_keeps_protocol_limits_and_an_open_nofollow_input(tmp_path, monkeypatch):
    path = tmp_path/'generated.wav'; path.write_bytes(b'generated descriptor fixture')
    payload = {'packets': [{'stream_index': 7, 'data_hash': HASH}], 'streams': [STREAM]}
    monkeypatch.setattr(media, '_process', lambda *_args, **_kwargs:
                        subprocess.CompletedProcess([], 0, json.dumps({'streams': [STREAM]}).encode(), b''))
    observed = []

    @contextlib.contextmanager
    def inspect_command(command, *, pass_fds, timeout):
        assert timeout == 300 and len(pass_fds) == 1
        fd = pass_fds[0]; observed.append(fd)
        assert os.fstat(fd).st_ino == path.stat().st_ino
        assert command[command.index('-i')+1] == '/proc/self/fd/'+str(fd)
        assert command[command.index('-protocol_whitelist')+1] == 'file,pipe'
        assert command[command.index('-format_whitelist')+1] == 'mp3,flac,mov,aac,ogg,wav,aiff,ape,wv,asf,matroska,dsf,iff,ac3'
        yield pieces(json.dumps(payload).encode())

    monkeypatch.setattr(packetstream, 'process_chunks', inspect_command)
    assert media.audio_identity(path) == legacy(payload)
    with pytest.raises(OSError):
        os.fstat(observed[0])


@pytest.mark.parametrize('size', [1, 11, 1024])
@pytest.mark.parametrize('streams', [[STREAM],
    [{'index': 9, 'codec_name': 'aac', 'channels': 2, 'extradata_hash': 'SHA256:example'}, STREAM]])
def test_exact_legacy_single_and_multistream_hashes_with_all_side_data(streams, size):
    packets = [{'stream_index': item['index'], 'data_hash': HASH,
                'side_data_list': [{'side_data_type': 'Skip Samples', 'skip_samples': 1105,
                                   'discard_padding': 37, 'unknown': {'unicode': 'пауза', 'flag': True,
                                                                    'fraction': 0.125, 'values': [None, 2, 'x']}}]}
               for item in reversed(streams)]
    packets += [{'stream_index': streams[0]['index'], 'data_hash': 'SHA256:'+'B'*64}]
    payload = {'packets': packets, 'streams': streams}
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    assert packetstream.identity(pieces(raw, size), header(streams)) == legacy(payload)
    # Top-level order and ignored stream metadata do not enter the identity.
    changed = {'streams': [dict(item, tags={'arbitrary': 'new'}, disposition={'default': 1}) for item in streams],
               'ignored': {'value': 'bounded'}, 'numeric': 1.25e-18, 'packets': packets}
    assert packetstream.identity(pieces(json.dumps(changed).encode(), size), header(streams)) == legacy(payload)


def test_stream_ordinal_order_is_not_first_packet_order_or_container_index():
    streams = [{'index': 50, 'codec_name': 'flac'}, {'index': 2, 'codec_name': 'flac'}]
    packets = [{'stream_index': 2, 'data_hash': HASH}, {'stream_index': 50, 'data_hash': HASH}]
    payload = {'packets': packets, 'streams': streams}
    first = packetstream.identity(pieces(json.dumps(payload).encode()), header(streams))
    assert first == legacy(payload)
    reverse = dict(payload, streams=list(reversed(streams)))
    assert packetstream.identity(pieces(json.dumps(reverse).encode()), header(reverse['streams'])) == legacy(reverse) != first
    with pytest.raises(packetstream.PacketStreamError):
        packetstream.identity(pieces(json.dumps(reverse).encode()), header(streams))


@pytest.mark.parametrize(('initial', 'final'), [(1, True), (2, 2.0)])
def test_stream_metadata_types_must_match_the_exact_legacy_canonical_bytes(initial, final):
    initial_streams = [dict(STREAM, channels=initial)]
    final_streams = [dict(STREAM, channels=final)]
    packets = [{'stream_index': 7, 'data_hash': HASH}]
    payload = {'packets': packets, 'streams': final_streams}
    assert initial_streams == final_streams
    assert legacy(dict(payload, streams=initial_streams)) != legacy(payload)
    with pytest.raises(packetstream.PacketStreamError) as error:
        packetstream.identity(pieces(json.dumps(payload).encode()), header(initial_streams))
    assert error.value.code == 'invalid_audio'


def test_finished_parser_cannot_emit_an_altered_second_digest_or_accept_more_bytes():
    payload = {'packets': [{'stream_index': 7, 'data_hash': HASH}], 'streams': [STREAM]}
    parser = packetstream.PacketIdentity(header([STREAM]))
    parser.feed(json.dumps(payload).encode())
    assert parser.finish() == legacy(payload)
    with pytest.raises(packetstream.PacketStreamError):
        parser.finish()
    with pytest.raises(packetstream.PacketStreamError):
        parser.feed(b' ')


@pytest.mark.parametrize('raw', [
    b'', b'null', b'[]', b'{"packets":[],"streams":[]}',
    b'{"packets":[],"streams":[{"index":7}]}',
    b'{"packets":[{"stream_index":7}],"streams":[{"index":7}]}',
    b'{"packets":[{"stream_index":8,"data_hash":"'+HASH.encode()+b'"}],"streams":[{"index":7}]}',
    b'{"packets":[{"stream_index":true,"data_hash":"'+HASH.encode()+b'"}],"streams":[{"index":7}]}',
    b'{"packets":[{"stream_index":7,"data_hash":"'+HASH.encode()+b'"},],"streams":[{"index":7}]}',
    b'{"packets":[{"stream_index":7,"data_hash":"'+HASH.encode()+b'"}],"streams":[{"index":7}],}',
    b'{"packets":[],"packets":[],"streams":[{"index":7}]}',
    b'{"packets":[{"stream_index":7,"stream_index":7,"data_hash":"'+HASH.encode()+b'"}],"streams":[{"index":7}]}',
    b'{"packets":[{"stream_index":7,"data_hash":"'+HASH.encode()+b'","side_data_list":[NaN]}],"streams":[{"index":7}]}',
    b'{"packets":[{"stream_index":7,"data_hash":"'+HASH.encode()+b'","side_data_list":[1e999]}],"streams":[{"index":7}]}',
    b'{"packets":[{"stream_index":7,"data_hash":"'+HASH.encode()+b'"}],"streams":[{"index":7},{"index":7}]}',
    b'{"packets":[{"stream_index":7,"data_hash":"'+HASH.encode()+b'"}],"streams":[{"index":7},{"index":8}]}',
    b'{"packets":[{"stream_index":7,"data_hash":"'+HASH.encode()+b'"}],"streams":[{"index":7}]} garbage',
    b'{"packets":[{"stream_index":7,"data_hash":"'+HASH.encode()+b'"}],"streams":[{"index":7}]}\xff',
])
def test_malformed_unknown_missing_and_nonfinite_input_never_returns_partial_identity(raw):
    with pytest.raises(packetstream.PacketStreamError) as error:
        packetstream.identity(pieces(raw), header([{'index': 7}]))
    assert error.value.code == 'invalid_audio'


def test_every_truncated_prefix_is_rejected():
    payload = {'packets': [{'stream_index': 7, 'data_hash': HASH}], 'streams': [STREAM]}
    raw = json.dumps(payload).encode()
    for end in range(len(raw)):
        with pytest.raises(packetstream.PacketStreamError):
            packetstream.identity(pieces(raw[:end]), header([STREAM]))


def test_per_value_and_header_limits_are_resource_refusals():
    raw = b'{"packets":[{"stream_index":7,"data_hash":"'+HASH.encode()+b'","side_data_list":["'+b'x'*(1024**2)+b'"]}],"streams":[]}'
    with pytest.raises(packetstream.PacketStreamError) as error:
        packetstream.identity(pieces(raw, packetstream.CHUNK_BYTES), header([STREAM]))
    assert error.value.code == 'verification_too_large'
    for raw in [b' '*(1024**2+1), json.dumps({'streams': [{'index': i} for i in range(65)]}).encode()]:
        with pytest.raises(packetstream.PacketStreamError) as error:
            packetstream.stream_header(raw)
        assert error.value.code == 'verification_too_large'


def test_more_than_32_mib_of_real_parser_input_has_bounded_peak_heap():
    # A fixed-size generated record, not a prebuilt/mirrored large JSON array.
    side = [{'side_data_type': 'fixture', 'payload': 'x'*4096}]
    packet = json.dumps({'stream_index': 7, 'data_hash': HASH, 'side_data_list': side}).encode()
    canonical = json.dumps({'hash': HASH, 'side_data': side}, sort_keys=True, separators=(',', ':')).encode()
    count = 8500
    raw_bytes = len(packet)*count+count-1
    assert raw_bytes > 32*1024**2
    expected = hashlib.sha256(b'{"packets":[')
    for index in range(count):
        if index:
            expected.update(b',')
        expected.update(canonical)
    expected.update(b'],"streams":[{"channels":1,"codec_name":"pcm_s16le","sample_rate":"8000"}]}')
    observed_bytes = 0
    prefix = b'{"packets":['
    suffix = b'],"streams":'+json.dumps([STREAM]).encode()+b'}'
    # The child itself emits >32MiB through the real bounded pipe runner. Neither
    # process constructs a complete JSON array or stores packet output on disk.
    batch = max(1, packetstream.CHUNK_BYTES//(len(packet)+1))
    program = ('import sys;w=sys.stdout.buffer.write;packet='+repr(packet)+';'
               'w('+repr(prefix)+');w(packet);remaining='+str(count-1)+';batch='+str(batch)+'\n'
               'while remaining:\n n=min(batch,remaining);w((b","+packet)*n);remaining-=n\n'
               'w('+repr(suffix)+');sys.stdout.flush()')

    def counted(chunks):
        nonlocal observed_bytes
        for raw in chunks:
            observed_bytes += len(raw)
            yield raw

    streams = header([STREAM])
    tracemalloc.start()
    try:
        with packetstream.process_chunks([sys.executable, '-c', program], timeout=30) as chunks:
            actual = packetstream.identity(counted(chunks), streams)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert observed_bytes == raw_bytes+len(prefix)+len(suffix)
    assert actual == expected.hexdigest()
    assert peak < 6*1024**2, 'Memory must remain independent of the >32MiB packet document'


def assert_reaped(pid_path):
    pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(os.name != 'posix', reason='PID reap observation requires POSIX')
@pytest.mark.parametrize('mode', ['timeout', 'callback_cancel', 'consumer_abort', 'malformed', 'stderr', 'exit'])
def test_real_child_failures_cancel_and_consumer_abort_reap_the_child(tmp_path, mode):
    pid_path = tmp_path/'pid'
    raw = json.dumps({'packets': [{'stream_index': 7, 'data_hash': HASH}], 'streams': [STREAM]}).encode()
    program = 'import os,sys,time;open('+repr(str(pid_path))+',"w").write(str(os.getpid()));'
    if mode == 'malformed':
        program += 'sys.stdout.buffer.write(b"not-json");sys.stdout.flush();time.sleep(20)'
    elif mode == 'stderr':
        program += 'sys.stderr.buffer.write(b"x"*(1024**2+1));sys.stderr.flush();time.sleep(20)'
    elif mode == 'exit':
        program += 'sys.stdout.buffer.write('+repr(raw)+');sys.stdout.flush();sys.exit(3)'
    else:
        program += 'sys.stdout.buffer.write(b" "*65536);sys.stdout.flush();time.sleep(20)'
    cancel = False
    expected = 'verification_too_large' if mode == 'stderr' else 'invalid_audio' if mode in {'malformed', 'exit'} else 'media_cancelled' if mode == 'callback_cancel' else 'media_timeout'
    if mode == 'consumer_abort':
        with pytest.raises(KeyboardInterrupt):
            with packetstream.process_chunks([sys.executable, '-c', program], timeout=3) as chunks:
                next(chunks)
                raise KeyboardInterrupt()
    else:
        with pytest.raises(packetstream.PacketStreamError) as error:
            with packetstream.process_chunks([sys.executable, '-c', program], timeout=1 if mode == 'timeout' else 3,
                                             cancelled=lambda: cancel) as chunks:
                if mode == 'callback_cancel':
                    next(chunks); cancel = True
                packetstream.identity(chunks, header([STREAM]))
        assert error.value.code == expected
    assert_reaped(pid_path)


@pytest.mark.parametrize('stage,code', [('header', 'invalid_audio'), ('packets', 'verification_too_large'), ('decode', 'invalid_audio')])
def test_audit_reports_the_stage_without_turning_resource_refusal_into_damage(tmp_path, monkeypatch, stage, code):
    roots = {key: tmp_path/key for key in ('source', 'work', 'publication')}
    for path in roots.values():
        path.mkdir()
    path = roots['work']/'generated.wav'; path.write_bytes(b'generated stage regression')

    def fail(*_args, **_kwargs):
        raise media.MediaError(code, 'Sanitized fixture failure')

    monkeypatch.setattr(media, 'probe_audio', fail if stage == 'header' else lambda _path: {'audio_stream_count': 1})
    monkeypatch.setattr(media, 'audio_identity', fail if stage == 'packets' else lambda _path: 'a'*64)
    monkeypatch.setattr(media, '_process', fail)
    result = media.audit_audio(path, roots)
    assert result['state'] == 'failed' and result['error'] == code and result['failure_stage'] == stage
    assert 'stderr' not in result and result['sha256'] == media.sha256_file(path)
