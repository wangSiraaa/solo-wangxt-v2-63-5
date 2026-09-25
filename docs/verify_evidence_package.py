#!/usr/bin/env python3
"""
证据封存包离线校验工具（纯标准库，无需 Django / 数据库）。

用法：
    python3 docs/verify_evidence_package.py <封存包导出.zip>

校验内容：
1. 包内 manifest.json 的字节级 SHA-256 == manifest.sha256 记录的摘要；
2. 清单中每个证据文件的实际 SHA-256 / 大小与清单一致；
3. 全部通过则退出码 0，并把还原出的唯一处罚单号、事件编号与证据清单
   以 JSON 打印到 stdout；任何一项不符退出码 1，参数/文件错误退出码 2。
"""
import hashlib
import json
import sys
import zipfile


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_export_archive(data: bytes) -> dict:
    checks = []
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
        names = set(zf.namelist())
        manifest_names = sorted(n for n in names if n.endswith("/manifest.json") and n.count("/") == 1)
        if len(manifest_names) != 1:
            return {"ok": False, "error": "未找到唯一的 manifest.json", "checks": []}
        manifest_name = manifest_names[0]
        base = manifest_name.rsplit("/", 1)[0]

        manifest_bytes = zf.read(manifest_name)
        manifest = json.loads(manifest_bytes.decode("utf-8"))

        computed_hash = sha256_hex(manifest_bytes)
        recorded_hash = None
        sha_name = f"{base}/manifest.sha256"
        if sha_name in names:
            recorded_hash = zf.read(sha_name).decode("ascii").split()[0]
        checks.append({
            "check": "manifest_hash",
            "ok": recorded_hash == computed_hash,
            "expected": recorded_hash,
            "actual": computed_hash,
        })

        files_total = 0
        files_ok = 0
        for photo in manifest.get("photos", []):
            file_info = photo.get("file") or {}
            path = file_info.get("path")
            if not path:
                continue
            files_total += 1
            entry = {"check": "file_sha256", "photo_id": photo.get("photo_id"),
                     "path": path, "expected_sha256": file_info.get("sha256")}
            arcname = f"{base}/files/{path}"
            if arcname not in names:
                entry.update(ok=False, actual_sha256=None, error="missing")
            else:
                payload = zf.read(arcname)
                digest = sha256_hex(payload)
                ok = digest == file_info.get("sha256") and len(payload) == file_info.get("size")
                entry.update(ok=ok, actual_sha256=digest)
                if not ok:
                    entry["error"] = "digest_mismatch"
            checks.append(entry)
            if entry["ok"]:
                files_ok += 1

    return {
        "ok": all(c["ok"] for c in checks),
        "package_no": manifest.get("package_no"),
        "kind": manifest.get("kind"),
        "parent_package_no": manifest.get("parent_package_no"),
        "penalty_no": (manifest.get("penalty") or {}).get("penalty_no"),
        "event_no": (manifest.get("event") or {}).get("event_no"),
        "contractor_name": (manifest.get("penalty") or {}).get("contractor_name"),
        "current_version_no": ((manifest.get("penalty") or {}).get("current_version") or {}).get("version_no"),
        "manifest_hash": computed_hash,
        "files_total": files_total,
        "files_ok": files_ok,
        "checks": checks,
    }


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        with open(argv[1], "rb") as fh:
            data = fh.read()
    except OSError as exc:
        print(f"无法读取文件：{exc}", file=sys.stderr)
        return 2
    try:
        report = verify_export_archive(data)
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"封存包无效：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
