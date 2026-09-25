"""
封存清单的规范化序列化与摘要工具。

清单(manifest)是封存包不可变性的核心：同一份内容必须在任何机器、任何
Python 进程里序列化出**逐字节一致**的 canonical JSON，摘要才可离线复算。

规则：
* sort_keys=True、ensure_ascii=False、固定分隔符；
* 末尾保留一个换行，便于与命令行工具(sha256sum 等)对齐；
* 不允许 NaN/Infinity（Python json 默认会放行，这里显式禁止）。
"""
import hashlib
import json
from typing import Any

CANONICAL_SEPARATORS = (",", ":")


class ManifestError(ValueError):
    """清单无法规范化（出现非有限数值等）。"""


def canonical_json_bytes(data: Any) -> bytes:
    """把清单结构序列化为确定性的 UTF-8 字节串。"""
    try:
        text = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=CANONICAL_SEPARATORS,
            allow_nan=False,
        )
    except (ValueError, OverflowError) as exc:  # pragma: no cover - 数据装配处已保证
        raise ManifestError(f"清单包含不可序列化的值: {exc}") from exc
    return (text + "\n").encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_manifest(data: Any) -> tuple[bytes, str]:
    """返回 (canonical 字节, sha256)。封存与离线校验必须共用本函数。"""
    raw = canonical_json_bytes(data)
    return raw, sha256_hex(raw)


def file_sha256(path, chunk_size: int = 1024 * 1024) -> str:
    """流式计算磁盘文件摘要（证据图片可能较大）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
