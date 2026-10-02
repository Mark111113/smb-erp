"""制造模块（v0.35，规划 docs/PLAN_manufacturing.md）。

- BOM 版本：bom_header / bom_line；启用版本时同步写 material_bom（组套/拆套与旧物料页继续可用）
- 工单：production 自制 / subcontract 委外 / rd 研发领料。领料 wo_issue、退料 wo_return、完工入库 wo_receipt
  都是挂 work_order_id 的已确认出入库单，成本在 inventory.replay 里算，凭证由 operations.synchronize 出：
    领料  借 500101 生产成本-直接材料（工单成本中心）/ 5301 研发支出（研发项目）/ 1408 委托加工物资（委外厂）  贷 存货
    完工  借 存货  贷 500101 / 1408（材料部分）、1234 GR/IR（委外加工费，等加工费发票冲平）
- 月末成本结转：500102/500103 + 5101 制造费用余额按工时分摊到当月有完工入库的自制工单；
  在库部分加到产品成本（wo_cost 只调金额），已出库部分直接进主营业务成本；5301 研发支出按项目转 660211 研究费用
- 报价成本测算、研发支出辅助账、关联交易汇总、批次库存
"""
import base64
import calendar
import csv
import io
import re
from datetime import date, datetime
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, inspect, select, text

from . import finance as F
from . import operations as O
from .inventory import confirm_movement, end_of_day, replay
from .models import (Account, BomHeader, BomLine, CostCenter, Invoice, Material, MaterialAlias, MaterialBom, MfgClose, Partner,
                     Payment, StockLocation, StockMovement, SupplierPrice, Voucher, VoucherLine, WorkOrder, WorkReport)

UNITS = ("个", "套", "批", "卷", "盘", "米", "千克", "片", "块", "根", "张", "支", "包", "瓶")


# ---------------- schema ----------------
COLUMNS = {
    "material": [("material_type", "VARCHAR(12) NOT NULL DEFAULT 'goods'"), ("manufacturer", "VARCHAR(64) NOT NULL DEFAULT ''"),
                 ("mpn", "VARCHAR(64) NOT NULL DEFAULT ''"), ("package", "VARCHAR(32) NOT NULL DEFAULT ''"),
                 ("msl", "VARCHAR(8) NOT NULL DEFAULT ''"), ("min_pack", "FLOAT NOT NULL DEFAULT 0"),
                 ("pack_unit", "VARCHAR(8) NOT NULL DEFAULT ''"), ("loss_rate", "FLOAT NOT NULL DEFAULT 0"),
                 ("lot_control", "BOOLEAN NOT NULL DEFAULT 0"), ("key_part", "BOOLEAN NOT NULL DEFAULT 0"),
                 ("std_hours", "FLOAT NOT NULL DEFAULT 0"), ("smt_points", "INTEGER NOT NULL DEFAULT 0")],
    "stock_location": [("purpose", "VARCHAR(12) NOT NULL DEFAULT 'general'")],
    "stock_movement": [("work_order_id", "INTEGER"), ("amount", "FLOAT"), ("lot_no", "VARCHAR(48) NOT NULL DEFAULT ''"),
                       ("lot_date", "DATE")],
    "partner": [("is_related", "BOOLEAN NOT NULL DEFAULT 0")],
}


def ensure_schema(engine, session_factory):
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    with session_factory() as db:          # 老 material_bom → V1 生效版本（幂等）
        migrate_legacy_bom(db)
        db.commit()


def migrate_legacy_bom(db):
    parents = {b.parent_material_id for b in db.scalars(select(MaterialBom).where(MaterialBom.active == True))}
    for pid in parents:
        if not db.scalar(select(BomHeader.id).where(BomHeader.product_id == pid)):
            h = BomHeader(product_id=pid, version="V1", status="active", effective_date=date.today(), change_note="由旧 BOM 迁移")
            for n, b in enumerate(db.scalars(select(MaterialBom).where(MaterialBom.parent_material_id == pid, MaterialBom.active == True)), 1):
                h.lines.append(BomLine(seq=n, material_id=b.child_material_id, qty=b.qty_per, remark=b.remark))
            db.add(h)
    db.flush()


# ---------------- 物料组与编码 ----------------
COMPONENT_GROUPS = {"10": "IC", "11": "阻容感", "12": "分立器件", "13": "连接器线材", "14": "PCB", "15": "磁材",
                    "16": "结构件散热器", "17": "辅料", "18": "包材", "19": "治具周转材料"}
DEFAULT_TYPE = {"17": "aux", "18": "pack", "19": "tool"}


def default_material_type(group: str) -> str:
    if group in COMPONENT_GROUPS:
        return DEFAULT_TYPE.get(group, "raw")
    return "goods"


def next_code(db, group: str) -> str:
    """成品组 OZ-组码+4 位；元器件组（10–19）OZ-组码+5 位。组内取最大+1，永不复用。"""
    width = 5 if group in COMPONENT_GROUPS else 4
    rows = db.scalars(select(Material.code).where(Material.code.like(f"OZ-{group}" + "_" * width))).all()
    mx = max((int(r[-width:]) for r in rows if r[-width:].isdigit()), default=0)
    return f"OZ-{group}{mx + 1:0{width}d}"


# ---------------- BOM ----------------
class BomLineIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    material_id: int
    qty: float = Field(gt=0)
    loss_rate: Optional[float] = Field(default=None, ge=0, lt=1)
    ref_des: str = ""
    side: str = ""
    alt_group: str = ""
    is_primary: bool = True
    remark: str = ""


class BomIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    product_id: int
    version: str = Field(min_length=1, max_length=16)
    change_note: str = ""
    lines: list[BomLineIn] = []
    copy_from: Optional[int] = None


def _bom_json(db, h: BomHeader) -> dict:
    p = db.get(Material, h.product_id)
    return {"id": h.id, "product_id": h.product_id, "product_code": p.code if p else "", "product_name": p.name if p else "",
            "product_spec": p.spec if p else "", "version": h.version, "status": h.status,
            "effective_date": str(h.effective_date or ""), "change_note": h.change_note, "source": h.source,
            "lines": [{"id": l.id, "seq": l.seq, "material_id": l.material_id, "code": l.material.code, "name": l.material.name,
                       "spec": l.material.spec, "mpn": l.material.mpn, "unit": l.material.unit, "qty": l.qty,
                       "loss_rate": l.loss_rate, "eff_loss": _loss(l), "ref_des": l.ref_des, "side": l.side,
                       "alt_group": l.alt_group, "is_primary": l.is_primary, "remark": l.remark} for l in h.lines]}


def _loss(l: BomLine) -> float:
    return l.loss_rate if l.loss_rate is not None else (l.material.loss_rate or 0)


def _check_lines(db, product_id, lines):
    for l in lines:
        m = db.get(Material, l.material_id)
        if not m or not m.active:
            raise ValueError(f"物料 {l.material_id} 不存在或已停用")
        if l.material_id == product_id:
            raise ValueError("BOM 子件不能是产品本身")


def create_bom(db, data: BomIn) -> BomHeader:
    p = db.get(Material, data.product_id)
    if not p or not p.active:
        raise ValueError("产品不存在或已停用")
    if db.scalar(select(BomHeader.id).where(BomHeader.product_id == data.product_id, BomHeader.version == data.version)):
        raise ValueError(f"版本 {data.version} 已存在")
    h = BomHeader(product_id=data.product_id, version=data.version, change_note=data.change_note, status="draft")
    src = db.get(BomHeader, data.copy_from) if data.copy_from else None
    lines = [BomLineIn(material_id=l.material_id, qty=l.qty, loss_rate=l.loss_rate, ref_des=l.ref_des, side=l.side,
                       alt_group=l.alt_group, is_primary=l.is_primary, remark=l.remark) for l in src.lines] if src else data.lines
    _check_lines(db, data.product_id, lines)
    for n, l in enumerate(lines, 1):
        h.lines.append(BomLine(seq=n, **l.model_dump()))
    db.add(h)
    db.flush()
    O.audit(db, "新建BOM版本", bom_id=h.id, product_id=h.product_id, version=h.version)
    return h


def replace_lines(db, h: BomHeader, lines: list[BomLineIn]):
    if h.status != "draft":
        raise ValueError("只有草稿版本可以改明细；生效版本要改，请复制成新版本（工程变更）")
    _check_lines(db, h.product_id, lines)
    h.lines.clear()
    db.flush()
    for n, l in enumerate(lines, 1):
        h.lines.append(BomLine(seq=n, **l.model_dump()))
    db.flush()


def _primary_qty(h: BomHeader) -> dict[int, float]:
    out = {}
    for l in h.lines:
        if l.is_primary or not l.alt_group:
            out[l.material_id] = out.get(l.material_id, 0) + l.qty
    return out


def activate_bom(db, h: BomHeader):
    if not h.lines:
        raise ValueError("空 BOM 不能启用")
    edges = {}
    for e in db.scalars(select(MaterialBom).where(MaterialBom.active == True, MaterialBom.parent_material_id != h.product_id)):
        edges.setdefault(e.parent_material_id, []).append(e.child_material_id)
    stack, seen = list(_primary_qty(h)), set()
    while stack:
        node = stack.pop()
        if node == h.product_id:
            raise ValueError("BOM 不能形成循环（子件的 BOM 里又用到了本产品）")
        if node not in seen:
            seen.add(node)
            stack.extend(edges.get(node, []))
    for o in db.scalars(select(BomHeader).where(BomHeader.product_id == h.product_id, BomHeader.status == "active", BomHeader.id != h.id)):
        o.status = "obsolete"
    h.status, h.effective_date = "active", h.effective_date or date.today()
    for e in db.scalars(select(MaterialBom).where(MaterialBom.parent_material_id == h.product_id, MaterialBom.active == True)):
        e.active = False
    for mid, q in _primary_qty(h).items():
        db.add(MaterialBom(parent_material_id=h.product_id, child_material_id=mid, qty_per=q, remark=f"BOM {h.version}"))
    db.flush()
    O.audit(db, "启用BOM版本", bom_id=h.id, product_id=h.product_id, version=h.version)


def sync_from_material_bom(db, product_id: int):
    """旧物料页直接改 material_bom 时，同步到生效版本（没有就建 V1），保留同物料行的位号等字段。"""
    edges = list(db.scalars(select(MaterialBom).where(MaterialBom.parent_material_id == product_id, MaterialBom.active == True)))
    h = db.scalar(select(BomHeader).where(BomHeader.product_id == product_id, BomHeader.status == "active"))
    if not h:
        if not edges:
            return
        n = 1 + (db.scalar(select(func.count(BomHeader.id)).where(BomHeader.product_id == product_id)) or 0)
        h = BomHeader(product_id=product_id, version=f"V{n}", status="active", effective_date=date.today(), change_note="物料页维护")
        db.add(h)
        db.flush()
    keep = {l.material_id: l for l in h.lines}
    h.lines.clear()
    db.flush()
    for n, e in enumerate(edges, 1):
        old = keep.get(e.child_material_id)
        h.lines.append(BomLine(seq=n, material_id=e.child_material_id, qty=e.qty_per, remark=e.remark,
                               loss_rate=old.loss_rate if old else None, ref_des=old.ref_des if old else "",
                               side=old.side if old else ""))
    db.flush()


# BOM 导入：Excel/CSV，表头按常见写法识别（EDA 导出、常见客户/供应商格式都能对上大部分）
HEADERS = {
    "code": ("物料编码", "我方编码", "编码", "料号", "part code", "partcode", "item code"),
    "mpn": ("制造商料号", "厂商料号", "mpn", "manufacturer part number", "mfr part", "mfr. part", "型号", "part number", "p/n"),
    "manufacturer": ("制造商", "厂商", "品牌", "manufacturer", "mfr"),
    "name": ("名称", "物料名称", "品名", "description", "comment", "描述"),
    "spec": ("规格", "规格型号", "value", "值", "参数"),
    "package": ("封装", "package", "footprint"),
    "qty": ("用量", "数量", "单台用量", "qty", "quantity"),
    "ref_des": ("位号", "designator", "reference", "ref", "refdes"),
    "loss_rate": ("损耗", "损耗率", "loss"),
    "alt_group": ("替代组", "替代", "alt"),
    "side": ("面", "贴装面", "side", "layer"),
}


def _rows_from_file(name: str, data: bytes) -> list[list]:
    if name.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True).worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if name.lower().endswith(".xls"):
        import xlrd
        sh = xlrd.open_workbook(file_contents=data).sheet_by_index(0)
        return [sh.row_values(r) for r in range(sh.nrows)]
    for enc in ("utf-8-sig", "gbk"):
        try:
            return list(csv.reader(io.StringIO(data.decode(enc))))
        except UnicodeDecodeError:
            continue
    raise ValueError("文件编码读不出来（试 UTF-8 或 GBK 的 CSV，或 xlsx）")


def parse_bom(name: str, data: bytes) -> list[dict]:
    rows = _rows_from_file(name, data)
    for hi, r in enumerate(rows[:15]):
        cols = {}
        for i, v in enumerate(r):
            t = str(v or "").strip().lower()
            for key, names in HEADERS.items():
                if key not in cols and t in [n.lower() for n in names]:
                    cols[key] = i
        if "qty" in cols and ({"code", "mpn"} & set(cols)):
            break
    else:
        raise ValueError("找不到表头：至少要有「用量/数量」和「物料编码」或「制造商料号」两列")
    out = []
    for r in rows[hi + 1:]:
        get = lambda k: (str(r[cols[k]]).strip() if k in cols and cols[k] < len(r) and r[cols[k]] is not None else "")
        if not any(get(k) for k in ("code", "mpn", "name")):
            continue
        try:
            qty = float(get("qty").replace(",", "") or 0)
        except ValueError:
            raise ValueError(f"用量读不出来：{get('qty')}")
        loss = get("loss_rate").rstrip("%")
        loss_v = (float(loss) / (100 if float(loss) >= 1 else 1)) if loss else None
        out.append({"code": get("code"), "mpn": get("mpn"), "manufacturer": get("manufacturer"), "name": get("name"),
                    "spec": get("spec"), "package": get("package"), "qty": qty, "ref_des": get("ref_des"),
                    "loss_rate": loss_v, "alt_group": get("alt_group"), "side": get("side").upper()[:8]})
    return out


def _norm(s):
    return re.sub(r"[\s\-_./]", "", (s or "").upper())


def match_rows(db, rows: list[dict]) -> list[dict]:
    mats = list(db.scalars(select(Material).where(Material.active == True)))
    by_code = {m.code: m for m in mats}
    by_mpn = {_norm(m.mpn): m for m in mats if m.mpn}
    from .models import MaterialAlias
    for a in db.scalars(select(MaterialAlias)):
        if a.alias_spec and a.material_id in {m.id for m in mats}:
            by_mpn.setdefault(_norm(a.alias_spec), db.get(Material, a.material_id))
    seen_alt = set()
    for r in rows:
        m = by_code.get(r["code"]) or (by_mpn.get(_norm(r["mpn"])) if r["mpn"] else None)
        r["material_id"], r["matched"] = (m.id, f"{m.code} {m.name} {m.spec}") if m else (None, "")
        r["is_primary"] = not (r["alt_group"] and r["alt_group"] in seen_alt)
        if r["alt_group"]:
            seen_alt.add(r["alt_group"])
    return rows


def import_bom(db, product_id: int, version: str, name: str, data: bytes, apply: bool, create_group: str = "") -> dict:
    rows = match_rows(db, parse_bom(name, data))
    missing = [r for r in rows if not r["material_id"]]
    result = {"rows": rows, "missing": len(missing), "total": len(rows)}
    if not apply:
        return result
    if missing and not create_group:
        raise ValueError(f"有 {len(missing)} 行没对上物料：先建物料（或选「按这个组新建缺失物料」）再导入")
    if missing and create_group not in COMPONENT_GROUPS:
        raise ValueError("新建缺失物料只能放进元器件组（10–19）")
    for r in missing:
        m = Material(code=next_code(db, create_group), name=r["name"] or r["mpn"] or "未命名", spec=r["spec"], unit="个",
                     mat_group=create_group, material_type=default_material_type(create_group), mpn=r["mpn"],
                     manufacturer=r["manufacturer"], package=r["package"], remark=f"BOM 导入新建（{name}）")
        db.add(m)
        db.flush()
        r["material_id"], r["matched"] = m.id, f"{m.code}（新建）"
    lines = [BomLineIn(material_id=r["material_id"], qty=r["qty"], loss_rate=r["loss_rate"], ref_des=r["ref_des"],
                       side=r["side"], alt_group=r["alt_group"], is_primary=r["is_primary"]) for r in rows if r["qty"] > 0]
    h = create_bom(db, BomIn(product_id=product_id, version=version, change_note=f"导入 {name}", lines=lines))
    h.source = name
    result["bom_id"] = h.id
    return result


# ---------------- 工单 ----------------
KIND_PREFIX = {"production": "WO", "subcontract": "SC", "rd": "RD"}
MOVE_PREFIX = {"wo_issue": "MI", "wo_return": "MR", "wo_receipt": "FG", "wo_cost": "MC"}


class WorkOrderIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    kind: str = "production"
    product_id: Optional[int] = None
    bom_id: Optional[int] = None
    qty: float = 0
    plan_start: Optional[date] = None
    plan_end: Optional[date] = None
    cost_center_id: Optional[int] = None
    partner_id: Optional[int] = None
    fee_price: float = Field(default=0, ge=0)
    remark: str = ""


def location_for(db, purpose: str) -> int:
    """按用途取默认仓：领料/退料 raw（原材料仓），完工 finished（成品仓）；没设就用默认地点"""
    from . import locations
    lid = db.scalar(select(StockLocation.id).where(StockLocation.purpose == purpose, StockLocation.active == True)
                    .order_by(StockLocation.id))
    return lid or locations.default_location_id(db)


class MoveLine(BaseModel):
    material_id: int
    qty: float = Field(gt=0)
    lot_no: str = ""
    lot_date: Optional[date] = None


class IssueIn(BaseModel):
    move_date: date
    from_bom: bool = False          # 按 BOM 未领数量自动生成
    lines: list[MoveLine] = []
    location_id: Optional[int] = None   # 领料从哪个仓出 / 退料退回哪个仓；空 = 原材料仓（没有就默认地点）
    remark: str = ""


class ReceiptIn(BaseModel):
    move_date: date
    qty: float = Field(gt=0)
    lot_no: str = ""
    location_id: Optional[int] = None   # 入哪个仓；空 = 成品仓（没有就默认地点）
    remark: str = ""


class ReportIn(BaseModel):
    report_date: date
    good_qty: float = Field(default=0, ge=0)
    bad_qty: float = Field(default=0, ge=0)
    hours: float = Field(default=0, ge=0)
    cost_center_id: Optional[int] = None
    remark: str = ""


def _cc(db, code):
    return db.scalar(select(CostCenter).where(CostCenter.code == code))


def _under(db, cc_id, root_code) -> bool:
    root = _cc(db, root_code)
    c = db.get(CostCenter, cc_id) if cc_id else None
    while c:
        if root and c.id == root.id:
            return True
        c = db.get(CostCenter, c.parent_id) if c.parent_id else None
    return False


def create_wo(db, data: WorkOrderIn) -> WorkOrder:
    if data.kind not in KIND_PREFIX:
        raise ValueError("工单类型只能是 自制 / 委外 / 研发")
    if data.kind in ("production", "subcontract"):
        p = db.get(Material, data.product_id) if data.product_id else None
        if not p or not p.active:
            raise ValueError("请选择要生产的产品")
        if data.qty <= 0:
            raise ValueError("计划数量必须大于 0")
        h = db.get(BomHeader, data.bom_id) if data.bom_id else db.scalar(
            select(BomHeader).where(BomHeader.product_id == p.id, BomHeader.status == "active"))
        if data.kind == "production" and not h:
            raise ValueError("该产品没有生效的 BOM，先维护 BOM 并启用")
        if h and (h.product_id != p.id or h.status == "draft"):
            raise ValueError("BOM 版本与产品不符或还是草稿")
        data.bom_id = h.id if h else None
    if data.kind == "subcontract":
        pt = db.get(Partner, data.partner_id) if data.partner_id else None
        if not pt:
            raise ValueError("委外工单要选委外加工厂")
    if data.kind == "rd" and not _under(db, data.cost_center_id, "CC20"):
        raise ValueError("研发领料要选研发项目（研发成本中心 CC20 下的子项）")
    if data.kind == "production" and not data.cost_center_id:
        data.cost_center_id = (_cc(db, "CC10") or _cc(db, "CC01")).id
    d = data.plan_start or date.today()
    prefix = f"{KIND_PREFIX[data.kind]}-{d.strftime('%Y%m')}-"
    n = len(db.scalars(select(WorkOrder.wo_no).where(WorkOrder.wo_no.like(prefix + "%"))).all())
    w = WorkOrder(wo_no=f"{prefix}{n + 1:03d}", **data.model_dump())
    db.add(w)
    db.flush()
    O.audit(db, "新建工单", work_order_id=w.id, wo_no=w.wo_no, kind=w.kind)
    return w


def _moves(db, w: WorkOrder):
    return list(db.scalars(select(StockMovement).where(StockMovement.work_order_id == w.id, StockMovement.status == "confirmed")
                           .order_by(StockMovement.move_date, StockMovement.id)))


def net_issued(db, w) -> dict[int, float]:
    out = {}
    for m in _moves(db, w):
        if m.move_type in ("wo_issue", "wo_return"):
            out[m.material_id] = out.get(m.material_id, 0) + (m.qty if m.move_type == "wo_issue" else -m.qty)
    return out


def requirements(db, w: WorkOrder) -> list[dict]:
    issued = net_issued(db, w)
    stock = replay(db)[0]
    rows, listed = [], set()
    if w.bom:
        for l in w.bom.lines:
            if not (l.is_primary or not l.alt_group):
                continue
            need = round(l.qty * w.qty * (1 + _loss(l)), 4)
            got = issued.get(l.material_id, 0)
            listed.add(l.material_id)
            rows.append({"material_id": l.material_id, "code": l.material.code, "name": l.material.name, "spec": l.material.spec,
                         "per_unit": l.qty, "loss_rate": _loss(l), "required": need, "issued": round(got, 4),
                         "remaining": round(max(need - got, 0), 4), "on_hand": round(stock.get(l.material_id, {}).get("qty", 0), 4),
                         "lot_control": l.material.lot_control})
    for mid, got in issued.items():
        if mid not in listed and abs(got) > 1e-9:
            m = db.get(Material, mid)
            rows.append({"material_id": mid, "code": m.code, "name": m.name, "spec": m.spec, "per_unit": 0, "loss_rate": 0,
                         "required": 0, "issued": round(got, 4), "remaining": 0,
                         "on_hand": round(stock.get(mid, {}).get("qty", 0), 4), "lot_control": m.lot_control})
    return rows


def _doc_no(db, move_type, d):
    prefix = f"{MOVE_PREFIX[move_type]}-{d.strftime('%Y%m%d')}-"
    n = len(db.scalars(select(StockMovement.doc_no).where(StockMovement.doc_no.like(prefix + "%"))).all())
    return f"{prefix}{n + 1:03d}"


def _add_move(db, w, move_type, material_id, qty, d, lot_no="", lot_date=None, remark="", amount=None, location_id=None):
    from . import locations
    m = StockMovement(doc_no=_doc_no(db, move_type, d), move_type=move_type, material_id=material_id, qty=qty, move_date=d,
                      status="draft", work_order_id=w.id if w else None, lot_no=lot_no, lot_date=lot_date,
                      remark=remark or (f"{w.wo_no}" if w else ""), amount=amount, tax_rate=0,
                      location_id=locations.check_location(db, location_id))
    db.add(m)
    db.flush()
    confirm_movement(db, m)
    db.flush()
    return m


def _settle(db, ids):
    """新单据按重放结果写成本快照，免得 synchronize 记一堆「成本快照重估」"""
    for m, cost, _ in replay(db)[1]:
        if m.id in ids and m.move_type != "wo_cost":
            m.unit_cost = cost
    O.synchronize(db, "工单过账")


def _open(w):
    if w.status != "released":
        state = {"closed": "关闭", "cancelled": "取消"}.get(w.status, w.status)
        raise ValueError(f"工单 {w.wo_no} 已{state}，不能再操作")


def issue(db, w: WorkOrder, data: IssueIn) -> list[StockMovement]:
    _open(w)
    O.assert_open(db, data.move_date)
    lines = list(data.lines)
    if data.from_bom:
        lines = [MoveLine(material_id=r["material_id"], qty=r["remaining"]) for r in requirements(db, w) if r["remaining"] > 1e-9]
    if not lines:
        raise ValueError("没有要领的料（BOM 已领齐，或没填明细）")
    want = {}
    for l in lines:
        m = db.get(Material, l.material_id)
        if not m:
            raise ValueError(f"物料 {l.material_id} 不存在")
        if m.lot_control and not l.lot_no:
            raise ValueError(f"{m.code} {m.name} 要按批次领料，请填批号")
        want[l.material_id] = want.get(l.material_id, 0) + l.qty
    stock = replay(db, before=end_of_day(data.move_date))[0]
    short = [f"{db.get(Material, k).code} 需 {v:g}、{data.move_date} 日终库存 {stock.get(k, {}).get('qty', 0):g}"
             for k, v in want.items() if stock.get(k, {}).get("qty", 0) + 1e-9 < v]
    if short:
        raise ValueError("库存不足，不能领料（先收货或改日期）：" + "；".join(short))
    # 领料认仓库（v0.37）：从哪个仓出，那个仓当日就得有这么多
    from . import locations
    loc = data.location_id or location_for(db, "raw")
    names = {l.id: l.name for l in db.scalars(select(StockLocation))}
    at = locations.location_stock(db, upto=data.move_date)
    short = []
    for k, v in want.items():
        here = at.get(k, {}).get(loc, 0)
        if here + 1e-9 < v:
            where = "、".join(f"{names.get(lid, lid)} {q:g}" for lid, q in at.get(k, {}).items() if q > 1e-9) or "各仓都没有"
            short.append(f"{db.get(Material, k).code} 需 {v:g}，{names.get(loc)} 只有 {here:g}（{where}）")
    if short:
        raise ValueError("领料仓库存不够，换个仓或先调拨：" + "；".join(short))
    moves = [_add_move(db, w, "wo_issue", l.material_id, l.qty, data.move_date, l.lot_no, l.lot_date, data.remark, location_id=loc)
             for l in lines]
    _settle(db, {m.id for m in moves})
    O.audit(db, "工单领料", work_order_id=w.id, movement_ids=[m.id for m in moves])
    return moves


def return_material(db, w: WorkOrder, data: IssueIn) -> list[StockMovement]:
    _open(w)
    O.assert_open(db, data.move_date)
    got = net_issued(db, w)
    for l in data.lines:
        if got.get(l.material_id, 0) + 1e-9 < l.qty:
            raise ValueError(f"{db.get(Material, l.material_id).code} 退料 {l.qty:g} 超过已领净额 {got.get(l.material_id, 0):g}")
    loc = data.location_id or location_for(db, "raw")
    moves = [_add_move(db, w, "wo_return", l.material_id, l.qty, data.move_date, l.lot_no, l.lot_date, data.remark, location_id=loc)
             for l in data.lines]
    _settle(db, {m.id for m in moves})
    O.audit(db, "工单退料", work_order_id=w.id, movement_ids=[m.id for m in moves])
    return moves


def receive(db, w: WorkOrder, data: ReceiptIn) -> StockMovement:
    _open(w)
    if w.kind == "rd":
        raise ValueError("研发领料单没有完工入库")
    if w.kind == "production" and not any(v > 1e-9 for v in net_issued(db, w).values()):
        raise ValueError("这张工单还没领料，完工入库成本会是 0；先领料（委外的纯加工单不受此限）")
    O.assert_open(db, data.move_date)
    p = db.get(Material, w.product_id)
    if p.lot_control and not data.lot_no:
        raise ValueError("该产品按批次管理，完工入库要填批号")
    m = _add_move(db, w, "wo_receipt", w.product_id, data.qty, data.move_date, data.lot_no, None, data.remark,
                  location_id=data.location_id or location_for(db, "finished"))
    _settle(db, {m.id})
    O.audit(db, "完工入库", work_order_id=w.id, movement_id=m.id, qty=data.qty)
    return m


def add_report(db, w: WorkOrder, data: ReportIn) -> WorkReport:
    _open(w)
    O.assert_open(db, data.report_date)
    r = WorkReport(work_order_id=w.id, **data.model_dump())
    r.cost_center_id = r.cost_center_id or w.cost_center_id
    db.add(r)
    db.flush()
    O.audit(db, "报工", work_order_id=w.id, report_id=r.id, hours=r.hours)
    return r


def close_wo(db, w: WorkOrder):
    _open(w)
    if w.kind != "rd" and not any(m.move_type == "wo_receipt" for m in _moves(db, w)):
        raise ValueError("还没有完工入库，不能关闭；不做了的话先把料全部退回再「取消」")
    w.status, w.closed_at = "closed", datetime.now()
    db.flush()
    O.synchronize(db, f"工单 {w.wo_no} 关闭（在制余额转入最后一张完工入库）")
    O.audit(db, "关闭工单", work_order_id=w.id)


def cancel_wo(db, w: WorkOrder):
    _open(w)
    if any(abs(v) > 1e-9 for v in net_issued(db, w).values()) or any(m.move_type == "wo_receipt" for m in _moves(db, w)):
        raise ValueError("工单已有领料净额或完工入库，不能取消；先退料、作废入库单")
    w.status = "cancelled"
    O.audit(db, "取消工单", work_order_id=w.id)


def reopen_wo(db, w: WorkOrder):
    if w.status != "closed":
        raise ValueError("只有已关闭的工单可以重开")
    w.status, w.closed_at = "released", None
    db.flush()
    O.synchronize(db, f"工单 {w.wo_no} 重开")
    O.audit(db, "重开工单", work_order_id=w.id)


def wip_balance(db, w) -> float:
    """工单在制材料余额（领料 − 退料 − 完工入库带走的材料）"""
    tot = 0.0
    wos = {w.id}
    for m, cost, delta in replay(db)[1]:
        if m.work_order_id in wos:
            if m.move_type == "wo_issue":
                tot += -delta
            elif m.move_type == "wo_return":
                tot -= round(m.qty * cost, 2)
            elif m.move_type == "wo_receipt":
                fee = round((w.fee_price or 0) * m.qty, 2) if w.kind == "subcontract" else 0
                tot -= round(m.qty * cost, 2) - fee
    return round(tot, 2) + 0.0   # 去掉 -0.0


def wo_json(db, w: WorkOrder, detail=False) -> dict:
    moves = _moves(db, w)
    recv = sum(m.qty for m in moves if m.move_type == "wo_receipt")
    reports = list(db.scalars(select(WorkReport).where(WorkReport.work_order_id == w.id, WorkReport.status == "active")))
    o = {"id": w.id, "wo_no": w.wo_no, "kind": w.kind, "status": w.status, "product_id": w.product_id,
         "product_code": w.product.code if w.product else "", "product_name": w.product.name if w.product else "",
         "product_spec": w.product.spec if w.product else "", "bom_id": w.bom_id, "bom_version": w.bom.version if w.bom else "",
         "qty": w.qty, "received": round(recv, 4), "plan_start": str(w.plan_start or ""), "plan_end": str(w.plan_end or ""),
         "cost_center_id": w.cost_center_id,
         "cost_center": (db.get(CostCenter, w.cost_center_id).name if w.cost_center_id else ""),
         "partner_id": w.partner_id, "partner": (db.get(Partner, w.partner_id).short_name or db.get(Partner, w.partner_id).name) if w.partner_id else "",
         "fee_price": w.fee_price, "remark": w.remark, "hours": round(sum(r.hours for r in reports), 2),
         "good_qty": round(sum(r.good_qty for r in reports), 4), "bad_qty": round(sum(r.bad_qty for r in reports), 4),
         "created_at": str(w.created_at or "")[:19]}
    if detail:
        mats = {}
        for m, cost, delta in replay(db)[1]:
            if m.work_order_id == w.id:
                mats[m.id] = cost
        o["requirements"] = requirements(db, w)
        locs = {l.id: l.name for l in db.scalars(select(StockLocation))}
        o["movements"] = [{"id": m.id, "doc_no": m.doc_no, "move_type": m.move_type, "material_code": m.material.code,
                           "location": locs.get(m.location_id, ""),
                           "material_name": m.material.name, "qty": m.qty, "unit_cost": round(mats.get(m.id, m.unit_cost or 0), 4),
                           "amount": round(m.qty * mats.get(m.id, m.unit_cost or 0), 2), "move_date": str(m.move_date),
                           "lot_no": m.lot_no, "remark": m.remark} for m in moves]
        o["reports"] = [{"id": r.id, "report_date": str(r.report_date), "good_qty": r.good_qty, "bad_qty": r.bad_qty,
                         "hours": r.hours, "remark": r.remark} for r in reports]
        o["wip"] = wip_balance(db, w)
        o["material_cost"] = round(sum(x["amount"] for x in o["movements"] if x["move_type"] == "wo_issue")
                                   - sum(x["amount"] for x in o["movements"] if x["move_type"] == "wo_return"), 2)
        o["receipt_value"] = round(sum(x["amount"] for x in o["movements"] if x["move_type"] == "wo_receipt"), 2)
        o["allocated"] = round(sum(d.get("x", 0) for c in db.scalars(select(MfgClose)) for d in c.detail.get("orders", []) if d["id"] == w.id), 2)
        total = o["receipt_value"] + o["allocated"]
        o["unit_cost"] = round(total / recv, 4) if recv else None
    return o


# ---------------- 月末成本结转 ----------------
def _month_end(month: str) -> date:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, calendar.monthrange(y, m)[1])


def _balances(db, prefixes, end: date, exclude_sources=()) -> list[tuple]:
    """(科目, 成本中心) 余额，借正"""
    q = (select(VoucherLine.account_code, VoucherLine.cost_center_id, func.sum(VoucherLine.debit - VoucherLine.credit))
         .join(Voucher, VoucherLine.voucher_id == Voucher.id)
         .where(Voucher.status.in_(("posted", "reversed")), Voucher.voucher_date <= end)
         .group_by(VoucherLine.account_code, VoucherLine.cost_center_id))
    out = []
    for code, cc, v in db.execute(q):
        if code.startswith(prefixes) and abs(v or 0) > 0.005:
            out.append((code, cc, round(v, 2)))
    return out


POOL_PREFIXES = ("500102", "500103", "5101")


def close_preview(db, month: str) -> dict:
    end = _month_end(month)
    start = end.replace(day=1)
    pools = _balances(db, POOL_PREFIXES, end)
    pool_total = round(sum(v for _, _, v in pools), 2)
    receipts = list(db.scalars(select(StockMovement).where(StockMovement.move_type == "wo_receipt", StockMovement.status == "confirmed",
                                                         StockMovement.move_date >= start, StockMovement.move_date <= end)))
    wos = {}
    for m in receipts:
        w = db.get(WorkOrder, m.work_order_id)
        if w and w.kind == "production":
            wos.setdefault(w.id, {"id": w.id, "wo_no": w.wo_no, "product_id": w.product_id, "received": 0.0, "hours": 0.0})
            wos[w.id]["received"] += m.qty
    for r in db.scalars(select(WorkReport).where(WorkReport.status == "active", WorkReport.report_date >= start, WorkReport.report_date <= end)):
        if r.work_order_id in wos:
            wos[r.work_order_id]["hours"] += r.hours
    orders = list(wos.values())
    hours = sum(o["hours"] for o in orders)
    basis = "hours" if hours > 0 else "qty"
    drivers = [o["hours"] if basis == "hours" else o["received"] for o in orders]
    total_driver = sum(drivers)
    stock = replay(db, before=end_of_day(end))[0]
    recv_by_p = {}
    for o in orders:
        recv_by_p[o["product_id"]] = recv_by_p.get(o["product_id"], 0) + o["received"]
    assigned = 0.0
    for i, o in enumerate(orders):
        o["x"] = round(pool_total - assigned, 2) if i == len(orders) - 1 else round(pool_total * drivers[i] / total_driver, 2) if total_driver else 0
        assigned += o["x"]
        on_hand = stock.get(o["product_id"], {}).get("qty", 0)
        f = max(0.0, min(1.0, on_hand / recv_by_p[o["product_id"]])) if recv_by_p[o["product_id"]] else 0
        o["on_hand_ratio"] = round(f, 4)
        o["inventory"] = round(o["x"] * f, 2)
        o["cogs"] = round(o["x"] - o["inventory"], 2)
        p = db.get(Material, o["product_id"])
        o["product"] = f"{p.code} {p.name}"
        o["unit_add"] = round(o["x"] / o["received"], 4) if o["received"] else 0
    rd = _balances(db, (F.ACC_RD,), end)
    warn = []
    if pool_total and not orders:
        warn.append(f"本月没有自制工单完工入库，人工和制造费用 {pool_total:,.2f} 留在原科目，下个有完工的月份一起分摊")
    rates = {"hours": round(hours, 2), "rate_per_hour": round(pool_total / hours, 2) if hours else None}
    return {"month": month, "end": str(end), "pools": [{"account": a, "cost_center_id": c, "amount": v} for a, c, v in pools],
            "pool_total": pool_total, "basis": basis, "orders": orders if orders else [], "rates": rates,
            "rd": [{"account": a, "cost_center_id": c, "amount": v,
                    "project": (db.get(CostCenter, c).name if c else "（未挂项目）")} for a, c, v in rd],
            "rd_total": round(sum(v for _, _, v in rd), 2), "warnings": warn,
            "done": bool(db.scalar(select(MfgClose.id).where(MfgClose.month == month)))}


def run_close(db, month: str) -> MfgClose:
    if db.scalar(select(MfgClose.id).where(MfgClose.month == month)):
        raise ValueError(f"{month} 已做过成本结转；要重做先撤销")
    later = db.scalar(select(MfgClose.month).where(MfgClose.month > month))
    if later:
        raise ValueError(f"{later} 已结转，不能回头结 {month}")
    pv = close_preview(db, month)
    end = _month_end(month)
    O.assert_open(db, end)
    vouchers, moves = [], []
    orders = [o for o in pv["orders"] if abs(o["x"]) > 0.005]
    if orders:
        lines, by_p = [], {}
        for o in orders:
            by_p[o["product_id"]] = by_p.get(o["product_id"], 0) + o["inventory"]
        inv_acc = {}
        for pid, v in by_p.items():
            acc = F.inventory_account(db.get(Material, pid))
            inv_acc[acc] = inv_acc.get(acc, 0) + v
        for acc, v in inv_acc.items():
            lines.append(dict(account=acc, debit=round(v, 2)) if v >= 0 else dict(account=acc, credit=round(-v, 2)))
        cogs = round(sum(o["cogs"] for o in orders), 2)
        if cogs:
            lines.append(dict(account=F.ACC_COGS, debit=cogs) if cogs > 0 else dict(account=F.ACC_COGS, credit=-cogs))
        for p in pv["pools"]:
            v = p["amount"]
            lines.append(dict(account=p["account"], credit=v, cost_center_id=p["cost_center_id"]) if v > 0
                         else dict(account=p["account"], debit=-v, cost_center_id=p["cost_center_id"]))
        v = F.create_voucher(db, end, "mfg_alloc", int(month.replace("-", "")), f"{month} 人工及制造费用分摊到完工产品", lines)
        vouchers.append(v.id)
        for pid, val in by_p.items():
            if abs(val) > 0.005:
                mv = _add_move(db, None, "wo_cost", pid, 0, end, remark=f"{month} 成本分摊", amount=round(val, 2))
                moves.append(mv.id)
    if pv["rd_total"]:
        lines = []
        for r in pv["rd"]:
            lines += [dict(account=F.ACC_RD_EXP, debit=r["amount"], cost_center_id=r["cost_center_id"]),
                      dict(account=F.ACC_RD, credit=r["amount"], cost_center_id=r["cost_center_id"])]
        v = F.create_voucher(db, end, "rd_expense", int(month.replace("-", "")), f"{month} 研发支出转研究费用", lines)
        vouchers.append(v.id)
    rec = MfgClose(month=month, detail=pv, voucher_ids=vouchers, movement_ids=moves)
    db.add(rec)
    db.flush()
    O.synchronize(db, f"{month} 成本结转")
    O.audit(db, "月末成本结转", month=month, vouchers=vouchers, movements=moves, pool=pv["pool_total"], rd=pv["rd_total"])
    return rec


def undo_close(db) -> str:
    rec = db.scalar(select(MfgClose).order_by(MfgClose.month.desc()))
    if not rec:
        raise ValueError("还没有成本结转记录")
    O.assert_open(db, _month_end(rec.month))
    for vid in rec.voucher_ids:
        v = db.get(Voucher, vid)
        if v and v.status == "posted":
            F.reverse_voucher(db, v, f"撤销 {rec.month} 成本结转")
    for mid in rec.movement_ids:
        m = db.get(StockMovement, mid)
        if m:
            m.status = "voided"
    month = rec.month
    db.delete(rec)
    db.flush()
    O.synchronize(db, f"撤销 {month} 成本结转")
    O.audit(db, "撤销成本结转", month=month)
    return month


# ---------------- 报价成本测算 ----------------
class QuoteLine(BaseModel):
    material_id: Optional[int] = None
    name: str = ""
    qty: float = Field(gt=0)
    loss_rate: float = Field(default=0, ge=0, lt=1)
    price: Optional[float] = None     # 不含税单价；空 = 取库存均价 / 最近收货价


class QuoteIn(BaseModel):
    bom_id: Optional[int] = None
    lines: list[QuoteLine] = []
    hours_per_unit: Optional[float] = Field(default=None, ge=0)   # 空 = 用产品标准工时（物料上的「单件工时」）
    labor_rate: Optional[float] = None       # 元/工时；空 = 用最近一次结转的实际费率
    overhead_rate: float = Field(default=0, ge=0)
    fee_per_unit: float = Field(default=0, ge=0)   # 委外加工费
    margin: float = Field(default=0.2, ge=0, lt=1)
    tax_rate: float = 0.13


def supplier_price(db, material_id) -> Optional[SupplierPrice]:
    """首选供应商价优先，没有首选取最低价（只看有效的）"""
    rows = list(db.scalars(select(SupplierPrice).where(SupplierPrice.material_id == material_id, SupplierPrice.active == True)))
    if not rows:
        return None
    pref = [r for r in rows if r.preferred]
    return pref[0] if pref else min(rows, key=lambda r: r.price)


def _price(db, material_id, stock):
    sp = supplier_price(db, material_id)
    if sp:
        return round(sp.price, 4), f"供应商价 {sp.partner.short_name or sp.partner.name}{'（首选）' if sp.preferred else '（最低）'}"
    st = stock.get(material_id)
    if st and st["qty"] > 1e-9 and st["avg_cost"] > 0:
        return round(st["avg_cost"], 4), "库存均价"
    last = db.scalar(select(StockMovement).where(StockMovement.material_id == material_id, StockMovement.move_type == "in",
                                                 StockMovement.status == "confirmed").order_by(StockMovement.move_date.desc()))
    if last and last.unit_cost:
        return round(last.unit_cost, 4), f"最近收货 {last.move_date}"
    return 0.0, "无价"


def last_rate(db) -> Optional[float]:
    rec = db.scalar(select(MfgClose).order_by(MfgClose.month.desc()))
    return rec.detail.get("rates", {}).get("rate_per_hour") if rec else None


def quote(db, data: QuoteIn) -> dict:
    stock = replay(db)[0]
    src = []
    hours = data.hours_per_unit
    if data.bom_id:
        h = db.get(BomHeader, data.bom_id)
        if not h:
            raise ValueError("BOM 不存在")
        if hours is None:
            hours = db.get(Material, h.product_id).std_hours or 0
        src = [QuoteLine(material_id=l.material_id, qty=l.qty, loss_rate=_loss(l)) for l in h.lines if l.is_primary or not l.alt_group]
    src += data.lines
    rows = []
    for l in src:
        m = db.get(Material, l.material_id) if l.material_id else None
        price, how = (l.price, "手填") if l.price is not None else (_price(db, l.material_id, stock) if m else (0.0, "无价"))
        ext = round(l.qty * (1 + l.loss_rate) * price, 4)
        rows.append({"material_id": l.material_id, "code": m.code if m else "", "name": (m.name + " " + m.spec) if m else l.name,
                     "qty": l.qty, "loss_rate": l.loss_rate, "price": price, "price_source": how, "amount": ext})
    material = round(sum(r["amount"] for r in rows), 4)
    rate = data.labor_rate if data.labor_rate is not None else (last_rate(db) or 0)
    hours = hours or 0
    labor = round(hours * rate, 4)
    overhead = round(hours * data.overhead_rate, 4)
    unit = round(material + labor + overhead + data.fee_per_unit, 4)
    price = round(unit / (1 - data.margin), 2) if data.margin < 1 else None
    return {"lines": rows, "material": material, "hours": hours, "labor_rate": rate, "labor": labor, "overhead": overhead,
            "fee": data.fee_per_unit, "unit_cost": unit, "margin": data.margin, "price_ex_tax": price,
            "price_tax": round(price * (1 + data.tax_rate), 2) if price else None,
            "no_price": [r["code"] or r["name"] for r in rows if r["price_source"] == "无价"]}


# ---------------- 研发支出辅助账 ----------------
RD_CATS = ["人员人工费用", "直接投入费用", "折旧费用", "其他相关费用"]


def rd_ledger(db, year: int) -> dict:
    root = _cc(db, "CC20")
    projects = {c.id: c for c in db.scalars(select(CostCenter)) if root and _under(db, c.id, "CC20") and c.id != root.id}
    out = {pid: {"project_id": pid, "code": c.code, "name": c.name, **{k: 0.0 for k in RD_CATS}, "expensed": 0.0}
           for pid, c in projects.items()}
    q = (select(VoucherLine, Voucher).join(Voucher, VoucherLine.voucher_id == Voucher.id)
         .where(Voucher.status.in_(("posted", "reversed")), Voucher.voucher_date >= date(year, 1, 1),
                Voucher.voucher_date <= date(year, 12, 31), VoucherLine.account_code.in_((F.ACC_RD, F.ACC_RD_EXP))))
    for l, v in db.execute(q):
        if l.cost_center_id not in out:
            continue
        if l.account_code == F.ACC_RD_EXP:
            out[l.cost_center_id]["expensed"] += l.debit - l.credit
            continue
        amt = l.debit - l.credit
        others = {x.account_code for x in v.lines}
        cat = ("直接投入费用" if v.source_type == "wo_issue" else "人员人工费用" if any(a.startswith("2211") for a in others)
               else "折旧费用" if any(a.startswith("1602") for a in others) else "其他相关费用")
        if v.source_type == "rd_expense":
            continue
        out[l.cost_center_id][cat] += amt
    rows = []
    for r in out.values():
        for k in RD_CATS + ["expensed"]:
            r[k] = round(r[k], 2)
        r["total"] = round(sum(r[k] for k in RD_CATS), 2)
        rows.append(r)
    return {"year": year, "projects": sorted(rows, key=lambda r: r["code"]),
            "total": round(sum(r["total"] for r in rows), 2), "categories": RD_CATS}


# ---------------- 关联交易汇总 ----------------
def related_party(db, year: int) -> dict:
    s, e = date(year, 1, 1), date(year, 12, 31)
    rows = []
    for p in db.scalars(select(Partner).where(Partner.is_related == True)):
        inv = list(db.scalars(O.active_documents(db, Invoice).where(Invoice.partner_id == p.id, Invoice.invoice_date >= s, Invoice.invoice_date <= e)))
        pays = list(db.scalars(O.active_documents(db, Payment).where(Payment.partner_id == p.id, Payment.pay_date >= s, Payment.pay_date <= e)))
        bal = lambda acc: F.account_balance(db, acc, p.id, e)
        rows.append({"partner_id": p.id, "name": p.name, "tax_no": p.tax_no,
                     "sales_ex_tax": round(sum(i.amount_ex_tax for i in inv if i.direction == "output"), 2),
                     "purchase_ex_tax": round(sum(i.amount_ex_tax for i in inv if i.direction == "input"), 2),
                     "received": round(sum(x.amount for x in pays if x.direction == "receive"), 2),
                     "paid": round(sum(x.amount for x in pays if x.direction == "pay"), 2),
                     "ar": round(bal(F.ACC_AR) + bal(F.ACC_ADVANCE), 2), "ap": round(-(bal(F.ACC_AP) + bal(F.ACC_PREPAY)), 2),
                     "capital": round(-sum(F.account_balance(db, a.code, p.id, e) for a in db.scalars(select(Account).where(Account.code.like("4001%"))) if a.code != "4001"), 2)})
    return {"year": year, "rows": rows}


# ---------------- 批次库存 ----------------
def lot_stock(db, material_id: Optional[int] = None) -> list[dict]:
    sign = {"in": 1, "opening": 1, "return_in": 1, "wo_return": 1, "wo_receipt": 1, "out": -1, "return_out": -1, "wo_issue": -1}
    q = select(StockMovement).where(StockMovement.status == "confirmed", StockMovement.lot_no != "")
    if material_id:
        q = q.where(StockMovement.material_id == material_id)
    acc = {}
    for m in db.scalars(q):
        k = (m.material_id, m.lot_no)
        s = sign.get(m.move_type, 1 if (m.move_type == "adjust" and m.qty > 0) else -1 if m.move_type == "adjust" else 0)
        a = acc.setdefault(k, {"material_id": m.material_id, "code": m.material.code, "name": m.material.name, "lot_no": m.lot_no,
                               "lot_date": str(m.lot_date or ""), "qty": 0.0, "first": str(m.move_date)})
        a["qty"] = round(a["qty"] + s * abs(m.qty), 4)
        if m.lot_date and not a["lot_date"]:
            a["lot_date"] = str(m.lot_date)
    return sorted([a for a in acc.values() if abs(a["qty"]) > 1e-9], key=lambda a: (a["code"], a["first"]))


# ---------------- 物料批量导入（v0.37） ----------------
MAT_HEADERS = {
    "code": ("物料编码", "编码", "料号", "物料代码", "代码"),
    "name": ("物料名称", "名称", "品名", "description"),
    "spec": ("规格型号", "规格", "型号", "value"),
    "group": ("物料组", "组码", "分组"),
    "material_type": ("物料类型", "类型"),
    "unit": ("单位", "基本单位", "计量单位"),
    "manufacturer": ("制造商", "厂商", "品牌", "manufacturer"),
    "mpn": ("制造商料号", "厂商料号", "mpn", "制造商型号", "manufacturer part number"),
    "package": ("封装", "package"),
    "msl": ("湿敏等级", "msl"),
    "min_pack": ("最小包装", "最小包装量", "包装数量", "spq", "mpq"),
    "pack_unit": ("包装单位",),
    "loss_rate": ("损耗率", "损耗"),
    "lot_control": ("批次管理", "批次"),
    "key_part": ("关键件",),
    "std_hours": ("单件工时", "标准工时"),
    "smt_points": ("贴片点数", "点数"),
    "remark": ("备注",),
}
TEXT_FIELDS = ("manufacturer", "mpn", "package", "msl", "pack_unit")


def _yes(v) -> bool:
    return str(v or "").strip().lower() in ("1", "是", "y", "yes", "true", "√", "✓", "x")


def _num(v, default=0.0):
    s = str(v if v is not None else "").replace(",", "").replace("%", "").strip()
    if not s:
        return default
    return float(s)


def parse_materials(name: str, data: bytes) -> list[dict]:
    rows = _rows_from_file(name, data)
    for hi, r in enumerate(rows[:15]):
        cols = {}
        for i, v in enumerate(r):
            t = str(v or "").strip().lower()
            for key, names in MAT_HEADERS.items():
                if key not in cols and t in [n.lower() for n in names]:
                    cols[key] = i
        if "name" in cols:
            break
    else:
        raise ValueError("找不到表头：至少要有「物料名称」列（可选：物料编码、规格型号、物料组、单位、制造商料号、封装、最小包装、损耗率…）")
    out = []
    for n, r in enumerate(rows[hi + 1:], hi + 2):
        get = lambda k: (str(r[cols[k]]).strip() if k in cols and cols[k] < len(r) and r[cols[k]] is not None else "")
        if not any(get(k) for k in ("code", "name", "mpn")):
            continue
        out.append({"line": n, **{k: get(k) for k in MAT_HEADERS}})
    return out


class MaterialImportIn(BaseModel):
    name: str
    content_b64: str
    apply: bool = False
    code_is: str = "own"              # own 表里的编码是我方编码 / alias 是对方料号（记成该单位的别名）/ ignore 不用
    partner_id: Optional[int] = None  # code_is=alias 时：对方单位
    default_group: str = ""           # 行里没写物料组时用
    fill_blank: bool = True           # 已有物料：只补空着的字段（制造商、料号、封装…），不覆盖已有值
    force_dup: bool = False           # 「疑似重复」的也新建（确认是不同实物时）


def import_materials(db, data: MaterialImportIn, raw: bytes) -> dict:
    from .main import MATERIAL_GROUPS, similar_materials
    if data.code_is not in ("own", "alias", "ignore"):
        raise ValueError("编码列含义只能是 我方编码 / 对方料号 / 不用")
    if data.code_is == "alias" and not (data.partner_id and db.get(Partner, data.partner_id)):
        raise ValueError("表里的编码是对方料号时，要选是哪家单位的料号")
    rows = parse_materials(data.name, raw)
    type_by_name = {v: k for k, v in F.MATERIAL_TYPES.items()}
    mats = list(db.scalars(select(Material).where(Material.active == True)))
    by_code = {m.code: m for m in mats}
    by_mpn = {_norm(m.mpn): m for m in mats if m.mpn}
    by_alias = {}
    for a in db.scalars(select(MaterialAlias)):
        if a.alias_spec:
            by_alias[(a.partner_id, _norm(a.alias_spec))] = a.material_id
    seen = {}
    for r in rows:
        r["errors"], r["fill"], r["alias"] = [], {}, None
        g = r["group"].split()[0] if r["group"] else data.default_group
        if g and g not in MATERIAL_GROUPS:
            g = next((k for k, v in MATERIAL_GROUPS.items() if v == r["group"].strip()), g)
        r["group"] = g
        t = r["material_type"]
        r["material_type"] = type_by_name.get(t, t) or default_material_type(g or "99")
        r["unit"] = r["unit"] or "个"
        try:
            r["min_pack"], r["loss_rate"] = _num(r["min_pack"]), _num(r["loss_rate"])
            r["std_hours"], r["smt_points"] = _num(r["std_hours"]), int(_num(r["smt_points"]))
        except ValueError:
            r["errors"].append("数字列读不出来")
        if r["loss_rate"] >= 1:
            r["loss_rate"] = r["loss_rate"] / 100
        r["lot_control"], r["key_part"] = _yes(r["lot_control"]), _yes(r["key_part"])
        if not r["name"]:
            r["errors"].append("缺物料名称")
        if r["unit"] not in UNITS:
            r["errors"].append(f"单位「{r['unit']}」不在 {'/'.join(UNITS)} 里")
        if r["material_type"] not in F.MATERIAL_TYPES:
            r["errors"].append(f"物料类型「{t}」无效")
        # 文件内重复：编码、制造商料号各自查（一行有料号、另一行没有也能抓到），都没有再按名称+规格
        keys = ([("编码", r["code"])] if r["code"] and data.code_is != "ignore" else []) +                ([("制造商料号", _norm(r["mpn"]))] if r["mpn"] else [])
        keys = keys or [("名称规格", r["name"] + "|" + r["spec"])]
        for kind, k in keys:
            if (kind, k) in seen:
                r["errors"].append(f"{kind}和第 {seen[(kind, k)]} 行重复")
                break
        for kind, k in keys:
            seen.setdefault((kind, k), r["line"])
        m = None
        if r["code"] and data.code_is == "own":
            m = by_code.get(r["code"])
            if not m:
                r["errors"].append(f"我方编码 {r['code']} 不存在（新物料编码留空，系统按组生成）")
        if not m and r["code"] and data.code_is == "alias":
            mid = by_alias.get((data.partner_id, _norm(r["code"])))
            m = db.get(Material, mid) if mid else None
        if not m and r["mpn"]:
            m = by_mpn.get(_norm(r["mpn"]))
        if r["errors"]:
            r["action"] = "error"
            continue
        if m:
            r["action"], r["material_id"], r["matched"] = "exists", m.id, f"{m.code} {m.name} {m.spec}"
            if data.fill_blank:
                r["fill"] = {k: r[k] for k in TEXT_FIELDS if r[k] and not getattr(m, k)}
        else:
            if not g:
                r["errors"].append("新物料要有物料组（表里填，或选默认组）")
                r["action"] = "error"
                continue
            dup = similar_materials(db, r["spec"]) if r["spec"] else []
            r["action"] = "dup" if dup and not data.force_dup else "create"
            r["matched"] = "；".join(f"{d['code']} {d['name']}（{d['via']}）" for d in dup)
        if data.code_is == "alias" and r["code"]:
            have = r.get("material_id") and by_alias.get((data.partner_id, _norm(r["code"]))) == r.get("material_id")
            if not have:
                r["alias"] = {"partner_id": data.partner_id, "alias_name": r["name"], "alias_spec": r["code"]}
    summary = {k: sum(1 for r in rows if r["action"] == k) for k in ("create", "exists", "dup", "error")}
    result = {"rows": rows, "summary": summary, "applied": False}
    if not data.apply:
        return result
    if summary["error"]:
        raise ValueError(f"有 {summary['error']} 行有错，先改表再导（预览里红色那几行）")
    created = 0
    for r in rows:
        if r["action"] == "dup":
            continue
        if r["action"] == "create":
            m = Material(code=next_code(db, r["group"]), name=r["name"], spec=r["spec"], unit=r["unit"], mat_group=r["group"],
                         material_type=r["material_type"], remark=r["remark"] or f"批量导入（{data.name}）",
                         **{k: r[k] for k in TEXT_FIELDS}, min_pack=r["min_pack"], loss_rate=r["loss_rate"],
                         lot_control=r["lot_control"], key_part=r["key_part"], std_hours=r["std_hours"], smt_points=r["smt_points"])
            db.add(m)
            db.flush()
            r["material_id"], r["matched"] = m.id, f"{m.code}（新建）"
            created += 1
        else:
            m = db.get(Material, r["material_id"])
            for k, v in r["fill"].items():
                setattr(m, k, v)
        if r["alias"]:
            db.add(MaterialAlias(material_id=r["material_id"], **r["alias"]))
    db.flush()
    O.audit(db, "批量导入物料", file=data.name, created=created, summary=summary)
    result["applied"] = True
    result["created"] = created
    return result


# ---------------- 供应商价目表（v0.37） ----------------
class SupplierPriceIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    material_id: int
    partner_id: int
    price: float = Field(ge=0)
    tax_rate: float = Field(default=0.13, ge=0, le=1)
    moq: float = Field(default=0, ge=0)
    lead_days: int = Field(default=0, ge=0)
    supplier_pn: str = ""
    preferred: bool = False
    valid_from: Optional[date] = None
    remark: str = ""
    active: bool = True


def _sp_json(r: SupplierPrice) -> dict:
    return {"id": r.id, "material_id": r.material_id, "code": r.material.code, "name": r.material.name, "spec": r.material.spec,
            "mpn": r.material.mpn, "partner_id": r.partner_id, "partner": r.partner.short_name or r.partner.name,
            "price": r.price, "price_tax": round(r.price * (1 + r.tax_rate), 4), "tax_rate": r.tax_rate, "moq": r.moq,
            "lead_days": r.lead_days, "supplier_pn": r.supplier_pn, "preferred": r.preferred,
            "valid_from": str(r.valid_from or ""), "remark": r.remark, "active": r.active}


def save_price(db, data: SupplierPriceIn, r: Optional[SupplierPrice] = None) -> SupplierPrice:
    if not db.get(Material, data.material_id) or not db.get(Partner, data.partner_id):
        raise ValueError("物料或供应商不存在")
    r = r or SupplierPrice()
    for k, v in data.model_dump().items():
        setattr(r, k, v)
    if r.id is None:
        db.add(r)
    db.flush()
    if r.preferred:
        for o in db.scalars(select(SupplierPrice).where(SupplierPrice.material_id == r.material_id, SupplierPrice.id != r.id)):
            o.preferred = False
    db.flush()
    return r


# ---------------- API ----------------
class FileIn(BaseModel):
    product_id: int
    version: str = Field(min_length=1, max_length=16)
    name: str
    content_b64: str
    apply: bool = False
    create_group: str = ""


class LinesIn(BaseModel):
    lines: list[BomLineIn]
    change_note: Optional[str] = None


class MonthIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")


def install(app, get_db):
    def _bom(db, bid):
        h = db.get(BomHeader, bid)
        if not h:
            raise HTTPException(404, "BOM 不存在")
        return h

    def _wo(db, wid):
        w = db.get(WorkOrder, wid)
        if not w:
            raise HTTPException(404, "工单不存在")
        return w

    @app.get("/api/mfg/meta")
    def meta(db=Depends(get_db)):
        ccs = list(db.scalars(select(CostCenter).where(CostCenter.active == True)))
        return {"material_types": F.MATERIAL_TYPES, "component_groups": COMPONENT_GROUPS,
                "production_centers": [{"id": c.id, "code": c.code, "name": c.name} for c in ccs if _under(db, c.id, "CC10")],
                "rd_projects": [{"id": c.id, "code": c.code, "name": c.name} for c in ccs if _under(db, c.id, "CC20") and c.code != "CC20"],
                "last_rate": last_rate(db)}

    @app.get("/api/boms")
    def list_boms(product_id: int = 0, db=Depends(get_db)):
        q = select(BomHeader).order_by(BomHeader.product_id, BomHeader.id.desc())
        if product_id:
            q = q.where(BomHeader.product_id == product_id)
        return [{k: v for k, v in _bom_json(db, h).items() if k != "lines"} | {"line_count": len(h.lines)} for h in db.scalars(q)]

    @app.get("/api/boms/{bid}")
    def get_bom(bid: int, db=Depends(get_db)):
        return _bom_json(db, _bom(db, bid))

    @app.post("/api/boms")
    def create_bom_api(data: BomIn, db=Depends(get_db)):
        h = create_bom(db, data)
        db.commit()
        return _bom_json(db, h)

    @app.put("/api/boms/{bid}/lines")
    def lines_api(bid: int, data: LinesIn, db=Depends(get_db)):
        h = _bom(db, bid)
        replace_lines(db, h, data.lines)
        if data.change_note is not None:
            h.change_note = data.change_note
        db.commit()
        return _bom_json(db, h)

    @app.post("/api/boms/{bid}/activate")
    def activate_api(bid: int, db=Depends(get_db)):
        h = _bom(db, bid)
        activate_bom(db, h)
        db.commit()
        return _bom_json(db, h)

    @app.delete("/api/boms/{bid}")
    def delete_bom_api(bid: int, db=Depends(get_db)):
        h = _bom(db, bid)
        if h.status != "draft":
            raise ValueError("只能删除草稿版本；生效或作废的版本保留留痕")
        if db.scalar(select(WorkOrder.id).where(WorkOrder.bom_id == bid)):
            raise ValueError("已有工单用了这个版本")
        db.delete(h)
        db.commit()
        return {"ok": True}

    @app.post("/api/boms/import")
    def import_api(data: FileIn, db=Depends(get_db)):
        try:
            raw = base64.b64decode(data.content_b64)
        except Exception:
            raise HTTPException(400, "文件内容不是 base64")
        r = import_bom(db, data.product_id, data.version, data.name, raw, data.apply, data.create_group)
        if data.apply:
            db.commit()
        else:
            db.rollback()
        return r

    @app.get("/api/work-orders")
    def list_wo(kind: str = "", status: str = "", db=Depends(get_db)):
        q = select(WorkOrder).order_by(WorkOrder.id.desc())
        if kind:
            q = q.where(WorkOrder.kind == kind)
        if status:
            q = q.where(WorkOrder.status == status)
        return [wo_json(db, w) for w in db.scalars(q)]

    @app.get("/api/work-orders/{wid}")
    def get_wo(wid: int, db=Depends(get_db)):
        return wo_json(db, _wo(db, wid), detail=True)

    @app.post("/api/work-orders")
    def create_wo_api(data: WorkOrderIn, db=Depends(get_db)):
        w = create_wo(db, data)
        db.commit()
        return wo_json(db, w)

    def _act(fn):
        def run(wid: int, data, db):
            w = _wo(db, wid)
            fn(db, w, data) if data is not None else fn(db, w)
            db.commit()
            return wo_json(db, w, detail=True)
        return run

    @app.post("/api/work-orders/{wid}/issue")
    def issue_api(wid: int, data: IssueIn, db=Depends(get_db)):
        return _act(issue)(wid, data, db)

    @app.post("/api/work-orders/{wid}/return")
    def return_api(wid: int, data: IssueIn, db=Depends(get_db)):
        return _act(return_material)(wid, data, db)

    @app.post("/api/work-orders/{wid}/receipt")
    def receipt_api(wid: int, data: ReceiptIn, db=Depends(get_db)):
        return _act(receive)(wid, data, db)

    @app.post("/api/work-orders/{wid}/report")
    def report_api(wid: int, data: ReportIn, db=Depends(get_db)):
        return _act(add_report)(wid, data, db)

    @app.post("/api/work-orders/reports/{rid}/void")
    def void_report(rid: int, db=Depends(get_db)):
        r = db.get(WorkReport, rid)
        if not r or r.status != "active":
            raise HTTPException(404, "报工记录不存在或已作废")
        O.assert_open(db, r.report_date)
        if db.scalar(select(MfgClose.id).where(MfgClose.month == str(r.report_date)[:7])):
            raise ValueError("该月已做成本结转，先撤销结转再改报工")
        r.status = "voided"
        O.audit(db, "作废报工", report_id=rid)
        db.commit()
        return {"ok": True}

    @app.post("/api/work-orders/{wid}/close")
    def close_api(wid: int, db=Depends(get_db)):
        return _act(close_wo)(wid, None, db)

    @app.post("/api/work-orders/{wid}/cancel")
    def cancel_api(wid: int, db=Depends(get_db)):
        return _act(cancel_wo)(wid, None, db)

    @app.post("/api/work-orders/{wid}/reopen")
    def reopen_api(wid: int, db=Depends(get_db)):
        return _act(reopen_wo)(wid, None, db)

    @app.get("/api/mfg/close")
    def close_get(month: str, db=Depends(get_db)):
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise HTTPException(400, "月份格式 YYYY-MM")
        rec = db.scalar(select(MfgClose).where(MfgClose.month == month))
        out = rec.detail | {"done": True, "voucher_ids": rec.voucher_ids} if rec else close_preview(db, month)
        out["history"] = [{"month": r.month, "pool_total": r.detail.get("pool_total"), "rd_total": r.detail.get("rd_total")}
                          for r in db.scalars(select(MfgClose).order_by(MfgClose.month.desc()))]
        return out

    @app.post("/api/mfg/close")
    def close_post(data: MonthIn, db=Depends(get_db)):
        rec = run_close(db, data.month)
        db.commit()
        return rec.detail | {"done": True, "voucher_ids": rec.voucher_ids}

    @app.post("/api/mfg/close/undo")
    def close_undo(db=Depends(get_db)):
        m = undo_close(db)
        db.commit()
        return {"ok": True, "month": m}

    @app.post("/api/mfg/quote")
    def quote_api(data: QuoteIn, db=Depends(get_db)):
        return quote(db, data)

    @app.get("/api/mfg/rd-ledger")
    def rd_api(year: int, db=Depends(get_db)):
        return rd_ledger(db, year)

    @app.get("/api/related-party")
    def related_api(year: int, db=Depends(get_db)):
        return related_party(db, year)

    @app.post("/api/materials/import")
    def materials_import_api(data: MaterialImportIn, db=Depends(get_db)):
        try:
            raw = base64.b64decode(data.content_b64)
        except Exception:
            raise HTTPException(400, "文件内容不是 base64")
        r = import_materials(db, data, raw)
        if data.apply:
            db.commit()
        else:
            db.rollback()
        return r

    @app.get("/api/supplier-prices")
    def prices_api(material_id: int = 0, partner_id: int = 0, all: bool = False, db=Depends(get_db)):
        q = select(SupplierPrice).order_by(SupplierPrice.material_id, SupplierPrice.price)
        if material_id:
            q = q.where(SupplierPrice.material_id == material_id)
        if partner_id:
            q = q.where(SupplierPrice.partner_id == partner_id)
        if not all:
            q = q.where(SupplierPrice.active == True)
        return [_sp_json(r) for r in db.scalars(q)]

    @app.post("/api/supplier-prices")
    def price_create(data: SupplierPriceIn, db=Depends(get_db)):
        r = save_price(db, data)
        db.commit()
        return _sp_json(r)

    @app.put("/api/supplier-prices/{pid}")
    def price_update(pid: int, data: SupplierPriceIn, db=Depends(get_db)):
        r = db.get(SupplierPrice, pid)
        if not r:
            raise HTTPException(404, "价目不存在")
        save_price(db, data, r)
        db.commit()
        return _sp_json(r)

    @app.get("/api/stock/lots")
    def lots_api(material_id: int = 0, db=Depends(get_db)):
        return lot_stock(db, material_id or None)
