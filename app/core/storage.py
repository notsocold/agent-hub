"""文件存储抽象：本地磁盘与 S3 兼容对象存储。

业务代码只依赖 ``FileStorage`` 协议，具体后端由 ``STORAGE_BACKEND`` 决定；
key 统一使用后端无关的相对键（例如 ``knowledge/<id>.md``）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.exceptions import ClientError

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 不同 S3 实现（AWS / MinIO）对「对象不存在」返回的错误码。
_S3_NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


class ObjectNotFoundError(Exception):
    """存储对象不存在；调用方据此判断「原始文件缺失」。"""


class FileStorage(Protocol):
    """后端无关的文件存储协议。"""

    async def put(self, key: str, data: bytes, *, content_type: str = "") -> None:
        """写入对象，同名对象直接覆盖。"""
        ...

    async def get(self, key: str) -> bytes:
        """读取对象字节；对象不存在时抛 :class:`ObjectNotFoundError`。"""
        ...

    async def delete(self, key: str) -> None:
        """删除对象；对象本就不存在时视为成功。"""
        ...

    async def exists(self, key: str) -> bool:
        """判断对象是否存在。"""
        ...


def _normalize_local_key(key: str) -> str:
    """去掉首尾空白与开头的斜杠并校验路径段；含 ``..`` 的 key 一律拒绝，防止目录穿越。"""
    normalized = key.strip().lstrip("/")
    if not normalized:
        raise ValueError("存储 key 不能为空")
    parts = normalized.split("/")
    if any(part in {".", ".."} for part in parts):
        raise ValueError(f"非法存储 key（含上级目录）：{key}")
    return normalized


class LocalFileStorage:
    """本地目录实现；写入走 ``tmp + replace`` 保证原子性（沿用附件模块写法）。"""

    def __init__(self, root: Path) -> None:
        # 初始化时固化根目录，避免之后工作目录变化影响路径拼接。
        self._root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        """把相对 key 解析为根目录内的绝对路径；越界或穿越直接抛 ``ValueError``。"""
        relative = _normalize_local_key(key)
        target = (self._root / relative).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError(f"存储 key 越界：{key}")
        return target

    async def put(self, key: str, data: bytes, *, content_type: str = "") -> None:
        # content_type 只对对象存储有意义，本地实现忽略。
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 随机 tmp 名避免同 key 并发写互相覆盖；失败时清理残留。
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(f"文件不存在：{key}") from exc

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    async def exists(self, key: str) -> bool:
        return self._path(key).is_file()


def _is_s3_not_found(exc: ClientError) -> bool:
    """仅按 ``Error.Code`` 判断对象不存在。

    不能退化为「HTTP 404 即不存在」：``NoSuchBucket`` 等存储故障同样返回 404，
    误判会让上层把配置错误当成「原始文件缺失」。其他 ``ClientError`` 一律上抛。
    """
    response = getattr(exc, "response", None) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    return code in _S3_NOT_FOUND_CODES


class S3FileStorage:
    """S3 兼容对象存储实现；所有 boto3 调用都放进 ``asyncio.to_thread``。"""

    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        # client 允许注入，测试用 stub，CI 不依赖 MinIO。
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def _object_key(self, key: str) -> str:
        # S3 键是扁平字面量，不存在路径穿越语义，因此只去斜杠、不做 ``..`` 校验。
        cleaned = key.strip().lstrip("/")
        if not cleaned:
            raise ValueError("存储 key 不能为空")
        return f"{self._prefix}/{cleaned}" if self._prefix else cleaned

    async def put(self, key: str, data: bytes, *, content_type: str = "") -> None:
        object_key = self._object_key(key)
        extra = {"ContentType": content_type} if content_type else {}
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=object_key,
            Body=data,
            **extra,
        )

    async def get(self, key: str) -> bytes:
        object_key = self._object_key(key)

        def _read() -> bytes:
            try:
                response = self._client.get_object(Bucket=self._bucket, Key=object_key)
            except ClientError as exc:
                if _is_s3_not_found(exc):
                    raise ObjectNotFoundError(f"对象不存在：{object_key}") from exc
                raise
            return response["Body"].read()

        return await asyncio.to_thread(_read)

    async def delete(self, key: str) -> None:
        object_key = self._object_key(key)
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=object_key)

    async def exists(self, key: str) -> bool:
        object_key = self._object_key(key)

        def _head() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=object_key)
            except ClientError as exc:
                if _is_s3_not_found(exc):
                    return False
                raise
            return True

        return await asyncio.to_thread(_head)


_storage: FileStorage | None = None


def get_storage(settings: Settings | None = None) -> FileStorage:
    """按配置返回进程级单例；首次调用后缓存，切换配置需 ``reset_storage()``。"""
    global _storage
    if _storage is None:
        config = settings or get_settings()
        if config.storage_backend == "s3":
            client = boto3.client(
                "s3",
                endpoint_url=config.s3_endpoint_url or None,
                aws_access_key_id=config.s3_access_key,
                aws_secret_access_key=config.s3_secret_key,
                region_name=config.s3_region,
            )
            logger.info(
                "文件存储使用 S3 后端 bucket=%s prefix=%s", config.s3_bucket, config.s3_prefix
            )
            _storage = S3FileStorage(client, config.s3_bucket, prefix=config.s3_prefix)
        else:
            logger.info("文件存储使用本地后端 root=%s", config.knowledge_dir)
            _storage = LocalFileStorage(config.knowledge_dir)
    return _storage


def reset_storage() -> None:
    """测试辅助：清空存储单例缓存（下次 ``get_storage`` 重新按配置构建）。"""
    global _storage
    _storage = None


__all__ = [
    "FileStorage",
    "LocalFileStorage",
    "ObjectNotFoundError",
    "S3FileStorage",
    "get_storage",
    "reset_storage",
]
