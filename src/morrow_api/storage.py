import os
import re
import tempfile
from pathlib import Path

import boto3
from botocore.config import Config


class StorageError(RuntimeError):
    pass


def validate_key(key: str):
    if not re.fullmatch(r"tenants/[0-9a-f-]{36}/artifacts/[0-9a-f-]{36}", key):
        raise ValueError("Invalid storage key")


class LocalStorage:
    def __init__(self, root: Path, limit: int):
        self.root = root.resolve()
        self.limit = limit

    def check(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def path(self, key: str) -> Path:
        validate_key(key)
        path = self.root / key
        if not path.resolve().is_relative_to(self.root):
            raise StorageError("Invalid artifact location")
        return path

    def put(self, key: str, body: bytes):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                tmp = Path(stream.name)
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            tmp.replace(path)
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)

    def get(self, key: str) -> bytes:
        with self.path(key).open("rb") as stream:
            body = stream.read(self.limit + 1)
        if len(body) > self.limit:
            raise StorageError("Artifact exceeds size limit")
        return body

    def delete(self, key: str):
        self.path(key).unlink(missing_ok=True)


class S3Storage:
    def __init__(self, settings):
        if not settings.s3_bucket:
            raise ValueError("S3_BUCKET is required")
        self.bucket = settings.s3_bucket
        self.limit = settings.max_artifact_bytes
        self.client = boto3.client("s3", region_name=settings.s3_region, endpoint_url=settings.s3_endpoint_url, config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}))

    def check(self):
        self.client.head_bucket(Bucket=self.bucket)
        block = self.client.get_public_access_block(Bucket=self.bucket)["PublicAccessBlockConfiguration"]
        if not all(block.get(name) for name in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
            raise StorageError("The artifact bucket must block all public access")

    def put(self, key: str, body: bytes):
        validate_key(key)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/octet-stream", ServerSideEncryption="AES256")

    def get(self, key: str) -> bytes:
        validate_key(key)
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        stream = response["Body"]
        try:
            body = stream.read(self.limit + 1)
            if len(body) > self.limit:
                raise StorageError("Artifact exceeds size limit")
            return body
        finally:
            stream.close()

    def delete(self, key: str):
        validate_key(key)
        self.client.delete_object(Bucket=self.bucket, Key=key)


def storage_for(settings):
    if settings.artifact_backend == "s3":
        return S3Storage(settings)
    return LocalStorage(settings.artifact_root, settings.max_artifact_bytes)
