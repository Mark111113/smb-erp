"""数据目录（v0.29）：一个 OWE_DATA_DIR 装下全部状态，搬服务器 = 拷这一个目录。

    <OWE_DATA_DIR>/
      owe.db               数据库
      invoice-archive/     发票原件（发票档案上传）
      contract-archive/    合同原件（合同详情上传）
      backup/              每日备份（scripts/nas_backup.py，宿主机 cron）

兼容：OWE_DB 单独指定数据库时，数据目录默认取它所在目录；OWE_ARCHIVE 仍可单独覆盖发票归档目录。
库里只存相对路径，目录整体挪动后无需改数据。
"""
import os
from pathlib import Path

_REPO_DATA = Path(__file__).resolve().parent.parent / "data"


def data_dir() -> Path:
    if os.environ.get("OWE_DATA_DIR"):
        return Path(os.environ["OWE_DATA_DIR"])
    if os.environ.get("OWE_DB"):
        return Path(os.environ["OWE_DB"]).parent
    return _REPO_DATA


def db_path() -> str:
    return os.environ.get("OWE_DB") or str(data_dir() / "owe.db")


def invoice_archive() -> Path:
    return Path(os.environ.get("OWE_ARCHIVE") or data_dir() / "invoice-archive")


def contract_archive() -> Path:
    return Path(os.environ.get("OWE_CONTRACT_ARCHIVE") or data_dir() / "contract-archive")
