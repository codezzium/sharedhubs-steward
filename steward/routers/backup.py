import logging
import subprocess
import threading
import time

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)
router = APIRouter()

VZDUMP_TIMEOUT_SECONDS = 4 * 60 * 60
_BACKUP_LOCK = threading.Lock()


class S3Creds(BaseModel):
    endpoint: str = Field(min_length=4)
    access_key: str = Field(min_length=4)
    secret_key: str = Field(min_length=4)
    bucket: str = Field(min_length=1)
    region: str = 'garage'


class VMBackupRequest(BaseModel):
    s3: S3Creds
    key: str = Field(min_length=1, max_length=1024)
    mode: str = Field(default='suspend', pattern=r'^(snapshot|suspend|stop)$')
    compress: str = Field(default='zstd', pattern=r'^(zstd|gzip|lzo|0)$')


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


@router.post('/v1/vms/{vmid}/backup')
def vm_backup(vmid: int, payload: VMBackupRequest) -> dict:
    if not _BACKUP_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='another backup is already in progress on this host')
    try:
        return _do_backup(vmid, payload)
    finally:
        _BACKUP_LOCK.release()


def _do_backup(vmid: int, payload: VMBackupRequest) -> dict:
    s3 = _build_s3_client(payload.s3)

    cmd = [
        'vzdump', str(vmid),
        '--stdout',
        '--mode', payload.mode,
        '--compress', payload.compress,
    ]

    logger.info('vzdump start vmid=%s key=%s mode=%s compress=%s',
                vmid, payload.key, payload.mode, payload.compress)
    start_ts = time.time()

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    transfer_cfg = TransferConfig(
        multipart_threshold=8 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=4,
        use_threads=True,
    )

    upload_error = None
    try:
        s3.upload_fileobj(proc.stdout, payload.s3.bucket, payload.key, Config=transfer_cfg)
    except (BotoCoreError, ClientError, OSError) as exc:
        upload_error = exc
    finally:
        if proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass

    try:
        rc = proc.wait(timeout=VZDUMP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        rc = -1

    stderr_out = ''
    if proc.stderr is not None:
        try:
            stderr_out = proc.stderr.read().decode('utf-8', errors='replace')[-4000:]
        finally:
            try:
                proc.stderr.close()
            except OSError:
                pass

    duration = round(time.time() - start_ts, 2)

    if rc != 0 or upload_error is not None:
        try:
            s3.delete_object(Bucket=payload.s3.bucket, Key=payload.key)
        except (BotoCoreError, ClientError):
            logger.warning('failed to clean up partial S3 object %s', payload.key)
        if upload_error is not None:
            raise HTTPException(status_code=502, detail=f'upload failed: {upload_error}')
        raise HTTPException(
            status_code=500,
            detail=f'vzdump exit {rc} after {duration}s: {stderr_out}',
        )

    size = 0
    try:
        head = s3.head_object(Bucket=payload.s3.bucket, Key=payload.key)
        size = head.get('ContentLength', 0) or 0
    except (BotoCoreError, ClientError):
        pass

    logger.info('vzdump ok vmid=%s key=%s size=%s duration=%ss',
                vmid, payload.key, size, duration)
    return {
        'ok': True,
        'vmid': vmid,
        'key': payload.key,
        'size': size,
        'duration_seconds': duration,
        'mode': payload.mode,
        'compress': payload.compress,
    }
