"""Blob storage for uploaded workbooks and run artifacts: local disk or S3.

Both backends hand out time-limited download URLs: S3 presigned GETs, or
HMAC-signed links served by the API for local development.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*(/[a-zA-Z0-9][a-zA-Z0-9_.\-]*){1,4}$")


class StorageError(KeyError):
    pass


def check_key(key: str) -> str:
    if not KEY_RE.match(key) or ".." in key:
        raise StorageError(f"invalid storage key {key!r}")
    return key


class Storage(Protocol):
    kind: str

    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def url(self, key: str, filename: str, expires_s: int = 900) -> str: ...


@dataclass
class LocalStorage:
    root: Path
    base_url: str
    secret: bytes
    kind: str = "local"

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / check_key(key)).resolve()
        if self.root.resolve() not in p.parents:
            raise StorageError(key)
        return p

    def put(self, key: str, data: bytes, content_type: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.is_file():
            raise StorageError(key)
        return p.read_bytes()

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).is_file()
        except StorageError:
            return False

    def sign(self, key: str, exp: int, filename: str) -> str:
        msg = f"{key}\n{exp}\n{filename}".encode()
        return hmac.new(self.secret, msg, hashlib.sha256).hexdigest()

    def verify(self, key: str, exp: int, filename: str, sig: str) -> bool:
        return exp >= time.time() and hmac.compare_digest(self.sign(key, exp, filename), sig)

    def url(self, key: str, filename: str, expires_s: int = 900) -> str:
        exp = int(time.time()) + expires_s
        q = urlencode({"exp": exp, "name": filename, "sig": self.sign(check_key(key), exp, filename)})
        return f"{self.base_url.rstrip('/')}/v1/files/{quote(key)}?{q}"


@dataclass
class S3Storage:
    bucket: str
    prefix: str = ""
    endpoint_url: str | None = None          # MinIO / LocalStack
    public_endpoint_url: str | None = None   # host the *browser* uses for presigned URLs
    region: str | None = None
    kind: str = "s3"

    def __post_init__(self) -> None:
        import boto3
        from botocore.config import Config
        cfg = Config(signature_version="s3v4", s3={"addressing_style": "path" if self.endpoint_url else "auto"},
                     retries={"max_attempts": 4, "mode": "standard"})
        self.client = boto3.client("s3", endpoint_url=self.endpoint_url, region_name=self.region, config=cfg)
        self.presign_client = (boto3.client("s3", endpoint_url=self.public_endpoint_url, region_name=self.region, config=cfg)
                               if self.public_endpoint_url else self.client)

    def _k(self, key: str) -> str:
        return f"{self.prefix.strip('/')}/{check_key(key)}" if self.prefix else check_key(key)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._k(key), Body=data, ContentType=content_type,
                               ServerSideEncryption="AES256")

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()
        except self.client.exceptions.NoSuchKey:
            raise StorageError(key) from None

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except ClientError:
            return False

    def url(self, key: str, filename: str, expires_s: int = 900) -> str:
        safe = re.sub(r'[^A-Za-z0-9_.\- ]', "_", filename)
        return self.presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._k(key),
                    "ResponseContentDisposition": f'attachment; filename="{safe}"'},
            ExpiresIn=expires_s)


def storage_from_env(base_url: str = "http://localhost:8000") -> Storage:
    bucket = os.environ.get("ARTIFACT_BUCKET")
    if bucket:
        return S3Storage(bucket=bucket, prefix=os.environ.get("ARTIFACT_PREFIX", ""),
                         endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
                         public_endpoint_url=os.environ.get("S3_PUBLIC_ENDPOINT_URL") or None,
                         region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    secret = os.environ.get("LOCAL_URL_SECRET", "").encode() or os.urandom(32)
    return LocalStorage(root=Path(os.environ.get("LOCAL_STORAGE_DIR", "./.data")),
                        base_url=os.environ.get("PUBLIC_BASE_URL", base_url), secret=secret)
