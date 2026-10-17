"""Attachment bytes: AIStor (S3) in production, a directory for local runs,
memory for tests. The browser never talks to the store; the API proxies."""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from .config import Settings

log = logging.getLogger("webdoc.storage")


class Store(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> Iterator[bytes]: ...
    def delete(self, key: str) -> None: ...


class MemoryStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, key, data, content_type):
        self.objects[key] = data

    def get(self, key):
        if key not in self.objects:
            raise KeyError(key)
        return iter([self.objects[key]])

    def delete(self, key):
        self.objects.pop(key, None)


class DirStore:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise KeyError(key)
        return p

    def put(self, key, data, content_type):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key):
        p = self._path(key)
        if not p.is_file():
            raise KeyError(key)
        return self._read(p)

    @staticmethod
    def _read(p: Path):
        with p.open("rb") as f:
            while chunk := f.read(64 * 1024):
                yield chunk

    def delete(self, key):
        try:
            os.remove(self._path(key))
        except (FileNotFoundError, KeyError):
            pass


class S3Store:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str):
        from minio import Minio

        u = urlparse(endpoint if "://" in endpoint else "https://" + endpoint)
        self.bucket = bucket
        self.client = Minio(u.netloc, access_key=access_key, secret_key=secret_key,
                            secure=u.scheme == "https", region=region)

    def put(self, key, data, content_type):
        self.client.put_object(self.bucket, key, io.BytesIO(data), len(data),
                               content_type=content_type)

    def get(self, key):
        from minio.error import S3Error

        try:
            res = self.client.get_object(self.bucket, key)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                raise KeyError(key) from None
            raise
        return self._stream(res)

    @staticmethod
    def _stream(res):
        try:
            yield from res.stream(64 * 1024)
        finally:
            res.close()
            res.release_conn()

    def delete(self, key):
        self.client.remove_object(self.bucket, key)


def from_settings(settings: Settings) -> Store | None:
    if settings.s3_endpoint and settings.s3_bucket:
        if not (settings.s3_access_key and settings.s3_secret_key):
            log.warning("S3_ENDPOINT set without S3_ACCESS_KEY/S3_SECRET_KEY: attachments off")
            return None
        return S3Store(settings.s3_endpoint, settings.s3_bucket, settings.s3_access_key,
                       settings.s3_secret_key, settings.s3_region)
    if settings.attachments_dir:
        return DirStore(settings.attachments_dir)
    return None
