#!/usr/bin/env python3
"""Fetch new/changed MLTD text assets and sync them to ParaTranz as source CSVs.

Pipeline
--------
1. Fetch the latest asset index (matsurihi version API + CloudFront msgpack index).
2. Diff it against a committed manifest (``state/manifest.json``) using the
   per-file bundle hash/size, so only new or changed bundles are touched --
   nothing is downloaded just to be compared.
3. Download only those bundles, decrypt the embedded TextAsset (AES-192-CBC),
   and convert the ``.gtx`` payload to the ParaTranz CSV format
   (``key,original,translation,note``, UTF-8 BOM, CRLF).
4. Upload: create missing files (``POST /projects/{id}/files``) and update the
   source text of changed files (``POST /projects/{id}/files/{fileId}`` --
   the endpoint that only touches originals, never translations).
5. Persist the updated manifest so the next run is again a small diff.

Config
------
``PARATRANZ_TOKEN`` / ``PARATRANZ_PROJECT_ID`` env vars, or the matching CLI
flags. The AES key/IV below are the game's public asset encryption constants.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import msgpack
import requests
import UnityPy
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

CDN_BASE = "https://d2sf4w9bkv485c.cloudfront.net"
ASSET_ROOT = "production/2018/Android"
VERSION_API = "https://api.matsurihi.me/api/mltd/v2/version/latest"
PARATRANZ_BASE = "https://paratranz.cn/api"

# MLTD asset encryption: AES-192-CBC.
KEY = base64.b64decode("rT8Pie5RxTdzHxeW91xxhAFhdW2g1IbJ")
IV = base64.b64decode("TkCziuvxqFMSLF+tzKNoXQ==")

DEFAULT_MANIFEST = "state/manifest.json"
DEFAULT_ALIASES = "state/aliases.json"
DEFAULT_WORK_DIR = ".work"
DEFAULT_FILTER = ".gtx"
DEFAULT_CONCURRENCY = 8
DOWNLOAD_RETRIES = 3
UPLOAD_RETRIES = 3


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# Asset index
# --------------------------------------------------------------------------- #


def normalize_items(raw_items: Any) -> dict[str, dict[str, Any]]:
    """Normalize one msgpack index entry to ``{name: {hash, name, size}}``."""
    items: dict[str, dict[str, Any]] = {}
    for key, value in raw_items.items():
        if isinstance(value, (list, tuple)):
            items[str(key)] = {
                "Hash": str(value[0]),
                "Name": str(value[1]),
                "Size": int(value[2]),
            }
        elif isinstance(value, dict):
            items[str(key)] = {
                "Hash": str(value["Hash"]),
                "Name": str(value["Name"]),
                "Size": int(value["Size"]),
            }
    return items


def fetch_index(index_file: str | None = None, timeout: int = 60) -> dict[str, Any]:
    """Return ``{"version": int, "items": {logical: {Hash, Name, Size}}}``."""
    if index_file:
        with open(index_file, encoding="utf-8") as f:
            data = json.load(f)
        log(f"使用本地 index: {index_file} (version {data['Version']})")
        return {
            "version": int(data["Version"]),
            "items": normalize_items(data["Items"]),
        }

    log("获取最新资源版本信息...")
    resp = requests.get(VERSION_API, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    version = int(payload["asset"]["version"])
    index_name = payload["asset"]["indexName"]

    index_url = f"{CDN_BASE}/{version}/{ASSET_ROOT}/{index_name}"
    log(f"下载资源 index: {index_url}")
    resp = requests.get(index_url, timeout=timeout)
    resp.raise_for_status()

    return {"version": version, "items": parse_index_blob(resp.content)}


def parse_index_blob(content: bytes) -> dict[str, dict[str, Any]]:
    """Parse the CDN's msgpack asset index into ``{logical: {Hash, Name, Size}}``."""
    unpacked = msgpack.unpackb(content)
    raw_items = unpacked[0] if isinstance(unpacked, list) and unpacked else unpacked
    return normalize_items(raw_items)


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def load_manifest(path: str) -> dict[str, Any]:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"version": None, "items": {}}


def save_manifest(path: str, manifest: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def compute_diff(
    current_items: dict[str, dict[str, Any]],
    manifest_items: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return (to_process, removed). A file is processed when its bundle
    hash/size differs from the manifest (new or changed)."""
    to_process: dict[str, dict[str, Any]] = {}
    for name, item in current_items.items():
        prev = manifest_items.get(name)
        if (
            prev is None
            or str(prev.get("hash", "")) != str(item["Hash"])
            or int(prev.get("size", -1)) != int(item["Size"])
        ):
            to_process[name] = item
    removed = [name for name in manifest_items if name not in current_items]
    return to_process, removed


# --------------------------------------------------------------------------- #
# Download / decrypt / convert
# --------------------------------------------------------------------------- #


def download_bundle(
    name: str,
    item: dict[str, Any],
    version: int,
    download_dir: Path,
) -> tuple[str, str | None]:
    dest = download_dir / name
    if dest.exists() and dest.stat().st_size == int(item["Size"]):
        return name, None  # already on disk (e.g. local rehearsal)

    url = f"{CDN_BASE}/{version}/{ASSET_ROOT}/{item['Name']}"
    error = "unknown error"
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 200:
                dest.write_bytes(resp.content)
                if dest.stat().st_size != int(item["Size"]):
                    error = "downloaded size mismatch"
                else:
                    return name, None
            else:
                error = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            error = str(exc)
        if attempt < DOWNLOAD_RETRIES - 1:
            time.sleep(2 ** attempt)
    return name, error


def decrypt_bundle(bundle_path: Path) -> list[tuple[str, bytes]]:
    """Return [(text_asset_name, decrypted_bytes), ...] for one bundle."""
    env = UnityPy.load(str(bundle_path))
    # 文件名必须以 index 中的逻辑名（小写）为准：部分 bundle 内 TextAsset 的
    # m_Name 是大写（如 cd_jp -> CD_jp），但 ParaTranz 上的文件都是小写名。
    logical_name = bundle_path.name[:-8] if bundle_path.name.lower().endswith(".unity3d") else bundle_path.stem
    extracted: list[tuple[str, bytes]] = []
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        script = getattr(data, "m_Script", None) or getattr(data, "script", None)
        if script is None:
            continue
        if isinstance(script, str):
            script = script.encode("utf-8", "surrogateescape")
        elif not isinstance(script, (bytes, bytearray)):
            script = bytes(script)
        if len(script) % AES.block_size != 0:
            raise ValueError("TextAsset 长度不是 AES 块大小的整数倍，可能未加密或格式异常")

        plain = AES.new(KEY, AES.MODE_CBC, IV).decrypt(bytes(script))
        try:
            plain = unpad(plain, AES.block_size)
        except ValueError:
            pass  # keep raw block; some assets are not PKCS7 padded

        asset_name = logical_name
        if extracted:
            asset_name = f"{asset_name}_{obj.path_id}"
        extracted.append((asset_name, plain))
    return extracted


def gtx_to_rows(content: bytes) -> list[list[str]]:
    """Parse a decrypted .gtx payload into ParaTranz CSV rows."""
    text = content.decode("utf-8")
    rows: list[list[str]] = []
    for entry in text.split("|"):
        entry = entry.strip()
        if not entry or "^" not in entry:
            continue
        key, original = entry.split("^", 1)
        rows.append([key.strip(), original.strip(), "", ""])
    return rows


def write_csv(path: Path, rows: list[list[str]]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        writer.writerows(rows)


# --------------------------------------------------------------------------- #
# ParaTranz upload
# --------------------------------------------------------------------------- #


def get_file_map(project_id: str, token: str) -> dict[str, int]:
    """Map ParaTranz file basename -> file id for the project."""
    url = f"{PARATRANZ_BASE}/projects/{project_id}/files"
    resp = requests.get(url, headers={"Authorization": token}, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("files") or data.get("data") or []
    mapping: dict[str, int] = {}
    for f in data:
        name = str(f.get("name") or f.get("path") or "")
        mapping[Path(name).name] = int(f["id"])
    return mapping


def expected_csv_name(bundle_name: str) -> str:
    """主 TextAsset 对应的 CSV 名（bundle 逻辑名去掉 .unity3d 再加 .csv）。"""
    logical = bundle_name[:-8] if bundle_name.lower().endswith(".unity3d") else bundle_name
    return f"{logical}.csv"


def load_aliases(path: str) -> dict[str, str]:
    """读取别名映射 {index里的bundle名: 已上传文件对应的bundle名}。

    用于游戏 index 里的重复/拼写变体条目（如 ``pecial_...`` 与 ``special_...``
    内容相同），避免补传时创建重复文件。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_bundle(bundle_name: str, aliases: dict[str, str]) -> str:
    return aliases.get(bundle_name, bundle_name)


def target_csv_names(
    bundle_name: str,
    asset_names: list[str],
    aliases: dict[str, str],
) -> list[str]:
    """每个 TextAsset 对应的上传文件名；别名 bundle 的主资产沿用别名文件名。"""
    resolved = resolve_bundle(bundle_name, aliases)
    if resolved == bundle_name:
        return [f"{name}.csv" for name in asset_names]
    return [expected_csv_name(resolved)] + [f"{name}.csv" for name in asset_names[1:]]


def find_missing_on_site(
    manifest_items: dict[str, Any],
    current_items: dict[str, dict[str, Any]],
    file_map: dict[str, int],
    aliases: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """找出 manifest 声称已同步、但 ParaTranz 上没有的文件。

    返回 (可补传, 无法补传)：
    - 可补传：文件仍在当前 index 中，可以重新下载并上传；
    - 无法补传：文件已被游戏从 index 移除，无法从 CDN 重建。
    """
    repairable: list[str] = []
    unrecoverable: list[str] = []
    for name in manifest_items:
        if expected_csv_name(resolve_bundle(name, aliases or {})) in file_map:
            continue
        (repairable if name in current_items else unrecoverable).append(name)
    return repairable, unrecoverable


def should_skip_upload(prev: dict[str, Any], text_hash: str, in_map: bool, force: bool) -> bool:
    """决定某个 CSV 是否跳过上传：
    - 网站上没有该文件（新建/补传）-> 必须传；
    - 补传模式强制 -> 必须传；
    - 解密文本与 manifest 记录一致 -> 跳过；
    - 否则 -> 更新。
    """
    if not in_map or force:
        return False
    return bool(prev.get("text")) and prev.get("text") == text_hash


def upload_csv(
    csv_path: Path,
    project_id: str,
    token: str,
    file_map: dict[str, int],
    filename: str | None = None,
) -> tuple[str, str]:
    """Upload one CSV; returns (status, detail). status in ok/new/updated/skipped/failed."""
    filename = filename or csv_path.name
    existing_id = file_map.get(filename)

    for attempt in range(UPLOAD_RETRIES):
        # Re-resolve the id on later attempts: a concurrent create may have raced
        # us, or we may have just learned the file exists after a 400.
        if existing_id is None:
            existing_id = file_map.get(filename)

        if existing_id is not None:
            url = f"{PARATRANZ_BASE}/projects/{project_id}/files/{existing_id}"
            action = "update"
        else:
            url = f"{PARATRANZ_BASE}/projects/{project_id}/files"
            action = "create"

        try:
            with open(csv_path, "rb") as f:
                resp = requests.post(
                    url,
                    headers={"Authorization": token},
                    files={"file": (filename, f, "text/csv")},
                    timeout=120,
                )
        except requests.RequestException as exc:
            detail = f"{action} 请求异常: {exc}"
        else:
            if resp.status_code in (200, 201):
                if action == "create":
                    try:
                        body = resp.json()
                        created = (body.get("file") or {}).get("id") if isinstance(body, dict) else None
                        if created:
                            file_map[filename] = int(created)
                    except ValueError:
                        pass
                else:
                    # 服务器按内容哈希判定文件无需更新（上传内容与现有文件一致）。
                    # 这种情况不算“更新成功”，避免统计虚报。
                    try:
                        body = resp.json()
                        if isinstance(body, dict) and str(body.get("status")) == "hashMatched":
                            return "skipped", "hashMatched（服务器判定内容一致，无需更新）"
                    except ValueError:
                        pass
                return ("created" if action == "create" else "updated"), "OK"

            detail = f"{action} HTTP {resp.status_code}: {resp.text[:300]}"
            if (
                action == "create"
                and resp.status_code == 400
                and any(s in resp.text.lower() for s in ("exists", "已存在", "conflict", "重复"))
            ):
                # File already exists under this name -> refresh map and update.
                try:
                    file_map.update(get_file_map(project_id, token))
                    existing_id = file_map.get(filename)
                except requests.RequestException as exc:
                    detail = f"刷新文件列表失败: {exc}"

        if attempt < UPLOAD_RETRIES - 1:
            time.sleep(3 * (attempt + 1))

    return "failed", detail


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", default=os.environ.get("PARATRANZ_TOKEN"))
    parser.add_argument("--project-id", default=os.environ.get("PARATRANZ_PROJECT_ID"))
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--aliases-file", default=DEFAULT_ALIASES)
    parser.add_argument("--work-dir", default=DEFAULT_WORK_DIR)
    parser.add_argument("--index-file", help="使用本地 index.json（离线调试用）")
    parser.add_argument("--filter", default=DEFAULT_FILTER, help="逻辑文件名包含该串的资源（默认 .gtx）")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--init-from-current", action="store_true", help="以当前 index 初始化 manifest，不下载不上传")
    parser.add_argument("--dry-run", action="store_true", help="只计算差异并打印，不做任何下载/上传")
    parser.add_argument("--skip-upload", action="store_true", help="下载、解密、转换并更新 manifest，但不上传")
    parser.add_argument("--repair-missing", action="store_true", help="上传前与 ParaTranz 文件列表对账，发现 manifest 声称已同步但网站上缺失的文件自动补传")
    args = parser.parse_args()

    index = fetch_index(args.index_file)
    current_items = {
        name: item for name, item in index["items"].items() if args.filter.lower() in name.lower()
    }

    manifest = load_manifest(args.manifest)
    manifest_items = manifest.get("items", {})
    aliases = load_aliases(args.aliases_file)

    if args.init_from_current:
        new_items = {
            name: {"hash": item["Hash"], "size": item["Size"], "text": ""}
            for name, item in current_items.items()
        }
        save_manifest(args.manifest, {"version": index["version"], "items": new_items})
        log(f"manifest 已初始化: {len(new_items)} 个文件 -> {args.manifest}")
        return 0

    to_process, removed = compute_diff(current_items, manifest_items)
    log(f"index 共 {len(current_items)} 个匹配文件，本次需要处理 {len(to_process)} 个（新增或变更）")
    if removed:
        log(f"注意：{len(removed)} 个文件已从 index 移除（不会自动删除 ParaTranz 上的文件）")

    if args.dry_run:
        for name in sorted(to_process)[:50]:
            log(f"  [待处理] {name}")
        if len(to_process) > 50:
            log(f"  ... 其余 {len(to_process) - 50} 个")
        return 0

    # 0. 上传模式下先拉取 ParaTranz 文件列表；开启补传时顺便对账。
    needs_upload = not args.skip_upload
    file_map: dict[str, int] = {}
    repair_names: set[str] = set()
    if needs_upload:
        if not args.token or not args.project_id:
            log("缺少 PARATRANZ_TOKEN / PARATRANZ_PROJECT_ID，无法上传（可用 --skip-upload 仅做本地处理）")
            return 2
        log("获取 ParaTranz 项目文件列表...")
        try:
            file_map = get_file_map(args.project_id, args.token)
        except requests.RequestException as exc:
            log(f"获取 ParaTranz 文件列表失败: {exc}")
            return 2
        if args.repair_missing:
            repairable, unrecoverable = find_missing_on_site(manifest_items, current_items, file_map, aliases)
            if repairable:
                log(f"发现 {len(repairable)} 个 manifest 已记录但网站上缺失的文件，将补传：")
                for name in sorted(repairable):
                    log(f"  [补传] {name}")
                repair_names.update(repairable)
                for name in repairable:
                    to_process.setdefault(name, current_items[name])
            if unrecoverable:
                log(f"注意：{len(unrecoverable)} 个缺失文件已不在当前 index，无法从 CDN 重建，需要手动处理：")
                for name in sorted(unrecoverable)[:20]:
                    log(f"  [无法补传] {name}")

    if not to_process:
        log("没有新增、变更或需要补传的文件，无需处理。")
        return 0

    # 1. Download changed bundles.
    work_dir = Path(args.work_dir)
    download_dir = work_dir / "downloads"
    decrypted_dir = work_dir / "decrypted"
    csv_dir = work_dir / "csv"
    for d in (download_dir, decrypted_dir, csv_dir):
        d.mkdir(parents=True, exist_ok=True)

    log("开始下载变更资源...")
    download_failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(download_bundle, name, item, index["version"], download_dir): name
            for name, item in to_process.items()
        }
        for future in as_completed(futures):
            name, error = future.result()
            if error:
                download_failures.append(name)
                log(f"  [下载失败] {name}: {error}")
            else:
                log(f"  [下载完成] {name}")

    if download_failures:
        log(f"!! {len(download_failures)} 个资源下载失败: {', '.join(download_failures[:10])}")
        return 1

    # 2. Decrypt + convert.
    log("开始解密并转换为 CSV...")
    prepared: dict[str, list[tuple[str, bytes]]] = {}
    convert_failures: list[tuple[str, str]] = []
    for name in sorted(to_process):
        bundle_path = download_dir / name
        try:
            extracted = decrypt_bundle(bundle_path)
        except Exception as exc:
            convert_failures.append((name, str(exc)))
            log(f"  [解密失败] {name}: {exc}")
            continue
        if not extracted:
            convert_failures.append((name, "bundle 中没有 TextAsset"))
            log(f"  [解密失败] {name}: bundle 中没有 TextAsset")
            continue

        prepared[name] = extracted
        text_hash_parts = []
        for asset_name, content in extracted:
            text_hash_parts.append(asset_name.encode("utf-8") + b"\x00" + content)
            try:
                rows = gtx_to_rows(content)
            except UnicodeDecodeError as exc:
                convert_failures.append((name, f"{asset_name} 不是 UTF-8: {exc}"))
                break
            write_csv(csv_dir / f"{asset_name}.csv", rows)
        else:
            log(f"  [转换完成] {name} -> {len(extracted)} 个 CSV")
            continue
        prepared.pop(name, None)

    if convert_failures:
        log(f"!! {len(convert_failures)} 个资源解密/转换失败")
        return 1

    # 3. Upload.
    upload_failures: list[str] = []
    stats = {"created": 0, "updated": 0, "skipped": 0}
    for name, extracted in prepared.items():
        prev = manifest_items.get(name, {})
        text_hash = hashlib.sha256(b"".join(content for _, content in extracted)).hexdigest()
        force = name in repair_names
        targets = target_csv_names(name, [asset_name for asset_name, _ in extracted], aliases)
        if not needs_upload:
            stats["skipped"] += 1
        else:
            ok = True
            detail = ""
            for (asset_name, _), target in zip(extracted, targets):
                csv_path = csv_dir / f"{asset_name}.csv"
                if not csv_path.exists():
                    ok = False
                    detail = f"{target} 不存在"
                    break
                in_map = target in file_map
                if should_skip_upload(prev, text_hash, in_map, force):
                    stats["skipped"] += 1
                    log(f"  [内容未变] {target}（与上次上传一致，跳过）")
                    continue
                status, detail = upload_csv(csv_path, args.project_id, args.token, file_map, filename=target)
                if status == "failed":
                    ok = False
                    break
                stats[status] += 1
                log(f"  [{status}] {target}" + (f"：{detail}" if status == "skipped" else ""))
            if not ok:
                upload_failures.append(name)
                log(f"  [上传失败] {name}: {detail}")
                continue

        manifest_items[name] = {
            "hash": to_process[name]["Hash"],
            "size": to_process[name]["Size"],
            "text": text_hash,
        }

    # 4. Persist manifest (successful/unchanged entries only).
    for name in removed:
        manifest_items.pop(name, None)
    save_manifest(args.manifest, {"version": index["version"], "items": manifest_items})
    log(f"manifest 已更新: {args.manifest} (version {index['version']})")

    log(
        f"完成：新建 {stats['created']}，更新 {stats['updated']}，跳过 {stats['skipped']}"
        + (f"，失败 {len(upload_failures)}" if upload_failures else "")
    )
    return 1 if upload_failures else 0


if __name__ == "__main__":
    sys.exit(main())
