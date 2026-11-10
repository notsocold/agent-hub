"""文件存储抽象单元测试。

不依赖数据库、不访问网络：本地后端用 ``tmp_path``，S3 后端注入 stub client。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.core import storage as storage_module
from app.core.config import Settings
from app.core.storage import (
    LocalFileStorage,
    ObjectNotFoundError,
    S3FileStorage,
    get_storage,
    reset_storage,
)


@pytest.fixture(autouse=True)
def _clean_storage_cache():
    """每个用例前后清空进程级存储缓存，避免用例互相污染。"""
    reset_storage()
    yield
    reset_storage()


class _Body:
    """模拟 boto3 的 StreamingBody，只需要 ``read()``。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


def _client_error(
    code: str, operation: str = "GetObject", *, http_status: int | None = None
) -> ClientError:
    response: dict = {"Error": {"Code": code}}
    if http_status is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": http_status}
    return ClientError(response, operation)


class _StubS3Client:
    """记录调用参数的 S3 stub；``found=False`` 时模拟对象不存在。"""

    def __init__(
        self,
        payload: bytes = b"x",
        *,
        found: bool = True,
        error_code: str = "",
        http_status: int | None = None,
    ) -> None:
        self.payload = payload
        self.found = found
        self.error_code = error_code
        self.http_status = http_status
        self.calls: dict[str, dict] = {}
        self.thread_names: list[str] = []

    def _record(self, name: str, kwargs: dict) -> None:
        self.calls[name] = kwargs
        self.thread_names.append(threading.current_thread().name)

    def put_object(self, **kwargs) -> None:
        self._record("put", kwargs)

    def get_object(self, **kwargs) -> dict:
        self._record("get", kwargs)
        if not self.found:
            raise _client_error(
                self.error_code or "NoSuchKey", "GetObject", http_status=self.http_status
            )
        return {"Body": _Body(self.payload)}

    def delete_object(self, **kwargs) -> None:
        self._record("delete", kwargs)

    def head_object(self, **kwargs) -> dict:
        self._record("head", kwargs)
        if not self.found:
            raise _client_error(
                self.error_code or "404", "HeadObject", http_status=self.http_status
            )
        return {}


async def test_local_roundtrip(tmp_path: Path) -> None:
    s = LocalFileStorage(tmp_path)
    await s.put("kb/a.txt", b"hello", content_type="text/plain")
    assert await s.exists("kb/a.txt")
    assert await s.get("kb/a.txt") == b"hello"
    await s.delete("kb/a.txt")
    assert not await s.exists("kb/a.txt")


async def test_local_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(ObjectNotFoundError):
        await LocalFileStorage(tmp_path).get("nope.txt")


async def test_local_put_is_atomic_and_leaves_no_tmp(tmp_path: Path) -> None:
    s = LocalFileStorage(tmp_path)
    await s.put("kb/a.txt", b"hello")
    # 真实文件内容正确，且 tmp 文件已被 replace 掉。
    assert (tmp_path / "kb" / "a.txt").read_bytes() == b"hello"
    assert list(tmp_path.rglob("*.tmp")) == []


async def test_local_put_failure_cleans_tmp(tmp_path: Path) -> None:
    s = LocalFileStorage(tmp_path)
    # 故意传错类型让 write_bytes 在创建临时文件后失败，验证 finally 清理残留。
    with pytest.raises(TypeError):
        await s.put("kb/a.txt", "not-bytes")  # type: ignore[arg-type]
    assert not (tmp_path / "kb" / "a.txt").exists()
    assert [p for p in (tmp_path / "kb").iterdir() if p.is_file()] == []


async def test_local_delete_missing_is_noop(tmp_path: Path) -> None:
    await LocalFileStorage(tmp_path).delete("nope.txt")


@pytest.mark.parametrize(
    "key",
    ["../etc/passwd", "kb/../../etc/passwd", "..", "/../etc/passwd", "kb/.."],
)
async def test_local_rejects_parent_traversal(tmp_path: Path, key: str) -> None:
    s = LocalFileStorage(tmp_path)
    with pytest.raises(ValueError):
        await s.put(key, b"x")
    with pytest.raises(ValueError):
        await s.get(key)
    with pytest.raises(ValueError):
        await s.exists(key)
    assert list(tmp_path.iterdir()) == []


async def test_local_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        await LocalFileStorage(root).put("link/a.txt", b"x")
    assert not (outside / "a.txt").exists()


async def test_local_strips_leading_slash(tmp_path: Path) -> None:
    s = LocalFileStorage(tmp_path)
    await s.put("/kb/a.txt", b"hello")
    assert (tmp_path / "kb" / "a.txt").read_bytes() == b"hello"


async def test_s3_uses_injected_client() -> None:
    stub = _StubS3Client()
    s3 = S3FileStorage(stub, "bucket", prefix="knowledge/")
    await s3.put("a.md", b"x", content_type="text/markdown")
    assert stub.calls["put"]["Bucket"] == "bucket"
    assert stub.calls["put"]["Key"] == "knowledge/a.md"
    assert stub.calls["put"]["Body"] == b"x"
    assert stub.calls["put"]["ContentType"] == "text/markdown"
    assert await s3.get("a.md") == b"x"
    assert stub.calls["get"]["Bucket"] == "bucket"
    assert stub.calls["get"]["Key"] == "knowledge/a.md"
    assert await s3.exists("a.md") is True
    assert stub.calls["head"]["Key"] == "knowledge/a.md"
    await s3.delete("a.md")
    assert stub.calls["delete"]["Key"] == "knowledge/a.md"
    # 全部 boto3 调用都应发生在 asyncio 的线程池里，不阻塞事件循环。
    assert stub.thread_names
    assert all(name != threading.main_thread().name for name in stub.thread_names)


async def test_s3_missing_maps_to_object_not_found() -> None:
    stub = _StubS3Client(found=False)
    s3 = S3FileStorage(stub, "bucket")
    with pytest.raises(ObjectNotFoundError):
        await s3.get("a.md")
    assert await s3.exists("a.md") is False


async def test_s3_other_client_error_propagates() -> None:
    stub = _StubS3Client(found=False, error_code="AccessDenied")
    s3 = S3FileStorage(stub, "bucket")
    with pytest.raises(ClientError):
        await s3.get("a.md")


async def test_s3_bucket_error_with_404_is_not_treated_as_missing() -> None:
    # NoSuchBucket（bucket 名写错/被删）也返回 HTTP 404，但不能当成「对象不存在」吞掉。
    stub = _StubS3Client(found=False, error_code="NoSuchBucket", http_status=404)
    s3 = S3FileStorage(stub, "bucket")
    with pytest.raises(ClientError):
        await s3.get("a.md")
    with pytest.raises(ClientError):
        await s3.exists("a.md")


def test_get_storage_local_is_cached(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/x",
        storage_backend="local",
        knowledge_dir=tmp_path,
    )
    first = get_storage(settings)
    assert isinstance(first, LocalFileStorage)
    assert get_storage() is first  # 进程级缓存：再次获取返回同一实例


def test_get_storage_s3_builds_client(monkeypatch) -> None:
    created: dict = {}
    stub = _StubS3Client()

    def fake_client(service_name: str, **kwargs):
        created["service"] = service_name
        created["kwargs"] = kwargs
        return stub

    monkeypatch.setattr(storage_module.boto3, "client", fake_client)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/x",
        storage_backend="s3",
        s3_endpoint_url="http://127.0.0.1:9002",
        s3_bucket="agent-hub",
        s3_access_key="access-key",
        s3_secret_key="secret-key",
        s3_region="us-east-1",
        s3_prefix="knowledge/",
    )
    storage = get_storage(settings)
    assert isinstance(storage, S3FileStorage)
    assert created["service"] == "s3"
    kwargs = created["kwargs"]
    assert kwargs["endpoint_url"] == "http://127.0.0.1:9002"
    assert kwargs["aws_access_key_id"] == "access-key"
    assert kwargs["aws_secret_access_key"] == "secret-key"
    assert kwargs["region_name"] == "us-east-1"
