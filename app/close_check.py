"""月结检查（v0.30）：某月结账前还差什么——只读，一页列清。

与 docs/MONTH_END_CLOSE.md（月结操作手册）一一对应：人看这页知道还剩多少事，agent 月结时先读它。
每项：key / 步骤号 / 名称 / 状态（ok 完成、todo 待处理、warn 需关注、info 提示）/ 数量 / 说明 / 跳转页面。
"""
from datetime import date, timedelta

from fastapi import Depends, HTTPException
from sqlalchemy import func, select

from . import finance as F
from . import operations as O
from .assets import month_end, next_month
from .inventory import compute_material_stock
from .models import (Account, Allocation, Asset, BankTxn, DepreciationLine, Invoice, InvoiceDoc, Payment, PeriodLock,
                     StockMovement, TodoItem, Voucher, VoucherLine)


def _item(key, step, label, status, count=None, detail="", link="", refs=None):
    """refs：结构化线索（给 agent/人逐条处理用），每条至少有 type、id，可带 suggestions。"""
    return {"key": key, "step": step, "label": label, "status": status, "count": count, "detail": detail, "link": link,
            "refs": refs or []}


FIXED_RULES = [  # (判断, 建议科目, 说明) —— 与 docs/MONTH_END_CLOSE.md「已定口径」一致
    (lambda t: "相城区支库" in t.counterparty_name, "221102", "社保（税务代征），冲应付社保"),
    (lambda t: "公积金" in t.counterparty_name, "221103", "住房公积金托收，冲应付公积金"),
    (lambda t: t.biz_type in ("代收费", "收费"), "660301", "银行手续费"),
    (lambda t: t.biz_type == "结息", "660302", "利息收入（贷方）"),
    (lambda t: "金库" in t.counterparty_name, "", "税款/罚没：按回单税种（所得税 6801、增值税 222103、城建/印花 6403xx、罚款 671102）"),
]


def unpaid_wage_months(db) -> list[tuple[str, float]]:
    """按计提凭证（221101 净贷方）先进先出扣掉已发放，剩下的月份和实发额。"""
    acc, paid = [], 0.0
    rows = db.execute(select(Voucher.voucher_date, func.sum(VoucherLine.credit - VoucherLine.debit))
                      .join(VoucherLine, VoucherLine.voucher_id == Voucher.id)
                      .where(VoucherLine.account_code == "221101", Voucher.status.in_(("posted", "reversed")))
                      .group_by(Voucher.id).order_by(Voucher.voucher_date, Voucher.id)).all()
    for d, net in rows:
        if net > 0.005:
            acc.append([d.strftime("%Y-%m"), round(net, 2)])
        else:
            paid += -net
    for a in acc:
        take = min(a[1], paid)
        a[1] = round(a[1] - take, 2)
        paid -= take
    return [(m, v) for m, v in acc if v > 0.005]


def _wage_hint(db, amt: float) -> str:
    months = unpaid_wage_months(db)
    total = round(sum(v for _, v in months), 2)
    text = f"这是发工资：冲 221101 应付工资（当前未发 {total:,.2f}：" + "、".join(f"{m} {v:,.2f}" for m, v in months) + "）"
    run = 0.0
    for i, (m, v) in enumerate(months):
        run = round(run + v, 2)
        if abs(run - amt) < 0.005:
            return text + f"；本笔 {amt:,.2f} = {months[0][0]}～{m} 共 {i + 1} 个月实发，摘要写清月份"
    return text + f"；本笔 {amt:,.2f} 与未发月份对不上，先对照结账包里的工资表，仍对不上列待确认"


def related_todos(db, since) -> list[dict]:
    """自 since 起动过（操作日志）的收付款/发票/出入库/流水/合同所涉及的合同和往来单位 → 提到它们的未完成待办。
    账上处理完，相关待办常常就过时了（实际遇到过：预付抵扣入账后，对应的待办没跟着改）。"""
    from datetime import datetime
    from . import bank as B
    from .models import ChangeLog, Contract, Partner
    models = {"payment": Payment, "invoice": Invoice, "bank_txn": BankTxn}   # 只看钱和票的变动，别把清单撑长
    cids, pids = set(), set()
    keys = db.execute(select(ChangeLog.table_name, ChangeLog.row_key).distinct()
                      .where(ChangeLog.at >= datetime.combine(since, datetime.min.time()),
                             ChangeLog.table_name.in_(list(models)))).all()
    for table, key in keys:
        row = db.get(models[table], int(key)) if str(key).isdigit() else None
        if row is None:
            continue
        if table == "bank_txn":
            p = B.partner_for(db, row)
            if p:
                pids.add(p.id)
        else:
            if row.contract_id:
                cids.add(row.contract_id)
            if row.partner_id:
                pids.add(row.partner_id)
    for c in db.scalars(select(Contract).where(Contract.id.in_(cids))) if cids else []:
        pids.add(c.partner_id)
    nos = {c.contract_no for c in db.scalars(select(Contract).where(Contract.id.in_(cids)))} if cids else set()
    tokens = set(nos)
    for p in db.scalars(select(Partner).where(Partner.id.in_(pids))) if pids else []:
        tokens |= {x for x in (p.short_name, B.norm_name(p.name)) if x and len(x) >= 2}
    out = []
    for t in db.scalars(select(TodoItem).where(TodoItem.status == "open").order_by(TodoItem.id)):
        text = f"{t.title} {t.detail}"
        hit = sorted(x for x in tokens if x in text) or (["挂在本月动过的合同上"] if t.contract_id in cids else [])
        if hit:
            strong = any(x in nos for x in hit) or t.contract_id in cids
            out.append({"type": "todo", "id": t.id, "title": t.title, "level": t.level, "matched": hit, "by_contract": strong})
    return sorted(out, key=lambda r: not r["by_contract"])     # 提到具体合同号的排前面


def bank_suggestions(db, t) -> list[dict]:
    """未处理流水的处理线索：固定口径 → 同户名历史入账 → 往来单位的未付合同（含预付余额抵扣）。只给建议，不入账。"""
    from . import bank as B
    from .models import Contract
    out = []
    for pred, acc, text in FIXED_RULES:
        if pred(t):
            out.append({"type": "rule", "account": acc, "text": text})
    # 同户名历史上怎么入的（人名尤其有用：转给员工 = 发工资冲 221101）
    prev = db.scalars(select(BankTxn).where(BankTxn.counterparty_name == t.counterparty_name, BankTxn.status == "posted",
                                            BankTxn.id != t.id).order_by(BankTxn.txn_date.desc()).limit(3)).all()
    for p in prev:
        v = db.get(Voucher, p.voucher_id) if p.voucher_id else None
        accs = sorted({l.account_code for l in v.lines if l.account_code != "100201"}) if v else []
        if accs and not any(a.startswith("4001") for a in accs):  # 股东投资不是可类推的口径
            text = f"同户名 {p.txn_date} 的 {abs(p.amount):,.2f} 入账到 {'、'.join(accs)}（{p.category or v.summary[:30]}）"
            if "221101" in accs and t.amount < 0:
                text += "；" + _wage_hint(db, abs(t.amount))
            out.append({"type": "history", "account": accs[0] if len(accs) == 1 else "", "text": text, "bank_txn_id": p.id})
            break
    # 往来单位：未付合同额、预付余额抵扣
    partner = B.partner_for(db, t)
    if partner:
        ctype = "purchase" if t.amount < 0 else "sales"
        direction = "pay" if t.amount < 0 else "receive"
        amt = abs(t.amount)
        open_c, over = [], []
        for c in db.scalars(select(Contract).where(Contract.partner_id == partner.id, Contract.contract_type == ctype, Contract.status == "active")):
            paid = sum(p.amount for p in db.scalars(select(Payment).where(Payment.contract_id == c.id, Payment.direction == direction))
                       if O.active(db, "payment", p.id))
            rest = round(c.amount_tax - paid, 2)
            if rest > 0.005:
                open_c.append((c, rest))
            elif rest < -0.005:
                over.append((c, -rest))
        for c, rest in open_c:
            if abs(rest - amt) < 0.005:
                out.append({"type": "contract", "contract_no": c.contract_no, "text": f"等于 {c.contract_no} 未付/未收 {rest:,.2f}：先登记收付款挂该合同再关联"})
            for oc, extra in over:
                if abs(rest - extra - amt) < 0.005:
                    out.append({"type": "contract_with_credit", "contract_no": c.contract_no, "credit_contract_no": oc.contract_no,
                                "text": f"{c.contract_no} 未付 {rest:,.2f} − {oc.contract_no} 多付 {extra:,.2f} = {amt:,.2f}：用收付款「拆分」把 {oc.contract_no} 多付的 {extra:,.2f} 挂到 {c.contract_no}，再登记本笔 {amt:,.2f} 并关联流水"})
        if not any(x["type"].startswith("contract") for x in out):
            out.append({"type": "partner", "text": f"往来单位 {partner.short_name or partner.name}：未结合同 " + "、".join(f"{c.contract_no} 余 {r:,.2f}" for c, r in open_c[:5])
                        + ("；多付 " + "、".join(f"{c.contract_no} {e:,.2f}" for c, e in over) if over else "") + "——对不上的列待确认"})
    if not out:
        out.append({"type": "unknown", "text": "性质不明：列入待确认，问用户（别猜）"})
    return out


def _sum_account(db, prefix, start, end, side="debit"):
    col = VoucherLine.debit if side == "debit" else VoucherLine.credit
    return round(db.scalar(select(func.coalesce(func.sum(col), 0)).join(Voucher, VoucherLine.voucher_id == Voucher.id)
                           .where(VoucherLine.account_code.startswith(prefix), Voucher.status.in_(("posted", "reversed")),
                                  Voucher.voucher_date >= start, Voucher.voucher_date <= end)) or 0, 2)


def check(db, month: str) -> dict:
    from . import bank as B
    from .invoice_docs import vat_ledger
    start = date.fromisoformat(month + "-01")
    end = month_end(month)
    items = []

    # 1 资料：银行流水导到月底了吗
    last = db.scalar(select(func.max(BankTxn.txn_date)))
    items.append(_item("bank_coverage", 1, "银行流水已导入到月底", "ok" if last and last >= end - timedelta(days=3) else "todo",
                       None, f"最新一笔流水 {last or '无'}；月底 {end}（中行导出 HISQRY CSV 后在银行流水页上传）", "bank"))
    # 2 流水处理
    un_rows = list(db.scalars(select(BankTxn).where(BankTxn.status == "unmatched", BankTxn.txn_date >= start, BankTxn.txn_date <= end)))
    un = len(un_rows)
    items.append(_item("bank_unmatched", 2, "当月流水已全部处理（匹配收付款 / 入账 / 忽略）", "ok" if not un else "todo", un,
                       "未处理的流水：贸易款先登记收付款再关联；非贸易款「入账」；每笔的线索见 refs", "bank",
                       [{"type": "bank_txn", "id": t.id, "date": str(t.txn_date), "amount": t.amount, "counterparty": t.counterparty_name,
                         "memo": " ".join(x for x in (t.purpose, t.memo) if x), "suggestions": bank_suggestions(db, t)} for t in un_rows]))
    rec = [r for r in B.reconcile(db) if r["month"] == month]
    gap = [r for r in rec if abs(r["gap"]) > 0.005]
    items.append(_item("bank_reconcile", 2, "月末银行存款 = 流水余额", "ok" if rec and not gap else ("warn" if gap else "info"),
                       len(gap) or None, "；".join(f"{r['account_no']} 差 {r['gap']:,.2f}" for r in gap) or ("当月无流水" if not rec else "一致"), "bank"))
    # 3 发票
    unbooked = [d for d in db.scalars(select(InvoiceDoc).where(InvoiceDoc.issue_date >= start, InvoiceDoc.issue_date <= end))
                if not (d.invoice_id or d.voucher_id or d.category == "none")]
    doc_ref = lambda d: {"type": "invoice_doc", "id": d.id, "invoice_no": d.invoice_no, "direction": d.direction,
                         "counterparty": d.seller_name if d.direction == "in" else d.buyer_name, "amount_tax": d.amount_tax,
                         "issue_date": str(d.issue_date or ""), "note": d.note}
    items.append(_item("invoice_unbooked", 3, "当月取得/开具的发票已入账", "ok" if not unbooked else "todo", len(unbooked),
                       "、".join(f"{d.seller_name if d.direction == 'in' else d.buyer_name} {d.amount_tax:,.2f}" for d in unbooked[:6]), "invdocs",
                       [doc_ref(d) for d in unbooked]))
    # 红冲对（红字票 + 被红冲的蓝字）互相抵消、不入账，不需要原件
    nofile_docs = [d for d in db.scalars(select(InvoiceDoc).where(~InvoiceDoc.files.any()))
                   if d.status == "normal" and d.category != "none"]
    items.append(_item("invoice_nofile", 3, "发票原件已归档", "ok" if not nofile_docs else "warn", len(nofile_docs),
                       "档案里有、原件没上传的票（税务导出建的档）；新原件从月结目录 02-发票 上传，不要去 ERP 数据目录找",
                       "invdocs", [doc_ref(d) for d in nofile_docs]))
    pend = list(db.scalars(select(InvoiceDoc).where(InvoiceDoc.direction == "in", InvoiceDoc.deduct_status == "pending")))
    deducted = db.scalar(select(func.count(InvoiceDoc.id)).where(InvoiceDoc.deduct_period == month)) or 0
    items.append(_item("vat_deduct", 3, "当月进项勾选已导入（用途确认/勾选清单）", "ok" if deducted else "todo", deducted,
                       f"本月已标勾选 {deducted} 张；未勾选专票 {len(pend)} 张，税额 {sum(d.tax_amount for d in pend):,.2f}（税务系统勾选后导出上传）", "invdocs"))
    led = {m["month"]: m for m in vat_ledger(db)}.get(month)
    if led:
        items.append(_item("vat_payable", 3, "增值税台账：本月应纳 / 留抵", "warn" if led["payable"] > 0.005 else "ok", None,
                           f"销项 {led['output']:,.2f}，进项 {led['input']:,.2f}，转出 {led['transfer_out']:,.2f}，上期留抵 {led['carry_in']:,.2f} → 应纳 {led['payable']:,.2f}，期末留抵 {led['carry_out']:,.2f}", "invdocs"))
    # 4 核销
    sp = db.begin_nested()
    try:
        can = O.auto_allocate(db)
    finally:
        sp.rollback()
    items.append(_item("allocation", 4, "同合同票款已核销", "ok" if not can else "todo", len(can),
                       "；".join(f"{x['contract_no']} {x['amount']:,.2f}" for x in can[:5]) + ("（业务核销与对账 →「按合同自动核销」）" if can else ""), "controls"))
    draft_rows = list(db.scalars(select(StockMovement).where(StockMovement.status == "draft", StockMovement.move_date <= end)))
    drafts = len(draft_rows)
    items.append(_item("drafts", 4, "出入库草稿已确认或作废", "ok" if not drafts else "warn", drafts,
                       "草稿不影响库存和凭证；实际收发货日期要问用户（台账迁移暂估的日期多半不准），不要自己定", "movements",
                       [{"type": "movement", "id": m.id, "doc_no": m.doc_no, "move_type": m.move_type, "move_date": str(m.move_date),
                         "material": m.material.code if m.material else "", "qty": m.qty,
                         "contract_no": m.contract.contract_no if m.contract else "", "remark": m.remark} for m in draft_rows]))
    neg = sum(1 for s in compute_material_stock(db).values() if s.get("neg"))
    items.append(_item("negative_stock", 4, "无负库存", "ok" if not neg else "warn", neg, "负库存说明有出库没对应入库（待补采购或录错）", "stock"))
    # 5 工资社保公积金
    wage = _sum_account(db, "660201", start, end)
    items.append(_item("payroll", 5, "工资社保公积金已计提", "ok" if wage > 0 else "todo", None,
                       f"660201 本月借方 {wage:,.2f}（每月末按工资表出计提凭证）", "vouchers"))
    owed = -F.account_balance(db, "221101", None, end)
    items.append(_item("payroll_owed", 5, "应付工资余额", "info", None, f"截至月末应付未付 {owed:,.2f}", "ledger"))
    # 6 折旧、转固
    assets = db.scalar(select(func.count(Asset.id)).where(Asset.status == "active", Asset.start_month <= month)) or 0
    done = db.scalar(select(func.count(DepreciationLine.id)).where(DepreciationLine.period == month)) or 0
    items.append(_item("depreciation", 6, "本月折旧已计提", "ok" if not assets or done >= assets else "todo", f"{done}/{assets}",
                       "固定资产页「计提折旧至该月」", "assets"))
    prepay = F.account_balance(db, F.ACC_PREPAY, None, end)
    items.append(_item("prepay", 6, "预付账款（工程/设备）是否有完工要转固", "info", None,
                       f"预付账款余额 {prepay:,.2f}；有验收单的工程在固定资产页「登记资产」转固", "assets"))
    # 6b 生产成本（v0.35）：工单、人工与制造费用分摊、研发支出转费用
    from .models import MfgClose, WorkOrder
    open_wo = [w for w in db.scalars(select(WorkOrder).where(WorkOrder.status == "released"))
               if db.scalar(select(StockMovement.id).where(StockMovement.work_order_id == w.id, StockMovement.status == "confirmed",
                                                          StockMovement.move_date <= end).limit(1))]
    if open_wo:
        items.append(_item("work_orders", 6, "工单状态已核对（做完的关闭，在制留着的确认属实）", "info", len(open_wo),
                           "未关闭的工单月末在制材料留在 500101 / 1408；做完的要关单，在制余额才会转进完工成本", "workorders",
                           [{"type": "work_order", "id": w.id, "wo_no": w.wo_no, "kind": w.kind} for w in open_wo]))
    pools = sum(F.account_balance(db, a.code, None, end) for a in db.scalars(select(Account).where(
        Account.code.like("5101%") | Account.code.like("500102%") | Account.code.like("500103%") | (Account.code == "5301"))))
    done_mfg = db.scalar(select(MfgClose.id).where(MfgClose.month == month))
    if done_mfg or abs(pools) > 0.005:
        items.append(_item("mfg_close", 6, "月末成本结转（人工、制造费用分摊；研发支出转费用）", "ok" if done_mfg else "todo", None,
                           "已结转" if done_mfg else f"人工 + 制造费用 + 研发支出余额 {pools:,.2f} 待结转：生产 → 成本结转", "mfgclose"))
    # 7a 系统负载（v0.36）：每次写入都全量重算库存和过账；超过 1 秒说明该改增量过账了（与数据库无关，SQLite 足够）
    import time
    from .inventory import replay
    t0 = time.perf_counter()
    replay(db)
    ms = round((time.perf_counter() - t0) * 1000)
    items.append(_item("perf", 7, "单据重算耗时正常", "ok" if ms < 1000 else "warn", None,
                       f"库存重放 {ms} 毫秒（每次保存单据都要跑一遍；超过 1 秒提醒开发侧改增量过账）", ""))
    # 7 其他
    rel = related_todos(db, start)
    items.append(_item("todos", 7, "相关待办已按本月处理结果更新", "warn" if rel else "ok", len(rel),
                       "本月动过的合同/往来单位相关的未完成待办：逐条重读，已解决的关掉，情况变了的改写（金额、谁欠谁），没变的不动", "dash", rel))
    bs = F.balance_sheet(db, end)
    items.append(_item("balanced", 7, "资产负债表平衡", "ok" if bs["balanced"] else "warn", None,
                       f"资产 {bs['total_assets']:,.2f} = 负债 {bs['total_liabilities']:,.2f} + 权益 {bs['total_equity']:,.2f}", "statements"))
    locked = bool(db.get(PeriodLock, month))
    items.append(_item("lock", 8, "期间已锁定（结账完成）", "ok" if locked else "todo", None,
                       "全部完成后在「业务核销与对账 → 期间控制」锁定本月，锁定后不能再改", "controls"))
    todo = sum(1 for x in items if x["status"] == "todo")
    warn = sum(1 for x in items if x["status"] == "warn")
    return {"month": month, "start": str(start), "end": str(end), "items": items, "todo": todo, "warn": warn, "locked": locked}


def install(app, get_db):
    @app.get("/api/close-check")
    def close_check(month: str, db=Depends(get_db)):
        import re
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise HTTPException(400, "月份格式 YYYY-MM")
        return check(db, month)
