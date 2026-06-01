import logging
import os
import shlex
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from steward.services.code_hash import AGENT_ROOT, compute_agent_hash


logger = logging.getLogger(__name__)
router = APIRouter()


class SelfUpdateRequest(BaseModel):
    tarball_url: str = Field(min_length=8)
    expected_hash: str = Field(default='', max_length=128)


UPDATE_SCRIPT = """#!/bin/bash
set -e
LOG=/var/log/steward-update.log
{{
  sleep 1
  echo "[$(date -Is)] update: stopping agent"
  systemctl stop steward || true
  echo "[$(date -Is)] update: removing old steward"
  rm -rf {root}/steward {root}/systemd {root}/requirements.txt
  echo "[$(date -Is)] update: extracting new"
  tar -xzf {tarball} -C {root}
  rm -f {tarball}
  if [ -f {root}/requirements.txt ]; then
    echo "[$(date -Is)] update: pip install"
    {venv_pip} install --quiet --upgrade -r {root}/requirements.txt || true
  fi
  if [ -f {root}/systemd/steward.service ]; then
    cp {root}/systemd/steward.service /etc/systemd/system/steward.service
    systemctl daemon-reload
  fi
  echo "[$(date -Is)] update: starting agent"
  systemctl start steward
  echo "[$(date -Is)] update: done"
}} >> "$LOG" 2>&1
"""


@router.get('/v1/admin/self-update/log')
def self_update_log(lines: int = 200) -> dict:
    log_path = Path('/var/log/steward-update.log')
    info = {
        'path': str(log_path),
        'exists': log_path.exists(),
        'current_hash': compute_agent_hash(),
    }
    if not log_path.exists():
        info['content'] = ''
        info['total_lines'] = 0
        return info
    try:
        with log_path.open('r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
        keep = max(int(lines), 1)
        info['content'] = ''.join(all_lines[-keep:])
        info['total_lines'] = len(all_lines)
        info['returned_lines'] = min(len(all_lines), keep)
        info['size_bytes'] = log_path.stat().st_size
    except OSError as exc:
        info['content'] = ''
        info['error'] = str(exc)
    return info


@router.post('/v1/admin/self-update', status_code=202)
def self_update(payload: SelfUpdateRequest) -> dict:
    logger.warning('self-update requested; fetching %s', payload.tarball_url)
    try:
        tmp = tempfile.NamedTemporaryFile(
            prefix='steward-update-',
            suffix='.tar.gz',
            delete=False,
            dir='/tmp',
        )
        tmp_path = Path(tmp.name)
        tmp.close()
        req = urllib.request.Request(
            payload.tarball_url,
            headers={
                'User-Agent': 'steward/1.0',
                'Accept': 'application/gzip, application/octet-stream, */*',
            },
        )
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            status = getattr(resp, 'status', None) or resp.getcode()
            logger.warning('self-update download status=%s', status)
            with tmp_path.open('wb') as fp:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fp.write(chunk)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode('utf-8', errors='replace')[:400]
        except Exception:
            body = ''
        logger.exception('self-update HTTPError')
        raise HTTPException(
            status_code=502,
            detail=f'tarball download failed: HTTP {exc.code} {exc.reason} body={body!r}',
        ) from exc
    except urllib.error.URLError as exc:
        logger.exception('self-update URLError')
        raise HTTPException(
            status_code=502,
            detail=f'tarball download failed (URLError): {exc.reason} url={payload.tarball_url}',
        ) from exc
    except Exception as exc:
        logger.exception('self-update unexpected error')
        raise HTTPException(
            status_code=502,
            detail=f'tarball download failed: {type(exc).__name__}: {exc}',
        ) from exc

    if tmp_path.stat().st_size < 100:
        raise HTTPException(status_code=502, detail='tarball too small')

    root = AGENT_ROOT
    venv_pip = root / '.venv' / 'bin' / 'pip'
    script_text = UPDATE_SCRIPT.format(
        root=shlex.quote(str(root)),
        tarball=shlex.quote(str(tmp_path)),
        venv_pip=shlex.quote(str(venv_pip)),
    )

    script_path = Path('/tmp/steward-update.sh')
    script_path.write_text(script_text, encoding='utf-8')
    script_path.chmod(0o755)

    try:
        subprocess.run(
            [
                'systemd-run',
                '--unit', 'steward-update',
                '--description', 'steward self-update',
                '--no-block',
                '--collect',
                '/bin/bash', str(script_path),
            ],
            check=True,
            timeout=10.0,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=500, detail=f'spawn update script failed: {exc}') from exc

    logger.warning('self-update scheduled — agent will restart in ~1s')
    return {
        'ok': True,
        'status': 'scheduled',
        'current_hash': compute_agent_hash(),
        'expected_hash': payload.expected_hash,
        'pid': os.getpid(),
    }
