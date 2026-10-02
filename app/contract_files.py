"""合同原件（v0.29）：文件收进系统（<数据目录>/contract-archive/{合同号}/），不再只登记 NAS 路径。

每份合同至少应有一份「双方签署版」（kind=signed，双方都盖章的最终版）；其余是附件（报价单、送货单、
开票用版本、出口管制承诺书等，kind=attachment）。未签草稿、Word 底稿不收（NAS 上留着即可）。
旧的 archive 表（v0.4 登记 NAS 路径字符串，搬服务器即失效）在 v0.29 迁移时清掉。
"""
import base64
import hashlib
import re
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime

from . import operations as O
from .models import Base, Contract
from .paths import contract_archive

KINDS = {"signed": "双方签署版", "attachment": "附件", "other": "其他"}


class ContractFile(Base):
    __tablename__ = "contract_file"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contract.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="attachment")
    file_name: Mapped[str] = mapped_column(String(256))
    stored_path: Mapped[str] = mapped_column(String(512))       # 相对 contract-archive
    file_type: Mapped[str] = mapped_column(String(8), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(256), default="")  # 导入来源（原 NAS 相对路径）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class UploadIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1)
    content_b64: str = Field(min_length=1)
    kind: str = Field(default="signed", pattern=r"^(signed|attachment|other)$")
    note: str = ""


class PatchIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    kind: Optional[str] = Field(default=None, pattern=r"^(signed|attachment|other)$")
    note: Optional[str] = None


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", s or "").strip()[:120]


def add_file(db, c: Contract, name: str, data: bytes, kind: str = "signed", note: str = "", source: str = "") -> tuple[ContractFile, bool]:
    sha = hashlib.sha256(data).hexdigest()
    old = db.scalar(select(ContractFile).where(ContractFile.contract_id == c.id, ContractFile.sha256 == sha))
    if old:
        return old, False
    rel = Path(_safe(c.contract_no)) / _safe(name)
    root = contract_archive()
    n = 1
    while (root / rel).exists():
        n += 1
        rel = rel.with_name(f"{Path(_safe(name)).stem}_{n}{Path(name).suffix}")
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(data)
    f = ContractFile(contract_id=c.id, kind=kind, file_name=name, stored_path=rel.as_posix(),
                     file_type=Path(name).suffix.lower().lstrip(".")[:8], size=len(data), sha256=sha, note=note, source=source)
    db.add(f)
    db.flush()
    return f, True


def files_of(db, contract_id: int) -> list[dict]:
    return [{"id": f.id, "kind": f.kind, "kind_label": KINDS.get(f.kind, f.kind), "file_name": f.file_name,
             "file_type": f.file_type, "size": f.size, "note": f.note, "created_at": str(f.created_at)[:16]}
            for f in db.scalars(select(ContractFile).where(ContractFile.contract_id == contract_id)
                                .order_by(ContractFile.kind != "signed", ContractFile.id))]


def signed_map(db) -> dict[int, int]:
    out = {}
    for f in db.scalars(select(ContractFile).where(ContractFile.kind == "signed")):
        out[f.contract_id] = out.get(f.contract_id, 0) + 1
    return out


def install(app, get_db):
    @app.get("/api/contracts/{cid}/files")
    def list_files(cid: int, db=Depends(get_db)):
        return files_of(db, cid)

    @app.post("/api/contracts/{cid}/files")
    def upload(cid: int, data: UploadIn, db=Depends(get_db)):
        c = db.get(Contract, cid)
        if not c:
            raise HTTPException(404, "合同不存在")
        raw = base64.b64decode(data.content_b64)
        if len(raw) > 30 * 1024 * 1024:
            raise ValueError("文件超过 30MB")
        f, new = add_file(db, c, data.name, raw, data.kind, data.note)
        O.audit(db, "上传合同原件", contract_id=cid, file_id=f.id)
        db.commit()
        return {"id": f.id, "new": new}

    @app.put("/api/contract-files/{fid}")
    def patch(fid: int, data: PatchIn, db=Depends(get_db)):
        f = db.get(ContractFile, fid)
        if not f:
            raise HTTPException(404, "文件不存在")
        for k, v in data.model_dump(exclude_unset=True).items():
            setattr(f, k, v if v is not None else "")
        db.commit()
        return {"ok": True}

    @app.delete("/api/contract-files/{fid}")
    def delete(fid: int, db=Depends(get_db)):
        """只删登记，不删归档目录里的文件（防误删；备份镜像也保留）。"""
        f = db.get(ContractFile, fid)
        if not f:
            raise HTTPException(404, "文件不存在")
        db.delete(f)
        db.commit()
        return {"ok": True}

    @app.get("/api/contract-files/{fid}")
    def get_file(fid: int, db=Depends(get_db)):
        f = db.get(ContractFile, fid)
        if not f:
            raise HTTPException(404, "文件不存在")
        root = contract_archive().resolve()
        path = (root / f.stored_path).resolve()
        if root not in path.parents or not path.exists():
            raise HTTPException(404, "归档文件缺失")
        media = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(f.file_type, "application/octet-stream")
        return FileResponse(path, media_type=media, filename=f.file_name,
                            content_disposition_type="inline" if f.file_type in ("pdf", "png", "jpg", "jpeg") else "attachment")
