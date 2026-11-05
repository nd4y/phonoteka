"""Package setup cannot turn missing media dependencies into a successful gate."""
import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1]/'dev/install-ci-codecs.sh'


def run_setup(tmp_path, mode):
    binaries=tmp_path/'bin';binaries.mkdir()
    ready=tmp_path/'ready';calls=tmp_path/'calls'
    if mode=='ready':ready.touch()
    programs={name:'#!/bin/bash\n[[ -f "$CODEC_READY" ]]\n' for name in ('ffmpeg','ffprobe','fpcalc','python3')}
    programs['sudo']='#!/bin/bash\nexec "$@"\n'
    programs['apt-get']='''#!/bin/bash
echo "$*" >> "$CODEC_CALLS"
case "$CODEC_MODE" in
  ready) exit 99 ;;
  missing_after_install) exit 0 ;;
  update_failed) exit 42 ;;
  refresh)
    if [[ "$*" == *update* ]]; then touch "$CODEC_UPDATED"; exit 0; fi
    if [[ -f "$CODEC_UPDATED" ]]; then touch "$CODEC_READY"; exit 0; fi
    exit 42 ;;
esac
exit 98
'''
    for name,content in programs.items():
        path=binaries/name;path.write_text(content);path.chmod(0o755)
    env={**os.environ,'PATH':str(binaries)+os.pathsep+os.environ['PATH'],
         'CODEC_READY':str(ready),'CODEC_CALLS':str(calls),'CODEC_MODE':mode,
         'CODEC_UPDATED':str(tmp_path/'updated')}
    result=subprocess.run(['bash',str(SCRIPT)],env=env,capture_output=True,text=True,timeout=15)
    return result,calls.read_text().splitlines() if calls.exists() else []


def test_ready_tools_do_not_contact_package_repositories(tmp_path):
    result,calls=run_setup(tmp_path,'ready')
    assert result.returncode==0 and not calls


def test_stale_indexes_refresh_once_then_verify_tools(tmp_path):
    result,calls=run_setup(tmp_path,'refresh')
    assert result.returncode==0 and len(calls)==3
    assert 'install -y' in calls[0] and calls[1].endswith('update') and 'install -y' in calls[2]


@pytest.mark.parametrize('mode,expected_calls',[('missing_after_install',1),('update_failed',2)])
def test_install_or_refresh_failure_cannot_pass_media_gate(tmp_path,mode,expected_calls):
    result,calls=run_setup(tmp_path,mode)
    assert result.returncode!=0 and len(calls)==expected_calls
