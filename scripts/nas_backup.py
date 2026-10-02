#!/usr/bin/env python3
"""NAS 每日备份：在线 SQLite 快照 + 完整性校验 + 保留最近 N 份。

部署在 NAS 宿主机（/usr/bin/python3，仅用标准库），不依赖 Docker 镜像标签。
crontab（别写 %，cron 会把它当换行）：
  35 4 * * * /usr/bin/python3 /srv/owe-erp/nas_backup.py >> /srv/owe-erp/backup/backup.log 2>&1
只清理本脚本生成的 owe-YYYYMMDD.db，手工快照（owe-pre-* 等）不动。
发票原件归档（v0.26，/vol2/.../owe-erp/invoice-archive）增量镜像到 backup/invoice-archive：只增不删
（归档文件写入后不改，删档案不删镜像，防误删）。
"""
import os
import re
import shutil
import sqlite3
import sys
import time

# 数据目录 = 容器挂载到 /data 的宿主机目录；搬服务器时只改这一个（或在 crontab 行前加 OWE_DATA_DIR=...）
DATA_DIR = os.environ.get("OWE_DATA_DIR", "/srv/owe-erp")
DB = os.environ.get("OWE_DB", os.path.join(DATA_DIR, "owe.db"))
BACKUP_DIR = os.environ.get("OWE_BACKUP_DIR", os.path.join(DATA_DIR, "backup"))
KEEP = int(os.environ.get("OWE_BACKUP_KEEP", "30"))
DAILY = re.compile(r"^owe-\d{8}\.db$")
ARCHIVES = ("invoice-archive", "contract-archive", "seals", "tax-filing")   # 发票原件、合同原件（v0.29）、章图（v0.31）、已申报报表（v0.34）


def mirror_archive() -> int:
    n = 0
    for name in ARCHIVES:
        n += _mirror(os.path.join(DATA_DIR, name), os.path.join(BACKUP_DIR, name))
    return n


def _mirror(src_root: str, dst_root: str) -> int:
    if not os.path.isdir(src_root):
        return 0
    n = 0
    for root, _, files in os.walk(src_root):
        for f in files:
            src = os.path.join(root, f)
            dst = os.path.join(dst_root, os.path.relpath(src, src_root))
            if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                n += 1
    return n


def main():
    stamp = time.strftime("%Y%m%d")
    target = os.path.join(BACKUP_DIR, f"owe-{stamp}.db")
    tmp = target + ".tmp"
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if os.path.exists(tmp):
        os.remove(tmp)

    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if check != "ok":
        os.remove(tmp)
        raise SystemExit(f"integrity_check failed: {check}")
    os.replace(tmp, target)

    dailies = sorted(f for f in os.listdir(BACKUP_DIR) if DAILY.match(f))
    removed = dailies[:-KEEP] if len(dailies) > KEEP else []
    for f in removed:
        os.remove(os.path.join(BACKUP_DIR, f))
    copied = mirror_archive()
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ok {target} "
          f"{os.path.getsize(target)}B kept={len(dailies) - len(removed)} removed={len(removed)} archive+{copied}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} FAIL {e}", file=sys.stderr)
        sys.exit(1)
