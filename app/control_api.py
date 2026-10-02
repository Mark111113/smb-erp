"""Correction, settlement, fulfillment and period controls."""
from datetime import date, timedelta
import math
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from .models import (Invoice, Payment, StockMovement, Contract, Material, MaterialBom,
                     Voucher, Allocation, PeriodLock, AuditEvent, StockConversion,
                     ConversionMovement, DocumentLink, Partner)
from . import operations as O, finance as F
from .inventory import replay, confirm_movement, end_of_day


class Reason(BaseModel):
    reason: str = Field(min_length=1)


class Settlement(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    payment_id: int
    invoice_id: int
    amount: float = Field(gt=0)
    allocation_date: date


class Credit(Reason):
    model_config = ConfigDict(allow_inf_nan=False)
    document_date: date
    amount: float = Field(gt=0)
    invoice_no: str = ""
    net: float = Field(default=0, ge=0)
    tax: float = Field(default=0, ge=0)


class Component(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    material_id: int
    qty: float = Field(gt=0)
    weight: float | None = Field(default=None, gt=0)


class Return(Reason):
    model_config = ConfigDict(allow_inf_nan=False)
    qty: float = Field(gt=0)
    move_date: date


class LinkLine(BaseModel):
    contract_line_id: int


class Conversion(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    kind: str
    move_date: date
    parent_material_id: int
    qty: float = Field(gt=0)
    components: list[Component] = Field(min_length=1)


def install(app, get_db):
    @app.post("/api/movements/{id_}/return")
    def stock_return(id_: int, data: Return, db=Depends(get_db)):
        original = db.get(StockMovement,id_)
        if not original or original.status != "confirmed" or original.move_type not in ("in","out") or db.get(ConversionMovement,id_):
            raise ValueError("请选择有效采购入库或销售出库原单")
        if data.move_date < original.move_date:
            raise ValueError("退货不能早于原单")
        O.assert_open(db,data.move_date)
        returned = sum(x.amount for x in db.scalars(select(DocumentLink).where(DocumentLink.kind == "stock_return",DocumentLink.original_id == id_)) if db.get(StockMovement,x.child_id).status == "confirmed")
        if data.qty+returned > original.qty+1e-9:
            raise ValueError("退货数量超过原单未退数量")
        if not original.unit_cost or original.unit_cost <= 0:
            raise ValueError("原单尚无可靠成本，请先处理暂估成本")
        m = StockMovement(doc_no="RETURN",move_type="return_out" if original.move_type == "in" else "return_in",material_id=original.material_id,partner_id=original.partner_id,contract_id=original.contract_id,contract_line_id=original.contract_line_id,qty=data.qty,unit_cost=original.unit_cost,tax_rate=original.tax_rate,move_date=data.move_date,status="confirmed",remark=f"原单 {original.doc_no} 退货：{data.reason}")
        db.add(m)
        db.flush()
        m.doc_no=f"RET-{m.id}"
        db.add(DocumentLink(kind="stock_return",original_id=id_,child_id=m.id,amount=data.qty))
        O.synchronize(db,"退货")
        O.audit(db,"退货",original_id=id_,movement_id=m.id,qty=data.qty,reason=data.reason)
        db.commit()
        return {"id":m.id}

    @app.post("/api/movements/{id_}/link-line")
    def link_line(id_: int, data: LinkLine, db=Depends(get_db)):
        from .models import ContractLine
        m,l=db.get(StockMovement,id_),db.get(ContractLine,data.contract_line_id)
        if not m or m.status == "voided" or not l or m.contract_id != l.contract_id or m.material_id != l.material_id:
            raise ValueError("单据、合同明细或物料不一致")
        O.assert_open(db,m.move_date)
        O.audit(db,"补合同明细关联",movement_id=id_,before=m.contract_line_id,after=l.id)
        m.contract_line_id=l.id
        db.commit()
        return {"ok":True}

    @app.post("/api/invoices/{id_}/void")
    def void_invoice(id_: int, data: Reason, db=Depends(get_db)):
        result = O.void_document(db, "invoice", id_, data.reason)
        db.commit()
        return result

    @app.post("/api/payments/{id_}/void")
    def void_payment(id_: int, data: Reason, db=Depends(get_db)):
        result = O.void_document(db, "payment", id_, data.reason)
        db.commit()
        return result

    @app.post("/api/vouchers/{id_}/reverse")
    def reverse_manual(id_: int, data: Reason, db=Depends(get_db)):
        v = db.get(Voucher, id_)
        if not v or v.source_type != "manual":
            raise ValueError("自动凭证请从原业务单据更正")
        rv = F.reverse_voucher(db, v, data.reason)
        O.audit(db, "手工凭证红冲", voucher_id=id_, reversal_id=rv.id, reason=data.reason)
        db.commit()
        return {"id": rv.id}

    @app.post("/api/invoices/{id_}/credit")
    def credit_invoice(id_: int, data: Credit, db=Depends(get_db)):
        i = db.get(Invoice, id_)
        if not i or i.amount_tax <= 0 or not O.active(db, "invoice", id_):
            raise ValueError("请选择有效原发票")
        O.assert_open(db, data.document_date)
        if not data.invoice_no.strip() or data.document_date < i.invoice_date or abs(data.net+data.tax-data.amount) > .005:
            raise ValueError("红字发票号、日期、价税金额无效")
        links = list(db.scalars(select(DocumentLink).where(DocumentLink.kind == "credit", DocumentLink.original_id == id_)))
        children = [db.get(Invoice, x.child_id) for x in links if O.active(db, "invoice", x.child_id)]
        if data.amount+sum(-x.amount_tax for x in children) > i.amount_tax+.005 or data.net+sum(-x.amount_ex_tax for x in children) > i.amount_ex_tax+.005 or data.tax+sum(-x.tax_amount for x in children) > i.tax_amount+.005:
            raise ValueError("红字金额超过原票剩余额度")
        # Cancel settlements so corrected net invoice can be explicitly settled again.
        for a in db.scalars(select(Allocation).where(Allocation.invoice_id == id_, Allocation.status == "active")):
            O.assert_open(db, a.allocation_date)
            a.status = "voided"
        new = Invoice(direction=i.direction, invoice_no=data.invoice_no, invoice_date=data.document_date,
                      partner_id=i.partner_id, contract_id=i.contract_id, amount_ex_tax=-data.net,
                      tax_amount=-data.tax, amount_tax=-data.amount, remark=f"红字 {i.invoice_no}：{data.reason}")
        db.add(new)
        db.flush()
        db.add(DocumentLink(kind="credit", original_id=id_, child_id=new.id, amount=data.amount))
        O.synchronize(db, data.reason)
        O.audit(db, "红字发票", original_id=id_, child_id=new.id, reason=data.reason)
        db.commit()
        return {"id": new.id}

    @app.post("/api/payments/{id_}/refund")
    def refund(id_: int, data: Credit, db=Depends(get_db)):
        p = db.get(Payment, id_)
        if not p or p.amount <= 0 or not O.active(db, "payment", id_):
            raise ValueError("请选择有效原收付款")
        O.assert_open(db, data.document_date)
        if data.document_date < p.pay_date:
            raise ValueError("退款不能早于原款")
        links = list(db.scalars(select(DocumentLink).where(DocumentLink.kind == "refund", DocumentLink.original_id == id_)))
        refunded = sum(x.amount for x in links if O.active(db, "payment", x.child_id))
        used = db.scalar(select(func.coalesce(func.sum(Allocation.amount), 0)).where(Allocation.payment_id == id_, Allocation.status == "active")) or 0
        if data.amount + refunded + used > p.amount+.005:
            raise ValueError("退款超过未核销款项；请先取消相关核销")
        new = Payment(direction=p.direction, partner_id=p.partner_id, contract_id=p.contract_id,
                      amount=-data.amount, pay_date=data.document_date, remark=f"原款 {id_} 退款：{data.reason}")
        db.add(new)
        db.flush()
        db.add(DocumentLink(kind="refund", original_id=id_, child_id=new.id, amount=data.amount))
        O.synchronize(db, data.reason)
        O.audit(db, "退款", original_id=id_, child_id=new.id, amount=data.amount)
        db.commit()
        return {"id": new.id}

    @app.get("/api/allocations")
    def allocations(db=Depends(get_db)):
        """核销记录（v0.28 起带单位、合同、票号、款日期、录入人与录入时间，页面不再只显示 ID）。"""
        from .models import ChangeLog
        made = {}
        for key, at, name in db.execute(select(ChangeLog.row_key, ChangeLog.at, ChangeLog.user_name)
                                        .where(ChangeLog.table_name == "allocation", ChangeLog.op == "insert")):
            made[key] = (str(at)[:16], name)
        out = []
        for a in db.scalars(select(Allocation).order_by(Allocation.id.desc())):
            p, i = db.get(Payment, a.payment_id), db.get(Invoice, a.invoice_id)
            partner = (i.partner or p.partner) if (i and i.partner) or (p and p.partner) else None
            at, who = made.get(str(a.id), ("", ""))
            out.append(dict(id=a.id, payment_id=a.payment_id, invoice_id=a.invoice_id, amount=a.amount,
                            allocation_date=str(a.allocation_date), status=a.status,
                            direction=p.direction if p else "", partner_short=(partner.short_name or partner.name) if partner else "",
                            contract_no=(i.contract.contract_no if i and i.contract else (p.contract.contract_no if p and p.contract else "")),
                            invoice_no=i.invoice_no if i else "", invoice_date=str(i.invoice_date) if i and i.invoice_date else "",
                            invoice_amount=i.amount_tax if i else None, pay_date=str(p.pay_date) if p else "",
                            pay_amount=p.amount if p else None, created_at=at, created_by=who))
        return out

    @app.post("/api/allocations")
    def create_allocation(data: Settlement, db=Depends(get_db)):
        a = O.allocate(db, data.payment_id, data.invoice_id, data.amount, data.allocation_date)
        O.synchronize(db)
        db.commit()
        return {"id": a.id}

    @app.post("/api/allocations/auto")
    def auto_allocation(db=Depends(get_db)):
        done = O.auto_allocate(db)
        O.synchronize(db, "按合同自动核销")
        O.audit(db, "按合同自动核销", count=len(done))
        db.commit()
        return {"allocated": done}

    @app.post("/api/allocations/{id_}/void")
    def void_allocation(id_: int, data: Reason, db=Depends(get_db)):
        a = db.get(Allocation, id_)
        if not a or a.status != "active":
            raise ValueError("核销不存在或已取消")
        O.assert_open(db, a.allocation_date)
        a.status = "voided"
        O.audit(db, "取消核销", id=id_, reason=data.reason)
        O.synchronize(db, data.reason)
        db.commit()
        return {"ok": True}

    @app.get("/api/settlement-open-items")
    def open_items(asof: date = date.today(), db=Depends(get_db)):
        rows = []
        for kind, model in (("invoice", Invoice), ("payment", Payment)):
            for obj in db.scalars(O.active_documents(db, model)):
                d = obj.invoice_date if kind == "invoice" else obj.pay_date
                if d > asof:
                    continue
                col = Allocation.invoice_id if kind == "invoice" else Allocation.payment_id
                used = db.scalar(select(func.coalesce(func.sum(Allocation.amount), 0)).where(col == obj.id, Allocation.status == "active", Allocation.allocation_date <= asof)) or 0
                linked = sum(x.amount for x in db.scalars(select(DocumentLink).where(DocumentLink.kind == ("credit" if kind == "invoice" else "refund"), DocumentLink.original_id == obj.id)) if O.active(db, kind, x.child_id) and (db.get(model, x.child_id).invoice_date if kind == "invoice" else db.get(model, x.child_id).pay_date) <= asof)
                total = obj.amount_tax if kind == "invoice" else obj.amount
                if total <= 0:
                    continue
                remain = round(total-linked-used, 2)
                days = (asof-d).days
                partner = db.get(Partner, obj.partner_id)
                rows.append(dict(kind=kind, id=obj.id, number=obj.invoice_no if kind == "invoice" else str(obj.id), partner_id=obj.partner_id,
                                 partner_name=partner.name if partner else "待补单位", direction=obj.direction, contract_id=obj.contract_id,
                                 date=str(d), amount=total, used=used, remaining=remain, age_days=days,
                                 bucket="0–30" if days <= 30 else "31–60" if days <= 60 else "61–90" if days <= 90 else "90+"))
        return rows

    @app.get("/api/reconciliation")
    def reconciliation(db=Depends(get_db)):
        r = O.reconciliation(db)
        r["cross_month_unbilled"] = []
        for c in db.scalars(select(Contract).where(Contract.contract_type == "sales", Contract.status == "active")):
            delivered = db.scalar(select(StockMovement.id).where(StockMovement.contract_id == c.id, StockMovement.status == "confirmed", StockMovement.move_type == "out", StockMovement.move_date < date.today().replace(day=1)).limit(1))
            billed = sum(x.amount_tax for x in db.scalars(O.active_documents(db, Invoice).where(Invoice.contract_id == c.id)))
            ff = O.fulfillment(db, c)
            prices = {x.id: x.price_tax for x in c.lines}
            delivered_value = sum(x["delivered"]*prices[x["line_id"]] for x in ff["lines"])
            if delivered and (delivered_value > billed+.01 or ff["unlinked_movement_ids"]):
                r["cross_month_unbilled"].append(dict(contract_no=c.contract_no, delivered_value=round(delivered_value,2), invoiced=billed, unlinked=ff["unlinked_movement_ids"]))
        return r

    @app.post("/api/contracts/{id_}/settle-cost")
    def settle_cost(id_: int, data: Reason, db=Depends(get_db)):
        c = db.get(Contract, id_)
        if not c or c.contract_type != "purchase" or not O.fulfillment(db,c)["complete"]:
            raise ValueError("必须是按合同明细全部收齐的采购合同")
        invoices = list(db.scalars(O.active_documents(db,Invoice).where(Invoice.contract_id == id_)))
        if not invoices or abs(sum(i.amount_tax for i in invoices)-c.amount_tax) > .01:
            raise ValueError("发票未开齐或与合同额不符，不能自动分摊价差")
        moves = list(db.scalars(select(StockMovement).where(StockMovement.contract_id == id_,StockMovement.status == "confirmed")))
        if any(m.move_type != "in" for m in moves):
            raise ValueError("含退货等复杂流转，请用明确的库存调整和清算凭证处理")
        total = round(sum(i.amount_ex_tax for i in invoices),2)
        before = sum(m.qty*(m.unit_cost or 0) for m in moves)
        if before <= 0 or total <= 0:
            raise ValueError("缺少有效成本分摊依据")
        assigned = 0
        for n,m in enumerate(moves):
            O.assert_open(db,m.move_date)
            value = round(total-assigned,2) if n == len(moves)-1 else round(total*m.qty*(m.unit_cost or 0)/before,2)
            assigned += value
            O.audit(db,"收票价差分摊",movement_id=m.id,before=m.unit_cost,after=value/m.qty,reason=data.reason)
            m.unit_cost = value/m.qty
        O.synchronize(db,data.reason)
        db.commit()
        return {"ok":True,"before":round(before,2),"after":total}

    @app.post("/api/finance/close-year/{year}")
    def close_year(year: int, data: Reason, db=Depends(get_db)):
        if year >= date.today().year:
            raise ValueError("仅允许结转已经结束的年度")
        d = date(year,12,31)
        O.assert_open(db,d)
        if db.scalar(select(Voucher.id).where(Voucher.source_type == "closing",Voucher.source_id == year,Voucher.status == "posted")):
            raise ValueError("该年度已结转")
        O.synchronize(db,"年结前对账")
        from .models import Account
        lines = []
        for a in db.scalars(select(Account).where(Account.acc_type.in_(("revenue","expense")),Account.active == True)):
            balance = F.account_balance(db,a.code,upto=d)
            if abs(balance) > .005:
                lines.append(dict(account=a.code,debit=max(-balance,0),credit=max(balance,0)))
        net = round(sum(x["credit"]-x["debit"] for x in lines),2)
        lines.append(dict(account=F.ACC_PROFIT,debit=max(net,0),credit=max(-net,0)))
        # 年末：本年利润转入 利润分配（4104，未分配利润）
        lines.append(dict(account=F.ACC_PROFIT,debit=max(-net,0),credit=max(net,0)))
        lines.append(dict(account="4104",debit=max(net,0),credit=max(-net,0)))
        lines = [l for l in lines if l["debit"] or l["credit"]]
        v = F.create_voucher(db,d,"closing",year,f"{year} 年度结转",lines)
        O.audit(db,"年度结转",year=year,voucher_id=v.id,reason=data.reason)
        db.commit()
        return {"id":v.id}

    @app.get("/api/periods")
    def periods(db=Depends(get_db)):
        return [dict(month=x.month, reason=x.reason) for x in db.scalars(select(PeriodLock).order_by(PeriodLock.month))]

    @app.post("/api/periods/{month}/lock")
    def lock(month: str, data: Reason, db=Depends(get_db)):
        date.fromisoformat(month+"-01")
        if db.get(PeriodLock, month):
            raise ValueError("期间已锁定")
        O.synchronize(db, "关账前对账")
        r = O.reconciliation(db)
        if abs(r["inventory_gap"]) > .01 or abs(r["trial_gap"]) > .01:
            raise ValueError("库存总账或借贷不平，不能锁定")
        db.add(PeriodLock(month=month, reason=data.reason))
        O.audit(db, "锁定期间", month=month, reason=data.reason)
        db.commit()
        return {"ok": True}

    @app.post("/api/periods/{month}/unlock")
    def unlock(month: str, data: Reason, db=Depends(get_db)):
        p = db.get(PeriodLock, month)
        if not p:
            raise ValueError("期间未锁定")
        db.delete(p)
        O.audit(db, "解除期间锁定", month=month, reason=data.reason)
        db.commit()
        return {"ok": True}

    @app.get("/api/audit-events")
    def audits(db=Depends(get_db)):
        return [dict(id=x.id, action=x.action, detail=x.detail, user_name=x.user_name or "", created_at=str(x.created_at)) for x in db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(200))]

    @app.get("/api/conversions")
    def conversions(db=Depends(get_db)):
        return [dict(id=x.id, kind=x.kind, move_date=str(x.move_date), status=x.status, detail=x.detail) for x in db.scalars(select(StockConversion).order_by(StockConversion.id.desc()))]

    @app.post("/api/conversions")
    def convert(data: Conversion, db=Depends(get_db)):
        if data.kind not in ("assemble", "disassemble"):
            raise ValueError("请选择组套或拆套")
        O.assert_open(db, data.move_date)
        ids = [data.parent_material_id]+[x.material_id for x in data.components]
        if len(ids) != len(set(ids)) or any(not db.get(Material, x) or not db.get(Material, x).active for x in ids):
            raise ValueError("物料不可重复且必须有效")
        bom = {x.child_material_id:x.qty_per for x in db.scalars(select(MaterialBom).where(MaterialBom.parent_material_id == data.parent_material_id, MaterialBom.active == True))}
        if not bom or set(bom) != {x.material_id for x in data.components} or any(abs(x.qty-bom[x.material_id]*data.qty)>1e-6 for x in data.components):
            raise ValueError("组成数量必须与当前BOM一致，请先维护BOM")
        state = replay(db, before=end_of_day(data.move_date))[0]
        source = [Component(material_id=data.parent_material_id, qty=data.qty)] if data.kind == "disassemble" else data.components
        targets = data.components if data.kind == "disassemble" else [Component(material_id=data.parent_material_id, qty=data.qty, weight=1)]
        total = 0
        for x in source:
            st = state.get(x.material_id, {})
            if st.get("qty", 0)+1e-9 < x.qty or st.get("avg_cost", 0) <= 0:
                raise ValueError("组拆套来源库存不足或无有效成本")
            total += x.qty*st["avg_cost"]
        weights = []
        for x in targets:
            w = x.weight if x.weight is not None else x.qty*state.get(x.material_id, {}).get("avg_cost", 0)
            if w <= 0:
                raise ValueError("子件没有参考成本，请填写本行成本权重")
            weights.append(w)
        conv = StockConversion(kind=data.kind, move_date=data.move_date, detail=dict(parent_material_id=data.parent_material_id, qty=data.qty, components=[x.model_dump() for x in data.components], weights=weights, total_cost=round(total, 2)))
        db.add(conv)
        db.flush()
        for n,x in enumerate(source):
            m = StockMovement(doc_no=f"CV-{conv.id}-O{n}", move_type="out", material_id=x.material_id, qty=x.qty, move_date=data.move_date, status="draft", remark="组拆套")
            db.add(m)
            db.flush()
            db.add(ConversionMovement(movement_id=m.id, conversion_id=conv.id))
            confirm_movement(db, m)
            db.flush()
        assigned = 0
        for n,x in enumerate(targets):
            share = round(total-assigned, 2) if n == len(targets)-1 else round(total*weights[n]/sum(weights), 2)
            assigned += share
            m = StockMovement(doc_no=f"CV-{conv.id}-I{n}", move_type="in", material_id=x.material_id, qty=x.qty, unit_cost=share/x.qty, move_date=data.move_date, status="confirmed", remark="组拆套")
            db.add(m)
            db.flush()
            db.add(ConversionMovement(movement_id=m.id, conversion_id=conv.id))
        O.synchronize(db, "组拆套")
        O.audit(db, "组拆套", conversion_id=conv.id, detail=conv.detail)
        db.commit()
        return {"id": conv.id}

    @app.post("/api/conversions/{id_}/void")
    def void_conversion(id_: int, data: Reason, db=Depends(get_db)):
        conv = db.get(StockConversion, id_)
        if not conv or conv.status != "confirmed":
            raise ValueError("组拆套单不存在或已作废")
        O.assert_open(db, conv.move_date)
        for x in db.scalars(select(ConversionMovement).where(ConversionMovement.conversion_id == id_)):
            db.get(StockMovement, x.movement_id).status = "voided"
        conv.status = "voided"
        O.synchronize(db, data.reason)
        O.audit(db, "作废组拆套", conversion_id=id_, reason=data.reason)
        db.commit()
        return {"ok": True}
