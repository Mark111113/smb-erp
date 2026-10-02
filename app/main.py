"""ERP — FastAPI 主应用"""
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Literal

from fastapi import FastAPI, HTTPException, Depends, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator, Field, ConfigDict
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, select, func, and_, or_
from sqlalchemy.orm import Session, sessionmaker

from .models import (Base, Partner, Material, MaterialAlias, MaterialBom, Contract, ContractLine,
                     StockMovement, Invoice, Payment, BankTxn, Expense, Asset, Archive,
                     Account, CostCenter, Voucher, VoucherLine)
from .inventory import compute_material_stock, confirm_movement
from . import finance, operations as O
from .validation import validate_contract, validate_movement, validate_document
from .auth import install as install_auth, ensure_schema as ensure_auth_schema
from . import assets, audit, bank, close_check, company, contract_files, invoice_docs, invoicing, locations, mfg, seals, statements, tax_filing, tax_forms, todos, prefs

from .paths import db_path
DB_PATH = db_path()   # v0.29：统一数据目录 OWE_DATA_DIR（兼容 OWE_DB），见 app/paths.py
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)

@event.listens_for(engine, "connect")
def sqlite_controls(connection, record):
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA journal_mode=WAL")   # v0.36：写入时不挡读；每日备份用 sqlite backup 接口，WAL 下照常

app = FastAPI(title="ERP", version="0.38.0")

@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """前端是多文件无构建直载：不设缓存头时浏览器会按启发式缓存旧 JS，升级后新旧脚本混跑报错。
    no-cache = 每次带 ETag 回源校验，未变返回 304。"""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(ValueError)
async def business_error(request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})

@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse(status_code=409, content={"detail": "数据重复或关联无效，请刷新后重试"})

class InputModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)



def get_db(request: Request):
    db = SessionLocal()
    try:
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        yield db
    finally:
        db.close()


install_auth(app, get_db, SessionLocal)
audit.install(SessionLocal)
audit.install_api(app, get_db)


@app.on_event("startup")
def init_db():
    Base.metadata.create_all(engine)
    ensure_auth_schema(engine)
    audit.ensure_schema(engine)
    locations.ensure_schema(engine, SessionLocal)
    bank.ensure_schema(engine)
    assets.ensure_schema(engine)
    company.ensure(SessionLocal)
    invoicing.ensure_schema(engine)
    mfg.ensure_schema(engine, SessionLocal)
    migrate_local_time(engine)
    with SessionLocal() as db:
        finance.seed_finance(db)


# 时间口径统一为北京时间（v0.36，2026-10-01 用户定）：这些表的 created_at 以前是 SQLite CURRENT_TIMESTAMP（UTC），
# 改成程序本地时间（容器 TZ=Asia/Shanghai），历史数据整体 +8 小时，只做一次（migration_record 记 key）。
UTC_CREATED_TABLES = ("archive", "material", "mfg_close", "migration_record", "partner", "tax_filing", "voucher", "asset",
                      "bank_txn", "bom_header", "contract", "expense", "material_alias", "material_bom", "work_order",
                      "stock_movement", "work_report")


def migrate_local_time(engine):
    from sqlalchemy import inspect, text
    key = "created_at_utc_to_local_20261001"
    have = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        if conn.execute(text("SELECT 1 FROM migration_record WHERE key = :k"), {"k": key}).first():
            return
        shifted = {}
        for t in UTC_CREATED_TABLES:
            if t in have:
                r = conn.execute(text(f"UPDATE {t} SET created_at = datetime(created_at, '+8 hours') WHERE created_at IS NOT NULL AND datetime(created_at) IS NOT NULL"))
                shifted[t] = r.rowcount
        conn.execute(text("INSERT INTO migration_record (key, detail, created_at) VALUES (:k, :d, :c)"),
                     {"k": key, "d": json.dumps(shifted, ensure_ascii=False), "c": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")})


# ---------- schemas ----------
class PartnerIn(InputModel):
    name: str
    code: str = ""
    short_name: str = ""
    is_customer: bool = False
    is_supplier: bool = False
    tax_no: str = ""
    address: str = ""
    phone: str = ""
    contact: str = ""
    contact_phone: str = ""
    bank_name: str = ""
    bank_account: str = ""
    remark: str = ""
    is_related: bool = False


class MaterialIn(InputModel):
    name: str
    code: str = ""
    spec: str = ""
    unit: str = "个"
    mat_group: str = ""   # 物料组两位码，如 01；组名由 MATERIAL_GROUPS 注册表给出
    remark: str = ""
    invoice_name: str = ""   # 开票项目名称（空=名称）
    tax_code: str = Field(default="", pattern=r"^(\d{19})?$")   # 税收分类编码 19 位（空=默认）
    # 制造属性（v0.35）；material_type 空 = 按物料组默认（成品组 goods，元器件组 raw/aux/pack/tool）
    material_type: str = ""
    manufacturer: str = ""
    mpn: str = ""
    package: str = ""
    msl: str = ""
    min_pack: float = Field(default=0, ge=0)
    pack_unit: str = ""
    loss_rate: float = Field(default=0, ge=0, lt=1)
    lot_control: bool = False
    key_part: bool = False
    std_hours: float = Field(default=0, ge=0)   # 产品单件标准工时（小时）
    smt_points: int = Field(default=0, ge=0)    # 贴片点数
    force: bool = False   # 新建时已确认「疑似重复」仍要新建


class AliasIn(InputModel):
    material_id: int
    partner_id: Optional[int] = None
    alias_name: str
    alias_spec: str = ""


class LineIn(InputModel):
    id: Optional[int] = None
    material_id: int
    qty: float = Field(gt=0)
    price_tax: float = Field(ge=0)
    remark: str = ""


class ContractIn(InputModel):
    contract_no: str
    contract_type: Literal["sales", "purchase"]
    partner_id: int
    sign_date: Optional[date] = None
    amount_tax: float = 0
    status: Literal["active", "draft", "replaced", "void"] = "active"
    ref_contract_no: str = ""
    project: str = ""
    remark: str = ""
    lines: list[LineIn] = []


class MovementIn(InputModel):
    move_type: Literal["in", "out", "adjust", "opening", "return_in", "return_out"]
    contract_id: Optional[int] = None
    contract_line_id: Optional[int] = None
    partner_id: Optional[int] = None
    material_id: int
    qty: float
    unit_cost: Optional[float] = Field(default=None, ge=0)  # 不含税账面成本（前端负责含税折算）
    tax_rate: float = Field(default=0.13, ge=0, le=1)             # 价税分离折算/暂估依据
    move_date: date
    remark: str = ""
    status: Literal["draft", "confirmed"] = "draft"
    location_id: Optional[int] = None   # 存放地点，空＝默认地点
    lot_no: str = ""                    # 批号（物料设了批次管理时必填，v0.35）
    lot_date: Optional[date] = None     # 生产日期 / date code


class InvoiceIn(InputModel):
    direction: Literal["input", "output"]
    invoice_no: str
    invoice_date: Optional[date] = None
    partner_id: Optional[int] = None
    contract_id: Optional[int] = None
    amount_ex_tax: float = 0
    tax_amount: float = 0
    amount_tax: float = 0
    file_path: str = ""
    verify_status: str = "未查验"
    remark: str = ""


class PaymentIn(InputModel):
    direction: Literal["pay", "receive"]
    partner_id: Optional[int] = None
    contract_id: Optional[int] = None
    amount: float = Field(gt=0)
    pay_date: date
    remark: str = ""
    source: str = "manual"


class ArchiveIn(InputModel):
    doc_type: str
    rel_type: str = ""
    rel_no: str
    file_path: str
    file_name: str = ""
    remark: str = ""


def r2(x):
    return round(float(x or 0), 2)


def _next_doc_no(db: Session, prefix: str) -> str:
    today = date.today().strftime("%Y%m%d")
    n = db.scalar(select(func.count(StockMovement.id)).where(StockMovement.doc_no.like(f"{prefix}-{today}%"))) or 0
    return f"{prefix}-{today}-{n + 1:03d}"


# ---------- partners ----------
@app.get("/api/partners")
def list_partners(db: Session = Depends(get_db)):
    out = []
    for p in db.scalars(select(Partner).order_by(Partner.id)):
        out.append({**p.__dict__, "_sa_instance_state": None} | {"_sa_instance_state": None})
    return [{k: v for k, v in p.__dict__.items() if k != "_sa_instance_state"} for p in db.scalars(select(Partner).order_by(Partner.id))]


@app.post("/api/partners")
def create_partner(data: PartnerIn, db: Session = Depends(get_db)):
    p = Partner(**data.model_dump())
    if not p.code:
        p.code = f"P{p.name[:4]}"
    db.add(p)
    db.commit()
    return {"id": p.id}


@app.put("/api/partners/{pid}")
def update_partner(pid: int, data: PartnerIn, db: Session = Depends(get_db)):
    p = db.get(Partner, pid) or _404()
    for k, v in data.model_dump().items():
        setattr(p, k, v)
    db.commit()
    return {"ok": True}


# ---------- materials ----------
@app.get("/api/materials")
def list_materials(db: Session = Depends(get_db)):
    mats = db.scalars(select(Material).where(Material.active == 1).order_by(Material.code)).all()
    aliases = db.scalars(select(MaterialAlias)).all()
    amap: dict[int, list] = {}
    for a in aliases:
        amap.setdefault(a.material_id, []).append(
            {"id": a.id, "partner_id": a.partner_id, "alias_name": a.alias_name, "alias_spec": a.alias_spec})
    all_mats = {m.id: m for m in db.scalars(select(Material)).all()}
    boms = db.scalars(select(MaterialBom).where(MaterialBom.active == 1)).all()
    bmap: dict[int, list] = {}
    for b in boms:
        ch = all_mats.get(b.child_material_id)
        bmap.setdefault(b.parent_material_id, []).append(
            {"id": b.id, "child_id": b.child_material_id, "child_code": ch.code if ch else "?",
             "child_name": ch.name if ch else "?", "child_spec": ch.spec if ch else "",
             "qty_per": b.qty_per, "remark": b.remark})
    return [{**{k: v for k, v in m.__dict__.items() if k != "_sa_instance_state"},
             "aliases": amap.get(m.id, []), "bom": bmap.get(m.id, [])} for m in mats]


def _norm_spec(s: str) -> str:
    import re
    return re.sub(r"[\s/／,，;；()（）\[\]【】\-_.·]+", "", (s or "").upper())


def similar_materials(db: Session, spec: str, exclude_id: int | None = None) -> list[dict]:
    """同一实物只允许一条物料：按规格/各方别名料号比对（去空白标点后互含，至少 6 位）。
    实际遇到过：客户改用自有料号时误建了新物料，而它就是供应商原来那个型号。"""
    key = _norm_spec(spec)
    is_part_no = lambda k: len(k) >= 6 and any(ch.isdigit() for ch in k)   # 纯品名（如「EC风机转换板」）不算料号
    if not is_part_no(key):
        return []
    partners = {p.id: p.short_name or p.name for p in db.scalars(select(Partner))}
    out = []
    for m in db.scalars(select(Material).where(Material.active == 1)):
        if m.id == exclude_id:
            continue
        cands = [("规格", m.spec)] + [((partners.get(a.partner_id) or "通用") + "料号", a.alias_spec)
                                     for a in db.scalars(select(MaterialAlias).where(MaterialAlias.material_id == m.id))]
        for via, text in cands:
            k = _norm_spec(text)
            if is_part_no(k) and (key in k or k in key):
                out.append({"id": m.id, "code": m.code, "name": m.name, "spec": m.spec, "via": via, "text": text})
                break
    return out


@app.get("/api/materials/similar")
def materials_similar(spec: str = "", exclude_id: int = 0, db: Session = Depends(get_db)):
    return similar_materials(db, spec, exclude_id or None)


@app.post("/api/materials")
def create_material(data: MaterialIn, db: Session = Depends(get_db)):
    if not data.name.strip() or (data.mat_group and data.mat_group not in MATERIAL_GROUPS) or data.unit not in UNITS:
        raise ValueError("物料名称、组码或单位无效")
    data.mat_group = data.mat_group or "99"
    data.material_type = data.material_type or mfg.default_material_type(data.mat_group)
    if data.material_type not in finance.MATERIAL_TYPES:
        raise ValueError("物料类型无效")
    dup = [] if data.force else similar_materials(db, data.spec)
    if dup:
        raise HTTPException(409, "疑似与已有物料重复：" + "；".join(f"{d['code']} {d['name']}（{d['via']} {d['text']}）" for d in dup)
                            + "。同一实物的对方叫法请在该物料上加别名，确认是新实物再强制新建")
    m = Material(**data.model_dump(exclude={"force"}))
    if not m.code:
        # 编码规矩：OZ-组码(2位)+组内序号（成品组 4 位、元器件组 10–19 为 5 位），组内取最大+1，永不复用
        m.code = mfg.next_code(db, m.mat_group or "99")
    db.add(m)
    db.commit()
    return {"id": m.id}


@app.put("/api/materials/{mid}")
def update_material(mid: int, data: MaterialIn, db: Session = Depends(get_db)):
    m = db.get(Material, mid) or _404()
    if data.code and data.code != m.code:
        raise ValueError("物料编码不可变更")
    if data.mat_group != m.mat_group:
        raise ValueError("物料已有组码编码，不能直接换组")
    if data.unit not in UNITS or not data.name.strip():
        raise ValueError("名称和单位无效")
    data.material_type = data.material_type or m.material_type or "goods"
    if data.material_type not in finance.MATERIAL_TYPES:
        raise ValueError("物料类型无效")
    if data.material_type != m.material_type and db.scalar(select(StockMovement.id).where(
            StockMovement.material_id == m.id, StockMovement.status == "confirmed").limit(1)):
        if finance.INV_ACCOUNT[data.material_type] != finance.INV_ACCOUNT.get(m.material_type or "goods"):
            raise ValueError("该物料已有出入库，改类型会换存货科目；请新建物料")
    for k, v in data.model_dump(exclude={"code", "force"}).items():
        setattr(m, k, v)
    db.commit()
    return {"ok": True}


@app.post("/api/material-aliases")
def create_alias(data: AliasIn, db: Session = Depends(get_db)):
    a = MaterialAlias(**data.model_dump())
    db.add(a)
    db.commit()
    return {"id": a.id}


@app.delete("/api/material-aliases/{aid}")
def delete_alias(aid: int, db: Session = Depends(get_db)):
    a = db.get(MaterialAlias, aid) or _404()
    db.delete(a)
    db.commit()
    return {"ok": True}


# ---------- material BOM：组套/拆套的构成主数据 ----------
MATERIAL_GROUPS = {"01": "驱动器", "02": "控制板", "03": "电抗器", "04": "温控器", "05": "传感器",
                   "06": "变压器", "07": "MCU", "08": "结构件", "09": "套件", **mfg.COMPONENT_GROUPS, "99": "其他"}
UNITS = mfg.UNITS


@app.get("/api/material-groups")
def material_groups():
    return [{"code": k, "name": v} for k, v in MATERIAL_GROUPS.items()]
class BomIn(InputModel):
    parent_material_id: int
    child_material_id: int
    qty_per: float = Field(default=1, gt=0)
    remark: str = ""


@app.post("/api/material-bom")
def add_bom(data: BomIn, db: Session = Depends(get_db)):
    if data.parent_material_id == data.child_material_id:
        raise HTTPException(400, "父子件不能相同")
    for mid in (data.parent_material_id, data.child_material_id):
        m = db.get(Material, mid)
        if not m or not m.active:
            raise HTTPException(404, f"物料 {mid} 不存在或已停用")
    if db.scalar(select(MaterialBom).where(MaterialBom.parent_material_id == data.parent_material_id,
                                           MaterialBom.child_material_id == data.child_material_id,
                                           MaterialBom.active == 1)):
        raise HTTPException(409, "该子件已存在")
    edges = {}
    for edge in db.scalars(select(MaterialBom).where(MaterialBom.active == True)):
        edges.setdefault(edge.parent_material_id, []).append(edge.child_material_id)
    stack, visited = [data.child_material_id], set()
    while stack:
        node = stack.pop()
        if node == data.parent_material_id:
            raise ValueError("BOM不能形成循环")
        if node not in visited:
            visited.add(node)
            stack.extend(edges.get(node, []))
    b = MaterialBom(**data.model_dump())
    db.add(b)
    db.flush()
    mfg.sync_from_material_bom(db, data.parent_material_id)
    db.commit()
    return {"id": b.id}


@app.delete("/api/material-bom/{bid}")
def delete_bom(bid: int, db: Session = Depends(get_db)):
    b = db.get(MaterialBom, bid) or _404()
    b.active = False
    db.flush()
    mfg.sync_from_material_bom(db, b.parent_material_id)
    db.commit()
    return {"ok": True}


@app.delete("/api/materials/{mid}/bom")
def clear_bom(mid: int, db: Session = Depends(get_db)):
    for b in db.scalars(select(MaterialBom).where(MaterialBom.parent_material_id == mid,
                                                  MaterialBom.active == 1)):
        b.active = False
    db.flush()
    mfg.sync_from_material_bom(db, mid)
    db.commit()
    return {"ok": True}


# ---------- contracts ----------
def contract_out(db: Session, c: Contract) -> dict:
    lines = [{"id": l.id, "line_no": l.line_no, "material_id": l.material_id,
              "material_name": l.material.name, "material_code": l.material.code,
              "spec": l.material.spec, "unit": l.material.unit,
              "qty": l.qty, "price_tax": l.price_tax, "amount_tax": l.amount_tax, "remark": l.remark}
             for l in c.lines]
    inv_sum = db.scalar(select(func.coalesce(func.sum(Invoice.amount_tax), 0)).where(
        Invoice.contract_id == c.id, Invoice.id.in_(O.active_documents(db, Invoice).with_only_columns(Invoice.id)))) or 0
    pay_sum = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.contract_id == c.id, Payment.id.in_(O.active_documents(db, Payment).with_only_columns(Payment.id)))) or 0
    moves = db.scalars(select(StockMovement).where(
        StockMovement.contract_id == c.id, StockMovement.status != "voided")).all()
    mv_lines = {(m.contract_line_id) for m in moves}
    stage, stage_cls, flows = contract_stage(db, c, inv_sum, pay_sum, sum(1 for m in moves if m.status == "confirmed"))
    return {
        "id": c.id, "contract_no": c.contract_no, "contract_type": c.contract_type,
        "partner_id": c.partner_id, "partner_name": c.partner.name if c.partner else "",
        "partner_short": c.partner.short_name if c.partner else "",
        "is_dual_role": bool(c.partner and c.partner.is_customer and c.partner.is_supplier) if c.partner else False,
        "sign_date": str(c.sign_date) if c.sign_date else "",
        "amount_tax": c.amount_tax, "status": c.status,
        "replaces_contract_id": c.replaces_contract_id,
        "ref_contract_no": c.ref_contract_no, "project": c.project, "remark": c.remark,
        "lines": lines,
        "fulfillment": O.fulfillment(db, c),
        "invoice_sum": r2(inv_sum), "payment_sum": r2(pay_sum),
        "moved_lines": len([x for x in mv_lines if x]) if mv_lines else 0,
        "total_lines": len(c.lines),
        "stage": stage, "stage_cls": stage_cls, "flows": flows,   # 四流状态（与执行看板同一规则）
    }


@app.get("/api/contracts")
def list_contracts(q: str = "", ctype: str = "", status: str = "", db: Session = Depends(get_db)):
    sel = select(Contract).order_by(Contract.sign_date.desc(), Contract.id.desc())
    rows = db.scalars(sel).all()
    out = []
    signed = contract_files.signed_map(db)
    for c in rows:
        if q and q.lower() not in (c.contract_no + (c.partner.name if c.partner else "")).lower():
            continue
        if ctype and c.contract_type != ctype:
            continue
        if status and c.status != status:
            continue
        o = contract_out(db, c)
        o.pop("lines", None)
        o["signed_files"] = signed.get(c.id, 0)
        out.append(o)
    return audit.attach_creators(db, "contract", out)


@app.get("/api/contracts/{cid}")
def get_contract(cid: int, db: Session = Depends(get_db)):
    c = db.get(Contract, cid) or _404()
    o = contract_out(db, c)
    o["invoices"] = [{"id": i.id, "direction": i.direction, "invoice_no": i.invoice_no,
                      "invoice_date": str(i.invoice_date) if i.invoice_date else "",
                      "amount_tax": i.amount_tax, "file_path": i.file_path, "remark": i.remark}
                     for i in db.scalars(O.active_documents(db, Invoice).where(Invoice.contract_id == cid))]
    o["payments"] = [{"id": p.id, "direction": p.direction, "amount": p.amount,
                      "pay_date": str(p.pay_date), "remark": p.remark, "source": p.source}
                     for p in db.scalars(O.active_documents(db, Payment).where(Payment.contract_id == cid).order_by(Payment.pay_date))]
    o["movements"] = [{"id": m.id, "doc_no": m.doc_no, "move_type": m.move_type,
                       "material_name": m.material.name, "qty": m.qty, "unit_cost": m.unit_cost,
                       "status": m.status, "move_date": str(m.move_date), "remark": m.remark,
                       "material_id": m.material_id, "contract_line_id": m.contract_line_id,
                       "location_id": m.location_id}
                      for m in db.scalars(select(StockMovement).where(StockMovement.contract_id == cid)
                                          .order_by(StockMovement.move_date, StockMovement.id))]
    o["archives"] = [{"id": a.id, "file_path": a.file_path, "file_name": a.file_name,
                      "doc_type": a.doc_type, "remark": a.remark}
                     for a in db.scalars(select(Archive).where(Archive.rel_no == c.contract_no))]
    if c.replaces_contract_id:
        old = db.get(Contract, c.replaces_contract_id)
        o["replaces_no"] = old.contract_no if old else ""
    replaced = db.scalar(select(Contract).where(Contract.replaces_contract_id == cid))
    o["replaced_by_no"] = replaced.contract_no if replaced else ""
    return o


@app.post("/api/contracts")
def create_contract(data: ContractIn, db: Session = Depends(get_db)):
    validate_contract(db, data)
    exists = db.scalar(select(Contract).where(Contract.contract_no == data.contract_no,
                                              Contract.contract_type == data.contract_type))
    if exists:
        raise HTTPException(400, f"合同号已存在：{data.contract_no}（{data.contract_type}）")
    c = Contract(**{k: v for k, v in data.model_dump().items() if k != "lines"})
    db.add(c)
    db.flush()
    for i, l in enumerate(data.lines, 1):
        db.add(ContractLine(contract_id=c.id, line_no=i, material_id=l.material_id,
                            qty=l.qty, price_tax=l.price_tax,
                            amount_tax=round(l.qty * l.price_tax, 2), remark=l.remark))
    db.commit()
    return {"id": c.id}


@app.put("/api/contracts/{cid}")
def update_contract(cid: int, data: ContractIn, db: Session = Depends(get_db)):
    c = db.get(Contract, cid) or _404()
    validate_contract(db, data)
    old = {l.id: l for l in c.lines}
    # 作废的出入库不算执行：录错的合同先作废单据即可改明细（实际遇到过：订单明细录成散件、原件是一套）
    used = {x for x in db.scalars(select(StockMovement.contract_line_id).where(
        StockMovement.contract_id == cid, StockMovement.status != "voided")) if x}
    executed = bool(used) or bool(db.scalar(select(Invoice.id).where(Invoice.contract_id == cid))) or bool(db.scalar(select(Payment.id).where(Payment.contract_id == cid)))
    if executed and (data.partner_id != c.partner_id or data.contract_type != c.contract_type or data.amount_tax != c.amount_tax or data.status != c.status):
        raise ValueError("已有执行记录，单位、方向、总额和状态变更请走合同改版")
    incoming = {l.id for l in data.lines if l.id}
    if used - incoming:
        raise ValueError("已关联出入库的合同明细不能删除；编辑时必须保留明细ID")
    for l in data.lines:
        if l.id and l.id not in old:
            raise ValueError("明细ID不属于该合同")
        if l.id in used and (old[l.id].material_id, old[l.id].qty, old[l.id].price_tax) != (l.material_id, l.qty, l.price_tax):
            raise ValueError("已执行明细不能改变物料、数量和价格，请走改版")
    for k, v in data.model_dump(exclude={"lines"}).items():
        setattr(c, k, v)
    for l in list(c.lines):
        if l.id not in incoming:
            for m in db.scalars(select(StockMovement).where(StockMovement.contract_line_id == l.id)):
                m.contract_line_id = None   # 只剩作废单据引用该行；保留作废记录，解除行关联
            db.delete(l)
    for n, l in enumerate(data.lines, 1):
        row = old.get(l.id) if l.id else ContractLine(contract_id=cid)
        for k,v in l.model_dump(exclude={"id"}).items():
            setattr(row, k, v)
        row.line_no, row.amount_tax = n, round(l.qty*l.price_tax, 2)
        db.add(row)
    O.audit(db, "编辑合同", contract_id=cid)
    db.commit()
    return {"ok": True}


# ---------- stock ----------
@app.get("/api/stock")
def stock_view(db: Session = Depends(get_db)):
    state = compute_material_stock(db)
    mats = {m.id: m for m in db.scalars(select(Material))}
    drafts = db.scalar(select(func.count(StockMovement.id)).where(StockMovement.status == "draft")) or 0
    by_loc = locations.location_stock(db)
    loc_names = {l.id: l.name for l in db.scalars(select(locations.StockLocation))}
    out = []
    for mid, s in state.items():
        m = mats.get(mid)
        if not m:
            continue
        out.append({"material_id": mid, "code": m.code, "name": m.name, "spec": m.spec,
                    "unit": m.unit, "mat_group": m.mat_group,
                    "qty": round(s["qty"], 3), "avg_cost": round(s["avg_cost"], 4),
                    "value": round(s["value"], 2), "neg": s["neg"],
                    "last_date": str(s["last_date"]) if s["last_date"] else "",
                    "locations": [{"id": lid, "name": loc_names.get(lid, "?"), "qty": round(q, 3)}
                                  for lid, q in sorted(by_loc.get(mid, {}).items(), key=lambda kv: -kv[1])]})
    out.sort(key=lambda x: (x["neg"] is False, -(x["value"] if True else 0)))
    total_value = r2(sum(x["value"] for x in out if True))
    return {"items": out, "total_value": total_value, "draft_count": drafts,
            "neg_count": sum(1 for x in out if x["neg"])}


@app.get("/api/movements")
def list_movements(status: str = "", mtype: str = "", contract_id: int = 0,
                   limit: int = Query(200, le=1000), db: Session = Depends(get_db)):
    sel = select(StockMovement).order_by(StockMovement.move_date.desc(), StockMovement.id.desc()).limit(limit)
    conds = []
    if status:
        conds.append(StockMovement.status == status)
    if mtype:
        conds.append(StockMovement.move_type == mtype)
    if contract_id:
        conds.append(StockMovement.contract_id == contract_id)
    if conds:
        sel = sel.where(and_(*conds))
    rows = audit.attach_creators(db, "stock_movement", [{"id": m.id, "doc_no": m.doc_no, "move_type": m.move_type,
             "contract_id": m.contract_id,
             "contract_no": m.contract.contract_no if m.contract else "",
             "contract_type": m.contract.contract_type if m.contract else "",
             "partner_name": m.partner.name if m.partner else "",
             "partner_short": (m.partner.short_name or m.partner.name) if m.partner else "",
             "material_code": m.material.code,
             "material_id": m.material_id, "material_name": m.material.name,
             "spec": m.material.spec, "unit": m.material.unit,
             "qty": m.qty, "unit_cost": m.unit_cost, "status": m.status,
             "move_date": str(m.move_date), "remark": m.remark,
             "contract_line_id": m.contract_line_id, "location_id": m.location_id}
            for m in db.scalars(sel)])
    confirmers = audit.status_actors(db, "stock_movement", [r["id"] for r in rows], "confirmed")
    for r in rows:
        r["confirmed_by"] = confirmers.get(str(r["id"]), "")
    return rows


@app.post("/api/movements")
def create_movement(data: MovementIn, db: Session = Depends(get_db)):
    if data.move_type in ("return_in", "return_out"):
        raise ValueError("退货必须从原出入库单发起")
    validate_movement(db, data)
    data.location_id = locations.check_location(db, data.location_id)
    m = StockMovement(**data.model_dump(exclude={"status"}), status="draft")
    prefix = {"in": "IN", "out": "OUT", "opening": "OPN", "adjust": "ADJ", "return_in": "RTI", "return_out": "RTO"}[data.move_type]
    m.doc_no = _next_doc_no(db, prefix)
    db.add(m)
    db.flush()
    if data.status == "confirmed":
        confirm_movement(db, m)
        O.synchronize(db)
    O.audit(db, "新增库存单", movement_id=m.id)
    db.commit()
    st = compute_material_stock(db, m.material_id).get(m.material_id, {})
    return {"id": m.id, "doc_no": m.doc_no, "status": m.status,
            "will_negative": st.get("qty", 0) < -1e-9}


@app.post("/api/movements/{mid}/confirm")
def confirm_api(mid: int, db: Session = Depends(get_db)):
    m = db.get(StockMovement, mid) or _404()
    if m.status != "draft":
        raise HTTPException(400, "只有草稿可确认")
    validate_movement(db, m, legacy=True)
    confirm_movement(db, m)
    result = O.synchronize(db)
    O.audit(db, "确认库存单", movement_id=m.id)
    db.commit()
    st = compute_material_stock(db, m.material_id).get(m.material_id, {})
    return {"ok": True, "qty_after": round(st.get("qty", 0), 3), "negative": st.get("qty", 0) < -1e-9,
            "voucher_no": result["created_ids"][-1] if result["created_ids"] else None}


class MoveReasonIn(InputModel):
    reason: str = Field(min_length=1)


class MoveRedateIn(InputModel):
    move_date: date
    reason: str = Field(min_length=1)


@app.post("/api/movements/{mid}/void")
def void_api(mid: int, data: MoveReasonIn, db: Session = Depends(get_db)):
    """作废须写原因（v0.26，记入备注；此前作废不留原因）。"""
    m = db.get(StockMovement, mid) or _404()
    if m.status == "voided":
        raise HTTPException(400, "已作废")
    O.assert_open(db, m.move_date)
    from .models import ConversionMovement, DocumentLink
    if any(db.get(StockMovement,x.child_id).status != "voided" for x in db.scalars(select(DocumentLink).where(DocumentLink.kind == "stock_return",DocumentLink.original_id == mid))):
        raise ValueError("原单有有效退货，请先处理退货单")
    if db.get(ConversionMovement, mid):
        raise ValueError("组拆套单必须整体作废")
    if m.move_type == "wo_cost":
        raise ValueError("成本分摊单随月末成本结转生成，要改请撤销成本结转")
    if m.work_order_id:
        from .models import WorkOrder
        w = db.get(WorkOrder, m.work_order_id)
        if w and w.status != "released":
            raise ValueError(f"工单 {w.wo_no} 已关闭或取消，先重开工单再作废它的单据")
    m.status = "voided"
    m.remark = (m.remark + "；" if m.remark else "") + f"作废：{data.reason}"
    O.audit(db, "作废库存单", movement_id=mid, reason=data.reason)
    O.synchronize(db, f"库存单 {m.doc_no} 作废")
    db.commit()
    return {"ok": True}


def redate_movement(db, m: StockMovement, new_date: date, reason: str):
    """已确认出入库单改日期（v0.26）：日期录错时用，成本按新顺序重放，凭证由 synchronize 红冲重过。"""
    if m.status != "confirmed":
        raise ValueError("只有已确认的单据可以改日期；草稿直接编辑、作废单不能改")
    from .models import ConversionMovement
    if db.get(ConversionMovement, m.id):
        raise ValueError("组拆套单不能单独改日期")
    O.assert_open(db, m.move_date)
    O.assert_open(db, new_date)
    old = m.move_date
    m.move_date = new_date
    m.remark = (m.remark + "；" if m.remark else "") + f"日期 {old}→{new_date}：{reason}"
    O.audit(db, "库存单改日期", movement_id=m.id, before=str(old), after=str(new_date), reason=reason)
    O.synchronize(db, f"库存单 {m.doc_no} 改日期")


@app.post("/api/movements/{mid}/redate")
def redate_api(mid: int, data: MoveRedateIn, db: Session = Depends(get_db)):
    m = db.get(StockMovement, mid) or _404()
    redate_movement(db, m, data.move_date, data.reason)
    db.commit()
    st = compute_material_stock(db, m.material_id).get(m.material_id, {})
    return {"ok": True, "qty_after": round(st.get("qty", 0), 3)}


# ---------- invoices / payments ----------
@app.get("/api/invoices")
def list_invoices(direction: str = "", db: Session = Depends(get_db)):
    sel = O.active_documents(db, Invoice).order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    if direction:
        sel = sel.where(Invoice.direction == direction)
    rows = [{"id": i.id, "direction": i.direction, "invoice_no": i.invoice_no,
             "invoice_date": str(i.invoice_date) if i.invoice_date else "",
             "partner_id": i.partner_id, "partner_name": i.partner.name if i.partner else "",
             "partner_short": (i.partner.short_name or i.partner.name) if i.partner else "",
             "contract_id": i.contract_id,
             "contract_no": i.contract.contract_no if i.contract else "",
             "amount_ex_tax": i.amount_ex_tax, "tax_amount": i.tax_amount,
             "amount_tax": i.amount_tax, "file_path": i.file_path,
             "verify_status": i.verify_status, "remark": i.remark}
            for i in db.scalars(sel)]
    return audit.attach_creators(db, "invoice", rows)


@app.post("/api/invoices")
def create_invoice(data: InvoiceIn, db: Session = Depends(get_db)):
    if db.scalar(O.active_documents(db, Invoice).where(Invoice.invoice_no == data.invoice_no)):
        raise HTTPException(400, "发票号码已存在")
    validate_document(db, data, "invoice")
    i = Invoice(**data.model_dump())
    db.add(i)
    db.flush()
    result = O.synchronize(db)
    O.audit(db, "新增发票", invoice_id=i.id)
    db.commit()
    return {"id": i.id, "voucher_no": result["created_ids"][-1] if result["created_ids"] else None}


@app.get("/api/payments")
def list_payments(direction: str = "", db: Session = Depends(get_db)):
    sel = O.active_documents(db, Payment).order_by(Payment.pay_date.desc(), Payment.id.desc())
    if direction:
        sel = O.active_documents(db, Payment).where(Payment.direction == direction).order_by(Payment.pay_date.desc())
    return audit.attach_creators(db, "payment", [{"id": p.id, "direction": p.direction,
             "partner_id": p.partner_id, "partner_name": p.partner.name if p.partner else "",
             "partner_short": (p.partner.short_name or p.partner.name) if p.partner else "",
             "contract_id": p.contract_id,
             "contract_no": p.contract.contract_no if p.contract else "",
             "amount": p.amount, "pay_date": str(p.pay_date), "source": p.source,
             "remark": p.remark} for p in db.scalars(sel)])


@app.post("/api/payments")
def create_payment(data: PaymentIn, db: Session = Depends(get_db)):
    validate_document(db, data, "payment")
    p = Payment(**data.model_dump())
    db.add(p)
    db.flush()
    result = O.synchronize(db)
    O.audit(db, "新增收付款", payment_id=p.id)
    db.commit()
    return {"id": p.id, "voucher_no": result["created_ids"][-1] if result["created_ids"] else None}


# ---------- archive ----------
@app.get("/api/archives")
def list_archives(rel_no: str = "", db: Session = Depends(get_db)):
    sel = select(Archive).order_by(Archive.id.desc())
    if rel_no:
        sel = sel.where(Archive.rel_no == rel_no)
    return [{k: v for k, v in a.__dict__.items() if k != "_sa_instance_state"} for a in db.scalars(sel)]


@app.post("/api/archives")
def create_archive(data: ArchiveIn, db: Session = Depends(get_db)):
    a = Archive(**data.model_dump())
    if not a.file_name:
        a.file_name = Path(a.file_path).name
    db.add(a)
    db.commit()
    return {"id": a.id}


@app.delete("/api/archives/{aid}")
def delete_archive(aid: int, db: Session = Depends(get_db)):
    a = db.get(Archive, aid) or _404()
    db.delete(a)
    db.commit()
    return {"ok": True}


# ---------- dashboard ----------
def contract_stage(db, c, inv_sum, pay_sum, moved):
    """四流（签约→收发货→开票→收付款）卡在哪一步。旧看板只看收付款，货已交、票已开的也显示「待执行」。"""
    buy = c.contract_type == "purchase"
    ff = O.fulfillment(db, c)
    goods_done = ff["complete"] if c.lines else moved > 0
    inv_done = inv_sum + 0.005 >= c.amount_tax
    pay_done = pay_sum + 0.005 >= c.amount_tax
    flows = [1, 1 if goods_done else 0, 1 if inv_done else 0, 1 if pay_done else 0]
    if goods_done and inv_done and pay_done:
        return "已闭合", "p-ok", flows
    if not goods_done:
        if moved:
            return ("部分收货" if buy else "部分发货"), "p-info", flows
        if inv_sum or pay_sum:
            return ("待收货" if buy else "待发货"), "p-info", flows
        return "待执行", "p-gray", flows
    if not inv_done:
        return ("待收票" if buy else "待开票") if not inv_sum else ("部分收票" if buy else "部分开票"), "p-draft", flows
    return ("待付款" if buy else "待收款") if not pay_sum else ("部分付款" if buy else "部分收款"), "p-red", flows


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db)):
    st = compute_material_stock(db)
    total_value = r2(sum(s["value"] for s in st.values() if True))
    neg_count = sum(1 for s in st.values() if s["neg"])
    draft_count = db.scalar(select(func.count(StockMovement.id)).where(StockMovement.status == "draft")) or 0

    # 只看有效合同：被替代/作废的旧版既不该出现在四流里，也不该计入应收应付（被替代的旧版曾被算进应收）
    contracts = db.scalars(select(Contract).where(Contract.status == "active")).all()
    pipeline = []
    ar_total = ap_total = 0.0
    for c in contracts:
        inv_sum = db.scalar(select(func.coalesce(func.sum(Invoice.amount_tax), 0)).where(
            Invoice.contract_id == c.id, Invoice.id.in_(O.active_documents(db, Invoice).with_only_columns(Invoice.id)))) or 0
        pay_sum = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.contract_id == c.id, Payment.id.in_(O.active_documents(db, Payment).with_only_columns(Payment.id)))) or 0
        moved = db.scalar(select(func.count(StockMovement.id)).where(
            StockMovement.contract_id == c.id, StockMovement.status == "confirmed")) or 0
        if c.contract_type == "sales":
            ar = c.amount_tax - pay_sum
            if ar > 0.005:
                ar_total += ar
        else:
            ap = c.amount_tax - pay_sum
            if ap > 0.005:
                ap_total += ap
        stage, stage_cls, flows = contract_stage(db, c, inv_sum, pay_sum, moved)
        pipeline.append({
            "id": c.id, "contract_no": c.contract_no, "contract_type": c.contract_type,
            "partner_name": c.partner.name if c.partner else "",
            "partner_short": (c.partner.short_name or c.partner.name) if c.partner else "",
            "sign_date": str(c.sign_date) if c.sign_date else "",
            "amount_tax": c.amount_tax, "status": c.status,
            "inv_sum": r2(inv_sum), "pay_sum": r2(pay_sum),
            "moved": moved, "flows": flows, "stage": stage, "stage_cls": stage_cls,
        })
    # 看板只放没走完的（按签订日早→晚）；已闭合的数一下，去合同页看（合同越来越多时看板不会被拉长，v0.27）
    closed_count = sum(1 for x in pipeline if x["stage"] == "已闭合")
    pipeline = [x for x in pipeline if x["stage"] != "已闭合"]
    pipeline.sort(key=lambda x: (x["contract_type"], x["sign_date"] or "9999", x["contract_no"]))

    # 风险清单：负库存 + 差额 + 草稿
    risks = []
    for mid, s in st.items():
        if s["neg"]:
            m = db.get(Material, mid)
            risks.append({"type": "negative", "level": "red",
                          "text": f"{m.name if m else mid} 现存量 {round(s['qty'], 2)} 为负，需补期初或确认入库"})
    for c in contracts:
        pay_sum = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.contract_id == c.id, Payment.id.in_(O.active_documents(db, Payment).with_only_columns(Payment.id)))) or 0
        diff = round(c.amount_tax - pay_sum, 2)
        if c.status == "active" and abs(diff) > 0.005 and diff != c.amount_tax:
            if diff > 0 and pay_sum > 0:
                risks.append({"type": "pay_gap", "level": "warn",
                              "text": f"{c.contract_no} {c.partner.name if c.partner else ''} 已付/收 {r2(pay_sum)}，差额 {diff}"})
    return {"kpi": {"total_value": total_value, "ar": r2(ar_total), "ap": r2(ap_total),
                    "draft_count": draft_count, "neg_count": neg_count},
            "pipeline": pipeline, "closed_count": closed_count, "risks": risks}


@app.get("/api/health")
def health():
    from .paths import data_dir
    return {"ok": True, "db": DB_PATH, "data_dir": str(data_dir())}


# ---------- 财务：科目 / 成本中心 / 凭证 / 账表 ----------
@app.get("/api/accounts")
def list_accounts(db: Session = Depends(get_db)):
    finance.seed_finance(db)
    return [{"code": a.code, "name": a.name, "acc_type": a.acc_type, "direction": a.direction,
             "parent_code": a.parent_code or "", "active": a.active, "memo": a.memo}
            for a in db.scalars(select(Account).order_by(Account.code))]


class AccountIn(InputModel):
    code: str
    name: str
    acc_type: str
    direction: str
    parent_code: Optional[str] = None
    memo: str = ""


@app.post("/api/accounts")
def create_account(data: AccountIn, db: Session = Depends(get_db)):
    if (not data.code.strip() or not data.name.strip() or data.acc_type not in
            ("asset", "liability", "equity", "revenue", "expense") or data.direction not in ("D", "C")):
        raise ValueError("科目代码、名称、类型或方向无效")
    if data.parent_code:
        parent = db.get(Account, data.parent_code)
        if not parent or not parent.active or parent.direction != data.direction:
            raise ValueError("父科目不存在、已停用或借贷方向不一致")
        if db.scalar(select(VoucherLine.id).where(VoucherLine.account_code == parent.code).limit(1)):
            raise ValueError("已有凭证的科目不能作为父科目；只允许末级科目过账")
    if db.get(Account, data.code):
        raise HTTPException(400, "科目代码已存在")
    db.add(Account(**data.model_dump())
           if data.parent_code is not None else
           Account(code=data.code, name=data.name, acc_type=data.acc_type,
                   direction=data.direction, parent_code=None, memo=data.memo))
    db.commit()
    return {"ok": True}


@app.get("/api/cost-centers")
def list_cost_centers(db: Session = Depends(get_db)):
    finance.seed_finance(db)
    return [{"id": c.id, "code": c.code, "name": c.name, "parent_id": c.parent_id,
             "active": c.active, "memo": c.memo, "full": cc_full_name(db, c)}
            for c in db.scalars(select(CostCenter).order_by(CostCenter.code))]


def cc_full_name(db, c):
    parts, cur, guard = [], c, 0
    while cur is not None and guard < 10:
        parts.insert(0, cur.name)
        cur = db.get(CostCenter, cur.parent_id)
        guard += 1
    return "/".join(parts)


class CostCenterIn(InputModel):
    code: str
    name: str
    parent_id: Optional[int] = None
    memo: str = ""


@app.post("/api/cost-centers")
def create_cost_center(data: CostCenterIn, db: Session = Depends(get_db)):
    if not data.code.strip() or not data.name.strip():
        raise ValueError("成本中心代码和名称必填")
    if data.parent_id and not db.get(CostCenter, data.parent_id):
        raise ValueError("父成本中心不存在")
    if db.scalar(select(CostCenter).where(CostCenter.code == data.code)):
        raise HTTPException(400, "成本中心代码已存在")
    c = CostCenter(code=data.code, name=data.name, parent_id=data.parent_id, memo=data.memo)
    db.add(c)
    db.commit()
    return {"id": c.id}


@app.put("/api/cost-centers/{cid}")
def update_cost_center(cid: int, data: CostCenterIn, db: Session = Depends(get_db)):
    c = db.get(CostCenter, cid) or _404()
    if data.parent_id == cid:
        raise HTTPException(400, "父级不能是自己")
    seen, parent = {cid}, data.parent_id
    while parent is not None:
        if parent in seen:
            raise ValueError("成本中心不能形成循环")
        seen.add(parent)
        ancestor = db.get(CostCenter,parent)
        if not ancestor:
            raise ValueError("父成本中心不存在")
        parent = ancestor.parent_id
    c.code, c.name, c.parent_id, c.memo = data.code, data.name, data.parent_id, data.memo
    db.commit()
    return {"ok": True}


class VoucherLineIn(InputModel):
    account: str
    debit: float = 0
    credit: float = 0
    partner_id: Optional[int] = None
    cost_center_id: Optional[int] = None
    summary: str = ""


class VoucherIn(InputModel):
    voucher_date: date
    summary: str = ""
    lines: list[VoucherLineIn]


@app.get("/api/vouchers")
def list_vouchers(from_date: str = "", to_date: str = "", source_type: str = "",
                  db: Session = Depends(get_db)):
    q = select(Voucher).order_by(Voucher.voucher_date.desc(), Voucher.id.desc()).limit(500)
    if from_date:
        q = q.where(Voucher.voucher_date >= from_date)
    if to_date:
        q = q.where(Voucher.voucher_date <= to_date)
    if source_type:
        q = q.where(Voucher.source_type == source_type)
    out = []
    for v in db.scalars(q):
        td = round(sum(l.debit for l in v.lines), 2)
        out.append({"id": v.id, "voucher_no": v.voucher_no, "voucher_date": str(v.voucher_date),
                    "source_type": v.source_type, "source_id": v.source_id,
                    "summary": v.summary, "status": v.status, "reversal_of": v.reversal_of,
                    "total": td, "line_count": len(v.lines)})
    return audit.attach_creators(db, "voucher", out)


@app.get("/api/vouchers/{vid}")
def get_voucher(vid: int, db: Session = Depends(get_db)):
    v = db.get(Voucher, vid) or _404()
    return {"id": v.id, "voucher_no": v.voucher_no, "voucher_date": str(v.voucher_date),
            "source_type": v.source_type, "source_id": v.source_id, "summary": v.summary,
            "status": v.status, "reversal_of": v.reversal_of,
            "lines": [{"line_no": l.line_no, "account": l.account_code,
                       "account_name": l.account.name if l.account else "",
                       "partner_id": l.partner_id,
                       "partner_name": l.partner.name if l.partner else "",
                       "cost_center_id": l.cost_center_id,
                       "cost_center_name": l.cost_center.name if l.cost_center else "",
                       "debit": l.debit, "credit": l.credit, "summary": l.summary}
                      for l in v.lines]}


@app.post("/api/vouchers")
def create_voucher_api(data: VoucherIn, db: Session = Depends(get_db)):
    try:
        v = finance.create_voucher(db, data.voucher_date, "manual", None, data.summary,
                                   [l.model_dump() for l in data.lines])
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return {"id": v.id, "voucher_no": v.voucher_no}


@app.get("/api/finance/ledger")
def finance_ledger(account: str, partner_id: Optional[int] = None,
                   from_date: str = "", to_date: str = "", db: Session = Depends(get_db)):
    return finance.ledger(db, account, partner_id, from_date or None, to_date or None)


@app.get("/api/finance/trial-balance")
def finance_trial_balance(from_date: str = "", to_date: str = "", db: Session = Depends(get_db)):
    return finance.trial_balance(db, from_date or None, to_date or None)


@app.get("/api/finance/ar-ap")
def finance_ar_ap(db: Session = Depends(get_db)):
    rows = finance.ar_ap_summary(db)
    pmap = {p.id: p for p in db.scalars(select(Partner))}
    for r in rows:
        p = pmap.get(r["partner_id"])
        r["partner_code"] = p.code if p else ""
        r["partner_name"] = p.name if p else ""
    return rows


@app.get("/api/finance/income-statement")
def finance_income_statement(from_date: str, to_date: str, db: Session = Depends(get_db)):
    return finance.income_statement(db, from_date, to_date)


@app.get("/api/finance/balance-sheet")
def finance_balance_sheet(asof: str, db: Session = Depends(get_db)):
    return finance.balance_sheet(db, asof)


# ---------- 三单匹配：合同明细履约、票货款差额闭环 ----------
@app.get("/api/three-way-match")
def three_way_match(contract_type: str = "purchase", db: Session = Depends(get_db)):
    if contract_type not in ("purchase", "sales"):
        raise ValueError("无效合同方向")
    return O.matching(db, contract_type)


# ---------- 台账导出：ERP 生成原台账同构 xlsx，Excel 仅作历史参考 ----------
@app.get("/api/ledger-export")
def ledger_export(db: Session = Depends(get_db)):
    import io
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "合同主表"
    ws.append(["合同编号", "合同方向", "客户名称", "供应商名称", "签约日期", "合同金额含税", "状态", "关联合同", "源文件", "备注"])
    for c in db.scalars(select(Contract).order_by(Contract.contract_no)):
        ws.append([c.contract_no, "销售" if c.contract_type == "sales" else "采购",
                   c.partner.name if c.contract_type == "sales" and c.partner else "",
                   c.partner.name if c.contract_type == "purchase" and c.partner else "",
                   str(c.sign_date) if c.sign_date else "", c.amount_tax, c.status,
                   c.ref_contract_no or "", "", c.remark or ""])

    def line_sheet(title, ctype):
        s = wb.create_sheet(title)
        s.append(["合同编号", "供应商" if ctype == "purchase" else "客户", "物料编码", "品名", "规格型号",
                  "单位", "数量", "单价含税", "金额含税", "备注"])
        for l in db.scalars(select(ContractLine).join(Contract).where(Contract.contract_type == ctype)
                            .order_by(Contract.contract_no, ContractLine.line_no)):
            m = l.material
            s.append([l.contract.contract_no, l.contract.partner.name if l.contract.partner else "",
                      m.code, m.name, m.spec, m.unit, l.qty, l.price_tax, l.amount_tax, l.remark])

    line_sheet("采购订单明细", "purchase")
    line_sheet("销售订单明细", "sales")
    s = wb.create_sheet("发货记录")
    s.append(["发货单号", "销售订单号", "客户", "发货日期", "物料编码", "品名", "数量", "状态"])
    for m in db.scalars(select(StockMovement).where(StockMovement.move_type == "out")
                        .order_by(StockMovement.move_date)):
        if m.status == "voided":
            continue
        s.append([m.doc_no, m.contract.contract_no if m.contract else "",
                  m.partner.name if m.partner else "", str(m.move_date), m.material.code, m.material.name,
                  m.qty, "已确认" if m.status == "confirmed" else "草稿"])
    s = wb.create_sheet("发票记录")
    s.append(["发票方向", "发票号码", "开票日期", "对方单位", "不含税金额", "税额", "价税合计", "挂接合同", "备注"])
    for i in db.scalars(O.active_documents(db, Invoice).order_by(Invoice.invoice_date)):
        s.append(["销项" if i.direction == "output" else "进项", i.invoice_no,
                  str(i.invoice_date) if i.invoice_date else "", i.partner.name if i.partner else "",
                  i.amount_ex_tax, i.tax_amount, i.amount_tax,
                  i.contract.contract_no if i.contract else "", i.remark or ""])
    s = wb.create_sheet("收付款记录")
    s.append(["收付方向", "日期", "对方单位", "挂接合同", "金额", "来源", "备注"])
    cmap = {c.id: c.contract_no for c in db.scalars(select(Contract))}
    for p in db.scalars(O.active_documents(db, Payment).order_by(Payment.pay_date)):
        s.append(["收款" if p.direction == "receive" else "付款", str(p.pay_date),
                  p.partner.name if p.partner else "", cmap.get(p.contract_id, ""),
                  p.amount, p.source, p.remark or ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=owe-ledger-{date.today():%Y%m%d}.xlsx"})


def _404():
    raise HTTPException(404, "not found")


from .control_api import install
install(app, get_db)
from . import contract_ops
contract_ops.install(app, get_db, ContractIn, _next_doc_no)
locations.install(app, get_db)
bank.install(app, get_db)
assets.install(app, get_db)
invoice_docs.install(app, get_db)
tax_filing.install(app, get_db)
tax_forms.install(app, get_db)
mfg.install(app, get_db)
statements.install(app, get_db)
company.install(app, get_db)
contract_files.install(app, get_db)
close_check.install(app, get_db)
seals.install(app, get_db)
invoicing.install(app, get_db)
todos.install(app, get_db)
prefs.install(app, get_db)

# ---------- 静态前端 ----------
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")

    @app.get("/")
    def index():
        return FileResponse(str(WEB_DIR / "index.html"))
