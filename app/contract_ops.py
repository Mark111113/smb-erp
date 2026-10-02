"""合同执行类操作：按明细收发货、批量确认草稿、单据改挂合同、合同改版。

这些原先只能靠脚本改数据，现在都给页面留了入口。
凭证仍由 operations.synchronize 统一生成；改动经 SessionLocal 自动进 change_log。
"""
from datetime import date
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from . import operations as O
from .inventory import compute_material_stock, confirm_movement, end_of_day, replay
from .models import Contract, ContractLine, Invoice, Payment, StockMovement
from .validation import assert_open, validate_contract, validate_movement
from .locations import check_location

EPS = 1e-9


class _In(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class FulfillLine(_In):
    line_id: int
    qty: float = Field(gt=0)


class FulfillIn(_In):
    move_date: date
    lines: list[FulfillLine] = Field(min_length=1)
    confirm: bool = True
    tax_rate: float = Field(default=0.13, ge=0, le=1)
    remark: str = ""
    location_id: Optional[int] = None   # 存放地点，空＝默认地点


class BatchConfirmIn(_In):
    ids: list[int] = Field(min_length=1)
    move_date: Optional[date] = None   # 填写则把草稿日期改为该日再确认


class RelinkIn(_In):
    contract_id: Optional[int] = None


class SplitPart(_In):
    amount: float = Field(gt=0)
    contract_id: Optional[int] = None
    remark: str = ""


class SplitIn(_In):
    parts: list[SplitPart] = Field(min_length=1)   # 拆出去的部分；原笔保留余额


def _direction(c: Contract):
    return ("in", "purchase") if c.contract_type == "purchase" else ("out", "sales")


def line_progress(db, c: Contract) -> dict[int, dict]:
    """每行：订购、已确认交付（含退货冲减）、未确认草稿、可再交付。"""
    done = {r["line_id"]: r["delivered"] for r in O.fulfillment(db, c)["lines"]}
    forward, _ = _direction(c)
    drafts = {}
    for m in db.scalars(select(StockMovement).where(StockMovement.contract_id == c.id,
                                                   StockMovement.status == "draft",
                                                   StockMovement.move_type == forward)):
        if m.contract_line_id:
            drafts[m.contract_line_id] = drafts.get(m.contract_line_id, 0) + m.qty
    out = {}
    for l in c.lines:
        d, dr = done.get(l.id, 0), drafts.get(l.id, 0)
        out[l.id] = {"line_id": l.id, "ordered": l.qty, "delivered": d, "draft": dr,
                     "open": round(l.qty - d - dr, 6)}
    return out


def _with_warnings(db, result: dict) -> dict:
    moves = result.pop("moves")
    confirmed = [m for m in moves if m.status == "confirmed"]
    return {**result, "negative": _negatives(db, [m.material_id for m in moves]),
            "short_on_date": _short_on_date(db, confirmed)}


def _negatives(db, material_ids) -> list[dict]:
    stock = compute_material_stock(db)
    res = []
    for mid in sorted(set(material_ids)):
        q = stock.get(mid, {}).get("qty", 0)
        if q < -EPS:
            res.append({"material_id": mid, "qty": round(q, 3)})
    return res


def _short_on_date(db, moves) -> list[dict]:
    """出库当日日终库存为负的单据：成本先按 0 暂估，等到货后由负库存重估补成本调整。"""
    res = []
    for m in moves:
        if m.move_type in ("in", "opening", "return_in"):
            continue
        q = replay(db, m.material_id, before=end_of_day(m.move_date))[0].get(m.material_id, {}).get("qty", 0)
        if q < -EPS:
            res.append({"movement_id": m.id, "doc_no": m.doc_no, "material_id": m.material_id,
                        "move_date": str(m.move_date), "qty_on_date": round(q, 3)})
    return res


def _relink_document(db, doc, kind: str, contract_id: Optional[int]):
    if not O.active(db, kind, doc.id):
        raise ValueError("已作废的单据不能改挂")
    assert_open(db, doc.invoice_date if kind == "invoice" else doc.pay_date)
    if contract_id:
        c = db.get(Contract, contract_id)
        if not c or c.status != "active":
            raise ValueError("目标合同不存在或非有效状态")
        purchase = doc.direction in ("input", "pay")
        if c.contract_type != ("purchase" if purchase else "sales"):
            raise ValueError("目标合同方向与单据不一致")
        if doc.partner_id != c.partner_id:
            raise ValueError("目标合同的往来单位与单据不一致")
    old = doc.contract_id
    doc.contract_id = contract_id
    O.audit(db, "改挂合同", kind=kind, id=doc.id, from_contract_id=old, to_contract_id=contract_id)


def split_payment(db, p: Payment, data: SplitIn) -> list[Payment]:
    """一笔银行款拆到多张合同：原笔改为余额，拆出部分新建同日同单位同方向的收付款。

    凭证由 synchronize 重算（原笔金额变化自动红冲重过）；已有核销与退款不得超过原笔新余额。
    """
    from sqlalchemy import func
    from .models import Allocation, DocumentLink
    if not O.active(db, "payment", p.id) or p.amount <= 0:
        raise ValueError("只能拆分有效的正数收付款")
    assert_open(db, p.pay_date)
    moved = round(sum(x.amount for x in data.parts), 2)
    rest = round(p.amount - moved, 2)
    if rest <= 0:
        raise ValueError(f"拆出合计 {moved:.2f} 须小于原金额 {p.amount:.2f}（原笔要保留余额）")
    used = db.scalar(select(func.coalesce(func.sum(Allocation.amount), 0))
                     .where(Allocation.payment_id == p.id, Allocation.status == "active")) or 0
    refunded = sum(-db.get(Payment, x.child_id).amount for x in db.scalars(
        select(DocumentLink).where(DocumentLink.kind == "refund", DocumentLink.original_id == p.id))
        if O.active(db, "payment", x.child_id))
    if used + refunded > rest + .005:
        raise ValueError(f"原笔已核销/退款 {used + refunded:.2f}，超过拆分后余额 {rest:.2f}；请先取消相关核销")
    original = p.amount
    created = []
    for x in data.parts:
        n = Payment(direction=p.direction, partner_id=p.partner_id, contract_id=None, amount=round(x.amount, 2),
                    pay_date=p.pay_date, source=p.source, bank_txn_id=p.bank_txn_id,
                    remark=(x.remark + "；" if x.remark else "") + f"拆自收付款#{p.id}（原额 {original:.2f}）")
        db.add(n)
        db.flush()
        if x.contract_id:
            _relink_document(db, n, "payment", x.contract_id)
        db.add(DocumentLink(kind="split", original_id=p.id, child_id=n.id, amount=n.amount))
        created.append(n)
    p.amount = rest
    p.remark = (p.remark + "；" if p.remark else "") + f"已拆出 {moved:.2f}（原额 {original:.2f}）"
    O.synchronize(db, "收付款拆分")
    O.audit(db, "收付款拆分", payment_id=p.id, before=original, after=rest,
            children=[n.id for n in created])
    return created


def fulfill(db, c: Contract, data: FulfillIn, next_doc_no) -> dict:
    if c.status != "active":
        raise ValueError("只有有效合同可以收发货")
    move_type, _ = _direction(c)
    prog = line_progress(db, c)
    lines = {l.id: l for l in c.lines}
    location_id = check_location(db, data.location_id)
    seen = set()
    created = []
    for x in data.lines:
        l = lines.get(x.line_id)
        if not l:
            raise ValueError("明细不属于该合同")
        if x.line_id in seen:
            raise ValueError("同一明细行不能重复提交")
        seen.add(x.line_id)
        if x.qty - prog[l.id]["open"] > 1e-6:
            raise ValueError(f"第 {l.line_no} 行 {l.material.name} 超过可交付数量 {prog[l.id]['open']:g}"
                             f"（已交付 {prog[l.id]['delivered']:g}，草稿 {prog[l.id]['draft']:g}）")
        unit_cost = round(l.price_tax / (1 + data.tax_rate), 6) if move_type == "in" else None
        m = StockMovement(move_type=move_type, contract_id=c.id, contract_line_id=l.id,
                          partner_id=c.partner_id, material_id=l.material_id, qty=x.qty,
                          unit_cost=unit_cost, tax_rate=data.tax_rate, move_date=data.move_date,
                          remark=data.remark, status="draft", location_id=location_id)
        validate_movement(db, m)
        m.doc_no = next_doc_no(db, "IN" if move_type == "in" else "OUT")
        db.add(m)
        db.flush()
        created.append(m)
    if data.confirm:
        for m in created:
            confirm_movement(db, m)
        O.synchronize(db)
    O.audit(db, "按合同收货" if move_type == "in" else "按合同发货", contract_id=c.id,
            movement_ids=[m.id for m in created], confirmed=data.confirm)
    db.flush()
    return {"created": [{"id": m.id, "doc_no": m.doc_no, "status": m.status} for m in created],
            "moves": created}


def batch_confirm(db, data: BatchConfirmIn) -> dict:
    moves = [db.get(StockMovement, i) for i in dict.fromkeys(data.ids)]
    if any(m is None for m in moves):
        raise HTTPException(404, "单据不存在")
    if any(m.status != "draft" for m in moves):
        raise ValueError("只有草稿可确认")
    for m in moves:
        if data.move_date:
            m.move_date = data.move_date
        validate_movement(db, m, legacy=True)
    for m in sorted(moves, key=lambda m: (m.move_date, m.id)):
        confirm_movement(db, m)
    O.synchronize(db)
    O.audit(db, "批量确认库存单", movement_ids=[m.id for m in moves],
            move_date=str(data.move_date) if data.move_date else None)
    db.flush()
    return {"confirmed": len(moves), "moves": moves}


def install(app, get_db, ContractIn, next_doc_no):
    @app.get("/api/contracts/{cid}/progress")
    def progress(cid: int, db=Depends(get_db)):
        c = db.get(Contract, cid) or _404()
        return list(line_progress(db, c).values())

    @app.post("/api/contracts/{cid}/fulfill")
    def fulfill_api(cid: int, data: FulfillIn, db=Depends(get_db)):
        """按合同明细一次生成多张收货/发货单；confirm=True 时直接过账。"""
        c = db.get(Contract, cid) or _404()
        result = fulfill(db, c, data, next_doc_no)
        db.commit()
        return _with_warnings(db, result)

    @app.post("/api/movements/batch-confirm")
    def batch_confirm_api(data: BatchConfirmIn, db=Depends(get_db)):
        """批量确认草稿；可统一改日期（草稿日期多为迁移时的暂估日）。"""
        result = batch_confirm(db, data)
        db.commit()
        return _with_warnings(db, result)

    @app.post("/api/invoices/{id_}/relink")
    def relink_invoice(id_: int, data: RelinkIn, db=Depends(get_db)):
        doc = db.get(Invoice, id_) or _404()
        _relink_document(db, doc, "invoice", data.contract_id)
        db.commit()
        return {"ok": True}

    @app.post("/api/payments/{id_}/relink")
    def relink_payment(id_: int, data: RelinkIn, db=Depends(get_db)):
        doc = db.get(Payment, id_) or _404()
        _relink_document(db, doc, "payment", data.contract_id)
        db.commit()
        return {"ok": True}

    @app.post("/api/payments/{id_}/split")
    def split_payment_api(id_: int, data: SplitIn, db=Depends(get_db)):
        p = db.get(Payment, id_) or _404()
        created = split_payment(db, p, data)
        db.commit()
        return {"id": p.id, "amount": p.amount, "children": [n.id for n in created]}

    class ReviseIn(ContractIn):
        move_documents: bool = True

    @app.post("/api/contracts/{cid}/revise")
    def revise(cid: int, data: ReviseIn, db=Depends(get_db)):
        """合同改版：新建新版本并替代旧版；可把旧版的发票、收付款、出入库一并转到新版。"""
        old = db.get(Contract, cid) or _404()
        if old.status not in ("active", "void"):
            raise ValueError("只有有效或作废的合同可以改版")
        if db.scalar(select(Contract.id).where(Contract.replaces_contract_id == old.id)):
            raise ValueError("该合同已有新版本")
        if data.contract_type != old.contract_type or data.partner_id != old.partner_id:
            raise ValueError("改版不能改变合同方向和往来单位；请另立新合同")
        if data.contract_no == old.contract_no:
            raise ValueError("新版合同号需与原合同不同，如加「-新」「-v2」")
        fields = data.model_dump(exclude={"lines", "move_documents"})
        fields["status"] = "active"
        validate_contract(db, ContractIn(**{**fields, "lines": data.lines}))
        if db.scalar(select(Contract.id).where(Contract.contract_no == data.contract_no,
                                               Contract.contract_type == data.contract_type)):
            raise ValueError(f"合同号已存在：{data.contract_no}")
        new = Contract(**fields, replaces_contract_id=old.id)
        db.add(new)
        db.flush()
        for i, l in enumerate(data.lines, 1):
            db.add(ContractLine(contract_id=new.id, line_no=i, material_id=l.material_id, qty=l.qty,
                                price_tax=l.price_tax, amount_tax=round(l.qty * l.price_tax, 2), remark=l.remark))
        db.flush()
        db.refresh(new)
        old.status = "replaced"
        moved = {"invoices": 0, "payments": 0, "movements": 0}
        kept = []
        if data.move_documents:
            for kind, model in (("invoice", Invoice), ("payment", Payment)):
                for doc in db.scalars(select(model).where(model.contract_id == old.id)):
                    if not O.active(db, kind, doc.id):
                        continue
                    try:
                        _relink_document(db, doc, kind, new.id)
                        moved[kind + "s"] += 1
                    except ValueError as e:
                        kept.append(f"{'发票' if kind == 'invoice' else '收付款'}#{doc.id}：{e}")
            by_material = {}
            for l in new.lines:
                by_material.setdefault(l.material_id, []).append(l)
            for m in db.scalars(select(StockMovement).where(StockMovement.contract_id == old.id,
                                                           StockMovement.status != "voided")):
                targets = by_material.get(m.material_id, [])
                if len(targets) != 1:
                    kept.append(f"库存单 {m.doc_no}：新版{'没有' if not targets else '有多行'}同物料明细，留在原合同")
                    continue
                m.contract_id, m.contract_line_id = new.id, targets[0].id
                moved["movements"] += 1
        O.audit(db, "合同改版", old_contract_id=old.id, new_contract_id=new.id, moved=moved, kept=kept)
        db.commit()
        return {"id": new.id, "moved": moved, "kept": kept}


def _404():
    raise HTTPException(404, "不存在")
