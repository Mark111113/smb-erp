"""Business controls and repeatable accounting reconciliation.

Business documents are authoritative. Corrections append reversal/replacement
vouchers; existing voucher lines are never edited or deleted.
"""
from collections import defaultdict
from datetime import date
from sqlalchemy import select, func
from .models import (Invoice, Payment, StockMovement, Voucher, VoucherLine,
                     DocumentState, AuditEvent, PeriodLock, Allocation,
                     Contract, ContractLine, ConversionMovement, StockConversion, DocumentLink, Material, WorkOrder)
from . import finance as F
from . import audit as _audit
from .inventory import replay

AUTO = {"goods_in", "goods_out", "stock_adjust", "stock_revalue", "stock_transfer",
        "invoice_input", "invoice_output", "payment_in", "payment_out", "allocation",
        "vat_deduct", "vat_transfer", "wo_issue", "wo_receipt"}


def audit(db, action, **detail):
    user_id, user_name = _audit.current_actor()
    db.add(AuditEvent(action=action, detail=detail, user_id=user_id, user_name=user_name))


def active(db, kind, document_id):
    return not db.scalar(select(DocumentState.id).where(
        DocumentState.kind == kind, DocumentState.document_id == document_id,
        DocumentState.status == "voided"))


def active_documents(db, model):
    kind = "invoice" if model is Invoice else "payment"
    excluded = select(DocumentState.document_id).where(DocumentState.kind == kind,
                                                       DocumentState.status == "voided")
    return select(model).where(model.id.not_in(excluded))


def assert_open(db, d):
    if db.get(PeriodLock, str(d)[:7]):
        raise ValueError("该期间已锁定，不能更改业务或凭证")


def pair(debit, credit, amount, pid=None, partner_accounts=()):
    amount = round(amount, 2)
    if amount < 0:
        debit, credit, amount = credit, debit, -amount
    return [dict(account=debit, debit=amount, partner_id=pid if debit in partner_accounts else None),
            dict(account=credit, credit=amount, partner_id=pid if credit in partner_accounts else None)]


def signature(lines):
    return sorted((l["account"], l.get("partner_id") or 0, l.get("cost_center_id") or 0,
                   round(l.get("debit") or 0, 2), round(l.get("credit") or 0, 2))
                  for l in lines if round(l.get("debit") or 0, 2) or round(l.get("credit") or 0, 2))


def synchronize(db, reason="业务过账"):
    """Reconcile expected postings atomically, including historical cost changes."""
    db.flush()
    expected = {}

    def put(kind, id_, d, summary, lines):
        lines = [l for l in lines if round(l.get("debit") or 0, 2) or round(l.get("credit") or 0, 2)]
        if lines:
            expected[(kind, id_)] = (d, summary, lines)

    conversions = {x.movement_id for x in db.scalars(select(ConversionMovement))}
    inv_of = {x.id: F.inventory_account(x) for x in db.scalars(select(Material))}   # 存货科目按物料类型（老物料 1405）
    wos = {w.id: w for w in db.scalars(select(WorkOrder))}
    _, entries = replay(db)
    for m, cost, delta in entries:
        if m.move_type == "wo_cost":
            continue    # 月末成本结转单独出凭证（app/mfg.py）
        INV = inv_of.get(m.material_id, F.ACC_INVENTORY)
        pid = m.partner_id or F._partner_of_contract(db, m.contract_id)
        if (m.move_type not in ("in", "opening") or m.id in conversions) and abs((m.unit_cost or 0) - cost) > 1e-8:
            assert_open(db, m.move_date)
            audit(db, "成本快照重估", movement_id=m.id, before=m.unit_cost, after=cost, reason=reason)
            m.unit_cost = cost
        if m.id in conversions:
            transfer = round(m.qty*cost, 2) if m.move_type == "in" else delta
            put("stock_transfer", m.id, m.move_date, f"组拆套 {m.doc_no}", pair(INV, "1234", transfer))
            put("stock_revalue", m.id, m.move_date, f"组拆套负库存调整 {m.doc_no}", pair(INV, F.ACC_COGS, delta-transfer))
        elif m.move_type in ("wo_issue", "wo_return", "wo_receipt"):
            w = wos.get(m.work_order_id)
            if not w:
                raise ValueError(f"出入库单 {m.doc_no} 挂的工单不存在")
            wip, dims = {"production": (F.ACC_WIP_MAT, dict(cost_center_id=w.cost_center_id)),
                         "rd": (F.ACC_RD, dict(cost_center_id=w.cost_center_id)),
                         "subcontract": (F.ACC_SUBCON, dict(partner_id=w.partner_id))}[w.kind]
            tag = {"wo_issue": "工单领料", "wo_return": "工单退料", "wo_receipt": "完工入库"}[m.move_type]
            if m.move_type == "wo_receipt":
                amount = round(m.qty * cost, 2)
                fee = round((w.fee_price or 0) * m.qty, 2) if w.kind == "subcontract" else 0.0
                lines = [dict(account=INV, debit=amount), dict(account=wip, credit=round(amount - fee, 2), **dims)]
                if fee:
                    lines.append(dict(account=F.ACC_GRIR, credit=fee, partner_id=w.partner_id))
                put("wo_receipt", m.id, m.move_date, f"{tag} {w.wo_no} {m.doc_no}", lines)
                put("stock_revalue", m.id, m.move_date, f"负库存及尾差调整 {m.doc_no}", pair(INV, F.ACC_COGS, delta - amount))
            else:
                amount = round(-delta if m.move_type == "wo_issue" else delta, 2)
                dr, cr = (dict(account=wip, **dims), dict(account=INV)) if m.move_type == "wo_issue" else (dict(account=INV), dict(account=wip, **dims))
                if amount < 0:
                    dr, cr, amount = cr, dr, -amount
                put("wo_issue", m.id, m.move_date, f"{tag} {w.wo_no} {m.doc_no}", [dict(dr, debit=amount), dict(cr, credit=amount)])
        elif m.move_type in ("in", "opening"):
            amount = round(m.qty * (m.unit_cost or 0), 2)
            contra = "4199" if m.move_type == "opening" else F.ACC_GRIR
            put("goods_in", m.id, m.move_date, f"收货 {m.doc_no}", pair(INV, contra, amount, pid, (F.ACC_GRIR,)))
            put("stock_revalue", m.id, m.move_date, f"负库存及尾差调整 {m.doc_no}", pair(INV, F.ACC_COGS, delta - amount))
        elif m.move_type == "out":
            put("goods_out", m.id, m.move_date, f"出库 {m.doc_no}", pair(F.ACC_COGS, INV, -delta))
        elif m.move_type == "return_in":
            put("goods_out", m.id, m.move_date, f"销售退货 {m.doc_no}", pair(INV, F.ACC_COGS, delta))
        elif m.move_type == "return_out":
            link = db.scalar(select(DocumentLink).where(DocumentLink.kind == "stock_return", DocumentLink.child_id == m.id))
            original = db.get(StockMovement, link.original_id) if link else None
            amount = round(m.qty*(original.unit_cost or 0),2) if original else -delta
            put("goods_in", m.id, m.move_date, f"采购退货 {m.doc_no}", pair(F.ACC_GRIR, INV, amount, pid, (F.ACC_GRIR,)))
            put("stock_revalue", m.id, m.move_date, f"采购退货成本差 {m.doc_no}", pair(INV,F.ACC_COGS,delta+amount))
        else:
            put("stock_adjust", m.id, m.move_date, f"库存调整 {m.doc_no}", pair(INV, "671101", delta))

    for i in db.scalars(active_documents(db, Invoice)):
        pid = i.partner_id or F._partner_of_contract(db, i.contract_id)
        net, tax, total = round(i.amount_ex_tax, 2), round(i.tax_amount, 2), round(i.amount_tax, 2)
        if abs(net + tax - total) > .005:
            raise ValueError(f"发票 {i.invoice_no} 价税不平，请先规范化")
        if i.direction == "input":
            lines = [dict(account=F.ACC_GRIR, debit=net, partner_id=pid),
                     dict(account=F.ACC_VAT_PENDING, debit=tax), dict(account=F.ACC_AP, credit=total, partner_id=pid)]
        else:
            lines = [dict(account=F.ACC_AR, debit=total, partner_id=pid),
                     dict(account=F.ACC_REVENUE, credit=net), dict(account=F.ACC_VAT_OUT, credit=tax)]
        for l in lines:
            if l.get("debit", 0) < 0:
                l["credit"], l["debit"] = -l["debit"], 0
            if l.get("credit", 0) < 0:
                l["debit"], l["credit"] = -l["credit"], 0
        put("invoice_" + i.direction, i.id, i.invoice_date or date.today(), f"发票 {i.invoice_no}", lines)

    for p in db.scalars(active_documents(db, Payment)):
        pid = p.partner_id or F._partner_of_contract(db, p.contract_id)
        lines = pair(F.ACC_PREPAY, F.ACC_BANK, p.amount, pid, (F.ACC_PREPAY,)) if p.direction == "pay" else pair(F.ACC_BANK, F.ACC_ADVANCE, p.amount, pid, (F.ACC_ADVANCE,))
        put("payment_out" if p.direction == "pay" else "payment_in", p.id, p.pay_date, f"收付款 {p.id} {p.remark}", lines)

    for a in db.scalars(select(Allocation).where(Allocation.status == "active")):
        p, i = db.get(Payment, a.payment_id), db.get(Invoice, a.invoice_id)
        if not active(db, "invoice", i.id) or not active(db, "payment", p.id):
            raise ValueError("作废单据仍有有效核销")
        debit, credit = (F.ACC_AP, F.ACC_PREPAY) if p.direction == "pay" else (F.ACC_ADVANCE, F.ACC_AR)
        put("allocation", a.id, a.allocation_date, f"核销 款{p.id}/票{i.invoice_no}", pair(debit, credit, a.amount, p.partner_id, (debit, credit)))

    # 进项勾选/转出（v0.26）：按发票档案的所属期，222104 待认证 → 222102 进项；转出 → 222105
    from .invoice_docs import deduct_postings
    for kind, id_, d, summary, lines in deduct_postings(db):
        put(kind, id_, d, summary, lines)

    current = defaultdict(list)
    for v in db.scalars(select(Voucher).where(Voucher.source_type.in_(AUTO), Voucher.status == "posted")):
        current[(v.source_type, v.source_id)].append(v)
    reversed_ids, created_ids = [], []
    for key in sorted(set(current) | set(expected), key=lambda k: (str(k[0]), k[1] or 0)):
        vs, want = current.get(key, []), expected.get(key)
        if len(vs) == 1 and want and vs[0].voucher_date == want[0] and signature([
            dict(account=l.account_code, partner_id=l.partner_id, cost_center_id=l.cost_center_id, debit=l.debit, credit=l.credit) for l in vs[0].lines]) == signature(want[2]):
            continue
        for v in vs:
            F.reverse_voucher(db, v, reason)
            reversed_ids.append(v.id)
        if want:
            v = F.create_voucher(db, want[0], key[0], key[1], want[1], want[2])
            created_ids.append(v.id)
    if reversed_ids or created_ids:
        audit(db, "凭证同步", reason=reason, reversed_ids=reversed_ids, created_ids=created_ids)
    db.flush()
    return dict(reversed_ids=reversed_ids, created_ids=created_ids)


def void_document(db, kind, id_, reason):
    model = Invoice if kind == "invoice" else Payment
    obj = db.get(model, id_)
    if not obj or not active(db, kind, id_):
        raise ValueError("单据不存在或已作废")
    assert_open(db, obj.invoice_date if kind == "invoice" else obj.pay_date)
    link_kind = "credit" if kind == "invoice" else "refund"
    if db.scalar(select(DocumentLink.id).where(DocumentLink.kind == link_kind, DocumentLink.original_id == id_)):
        raise ValueError("原单已有红字或退款记录，请先处理关联单据，不能直接作废原单")
    for a in db.scalars(select(Allocation).where(
            (Allocation.invoice_id == id_) if kind == "invoice" else (Allocation.payment_id == id_), Allocation.status == "active")):
        assert_open(db, a.allocation_date)
        a.status = "voided"
    db.add(DocumentState(kind=kind, document_id=id_, status="voided", reason=reason))
    audit(db, "单据作废", kind=kind, id=id_, reason=reason)
    return synchronize(db, reason)


def allocate(db, payment_id, invoice_id, amount, d):
    p, i = db.get(Payment, payment_id), db.get(Invoice, invoice_id)
    if not p or not i or not active(db, "payment", payment_id) or not active(db, "invoice", invoice_id):
        raise ValueError("核销单据无效")
    if p.partner_id is None or p.partner_id != i.partner_id or ((p.direction == "pay") != (i.direction == "input")):
        raise ValueError("核销必须同单位、同方向")
    if d < max(p.pay_date, i.invoice_date or p.pay_date):
        raise ValueError("核销日期不能早于票款日期")
    assert_open(db, d)
    def used(col, val):
        return db.scalar(select(func.coalesce(func.sum(Allocation.amount), 0)).where(col == val, Allocation.status == "active")) or 0
    def reductions(kind, id_, doc_kind):
        return sum(x.amount for x in db.scalars(select(DocumentLink).where(DocumentLink.kind == kind, DocumentLink.original_id == id_)) if active(db, doc_kind, x.child_id))
    if amount <= 0 or round(amount + used(Allocation.payment_id, p.id) + reductions("refund", p.id, "payment") - p.amount, 2) > 0 or round(amount + used(Allocation.invoice_id, i.id) + reductions("credit", i.id, "invoice") - i.amount_tax, 2) > 0:
        raise ValueError("核销金额超过票款未核销余额")
    a = Allocation(payment_id=p.id, invoice_id=i.id, amount=round(amount, 2), allocation_date=d)
    db.add(a)
    db.flush()
    audit(db, "核销", allocation_id=a.id, amount=a.amount)
    return a


def auto_allocate(db) -> list[dict]:
    """按合同自动核销（v0.28）：只核同合同 + 同单位 + 同方向的票款，按日期先后配对，核销日取票款较晚一天。
    跨合同、没挂合同的款仍要人工在核销页确认（用户 2026-09-28 问「什么时候自动核销」时补的按钮）。"""
    def used(col, val):
        return db.scalar(select(func.coalesce(func.sum(Allocation.amount), 0)).where(col == val, Allocation.status == "active")) or 0

    def reductions(kind, id_, doc_kind):
        return sum(x.amount for x in db.scalars(select(DocumentLink).where(DocumentLink.kind == kind, DocumentLink.original_id == id_))
                   if active(db, doc_kind, x.child_id))

    done = []
    for p in db.scalars(active_documents(db, Payment).order_by(Payment.pay_date, Payment.id)):
        if not p.contract_id or not p.partner_id or p.amount <= 0:
            continue
        left = round(p.amount - used(Allocation.payment_id, p.id) - reductions("refund", p.id, "payment"), 2)
        direction = "input" if p.direction == "pay" else "output"
        for i in db.scalars(active_documents(db, Invoice).where(Invoice.contract_id == p.contract_id, Invoice.partner_id == p.partner_id,
                                                                Invoice.direction == direction).order_by(Invoice.invoice_date, Invoice.id)):
            if left <= 0.005:
                break
            rest = round(i.amount_tax - used(Allocation.invoice_id, i.id) - reductions("credit", i.id, "invoice"), 2)
            amount = round(min(left, rest), 2)
            if amount <= 0.005:
                continue
            a = allocate(db, p.id, i.id, amount, max(p.pay_date, i.invoice_date or p.pay_date))
            left = round(left - amount, 2)
            done.append({"allocation_id": a.id, "payment_id": p.id, "invoice_id": i.id, "invoice_no": i.invoice_no,
                         "contract_no": i.contract.contract_no if i.contract else "", "amount": amount,
                         "date": str(a.allocation_date)})
    return done


def fulfillment(db, c):
    moves = list(db.scalars(select(StockMovement).where(StockMovement.contract_id == c.id, StockMovement.status == "confirmed")))
    forward, reverse = ("in", "return_out") if c.contract_type == "purchase" else ("out", "return_in")
    result = []
    for l in c.lines:
        qty = sum(m.qty * (1 if m.move_type == forward else -1) for m in moves if m.contract_line_id == l.id and m.move_type in (forward, reverse))
        result.append(dict(line_id=l.id, material_id=l.material_id, material_name=l.material.name,
                           ordered=l.qty, delivered=qty, remaining=round(l.qty-qty, 6)))
    unlinked = [m.id for m in moves if not m.contract_line_id and m.move_type in (forward, reverse)]
    return dict(lines=result, unlinked_movement_ids=unlinked,
                complete=bool(result) and not unlinked and all(abs(r["remaining"]) < 1e-6 for r in result))


def matching(db, ctype):
    rows = []
    for c in db.scalars(select(Contract).where(Contract.contract_type == ctype, Contract.status == "active")):
        ff = fulfillment(db, c)
        invoices = list(db.scalars(active_documents(db, Invoice).where(Invoice.contract_id == c.id)))
        paid = round(sum(p.amount for p in db.scalars(active_documents(db, Payment).where(Payment.contract_id == c.id))), 2)
        net, total = round(sum(i.amount_ex_tax for i in invoices), 2), round(sum(i.amount_tax for i in invoices), 2)
        moves = list(db.scalars(select(StockMovement).where(StockMovement.contract_id == c.id, StockMovement.status == "confirmed")))
        if ctype == "purchase":
            gr = round(sum(m.qty*(m.unit_cost or 0)*(1 if m.move_type == "in" else -1) for m in moves if m.move_type in ("in", "return_out")), 2)
        else:
            prices = {l.id: l.price_tax for l in c.lines}
            gr = round(sum(m.qty*prices.get(m.contract_line_id, 0)*(1 if m.move_type == "out" else -1) for m in moves if m.move_type in ("out", "return_in")), 2)
        gap = round(gr - (net if ctype == "purchase" else total), 2)
        unpaid = round(total-paid, 2)
        order_gap = round(c.amount_tax-total, 2)
        st = "closed" if ff["complete"] and abs(gap) <= .01 and abs(unpaid) <= .01 and abs(order_gap) <= .01 else "gr_gap" if gap > .01 else "ir_gap" if gap < -.01 else "pay_gap" if abs(unpaid) > .01 else "todo"
        rows.append(dict(contract_id=c.id, contract_no=c.contract_no, partner_name=c.partner.name,
                         partner_short=c.partner.short_name or c.partner.name, order=c.amount_tax,
                         gr_net=gr, gr_tax_ref=gr, inv_net=net, inv_total=total, inv_count=len(invoices),
                         gr_ir_gap=gap, paid=paid, unpaid=unpaid, order_gap=order_gap, status=st, fulfillment=ff))
    return rows


def reconciliation(db):
    state, _ = replay(db)
    stock = round(sum(round(s["value"], 2) for s in state.values()), 2)
    gl = round(sum(F.account_balance(db, a) for a in F.INV_ACCOUNTS), 2)   # 1403 + 1405 + 1411
    tb = F.trial_balance(db)
    partners = {x.partner_id for x in db.scalars(active_documents(db, Invoice))} | {x.partner_id for x in db.scalars(active_documents(db, Payment))}
    subledger = []
    for pid in partners:
        for direction, cash_direction, account, advance in (("input", "pay", F.ACC_AP, F.ACC_PREPAY), ("output", "receive", F.ACC_AR, F.ACC_ADVANCE)):
            invs = list(db.scalars(active_documents(db, Invoice).where(Invoice.partner_id == pid, Invoice.direction == direction)))
            pays = list(db.scalars(active_documents(db, Payment).where(Payment.partner_id == pid, Payment.direction == cash_direction)))
            ids = [x.id for x in invs]
            settled = db.scalar(select(func.coalesce(func.sum(Allocation.amount), 0)).where(Allocation.invoice_id.in_(ids), Allocation.status == "active")) or 0
            for acc, expected, sign in ((account, sum(x.amount_tax for x in invs)-settled, -1 if direction == "input" else 1),
                                        (advance, sum(x.amount for x in pays)-settled, 1 if direction == "input" else -1)):
                # Explicit NULL dimension, rather than account_balance(None)'s all-partner meaning.
                actual = db.scalar(select(func.coalesce(func.sum(VoucherLine.debit-VoucherLine.credit),0)).join(Voucher).where(VoucherLine.account_code == acc, VoucherLine.partner_id == pid, Voucher.status.in_(("posted","reversed")))) or 0
                subledger.append(dict(partner_id=pid, account=acc, expected=round(expected,2), actual=round(actual*sign,2), gap=round(actual*sign-expected,2)))
    return dict(stock_value=stock, inventory_gl=gl, inventory_gap=round(gl-stock, 2), ar_ap=subledger,
                trial_gap=round(sum(r["debit"]-r["credit"] for r in tb), 2),
                negative_materials=[k for k,s in state.items() if s["neg"]],
                purchase=matching(db, "purchase"), sales=matching(db, "sales"))
