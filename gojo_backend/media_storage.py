"""Cloudflare R2 object storage (S3-compatible).

Credentials stay on the backend. The bucket is private.
Callers persist object_key only; signed GET URLs are minted at read time
and must never be written to the database.
"""
import os

import boto3
from botocore.config import Config


DEFAULT_SIGNED_URL_TTL = 21600


class MediaStorageError(RuntimeError):
    pass


def _env(name, default=None):
    value = os.environ.get(name)
    if value is None or str(value).strip() == '':
        return default
    return str(value).strip()


def is_configured():
    return bool(
        _env('R2_ENDPOINT')
        and _env('R2_ACCESS_KEY_ID')
        and _env('R2_SECRET_ACCESS_KEY')
        and _env('R2_BUCKET')
    )


def signed_url_ttl(expires_in=None):
    if expires_in is not None:
        return max(1, int(expires_in))
    raw = _env('R2_SIGNED_URL_TTL_SECONDS', str(DEFAULT_SIGNED_URL_TTL))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_SIGNED_URL_TTL


def _client():
    if not is_configured():
        raise MediaStorageError('r2_not_configured')
    return boto3.client(
        's3',
        endpoint_url=_env('R2_ENDPOINT'),
        aws_access_key_id=_env('R2_ACCESS_KEY_ID'),
        aws_secret_access_key=_env('R2_SECRET_ACCESS_KEY'),
        region_name=_env('R2_REGION', 'auto'),
        config=Config(signature_version='s3v4'),
    )


def _bucket():
    bucket = _env('R2_BUCKET')
    if not bucket:
        raise MediaStorageError('r2_not_configured')
    return bucket


def put_bytes(object_key, data, mime_type):
    if not object_key:
        raise MediaStorageError('missing_object_key')
    body = data if isinstance(data, (bytes, bytearray)) else b''
    _client().put_object(
        Bucket=_bucket(),
        Key=object_key,
        Body=bytes(body),
        ContentType=mime_type or 'application/octet-stream',
    )
    return object_key


def object_exists(object_key):
    if not object_key:
        return False
    try:
        _client().head_object(Bucket=_bucket(), Key=object_key)
        return True
    except Exception:
        return False


def delete_object(object_key):
    if not object_key:
        return False
    _client().delete_object(Bucket=_bucket(), Key=object_key)
    return True


def signed_get_url(object_key, expires_in=None):
    if not object_key:
        raise MediaStorageError('missing_object_key')
    return _client().generate_presigned_url(
        'get_object',
        Params={'Bucket': _bucket(), 'Key': object_key},
        ExpiresIn=signed_url_ttl(expires_in),
    )
