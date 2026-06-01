import logging
import subprocess
import threading
import time

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)
router = APIRouter()

QMRESTORE_TIMEOUT_SECONDS = 4 * 60 * 60
_RESTORE_LOCK = threading.Lock()


class S3Creds(BaseModel):
    endpoint: str = Field(min_length=4)
    access_key: str = Field(min_length=4)
    secret_key: str = Field(min_length=4)
    bucket: str = Field(min_length=1)
    region: str = 'garage'


class VMRestoreRequest(BaseModel):
    s3: S3Creds
    key: str = Field(min_length=1, max_length=1024)
    target_vmid: int = Field(ge=100, le=8999)
    target_name: str = Field(min_length=1, max_length=64)
    vm_ip: str = Field(min_length=7)
    bridge: str = 'vmbr1'
    storage: str = ''
    start_after_restore: bool = True


def _build_s3_client(creds: S3Creds):
    return boto3.client(
        's3',
        endpoint_url=creds.endpoint,
        aws_access_key_id=creds.access_key,
        aws_secret_access_key=creds.secret_key,
        region_name=creds.region,
        config=Config(
            signature_version='s3v4',
            s3={'addressing_style': 'path'},
            connect_timeout=10,
            read_timeout=300,
            retries={'max_attempts': 2},
        ),
    )


@router.post('/v1/vms/restore')
def vm_restore(payload: VMRestoreRequest, request: Request) -> dict:
    if not _RESTORE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='another restore is already in progress on this host')
    try:
        return _do_restore(payload, request.app.state.config)
    finally:
        _RESTORE_LOCK.release()


def _decompressor_cmd(key: str) -> list[str] | None:
    k = key.lower()
    if k.endswith('.zst') or k.endswith('.zstd'):
        return ['zstd', '-d', '-c']
    if k.endswith('.gz') or k.endswith('.gzip'):
        return ['gunzip', '-c']
    if k.endswith('.lzo'):
        return ['lzop', '-d', '-c']
    return None


def _do_restore(payload: VMRestoreRequest, agent_config) -> dict:
    storage = payload.storage or agent_config.pve_storage
    s3 = _build_s3_client(payload.s3)

    try:
        obj = s3.get_object(Bucket=payload.s3.bucket, Key=payload.key)
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(status_code=502, detail=f'cannot fetch dump from S3: {exc}')
    body = obj['Body']

    qmrestore_cmd = [
        'qmrestore', '-',
        str(payload.target_vmid),
        '--storage', storage,
        '--unique', '1',
        '--force', '1',
    ]
    decomp_cmd = _decompressor_cmd(payload.key)

    logger.info('qmrestore start vmid=%s key=%s storage=%s decomp=%s',
                payload.target_vmid, payload.key, storage, decomp_cmd)
    start_ts = time.time()

    decomp_proc = None
    if decomp_cmd:
        decomp_proc = subprocess.Popen(
            decomp_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        qmrestore_proc = subprocess.Popen(
            qmrestore_cmd,
            stdin=decomp_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        decomp_proc.stdout.close()
        sink = decomp_proc.stdin
    else:
        qmrestore_proc = subprocess.Popen(
            qmrestore_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sink = qmrestore_proc.stdin

    pipe_error = None
    try:
        for chunk in body.iter_chunks(chunk_size=8 * 1024 * 1024):
            if not chunk:
                continue
            try:
                sink.write(chunk)
            except BrokenPipeError as exc:
                pipe_error = exc
                break
    except (BotoCoreError, ClientError, OSError) as exc:
        pipe_error = exc
    finally:
        try:
            body.close()
        except Exception:
            pass
        try:
            sink.close()
        except OSError:
            pass

    decomp_stderr = ''
    if decomp_proc is not None:
        try:
            decomp_rc = decomp_proc.wait(timeout=QMRESTORE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            decomp_proc.kill()
            decomp_proc.wait()
            decomp_rc = -1
        if decomp_proc.stderr is not None:
            try:
                decomp_stderr = decomp_proc.stderr.read().decode('utf-8', errors='replace')[-2000:]
            finally:
                try:
                    decomp_proc.stderr.close()
                except OSError:
                    pass
    else:
        decomp_rc = 0

    try:
        rc = qmrestore_proc.wait(timeout=QMRESTORE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        qmrestore_proc.kill()
        qmrestore_proc.wait()
        rc = -1

    stderr_out = ''
    if qmrestore_proc.stderr is not None:
        try:
            stderr_out = qmrestore_proc.stderr.read().decode('utf-8', errors='replace')[-4000:]
        finally:
            try:
                qmrestore_proc.stderr.close()
            except OSError:
                pass
    if qmrestore_proc.stdout is not None:
        try:
            qmrestore_proc.stdout.close()
        except OSError:
            pass

    duration = round(time.time() - start_ts, 2)

    if rc != 0 or decomp_rc != 0 or pipe_error is not None:
        detail = stderr_out or decomp_stderr or (str(pipe_error) if pipe_error else 'unknown error')
        raise HTTPException(
            status_code=500,
            detail=f'qmrestore exit {rc} (decomp={decomp_rc}) after {duration}s: {detail}',
        )

    try:
        subprocess.run(
            ['qm', 'set', str(payload.target_vmid),
             '--name', payload.target_name,
             '--net0', f'virtio,bridge={payload.bridge}',
             '--ipconfig0', f'ip={payload.vm_ip}/16,gw=10.10.0.1',
             '--agent', 'enabled=1'],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        raise HTTPException(
            status_code=500,
            detail=f'qm set failed: {(exc.stderr or exc.stdout or "")[-2000:]}',
        )

    started = False
    start_error = ''
    if payload.start_after_restore:
        attempts = 0
        max_attempts = 6
        while attempts < max_attempts:
            attempts += 1
            try:
                subprocess.run(
                    ['qm', 'start', str(payload.target_vmid)],
                    check=True, capture_output=True, text=True, timeout=120,
                )
                started = True
                start_error = ''
                break
            except subprocess.CalledProcessError as exc:
                start_error = (exc.stderr or exc.stdout or '')[-2000:].strip()
                lower = start_error.lower()
                if 'lock' in lower or 'in progress' in lower or 'busy' in lower:
                    logger.info('qm start vmid=%s locked (attempt %s/%s), waiting...',
                                payload.target_vmid, attempts, max_attempts)
                    time.sleep(3)
                    continue
                logger.warning('qm start failed vmid=%s: %s', payload.target_vmid, start_error)
                break
            except subprocess.TimeoutExpired:
                start_error = 'qm start timed out after 120s'
                logger.warning('qm start timeout vmid=%s', payload.target_vmid)
                break

    logger.info('qmrestore ok vmid=%s duration=%ss started=%s',
                payload.target_vmid, duration, started)
    return {
        'ok': True,
        'vmid': payload.target_vmid,
        'name': payload.target_name,
        'vm_ip': payload.vm_ip,
        'storage': storage,
        'started': started,
        'start_error': start_error,
        'duration_seconds': duration,
    }
