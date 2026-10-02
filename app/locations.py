"""存放地点与调拨（v0.17）。

货会分散在自己仓库、供应商寄存、客户处等地，ERP 原先只记总数（找货靠翻聊天记录拼位置）。
地点只管数量：成本仍按全公司移动加权平均（inventory.replay），调拨不出凭证。
出入库单 location_id 为空＝默认地点；各地点数量 = 已确认出入库按地点汇总 ± 已确认调拨。
"""
from datetime import date, datetime
from typing import Literal, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect, select, text

from . import operations as O
from .models import Material, Partner, StockLocation, StockMovement, StockTransfer

EPS = 1e-9
DEFAULT_CODE = "SZ-FAC"


class _In(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, str_strip_whitespace=True)


class LocationIn(_In):
    code: str = Field(min_length=1, max_length=24)
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["own", "third_party"] = "own"
    purpose: Literal["general", "raw", "wip", "finished", "defect", "rd"] = "general"
    partner_id: Optional[int] = None
    address: str = ""
    contact: str = ""
    is_default: bool = False
    active: bool = True
    remark: str = ""


class TransferIn(_In):
    material_id: int
    qty: float = Field(gt=0)
    from_location_id: int
    to_location_id: int
    move_date: date
    remark: str = ""
    confirm: bool = True


class DateIn(_In):
    move_date: Optional[date] = None


class MoveLocationIn(_In):
    location_id: int


def ensure_schema(engine, session_factory):
    cols = {c["name"] for c in inspect(engine).get_columns("stock_movement")}
    if "location_id" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE stock_movement ADD COLUMN location_id INTEGER REFERENCES stock_location(id)"))
    with session_factory() as db:
        if not db.scalar(select(StockLocation.id).limit(1)):
            db.add(StockLocation(code=DEFAULT_CODE, name="主仓库", kind="own", is_default=True))   # 名称地址在 主数据 → 存放地点 改
            db.commit()


def default_location_id(db) -> int:
    lid = db.scalar(select(StockLocation.id).where(StockLocation.is_default == True, StockLocation.active == True))
    if lid is None:
        raise ValueError("未设置默认存放地点")
    return lid


def check_location(db, location_id: Optional[int]) -> int:
    if location_id is None:
        return default_location_id(db)
    loc = db.get(StockLocation, location_id)
    if not loc or not loc.active:
        raise ValueError("存放地点不存在或已停用")
    return loc.id


def _signed(m: StockMovement) -> float:
    if m.move_type in ("in", "opening", "return_in", "wo_return", "wo_receipt"):
        return m.qty
    if m.move_type in ("adjust", "wo_cost"):      # wo_cost 只调金额，qty=0
        return m.qty
    return -abs(m.qty)


def location_stock(db, material_id: Optional[int] = None, upto: Optional[date] = None) -> dict[int, dict[int, float]]:
    """{material_id: {location_id: qty}}；upto 给定时只算到该日（含）。"""
    dflt = default_location_id(db)
    out: dict[int, dict[int, float]] = {}

    def add(mid, lid, q):
        d = out.setdefault(mid, {})
        d[lid] = d.get(lid, 0.0) + q

    q = select(StockMovement).where(StockMovement.status == "confirmed")
    t = select(StockTransfer).where(StockTransfer.status == "confirmed")
    if material_id:
        q, t = q.where(StockMovement.material_id == material_id), t.where(StockTransfer.material_id == material_id)
    if upto:
        q, t = q.where(StockMovement.move_date <= upto), t.where(StockTransfer.move_date <= upto)
    for m in db.scalars(q):
        add(m.material_id, m.location_id or dflt, _signed(m))
    for x in db.scalars(t):
        add(x.material_id, x.from_location_id, -x.qty)
        add(x.material_id, x.to_location_id, x.qty)
    return {mid: {lid: round(v, 6) for lid, v in d.items() if abs(v) > EPS} for mid, d in out.items()}


def _loc_json(loc: StockLocation, partners: dict) -> dict:
    return {"id": loc.id, "code": loc.code, "name": loc.name, "kind": loc.kind, "purpose": loc.purpose or "general", "partner_id": loc.partner_id,
            "partner_name": partners.get(loc.partner_id, ""), "address": loc.address, "contact": loc.contact,
            "is_default": loc.is_default, "active": loc.active, "remark": loc.remark}


def _next_transfer_no(db) -> str:
    today = date.today().strftime("%Y%m%d")
    count = len(db.scalars(select(StockTransfer.id).where(StockTransfer.doc_no.like(f"TR-{today}%"))).all())
    return f"TR-{today}-{count + 1:03d}"


def _short_after(db, x: StockTransfer) -> Optional[float]:
    """调出地点在调拨日日终的数量为负则返回该数（货还没到就调走了）。"""
    q = location_stock(db, x.material_id, upto=x.move_date).get(x.material_id, {}).get(x.from_location_id, 0)
    return round(q, 3) if q < -EPS else None


def confirm_transfer(db, x: StockTransfer, move_date: Optional[date] = None):
    if x.status != "draft":
        raise ValueError("只有草稿可确认")
    if move_date:
        x.move_date = move_date
    O.assert_open(db, x.move_date)
    x.status = "confirmed"
    x.confirmed_at = datetime.now()
    O.audit(db, "确认调拨", transfer_id=x.id)


def create_transfer(db, data: TransferIn) -> StockTransfer:
    if data.from_location_id == data.to_location_id:
        raise ValueError("调出和调入地点不能相同")
    m = db.get(Material, data.material_id)
    if not m or not m.active:
        raise ValueError("请选择有效物料")
    for lid in (data.from_location_id, data.to_location_id):
        check_location(db, lid)
    O.assert_open(db, data.move_date)
    x = StockTransfer(doc_no=_next_transfer_no(db), material_id=m.id, qty=data.qty,
                      from_location_id=data.from_location_id, to_location_id=data.to_location_id,
                      move_date=data.move_date, remark=data.remark, status="draft")
    db.add(x)
    db.flush()
    O.audit(db, "新建调拨", transfer_id=x.id)
    if data.confirm:
        confirm_transfer(db, x)
    db.flush()
    return x


def install(app, get_db):
    def partners(db):
        return {p.id: p.short_name or p.name for p in db.scalars(select(Partner))}

    @app.get("/api/locations")
    def list_locations(db=Depends(get_db)):
        pn = partners(db)
        stock = location_stock(db)
        totals: dict[int, int] = {}
        for d in stock.values():
            for lid, qty in d.items():
                totals[lid] = totals.get(lid, 0) + (1 if qty else 0)
        return [{**_loc_json(l, pn), "sku_count": totals.get(l.id, 0)}
                for l in db.scalars(select(StockLocation).order_by(StockLocation.is_default.desc(), StockLocation.code))]

    def _save(db, loc: StockLocation, data: LocationIn):
        if data.kind == "third_party" and not data.partner_id:
            raise ValueError("寄存在往来单位处的地点须选择该单位")
        if data.partner_id and not db.get(Partner, data.partner_id):
            raise ValueError("往来单位不存在")
        dup = db.scalar(select(StockLocation.id).where(StockLocation.code == data.code, StockLocation.id != (loc.id or 0)))
        if dup:
            raise ValueError("地点编码已存在")
        if loc.id and loc.is_default and not data.is_default:
            raise ValueError("请把另一个地点设为默认，而不是直接取消默认")
        if loc.id and not data.active:
            left = {mid: d.get(loc.id) for mid, d in location_stock(db).items() if d.get(loc.id)}
            if left:
                raise ValueError("该地点还有库存，先调拨清空再停用")
        for k, v in data.model_dump().items():
            setattr(loc, k, v)
        if data.is_default:
            for other in db.scalars(select(StockLocation).where(StockLocation.id != (loc.id or 0))):
                other.is_default = False

    @app.post("/api/locations")
    def create_location(data: LocationIn, db=Depends(get_db)):
        loc = StockLocation()
        _save(db, loc, data)
        db.add(loc)
        db.commit()
        return {"id": loc.id}

    @app.put("/api/locations/{lid}")
    def update_location(lid: int, data: LocationIn, db=Depends(get_db)):
        loc = db.get(StockLocation, lid) or _404()
        _save(db, loc, data)
        db.commit()
        return {"ok": True}

    @app.get("/api/locations/stock")
    def stock_by_location(db=Depends(get_db)):
        mats = {m.id: m for m in db.scalars(select(Material))}
        locs = {l.id: l for l in db.scalars(select(StockLocation))}
        rows = []
        for mid, d in location_stock(db).items():
            for lid, qty in d.items():
                m, l = mats[mid], locs[lid]
                rows.append({"material_id": mid, "code": m.code, "name": m.name, "spec": m.spec, "unit": m.unit,
                             "location_id": lid, "location_name": l.name, "qty": qty})
        return sorted(rows, key=lambda r: (r["location_name"], r["code"]))

    def _transfer_json(x: StockTransfer, locs) -> dict:
        return {"id": x.id, "doc_no": x.doc_no, "material_id": x.material_id, "code": x.material.code,
                "material_name": x.material.name, "spec": x.material.spec, "unit": x.material.unit, "qty": x.qty,
                "from_location_id": x.from_location_id, "from_name": locs[x.from_location_id].name,
                "to_location_id": x.to_location_id, "to_name": locs[x.to_location_id].name,
                "move_date": str(x.move_date), "status": x.status, "remark": x.remark}

    @app.get("/api/transfers")
    def list_transfers(db=Depends(get_db)):
        locs = {l.id: l for l in db.scalars(select(StockLocation))}
        from . import audit
        rows = [_transfer_json(x, locs) for x in db.scalars(select(StockTransfer).order_by(
            StockTransfer.move_date.desc(), StockTransfer.id.desc()))]
        return audit.attach_creators(db, "stock_transfer", rows)

    @app.post("/api/transfers")
    def create_transfer_api(data: TransferIn, db=Depends(get_db)):
        x = create_transfer(db, data)
        db.commit()
        return {"id": x.id, "doc_no": x.doc_no, "status": x.status,
                "short_on_date": _short_after(db, x) if x.status == "confirmed" else None}

    @app.post("/api/transfers/{tid}/confirm")
    def confirm_transfer_api(tid: int, data: DateIn, db=Depends(get_db)):
        x = db.get(StockTransfer, tid) or _404()
        confirm_transfer(db, x, data.move_date)
        db.commit()
        return {"ok": True, "short_on_date": _short_after(db, x)}

    @app.post("/api/transfers/{tid}/void")
    def void_transfer(tid: int, db=Depends(get_db)):
        x = db.get(StockTransfer, tid) or _404()
        if x.status == "voided":
            raise ValueError("已作废")
        O.assert_open(db, x.move_date)
        x.status = "voided"
        O.audit(db, "作废调拨", transfer_id=x.id)
        db.commit()
        return {"ok": True}

    @app.post("/api/movements/{mid}/location")
    def set_movement_location(mid: int, data: MoveLocationIn, db=Depends(get_db)):
        """改出入库单的存放地点：只影响地点数量，不影响成本和凭证。"""
        m = db.get(StockMovement, mid) or _404()
        if m.status == "voided":
            raise ValueError("已作废的单据不能改地点")
        old = m.location_id
        m.location_id = check_location(db, data.location_id)
        O.audit(db, "修改出入库地点", movement_id=m.id, before=old, after=m.location_id)
        db.commit()
        return {"ok": True}


def _404():
    raise HTTPException(404, "不存在")
