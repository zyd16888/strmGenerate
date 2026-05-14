#!/usr/bin/env python3
"""
strm_sync.py - Google Drive → 本地 STRM + 元数据同步工具
用途：
  1. 初次迁移：将网盘现有结构全量生成 STRM 并同步元数据
  2. 增量同步：检测网盘新增/删除，同步 STRM 和元数据
  3. 触发 Emby 按目录精确刷新（不全量扫库）
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import requests

# ─────────────────────────────────────────────
# 配置区 - 按你的实际情况修改
# ─────────────────────────────────────────────
CONFIG = {
    # rclone 远端名称和路径（网盘侧根）
    "rclone_remote": "gdrive:Media",

    # rclone 挂载的本地路径（STRM 文件内写入此路径前缀）
    "rclone_mount": "/mnt/gdrive/Media",

    # 本地媒体库根目录（Emby 指向这里）
    "local_media": "/local/media",

    # Emby 配置
    "emby_host": "http://localhost:8096",
    "emby_api_key": "YOUR_EMBY_API_KEY",

    # 视频扩展名（这些文件只生成 STRM，不下载本体）
    "video_extensions": {
        ".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov",
        ".wmv", ".flv", ".rmvb", ".iso", ".m2ts", ".bdmv"
    },

    # 元数据扩展名（这些文件下载到本地）
    "metadata_extensions": {
        ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".tbn",
        ".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"
    },

    # rclone 并发参数
    "rclone_transfers": 8,
    "rclone_checkers": 16,

    # 状态文件路径
    "state_file": "/local/media/.strm_sync_state.json",

    # 日志文件路径（None 表示仅 stdout）
    "log_file": "/var/log/strm_sync.log",
}
# ─────────────────────────────────────────────


def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    log_file = CONFIG.get("log_file")
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as e:
            print(f"WARN: 日志文件不可写 {log_file}: {e}", file=sys.stderr)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


setup_logging()
log = logging.getLogger(__name__)


def build_scope(subdir: str | None) -> dict:
    """
    根据可选子目录构造实际作用域。
    所有后续操作（远端扫描、本地写入、清理、Emby 通知）都限定在 scope 内。
    """
    if subdir:
        subdir = subdir.strip("/").strip("\\")
        remote = f"{CONFIG['rclone_remote'].rstrip('/')}/{subdir}"
        local = Path(CONFIG["local_media"]) / subdir
        mount = f"{CONFIG['rclone_mount'].rstrip('/')}/{subdir}"
    else:
        remote = CONFIG["rclone_remote"]
        local = Path(CONFIG["local_media"])
        mount = CONFIG["rclone_mount"].rstrip("/")
    return {"remote": remote, "local": local, "mount": mount, "subdir": subdir}


def run_rclone(args: list, capture=True) -> subprocess.CompletedProcess:
    cmd = ["rclone"] + args
    log.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if result.returncode != 0 and capture:
        log.warning(f"rclone stderr: {result.stderr[:500]}")
    return result


def list_remote_full(scope: dict) -> list[dict]:
    """
    一次性列出网盘指定范围的所有文件（取代之前的 sync + lsf 两次扫描）。
    返回: [{"path", "ext", "size", "modtime"}, ...]
    """
    log.info(f"扫描网盘: {scope['remote']}")

    result = run_rclone([
        "lsjson",
        scope["remote"],
        "--recursive",
        "--files-only",
        "--fast-list",
    ])

    if result.returncode != 0:
        log.error("rclone lsjson 失败")
        return []

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        log.error(f"解析 rclone 输出失败: {e}")
        return []

    files = []
    for item in raw:
        path = item.get("Path", "")
        if not path:
            continue
        ext = Path(path).suffix.lower()
        files.append({
            "path": path,
            "ext": ext,
            "size": item.get("Size", 0),
            "modtime": item.get("ModTime", ""),
        })

    log.info(f"网盘共找到 {len(files)} 个文件")
    return files


def sync_metadata_files(metadata_files: list[dict], scope: dict) -> bool:
    """
    用 rclone copy --files-from 精确传输元数据，避免再做一次远端 walk。
    """
    if not metadata_files:
        log.info("无元数据文件需要同步")
        return True

    log.info(f"同步 {len(metadata_files)} 个元数据文件...")
    scope["local"].mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tf:
        for f in metadata_files:
            tf.write(f["path"] + "\n")
        list_file = tf.name

    try:
        result = run_rclone([
            "copy",
            scope["remote"],
            str(scope["local"]),
            "--files-from", list_file,
            "--transfers", str(CONFIG["rclone_transfers"]),
            "--checkers", str(CONFIG["rclone_checkers"]),
            "--use-server-modtime",
            "--update",
            "--progress",
            "--stats", "1s",
        ], capture=False)
        log.info("元数据同步完成")
        return result.returncode == 0
    finally:
        try:
            os.unlink(list_file)
        except OSError:
            pass


def generate_strm_files(video_files: list[dict], scope: dict) -> tuple[int, int, set]:
    """根据视频文件列表生成/更新 STRM 文件。"""
    created = 0
    skipped = 0
    changed_dirs = set()

    log.info(f"处理 {len(video_files)} 个视频文件")

    for item in video_files:
        remote_path = item["path"]
        stem = Path(remote_path).stem
        parent = Path(remote_path).parent

        strm_local = scope["local"] / parent / f"{stem}.strm"
        mount_path = f"{scope['mount']}/{remote_path}"

        if strm_local.exists():
            existing = strm_local.read_text(encoding="utf-8").strip()
            if existing == mount_path:
                skipped += 1
                continue

        strm_local.parent.mkdir(parents=True, exist_ok=True)
        strm_local.write_text(mount_path, encoding="utf-8")
        log.info(f"  [STRM] {strm_local.relative_to(CONFIG['local_media'])}")
        created += 1
        changed_dirs.add(str(strm_local.parent))

    log.info(f"STRM 完成: 新建 {created} 个，跳过 {skipped} 个")
    return created, skipped, changed_dirs


def cleanup_orphan_files(remote_files: list[dict], scope: dict) -> tuple[int, set]:
    """
    清理 scope 范围内本地存在但网盘已删除的 STRM 与元数据文件。
    单次 rglob 扫描覆盖两种文件类型。
    """
    log.info(f"检查孤立文件（范围: {scope['local']}）...")

    expected_strm_stems = set()
    expected_metadata = set()
    for f in remote_files:
        rel = Path(f["path"])
        if f["ext"] in CONFIG["video_extensions"]:
            expected_strm_stems.add(str(rel.parent / rel.stem))
        elif f["ext"] in CONFIG["metadata_extensions"]:
            expected_metadata.add(str(rel))

    deleted = 0
    changed_dirs = set()
    local_root = scope["local"]

    if not local_root.exists():
        log.info("本地作用域不存在，跳过清理")
        return 0, set()

    for f in local_root.rglob("*"):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        rel = f.relative_to(local_root)

        if ext == ".strm":
            stem_path = str(rel.parent / rel.stem)
            if stem_path not in expected_strm_stems:
                log.info(f"  [清理 STRM] {rel}")
                f.unlink()
                deleted += 1
                changed_dirs.add(str(f.parent))
        elif ext in CONFIG["metadata_extensions"]:
            if str(rel) not in expected_metadata:
                log.info(f"  [清理 META] {rel}")
                f.unlink()
                deleted += 1
                changed_dirs.add(str(f.parent))

    # 向上递归清理空目录
    for d in sorted(changed_dirs, key=len, reverse=True):
        p = Path(d)
        while p != local_root and p.exists():
            try:
                p.rmdir()
                log.info(f"  [清理目录] {p.relative_to(CONFIG['local_media'])}")
                p = p.parent
            except OSError:
                break

    log.info(f"孤立文件清理完成: 删除 {deleted} 个")
    return deleted, changed_dirs


def notify_emby_refresh(changed_dirs: set):
    """
    用 /Library/Media/Updated 按路径精确通知 Emby，
    Emby 只会重扫这些目录而不是整库。
    """
    if not changed_dirs:
        log.info("无变动目录，跳过 Emby 通知")
        return

    api_key = CONFIG["emby_api_key"]
    if api_key == "YOUR_EMBY_API_KEY":
        log.warning("未配置 Emby API Key，跳过 Emby 通知")
        return

    url = f"{CONFIG['emby_host']}/Library/Media/Updated"
    payload = {
        "Updates": [
            {"Path": d, "UpdateType": "Created"} for d in sorted(changed_dirs)
        ]
    }

    try:
        resp = requests.post(
            url,
            params={"api_key": api_key},
            json=payload,
            timeout=15,
        )
        if resp.status_code in (200, 204):
            log.info(f"Emby 已通知 {len(changed_dirs)} 个变动路径")
        else:
            log.warning(f"Emby 通知失败 HTTP {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as e:
        log.warning(f"Emby 通知失败: {e}")


def load_state() -> dict:
    state_file = Path(CONFIG["state_file"])
    if state_file.exists():
        with open(state_file, encoding="utf-8") as f:
            return json.load(f)
    return {"last_sync": None, "total_strm": 0}


def save_state(state: dict):
    state_file = Path(CONFIG["state_file"])
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, state_file)


def run_sync(subdir: str | None = None, full_cleanup: bool = False):
    start = datetime.now()
    scope = build_scope(subdir)

    log.info("=" * 60)
    log.info(f"开始同步 | {start.strftime('%Y-%m-%d %H:%M:%S')}")
    if subdir:
        log.info(f"作用域: {scope['remote']} → {scope['local']}")
    log.info("=" * 60)

    state = load_state()

    remote_files = list_remote_full(scope)
    if not remote_files:
        log.error("网盘文件清单为空或获取失败，终止")
        return

    video_files = [f for f in remote_files if f["ext"] in CONFIG["video_extensions"]]
    metadata_files = [f for f in remote_files if f["ext"] in CONFIG["metadata_extensions"]]
    other = len(remote_files) - len(video_files) - len(metadata_files)
    log.info(f"分类: 视频 {len(video_files)} | 元数据 {len(metadata_files)} | 其它（忽略） {other}")

    all_changed_dirs = set()

    sync_metadata_files(metadata_files, scope)

    _, _, strm_changed = generate_strm_files(video_files, scope)
    all_changed_dirs.update(strm_changed)

    if full_cleanup:
        _, cleanup_changed = cleanup_orphan_files(remote_files, scope)
        all_changed_dirs.update(cleanup_changed)

    notify_emby_refresh(all_changed_dirs)

    local_root = Path(CONFIG["local_media"])
    local_strm_count = sum(1 for _ in local_root.rglob("*.strm")) if local_root.exists() else 0
    state["last_sync"] = datetime.now().isoformat()
    state["total_strm"] = local_strm_count
    save_state(state)

    elapsed = (datetime.now() - start).total_seconds()
    log.info(f"同步完成 | 耗时 {elapsed:.1f}s | 本地 STRM 总数: {local_strm_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Google Drive → STRM 同步工具")
    parser.add_argument(
        "--subdir",
        type=str,
        default=None,
        help="只同步网盘下指定子目录（相对路径，如 'Movies/Test'），用于小批量测试"
    )
    parser.add_argument(
        "--full-cleanup",
        action="store_true",
        help="同时清理本地孤立的 STRM 和元数据文件（建议每周运行一次）"
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="只同步元数据，不处理 STRM"
    )
    args = parser.parse_args()

    if args.metadata_only:
        scope = build_scope(args.subdir)
        files = list_remote_full(scope)
        metadata = [f for f in files if f["ext"] in CONFIG["metadata_extensions"]]
        sync_metadata_files(metadata, scope)
    else:
        run_sync(subdir=args.subdir, full_cleanup=args.full_cleanup)
