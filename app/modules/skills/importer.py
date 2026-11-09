"""技能包 zip 导入校验与 SKILL.md 解析。

安全要求：拒绝路径穿越/绝对路径/符号链接/大小写冲突/超限文件；
只接受文本类型文件；``scripts/`` 目录忽略不执行。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

import yaml

from app.core.config import get_settings
from app.core.errors import PayloadTooLargeError, ValidationFailedError

_ALLOWED_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_H1_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)

_IGNORED_DIRS = {"scripts"}
_IGNORED_EXTENSIONS = {".py", ".sh", ".exe", ".bin", ".so", ".dll", ".bat", ".cmd", ".jar"}


@dataclass
class SkillFileData:
    path: str
    content: str
    size_bytes: int


@dataclass
class ParsedSkill:
    slug: str
    name: str
    description: str
    content: str
    version: str
    metadata: dict[str, Any]
    files: list[SkillFileData] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    estimated_tokens: int = 0
    size_bytes: int = 0


def estimate_tokens(text: str) -> int:
    """保守估算：CJK 1 token/字，其余 0.3 token/字符。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + max(0, int((len(text) - cjk) * 0.3))


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValidationFailedError("SKILL.md 必须以 YAML frontmatter（--- 块）开头", code="skill_invalid")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            block = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            break
    else:
        raise ValidationFailedError("SKILL.md frontmatter 未闭合", code="skill_invalid")
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise ValidationFailedError(f"SKILL.md frontmatter 不是合法 YAML：{exc}", code="skill_invalid") from exc
    if not isinstance(parsed, dict):
        raise ValidationFailedError("SKILL.md frontmatter 必须是键值对", code="skill_invalid")
    return parsed, body


def _validate_frontmatter(meta: dict[str, Any]) -> tuple[str, str, str]:
    name = meta.get("name")
    if not isinstance(name, str) or not _SLUG_PATTERN.match(name):
        raise ValidationFailedError(
            "frontmatter.name 必填且需匹配 ^[a-z0-9][a-z0-9-]{1,63}$", code="skill_invalid"
        )
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValidationFailedError("frontmatter.description 必填", code="skill_invalid")
    if len(description) > 500:
        raise ValidationFailedError("frontmatter.description 不能超过 500 字符", code="skill_invalid")
    version = str(meta.get("version") or "1.0")[:32]
    return name, description.strip(), version


def _display_name(meta_name: str, body: str) -> str:
    match = _H1_PATTERN.search(body)
    if match:
        title = match.group(1).strip()
        if title:
            return title[:128]
    return meta_name


def _normalize_path(name: str) -> str | None:
    """把 zip 条目名转成安全的相对路径；非法返回 None。"""
    name = name.replace("\\", "/")
    if name.startswith("/"):
        return None
    if ":" in name and re.match(r"^[a-zA-Z]:", name):  # Windows 盘符
        return None
    parts = [part for part in name.split("/") if part not in {"", "."}]
    if not parts:
        return None
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _ignored_path(path: str, mode: int) -> bool:
    top = path.split("/")[0].lower()
    if top in _IGNORED_DIRS:
        return True
    lower = path.lower()
    if any(lower.endswith(ext) for ext in _IGNORED_EXTENSIONS):
        return True
    return mode & 0o111 != 0  # 可执行位一律忽略


def parse_skill_zip(raw: bytes, *, max_upload_bytes: int | None = None) -> ParsedSkill:
    """解析并校验技能包 zip；失败抛 ``skill_invalid`` / ``payload_too_large``。"""
    settings = get_settings()
    max_upload = max_upload_bytes or settings.skill_max_upload_bytes
    if len(raw) > max_upload:
        raise PayloadTooLargeError(
            f"技能包超过 {max_upload // (1024 * 1024)}MB 上限"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > settings.skill_max_files:
                raise ValidationFailedError(
                    f"技能包文件数超过 {settings.skill_max_files} 个上限", code="skill_invalid"
                )
            entries: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:  # 符号链接
                    raise ValidationFailedError("技能包不允许包含符号链接", code="skill_invalid")
                path = _normalize_path(info.filename)
                if path is None:
                    raise ValidationFailedError(
                        f"非法文件路径：{info.filename}", code="skill_invalid"
                    )
                lower = path.lower()
                if lower in entries:
                    raise ValidationFailedError(
                        f"大小写冲突路径：{path}", code="skill_invalid"
                    )
                entries[lower] = info

            skill_md_entries = [
                path for path in entries if path.endswith("/skill.md") or path == "skill.md"
            ]
            if not skill_md_entries:
                raise ValidationFailedError("技能包缺少 SKILL.md", code="skill_invalid")
            if len(skill_md_entries) > 1:
                raise ValidationFailedError("技能包只能包含一个 SKILL.md", code="skill_invalid")
            skill_md_path = skill_md_entries[0]

            with archive.open(entries[skill_md_path]) as handle:
                skill_raw = handle.read(settings.skill_max_skill_md_bytes + 1)
            if len(skill_raw) > settings.skill_max_skill_md_bytes:
                raise ValidationFailedError("SKILL.md 超过 200KB 上限", code="skill_invalid")
            try:
                skill_text = skill_raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationFailedError("SKILL.md 必须是 UTF-8 编码", code="skill_invalid") from exc

            meta, body = _parse_frontmatter(skill_text)
            slug, description, version = _validate_frontmatter(meta)
            body_tokens = estimate_tokens(body)
            if body_tokens > settings.skill_max_body_tokens:
                raise ValidationFailedError(
                    f"SKILL.md 正文约 {body_tokens} tokens，超过 {settings.skill_max_body_tokens} 上限",
                    code="skill_invalid",
                )

            files: list[SkillFileData] = []
            ignored: list[str] = []
            total_size = 0
            for path, info in entries.items():
                if path == skill_md_path:
                    continue
                with archive.open(info) as handle:
                    data = handle.read(settings.skill_max_file_bytes + 1)
                if len(data) > settings.skill_max_file_bytes:
                    raise ValidationFailedError(
                        f"文件 {path} 超过 {settings.skill_max_file_bytes // 1024}KB 上限",
                        code="skill_invalid",
                    )
                ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
                if ext not in _ALLOWED_EXTENSIONS or _ignored_path(path, (info.external_attr >> 16) & 0o777):
                    ignored.append(path)
                    continue
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValidationFailedError(
                        f"文件 {path} 必须是 UTF-8 文本", code="skill_invalid"
                    ) from exc
                files.append(SkillFileData(path=path, content=content, size_bytes=len(data)))
                total_size += len(data)
    except zipfile.BadZipFile as exc:
        raise ValidationFailedError("不是合法的 zip 压缩包", code="skill_invalid") from exc

    display = _display_name(slug, body)
    metadata = {key: value for key, value in meta.items() if key not in {"name", "description", "version"}}
    return ParsedSkill(
        slug=slug,
        name=display,
        description=description,
        content=body,
        version=version,
        metadata=metadata,
        files=sorted(files, key=lambda item: item.path),
        ignored=ignored,
        estimated_tokens=body_tokens,
        size_bytes=total_size + len(skill_raw),
    )
