"""ERP — 财务核心（v0.10）

模型对齐 SAP FI：凭证（头+行项目）+ 科目 + 辅助核算维度（往来单位/成本中心）。
自动过账规则挂在业务流上：
  销售开票   借应收[客户]           贷主营业务收入 + 销项税额
  采购收票   借GR/IR清算+进项税      贷应付[供应商]
  收货确认   借库存商品              贷货到票未到清算[供应商]（SAP GR/IR）
  出库确认   借主营业务成本          贷库存商品（按快照含税加权价）
  收款      借银行存款              贷预收款[客户]
  付款      借预付款[供应商]        贷银行存款
  票款核销   借预收款/贷应收 或 借应付/贷预付款
  作废      红字反向凭证冲销
发票登记必须提供原始不含税额与税额；业务更正由 operations.synchronize 追加红冲和新凭证。
金额一律 round(x,2)；凭证构造即保证借贷平衡，create_voucher 兜底校验。
"""
from datetime import date, timedelta
import math
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .models import (Account, CostCenter, Voucher, VoucherLine, Invoice, Payment,
                     StockMovement, Contract, Partner, PeriodLock)

# ---- 科目代号（改科目表时同步这里） ----
ACC_BANK = "100201"      # 银行存款-基本户（多个账户在 本单位设置 → 开户账户 里各挂一个 1002xx）
ACC_AR = "1122"          # 应收账款
ACC_PREPAY = "1123"      # 预付账款
ACC_GRIR = "1234"        # 货到票未到清算（GR/IR）
ACC_INVENTORY = "1405"   # 库存商品
ACC_AP = "2202"          # 应付账款
ACC_ADVANCE = "2203"     # 预收账款
ACC_VAT_OUT = "222101"   # 应交增值税-销项税额
ACC_VAT_IN = "222102"   # 应交增值税-进项税额（=申报表已勾选抵扣的进项）
ACC_VAT_PENDING = "222104"  # 应交增值税-待认证进项税额（收到专票未勾选）
ACC_PROFIT = "4103"      # 本年利润
# 科目编码统一用企业会计准则编码（损益 6xxx、权益 4xxx；v0.28 把实收资本 3001→4001），
# 对外报表按小企业会计准则格式出（app/statements.py 映射）；换准则只换报表映射
ACC_REVENUE = "6001"     # 主营业务收入
ACC_COGS = "6401"        # 主营业务成本
ACC_FIN_EXP = "6603"     # 财务费用

CHART = [
    ("1002", "银行存款", "asset", "D", None),
    ("100201", "基本户", "asset", "D", "1002"),
    ("1122", "应收账款", "asset", "D", None),
    ("1123", "预付账款", "asset", "D", None),
    ("1234", "货到票未到清算", "liability", "C", None),
    ("1405", "库存商品", "asset", "D", None),
    ("2202", "应付账款", "liability", "C", None),
    ("2203", "预收账款", "liability", "C", None),
    ("2221", "应交税费", "liability", "C", None),
    ("222101", "应交增值税-销项税额", "liability", "C", "2221"),
    ("222102", "应交增值税-进项税额", "liability", "C", "2221"),
    ("4001", "实收资本", "equity", "C", None),
    ("4103", "本年利润", "equity", "C", None),
    ("4104", "利润分配", "equity", "C", None),
    ("6001", "主营业务收入", "revenue", "C", None),
    ("6401", "主营业务成本", "expense", "D", None),
    ("6601", "销售费用", "expense", "D", None),
    ("6602", "管理费用", "expense", "D", None),
    ("6603", "财务费用", "expense", "D", None),
    ("6711", "营业外支出", "expense", "D", None),
    ("671101", "存货盘亏及调整", "expense", "D", "6711"),
    ("671102", "罚款及滞纳金", "expense", "D", "6711"),
    ("4199", "期初权益（过渡）", "equity", "C", None),
    # v0.25 非贸易入账补充（2026-09-28）
    ("1221", "其他应收款", "asset", "D", None),
    ("1601", "固定资产", "asset", "D", None),
    ("160101", "房屋及建筑物", "asset", "D", "1601"),
    ("160102", "机器设备", "asset", "D", "1601"),
    ("160103", "电子及其他设备", "asset", "D", "1601"),
    ("1602", "累计折旧", "asset", "C", None),
    ("2211", "应付职工薪酬", "liability", "C", None),
    ("221101", "工资", "liability", "C", "2211"),
    ("221102", "社会保险费", "liability", "C", "2211"),
    ("221103", "住房公积金", "liability", "C", "2211"),
    ("222103", "应交增值税-已交税金", "liability", "C", "2221"),
    ("222104", "应交增值税-待认证进项税额", "liability", "C", "2221"),
    ("222105", "应交增值税-进项税额转出", "liability", "C", "2221"),
    ("2241", "其他应付款", "liability", "C", None),
    ("6403", "税金及附加", "expense", "D", None),
    ("640301", "城市维护建设税", "expense", "D", "6403"),
    ("640302", "教育费附加", "expense", "D", "6403"),
    ("640303", "地方教育附加", "expense", "D", "6403"),
    ("640304", "印花税", "expense", "D", "6403"),
    ("640305", "房产税", "expense", "D", "6403"),
    ("640306", "城镇土地使用税", "expense", "D", "6403"),
    ("660201", "工资", "expense", "D", "6602"),
    ("660202", "社会保险费", "expense", "D", "6602"),
    ("660203", "住房公积金", "expense", "D", "6602"),
    ("660204", "办公及网络费", "expense", "D", "6602"),
    ("660205", "交通差旅费", "expense", "D", "6602"),
    ("660206", "快递运输费", "expense", "D", "6602"),
    ("660207", "水电费", "expense", "D", "6602"),
    ("660208", "职工福利费", "expense", "D", "6602"),
    ("660209", "折旧费", "expense", "D", "6602"),
    ("660210", "其他", "expense", "D", "6602"),
    ("660301", "手续费", "expense", "D", "6603"),
    ("660302", "利息收入", "expense", "D", "6603"),
    ("6801", "所得税费用", "expense", "D", None),
    # 制造（v0.35，docs/PLAN_manufacturing.md）：存货分科目、生产成本、制造费用、研发支出
    ("1403", "原材料", "asset", "D", None),
    ("1408", "委托加工物资", "asset", "D", None),
    ("1411", "周转材料", "asset", "D", None),
    ("5001", "生产成本", "asset", "D", None),
    ("500101", "生产成本-直接材料", "asset", "D", "5001"),
    ("500102", "生产成本-直接人工", "asset", "D", "5001"),
    ("500103", "生产成本-制造费用", "asset", "D", "5001"),
    ("5101", "制造费用", "asset", "D", None),
    ("510101", "制造费用-折旧", "asset", "D", "5101"),
    ("510102", "制造费用-电费", "asset", "D", "5101"),
    ("510103", "制造费用-机物料消耗", "asset", "D", "5101"),
    ("510104", "制造费用-车间管理人员薪酬", "asset", "D", "5101"),
    ("510105", "制造费用-维修", "asset", "D", "5101"),
    ("510199", "制造费用-其他", "asset", "D", "5101"),
    ("5301", "研发支出", "asset", "D", None),
    ("660211", "研究费用", "expense", "D", "6602"),
]

DEFAULT_COST_CENTERS = [("CC01", "综合", None), ("CC02", "销售", None), ("CC03", "管理", None)]
# 制造成本中心（v0.35）：缺哪个补哪个，不动已有的；研发项目建在 CC20 下（一个项目一个子成本中心）
MFG_COST_CENTERS = [("CC10", "生产", None), ("CC11", "SMT", "CC10"), ("CC12", "DIP", "CC10"), ("CC13", "电感组装", "CC10"),
                    ("CC14", "测试包装", "CC10"), ("CC20", "研发", None), ("CC30", "仓储物流", None)]

# 物料类型 → 存货科目。老物料都是 goods（外购商品），仍记 1405，原有凭证不变
MATERIAL_TYPES = {"goods": "外购商品", "finished": "产成品", "semi": "半成品", "raw": "原材料", "aux": "辅料",
                  "pack": "包材", "tool": "周转材料"}
INV_ACCOUNT = {"goods": "1405", "finished": "1405", "semi": "1405", "raw": "1403", "aux": "1403", "pack": "1403", "tool": "1411"}
INV_ACCOUNTS = ("1403", "1405", "1411")
ACC_WIP_MAT = "500101"    # 生产成本-直接材料（工单在制）
ACC_WIP_LABOR = "500102"  # 生产成本-直接人工（一线人员工资计提挂这里，月末分摊）
ACC_MOH = "5101"          # 制造费用
ACC_SUBCON = "1408"       # 委托加工物资
ACC_RD = "5301"           # 研发支出
ACC_RD_EXP = "660211"     # 研究费用（利润表第 17 行）


def inventory_account(material) -> str:
    return INV_ACCOUNT.get(getattr(material, "material_type", None) or "goods", "1405")


def seed_finance(db: Session):
    """幂等种子：科目表 + 默认成本中心"""
    for code, name, t, d, parent in CHART:
        if not db.get(Account, code):
            db.add(Account(code=code, name=name, acc_type=t, direction=d, parent_code=parent))
            db.flush()
    if not db.scalar(select(func.count(CostCenter.id))):
        for code, name, parent in DEFAULT_COST_CENTERS:
            db.add(CostCenter(code=code, name=name))
        db.flush()
    for code, name, parent in MFG_COST_CENTERS:
        if not db.scalar(select(CostCenter.id).where(CostCenter.code == code)):
            pid = db.scalar(select(CostCenter.id).where(CostCenter.code == parent)) if parent else None
            db.add(CostCenter(code=code, name=name, parent_id=pid))
            db.flush()
    db.commit()


def tax_mode(db: Session) -> str:
    """价税分离（deductive）：一般纳税人正规口径——进项税入账抵扣，库存/成本按不含税本位。
    含税录入价在入库层按 stock_movement.tax_rate 折算；口径切换需整体重估库存（scripts/history/switch_to_deductive_20260926.py）。"""
    return "deductive"


def _next_voucher_no(db: Session, d: date) -> str:
    prefix = f"记-{d.strftime('%Y%m')}-"
    numbers = db.scalars(select(Voucher.voucher_no).where(Voucher.voucher_no.like(prefix + "%"))).all()
    n = max((int(no.rsplit("-",1)[-1]) for no in numbers), default=0)
    return f"{prefix}{n + 1:04d}"


def create_voucher(db: Session, voucher_date: date, source_type: str, source_id: int | None,
                   summary: str, lines: list[dict], status: str = "posted") -> Voucher:
    """lines: [{account, debit, credit, partner_id?, cost_center_id?, summary?}]；借贷不平衡直接抛错"""
    if db.get(PeriodLock, str(voucher_date)[:7]):
        raise ValueError("该会计期间已锁定")
    for l in lines:
        a = db.get(Account, l["account"])
        if not a or not a.active or db.scalar(select(Account.code).where(Account.parent_code == a.code).limit(1)):
            raise ValueError("必须使用有效末级科目")
        d, c = l.get("debit") or 0, l.get("credit") or 0
        if not math.isfinite(d) or not math.isfinite(c) or d < 0 or c < 0 or (d and c):
            raise ValueError("凭证行借贷必须为非负有限金额且只能有一方")
        if l.get("partner_id") and not db.get(Partner, l["partner_id"]):
            raise ValueError("往来单位不存在")
        if l.get("cost_center_id") and not db.get(CostCenter, l["cost_center_id"]):
            raise ValueError("成本中心不存在")
    td = round(sum(round(l.get("debit") or 0, 2) for l in lines), 2)
    tc = round(sum(round(l.get("credit") or 0, 2) for l in lines), 2)
    if abs(td - tc) > 0.005:
        raise ValueError(f"凭证不平衡：借 {td} ≠ 贷 {tc}（{summary}）")
    if td == 0:
        raise ValueError("零金额凭证拒绝过账")
    v = Voucher(voucher_no=_next_voucher_no(db, voucher_date), voucher_date=voucher_date,
                source_type=source_type, source_id=source_id, summary=summary, status=status)
    for i, l in enumerate(lines, 1):
        v.lines.append(VoucherLine(line_no=i, account_code=l["account"],
                                   partner_id=l.get("partner_id"), cost_center_id=l.get("cost_center_id"),
                                   debit=round(l.get("debit") or 0, 2), credit=round(l.get("credit") or 0, 2),
                                   summary=l.get("summary") or summary))
    db.add(v)
    db.flush()
    return v


def reverse_voucher(db: Session, v: Voucher, reason: str) -> Voucher:
    """红字冲销：同日期、借贷对调、行摘要标注红冲"""
    if v.status != "posted":
        raise ValueError("凭证已冲销")
    lines = [{"account": l.account_code, "debit": l.credit, "credit": l.debit,
              "partner_id": l.partner_id, "cost_center_id": l.cost_center_id,
              "summary": f"红冲 {v.voucher_no}：{reason}"} for l in v.lines]
    rv = create_voucher(db, v.voucher_date, "reversal", v.id, f"红冲 {v.voucher_no}：{reason}", lines)
    rv.reversal_of = v.id
    v.status = "reversed"
    db.flush()
    return rv


# ---------------- 业务钩子（自动过账） ----------------

def _partner_of_contract(db: Session, contract_id: int | None) -> int | None:
    if not contract_id:
        return None
    c = db.get(Contract, contract_id)
    return c.partner_id if c else None


def _post_source(db, kind, id_):
    from .operations import synchronize
    db.flush()
    synchronize(db)
    return db.scalar(select(Voucher).where(Voucher.source_type == kind, Voucher.source_id == id_, Voucher.status == "posted"))


def post_invoice(db, inv):
    db.flush()
    return _post_source(db, "invoice_" + inv.direction, inv.id)


def post_movement_confirm(db, m):
    db.flush()
    return _post_source(db, "goods_in" if m.move_type in ("in", "opening") else "goods_out", m.id)


def account_balance(db: Session, account_code: str, partner_id: int | None = None,
                    upto: date | None = None) -> float:
    """科目（+往来维度）余额，借正贷负；只算已过账未被冲销的原始凭证行"""
    q = (select(func.coalesce(func.sum(VoucherLine.debit - VoucherLine.credit), 0))
         .join(Voucher, VoucherLine.voucher_id == Voucher.id)
         .where(VoucherLine.account_code == account_code, Voucher.status.in_(("posted", "reversed"))))
    if partner_id is not None:
        q = q.where(VoucherLine.partner_id == partner_id)
    if upto is not None:
        q = q.where(Voucher.voucher_date <= upto)
    return round(db.scalar(q) or 0, 2)


def post_payment(db, pm):
    db.flush()
    return _post_source(db, "payment_out" if pm.direction == "pay" else "payment_in", pm.id)


# ---------------- 账表查询 ----------------

def ledger(db: Session, account_code: str, partner_id: int | None = None,
           from_date: date | None = None, to_date: date | None = None) -> list[dict]:
    """明细账（含往来/成本中心维度过滤），带运行余额"""
    q = (select(VoucherLine, Voucher).join(Voucher, VoucherLine.voucher_id == Voucher.id)
         .where(VoucherLine.account_code == account_code, Voucher.status.in_(("posted", "reversed")))
         .order_by(Voucher.voucher_date, Voucher.id, VoucherLine.line_no))
    if partner_id is not None:
        q = q.where(VoucherLine.partner_id == partner_id)
    if from_date:
        q = q.where(Voucher.voucher_date >= from_date)
    if to_date:
        q = q.where(Voucher.voucher_date <= to_date)
    acc = db.get(Account, account_code)
    direction = 1 if (acc and acc.direction == "D") else -1
    opening = account_balance(db, account_code, partner_id, upto=(date.fromisoformat(str(from_date)) - timedelta(days=1))) if from_date else 0
    out, bal = [], opening
    for l, v in db.execute(q):
        bal += l.debit - l.credit
        out.append({"voucher_no": v.voucher_no, "date": str(v.voucher_date), "summary": l.summary or v.summary,
                    "debit": l.debit, "credit": l.credit,
                    "balance": round(bal * direction, 2), "partner_id": l.partner_id,
                    "cost_center_id": l.cost_center_id, "source_type": v.source_type})
    return out


def trial_balance(db: Session, from_date: date | None = None, to_date: date | None = None) -> list[dict]:
    """科目余额表（SAP 式：期初/本期借/本期贷/期末）"""
    rows = []
    for a in db.scalars(select(Account).where(Account.active == 1).order_by(Account.code)):
        q = (select(func.coalesce(func.sum(VoucherLine.debit), 0), func.coalesce(func.sum(VoucherLine.credit), 0))
             .join(Voucher, VoucherLine.voucher_id == Voucher.id)
             .where(VoucherLine.account_code == a.code, Voucher.status.in_(("posted", "reversed"))))
        qb = q
        if from_date:
            qb = qb.where(Voucher.voucher_date < from_date)
        d0 = db.execute(qb).first()
        opening = round((d0[0] or 0) - (d0[1] or 0), 2) if from_date else 0
        qp = q
        rng = []
        if from_date:
            rng.append(Voucher.voucher_date >= from_date)
        if to_date:
            rng.append(Voucher.voucher_date <= to_date)
        if rng:
            qp = qp.where(*rng)
        dp = db.execute(qp).first()
        pd_, pc_ = round(dp[0] or 0, 2), round(dp[1] or 0, 2)
        closing = round(opening + pd_ - pc_, 2)
        if not (opening or pd_ or pc_):
            continue
        direction = 1 if a.direction == "D" else -1
        rows.append({"code": a.code, "name": a.name, "acc_type": a.acc_type,
                     "opening": round(opening * direction, 2), "debit": pd_, "credit": pc_,
                     "closing": round(closing * direction, 2)})
    return rows


def ar_ap_summary(db: Session) -> list[dict]:
    """客户应收/供应商应付往来余额表（含预收预付并轨显示）"""
    out = []
    for acc_code, label in ((ACC_AR, "应收账款"), (ACC_ADVANCE, "预收款"),
                            (ACC_AP, "应付账款"), (ACC_PREPAY, "预付账款")):
        q = (select(VoucherLine.partner_id, func.sum(VoucherLine.debit - VoucherLine.credit))
             .join(Voucher, VoucherLine.voucher_id == Voucher.id)
             .where(VoucherLine.account_code == acc_code, Voucher.status.in_(("posted", "reversed")),
                    VoucherLine.partner_id.isnot(None))
             .group_by(VoucherLine.partner_id))
        for pid, s in db.execute(q):
            out.append({"partner_id": pid, "account": acc_code, "account_name": label,
                        "balance": round((s or 0) * (1 if acc_code in (ACC_AR, ACC_PREPAY) else -1), 2)})
    return sorted(out, key=lambda r: (r["partner_id"], r["account"]))


def income_statement(db: Session, from_date: date, to_date: date) -> dict:
    """利润表（单步：收入-成本-费用）"""
    def acc_sum(code):
        q = (select(func.coalesce(func.sum(VoucherLine.credit - VoucherLine.debit), 0))
             .join(Voucher, VoucherLine.voucher_id == Voucher.id)
             .where(VoucherLine.account_code == code, Voucher.status.in_(("posted", "reversed")),
                    Voucher.voucher_date >= from_date, Voucher.voucher_date <= to_date, Voucher.source_type != "closing"))
        return round(db.scalar(q) or 0, 2)

    def acc_sum_d(code):
        q = (select(func.coalesce(func.sum(VoucherLine.debit - VoucherLine.credit), 0))
             .join(Voucher, VoucherLine.voucher_id == Voucher.id)
             .where(VoucherLine.account_code == code, Voucher.status.in_(("posted", "reversed")),
                    Voucher.voucher_date >= from_date, Voucher.voucher_date <= to_date, Voucher.source_type != "closing"))
        return round(db.scalar(q) or 0, 2)

    revenue = round(sum(acc_sum(a.code) for a in db.scalars(select(Account).where(Account.acc_type == "revenue", Account.active == 1))), 2)
    cogs = acc_sum_d(ACC_COGS)
    accs = list(db.scalars(select(Account).where(Account.acc_type == "expense", Account.active == 1,
                                                  Account.code != ACC_COGS).order_by(Account.code)))
    expenses = {a.code: acc_sum_d(a.code) for a in accs}
    names = {a.code: (db.get(Account, a.parent_code).name + "-" if a.parent_code and db.get(Account, a.parent_code) else "") + a.name
             for a in accs}
    profit = round(revenue - cogs - sum(expenses.values()), 2)
    return {"revenue": revenue, "cogs": cogs, "expenses": expenses, "expense_names": names, "profit": profit}


def balance_sheet(db: Session, asof: date) -> dict:
    """资产负债表（简化）：资产=负债+权益；权益含未结转损益净额（收入-成本-费用余额）"""
    assets, liabilities, equity = [], [], []
    unclosed = 0.0  # 损益类未结转净额（贷方为盈利）
    tax_net = 0.0
    for a in db.scalars(select(Account).where(Account.active == 1).order_by(Account.code)):
        # 按科目类别定正负（不是按余额方向）：累计折旧等备抵科目在资产方显示为负数
        s = round(account_balance(db, a.code, upto=asof) * (1 if a.acc_type in ("asset", "expense") else -1), 2)
        if a.acc_type == "revenue":
            unclosed += s
            continue
        if a.acc_type == "expense":
            unclosed -= s
            continue
        if a.code.startswith("2221"):
            tax_net += s          # 应交税费按净额列示：净借方（进项留抵）放到资产方
            continue
        if abs(s) < 0.005:
            continue
        parent = db.get(Account, a.parent_code) if a.parent_code else None
        row = {"code": a.code, "name": (parent.name + "-" if parent else "") + a.name, "balance": s}
        (assets if a.acc_type == "asset" else liabilities if a.acc_type == "liability" else equity).append(row)
    if tax_net > 0.005:
        liabilities.append({"code": "2221", "name": "应交税费", "balance": round(tax_net, 2)})
    elif tax_net < -0.005:
        assets.append({"code": "2221", "name": "应交税费-留抵税额", "balance": round(-tax_net, 2)})
    ta = round(sum(r["balance"] for r in assets), 2)
    tl = round(sum(r["balance"] for r in liabilities), 2)
    te = round(sum(r["balance"] for r in equity), 2)
    if abs(unclosed) >= 0.005:
        equity.append({"code": "3999", "name": "本年未结转损益", "balance": round(unclosed, 2)})
        te = round(te + unclosed, 2)
    return {"assets": assets, "liabilities": liabilities, "equity": equity,
            "total_assets": ta, "total_liabilities": tl, "total_equity": te,
            "balanced": abs(ta - tl - te) < 0.01}
