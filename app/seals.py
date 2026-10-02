"""电子印章（v0.31）：透明 PNG 章叠加到合同 PDF（PyMuPDF，保留原文字层），移植自 OpenClaw 技能 pdf-stamp-overlay。

四原则（照搬技能）：① 保留原 PDF 内容层，不整页重建；② 位置可锚点/镜像/定点，**尺寸永远按己方章的物理尺寸**
（章图像素 ÷ DPI，合同章 40.51×40.77mm、公章 40.00×40.34mm，生产校准过，别按对方章大小改）；③ 不开 OCR；
④ 先预览再盖。透明度统一按印章档案（默认 0.52，原技能只有镜像脚本套了透明度，这里三种方式一致）。

⚠ 这是图片叠加章，不是 CA 数字签名。章图存 <数据目录>/seals/，只有管理员能换章图（ADMIN_PATH）；
可读写账号可以盖章，每次盖章进 change_log + audit_event（谁、哪份文件、哪枚章、位置）。
"""
import base64
import hashlib
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from . import operations as O
from .paths import contract_archive, data_dir

PT_PER_MM = 72 / 25.4
DEFAULT_OPACITY = 0.52


class StampIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    seal: str = "合同章"
    mode: str = Field(default="anchor", pattern=r"^(anchor|mirror|fixed)$")
    page: int = 0                          # 1 起；0 = 末页
    anchors: str = ""                       # 锚点文字，| 分隔；空 = 按合同方向取默认
    placement: str = Field(default="right", pattern=r"^(center|right)$")    # 锚点右侧（原技能默认，右移 gap_mm）/ 盖在锚点上
    gap_mm: float = 10.0
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    x_mm: Optional[float] = None            # 定点：章中心距页面左上角（mm）
    y_mm: Optional[float] = None


class SealIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=20)
    content_b64: str = Field(min_length=1)
    dpi: float = Field(default=600, gt=50)
    opacity: float = Field(default=DEFAULT_OPACITY, gt=0, le=1)


def seal_dir() -> Path:
    return data_dir() / "seals"


def profiles(db) -> dict:
    from .company import get
    return (get(db).get("seals") or {})


def _baked(db, name: str):
    """章图按透明度预处理（缓存成 <名>@<透明度>.png），返回 (路径, 宽pt, 高pt)。"""
    import pymupdf
    prof = profiles(db).get(name)
    if not prof:
        raise ValueError(f"没有「{name}」的印章档案（系统→本单位设置→印章）")
    src = seal_dir() / prof["file"]
    if not src.exists():
        raise ValueError(f"印章文件缺失：{prof['file']}")
    op = float(prof.get("opacity") or DEFAULT_OPACITY)
    out = seal_dir() / f".{Path(prof['file']).stem}@{op:.2f}.png"
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        pix = pymupdf.Pixmap(str(src))
        if not pix.alpha:
            raise ValueError("章图须是带透明通道的 PNG")
        alpha = bytes(pix.samples[pix.n - 1::pix.n])
        if op < 0.999:
            pix.set_alpha(bytes(int(a * op) for a in alpha))
        pix.save(str(out))
    return out, prof["width_mm"] * PT_PER_MM, prof["height_mm"] * PT_PER_MM


def _default_anchors(contract_type: str) -> list[str]:
    # 采购合同我方是甲方/需方/买方；销售合同我方是乙方/供方/卖方
    if contract_type == "purchase":
        roles = ["甲方", "需方", "买方", "采购方"]
    else:
        roles = ["乙方", "供方", "卖方", "供货方"]
    out = []
    for r in roles:
        out += [f"{r}（盖章）", f"{r}(盖章)", f"{r}（签章）", f"{r}（公章）", f"{r}盖章"]
    return out


def _find_red_stamp(page):
    """渲染页面找对方红章（最大的红色连通块），返回其 PDF 坐标矩形；找不到返回 None。"""
    import pymupdf
    zoom = 60 / 72
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    w, h, n, s = pix.width, pix.height, pix.n, pix.samples
    red = bytearray(w * h)
    for i in range(w * h):
        r, g, b = s[i * n], s[i * n + 1], s[i * n + 2]
        if r > 130 and r - max(g, b) > 25:
            red[i] = 1
    seen = bytearray(w * h)
    best = None
    for i in range(w * h):
        if not red[i] or seen[i]:
            continue
        q = deque([i])
        seen[i] = 1
        xs0 = xs1 = i % w
        ys0 = ys1 = i // w
        area = 0
        while q:
            j = q.popleft()
            area += 1
            x, y = j % w, j // w
            xs0, xs1, ys0, ys1 = min(xs0, x), max(xs1, x), min(ys0, y), max(ys1, y)
            for k in (j - 1, j + 1, j - w, j + w, j - w - 1, j - w + 1, j + w - 1, j + w + 1):
                if 0 <= k < w * h and red[k] and not seen[k] and abs((k % w) - x) <= 1:
                    seen[k] = 1
                    q.append(k)
        if area < 60:
            continue
        cx = (xs0 + xs1) / 2
        score = area + cx * 2 + (ys0 + ys1) / 2
        if best is None or score > best[0]:
            best = (score, (xs0, ys0, xs1 + 1, ys1 + 1))
    if not best:
        return None
    x0, y0, x1, y1 = best[1]
    return pymupdf.Rect(x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom)


def place(db, doc, data: StampIn, contract_type: str) -> dict:
    """在 doc 上盖章（原地修改），返回位置说明。"""
    import pymupdf
    stamp, wpt, hpt = _baked(db, data.seal)
    pno = (data.page or len(doc)) - 1
    if pno < 0 or pno >= len(doc):
        raise ValueError(f"页码超出范围（共 {len(doc)} 页）")
    page = doc[pno]
    info = {"page": pno + 1, "mode": data.mode, "seal": data.seal}
    if data.mode == "anchor":
        variants = [a.strip() for a in data.anchors.split("|") if a.strip()] or _default_anchors(contract_type)
        found = None
        for v in variants:
            rects = page.search_for(v)
            if rects:
                found = (v, rects[-1] if len(rects) > 1 else rects[0])
                break
        if not found:
            raise ValueError(f"第 {pno + 1} 页找不到锚点文字（{'、'.join(variants[:4])}…）；扫描件请用「镜像对方章」或「定点」")
        v, a = found
        cx = a.x1 + data.gap_mm * PT_PER_MM + wpt / 2 if data.placement == "right" else (a.x0 + a.x1) / 2
        cy = (a.y0 + a.y1) / 2
        info["anchor"] = v
    elif data.mode == "mirror":
        r = _find_red_stamp(page)
        if not r:
            raise ValueError(f"第 {pno + 1} 页没有找到对方红章；请用「文字锚点」或「定点」")
        mid = (page.rect.x0 + page.rect.x1) / 2
        cx, cy = 2 * mid - (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        info["counter_stamp"] = [round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1)]
    else:
        if data.x_mm is None or data.y_mm is None:
            raise ValueError("定点盖章需填章中心位置（距页面左、上边缘 mm）")
        cx, cy = data.x_mm * PT_PER_MM, data.y_mm * PT_PER_MM
    cx += data.offset_x_mm * PT_PER_MM
    cy += data.offset_y_mm * PT_PER_MM
    rect = pymupdf.Rect(cx - wpt / 2, cy - hpt / 2, cx + wpt / 2, cy + hpt / 2)
    page.insert_image(rect, filename=str(stamp), overlay=True, keep_proportion=True)
    info["rect_mm"] = [round(v / PT_PER_MM, 1) for v in (rect.x0, rect.y0, rect.x1, rect.y1)]
    return info


def install(app, get_db):
    from .contract_files import ContractFile, add_file
    from .models import Contract

    def _file(db, fid):
        f = db.get(ContractFile, fid)
        if not f:
            raise HTTPException(404, "文件不存在")
        if f.file_type != "pdf":
            raise ValueError("只能给 PDF 盖章（DOCX 请先导出 PDF）")
        path = contract_archive() / f.stored_path
        if not path.exists():
            raise ValueError("归档文件缺失")
        return f, path

    @app.get("/api/seals")
    def list_seals(db=Depends(get_db)):
        return [{"name": k, **{x: v.get(x) for x in ("width_mm", "height_mm", "opacity", "dpi")},
                 "exists": (seal_dir() / v["file"]).exists()} for k, v in profiles(db).items()]

    @app.post("/api/seals")
    def upload_seal(data: SealIn, db=Depends(get_db)):
        """管理员上传/更换章图；尺寸按 PNG 像素 ÷ DPI 自动算（物理尺寸）。"""
        import pymupdf
        from .company import CompanySetting, get
        raw = base64.b64decode(data.content_b64)
        pix = pymupdf.Pixmap(raw)
        if not pix.alpha:
            raise ValueError("章图须是带透明通道的 PNG")
        seal_dir().mkdir(parents=True, exist_ok=True)
        fname = f"{data.name}.png"
        (seal_dir() / fname).write_bytes(raw)
        c = get(db)
        seals = dict(c.get("seals") or {})
        seals[data.name] = {"file": fname, "dpi": data.dpi, "opacity": data.opacity,
                            "width_mm": round(pix.width / data.dpi * 25.4, 2), "height_mm": round(pix.height / data.dpi * 25.4, 2),
                            "sha256": hashlib.sha256(raw).hexdigest()}
        row = db.get(CompanySetting, 1) or CompanySetting(id=1, data={})
        row.data = {**c, "seals": seals}
        db.add(row)
        O.audit(db, "上传印章", name=data.name, **{k: seals[data.name][k] for k in ("width_mm", "height_mm", "opacity")})
        db.commit()
        return seals[data.name]

    @app.post("/api/contract-files/{fid}/stamp-preview")
    def preview(fid: int, data: StampIn, db=Depends(get_db)):
        import pymupdf
        f, path = _file(db, fid)
        c = db.get(Contract, f.contract_id)
        doc = pymupdf.open(str(path))
        try:
            info = place(db, doc, data, c.contract_type)
            pix = doc[info["page"] - 1].get_pixmap(matrix=pymupdf.Matrix(1.3, 1.3))
            return {**info, "pages": len(doc), "png_b64": base64.b64encode(pix.tobytes("png")).decode()}
        finally:
            doc.close()

    @app.post("/api/contract-files/{fid}/stamp")
    def stamp(fid: int, data: StampIn, db=Depends(get_db)):
        import pymupdf
        f, path = _file(db, fid)
        c = db.get(Contract, f.contract_id)
        doc = pymupdf.open(str(path))
        try:
            info = place(db, doc, data, c.contract_type)
            out = doc.tobytes(garbage=4, deflate=True)
        finally:
            doc.close()
        both = data.mode == "mirror"      # 镜像 = 对方章已在 → 盖完即双方签署版
        stem = Path(f.file_name).stem
        name = f"{stem}-我方{data.seal}.pdf"
        note = (("双方签署：对方原章 + 我方" if both else "我方") + f"{data.seal}电子章叠加（第 {info['page']} 页，"
                + {"anchor": f"锚点「{info.get('anchor')}」", "mirror": "镜像对方章位置", "fixed": "定点"}[data.mode]
                + "）" + ("" if both else "；待对方回签"))
        nf, new = add_file(db, c, name, out, "signed" if both else "other", note, f"盖章自 #{f.id} {f.file_name}")
        O.audit(db, "合同盖章", contract_id=c.id, source_file_id=f.id, new_file_id=nf.id, **info)
        db.commit()
        return {"id": nf.id, "kind": nf.kind, "file_name": nf.file_name, **info}
