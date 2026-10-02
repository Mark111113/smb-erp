"""纳税申报口径（v0.26）：登记已申报的季度/年度报表数，按「本年累计 − 上期已申报累计」出本期申报数。

背景（2026-09-28 用户）：2026Q1/Q2 所得税已申报且与税务系统核对无误，与后来 ERP 全面入账的口径不同；
差异必须全部放在 Q3。ERP 按真实业务日期记账不回改，季度预缴按本年累计申报，所以
Q3 本期 = ERP 1~9 月累计 − 已申报 1~6 月累计，差异自然落在 Q3。
利润表行按小企业会计准则：营业收入 / 营业成本 / 税金及附加 / 销售费用 / 管理费用 / 财务费用 / 营业外收支 / 所得税。
"""
from datetime import date, datetime
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Date, DateTime, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from . import operations as O
from .models import Account, Base, Voucher, VoucherLine

LINES = [  # key, 名称, 科目前缀（借方为正的费用类取借-贷，收入类取贷-借）
    ("revenue", "营业收入", ("6001", "6051")),
    ("cost", "营业成本", ("6401", "6402")),
    ("taxes", "税金及附加", ("6403",)),
    ("selling", "销售费用", ("6601",)),
    ("admin", "管理费用", ("6602",)),
    ("finance", "财务费用", ("6603",)),
    ("nonop_income", "营业外收入", ("6301",)),
    ("nonop_expense", "营业外支出", ("6711",)),
    ("income_tax", "所得税费用", ("6801",)),
]
DERIVED = [("profit_total", "利润总额"), ("net_profit", "净利润")]
BS = [("total_assets", "资产总额"), ("total_liabilities", "负债总额"), ("equity", "所有者权益")]


class TaxFiling(Base):
    __tablename__ = "tax_filing"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[str] = mapped_column(String(8), unique=True)      # 2025、2026-Q1 …（数为本年累计口径）
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    filed_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[Optional[object]] = mapped_column(DateTime, default=datetime.now)


class FilingIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, str_strip_whitespace=True)
    period: str = Field(pattern=r"^\d{4}(-Q[1-4])?$")
    data: dict[str, float]
    filed_date: Optional[date] = None
    remark: str = ""


def period_range(period: str) -> tuple[date, date]:
    y = int(period[:4])
    if len(period) == 4:
        return date(y, 1, 1), date(y, 12, 31)
    q = int(period[-1])
    end = date(y + (q == 4), (q * 3) % 12 + 1, 1)
    return date(y, 1, 1), date.fromordinal(end.toordinal() - 1)


def erp_figures(db, start: date, end: date) -> dict:
    sums = {}
    for code, dr, cr in db.execute(
            select(VoucherLine.account_code, func.sum(VoucherLine.debit), func.sum(VoucherLine.credit))
            .join(Voucher, VoucherLine.voucher_id == Voucher.id)
            .where(Voucher.status.in_(("posted", "reversed")), Voucher.source_type != "closing",
                   Voucher.voucher_date >= start, Voucher.voucher_date <= end)
            .group_by(VoucherLine.account_code)):
        sums[code] = (dr or 0) - (cr or 0)
    out = {}
    for key, _, prefixes in LINES:
        v = sum(val for code, val in sums.items() if code.startswith(prefixes))
        out[key] = round(-v if key in ("revenue", "nonop_income") else v, 2) + 0.0
    out["profit_total"] = round(out["revenue"] - out["cost"] - out["taxes"] - out["selling"] - out["admin"]
                                - out["finance"] + out["nonop_income"] - out["nonop_expense"], 2) + 0.0
    out["net_profit"] = round(out["profit_total"] - out["income_tax"], 2) + 0.0
    out.update(_bs_totals(db, end))
    return out


def _bs_totals(db, asof: date) -> dict:
    """与对外报表（会小企01表）同口径的资产/负债/权益合计——申报表上填的就是这个"""
    from .statements import balance_sheet
    b = {x["row"]: x["amount"] for x in balance_sheet(db, asof)}
    return {"total_assets": b[30], "total_liabilities": b[47], "equity": b[52]}


PAYROLL_EXPENSE = ("660201", "660202", "660203", "660208")   # 工资、单位社保、单位公积金、职工福利


def payroll(db, start: date, end: date) -> dict:
    """A200000 附报「职工薪酬」（本年累计）：计入成本费用的 = 上列费用科目借−贷；
    实际支付的 = 银行付出冲 2211 应付职工薪酬（发工资 + 缴社保/公积金，个人代扣部分随缴款一起算，不重复计内部代扣分录）"""
    from sqlalchemy import exists
    from sqlalchemy.orm import aliased
    W = (Voucher.status.in_(("posted", "reversed")), Voucher.voucher_date >= start, Voucher.voucher_date <= end)
    expensed = db.scalar(select(func.coalesce(func.sum(VoucherLine.debit - VoucherLine.credit), 0))
                         .join(Voucher, VoucherLine.voucher_id == Voucher.id)
                         .where(*W, VoucherLine.account_code.in_(PAYROLL_EXPENSE))) or 0
    bank = aliased(VoucherLine)
    paid = db.scalar(select(func.coalesce(func.sum(VoucherLine.debit), 0)).join(Voucher, VoucherLine.voucher_id == Voucher.id)
                     .where(*W, VoucherLine.account_code.like("2211%"), VoucherLine.debit > 0,
                            exists().where(bank.voucher_id == Voucher.id, bank.account_code.like("1002%"), bank.credit > 0))) or 0
    return {"expensed": round(expensed, 2), "paid": round(paid, 2)}


def prior_period(period: str) -> Optional[str]:
    if len(period) == 4 or period.endswith("Q1"):
        return None
    return f"{period[:4]}-Q{int(period[-1]) - 1}"


def report(db, period: str) -> dict:
    start, end = period_range(period)
    erp = erp_figures(db, start, end)
    prev = prior_period(period)
    prev_row = db.scalar(select(TaxFiling).where(TaxFiling.period == prev)) if prev else None
    own = db.scalar(select(TaxFiling).where(TaxFiling.period == period))
    prev_data = (prev_row.data if prev_row else {}) if prev else {}
    rows = []
    for key, label in [(k, l) for k, l, _ in LINES] + DERIVED:
        p = prev_data.get(key, 0.0) if prev else 0.0
        rows.append({"key": key, "label": label, "erp_ytd": erp[key], "prior_filed_ytd": round(p, 2),
                     "current": round(erp[key] - p, 2) + 0.0,
                     "filed_ytd": (own.data.get(key) if own else None)})
    bs = [{"key": k, "label": l, "erp": erp[k], "filed": (own.data.get(k) if own else None)} for k, l in BS]
    from .models import PeriodLock
    months, m = [], date(start.year, (end.month - 2 if len(period) > 4 else 1), 1)
    while m <= end:
        months.append(m.strftime("%Y-%m"))
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    locked = {p.month for p in db.scalars(select(PeriodLock).where(PeriodLock.month.in_(months)))}
    bs_open = _bs_totals(db, date.fromordinal(date(start.year, end.month - 2, 1).toordinal() - 1)) if len(period) > 4 else None
    if bs_open and prev_row and prev_row.data.get("total_assets") is not None:
        bs_open["total_assets"] = prev_row.data["total_assets"]   # 税局季初取上季已申报季末，不用 ERP 重算数
    return {"period": period, "start": str(start), "end": str(end), "prior": prev,
            "unlocked": [x for x in months if x not in locked], "files": (own.data or {}).get("files", []) if own else [],
            "assets_open": bs_open["total_assets"] if bs_open else None, "assets_close": erp["total_assets"],
            "payroll": payroll(db, start, end),
            "prior_missing": bool(prev and not prev_row), "rows": rows, "balance": bs,
            "filed": bool(own)}


def install(app, get_db):
    @app.get("/api/tax-filings")
    def list_filings(db=Depends(get_db)):
        return [{"id": t.id, "period": t.period, "data": t.data, "filed_date": str(t.filed_date or ""), "remark": t.remark}
                for t in db.scalars(select(TaxFiling).order_by(TaxFiling.period))]

    @app.put("/api/tax-filings")
    def upsert(data: FilingIn, db=Depends(get_db)):
        t = db.scalar(select(TaxFiling).where(TaxFiling.period == data.period))
        if not t:
            t = TaxFiling(period=data.period)
            db.add(t)
        keep = {k: v for k, v in (t.data or {}).items() if k in ("bs", "is", "files")}   # 导入的全表与原件保留
        t.data = {**keep, **{k: round(v, 2) for k, v in data.data.items()}}
        t.filed_date, t.remark = data.filed_date, data.remark
        O.audit(db, "登记申报数", period=data.period)
        db.commit()
        return {"ok": True}

    @app.get("/api/tax-filings/report")
    def report_api(period: str, db=Depends(get_db)):
        import re
        if not re.fullmatch(r"\d{4}(-Q[1-4])?", period):
            raise HTTPException(400, "期间格式：2026-Q3 或 2026")
        return report(db, period)

    @app.get("/api/tax-filings/lines")
    def lines():
        return [{"key": k, "label": l} for k, l, _ in LINES] + [{"key": k, "label": l} for k, l in DERIVED + BS]
