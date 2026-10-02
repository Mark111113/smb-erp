"""固定资产台账与折旧（v0.25）。

- 登记资产 = 建卡片；可同时生成入账凭证（借 资产科目 原值 / 贷 预付、应付等，贷方可多行），
  常见用法：工程/设备验收后从 1123 预付转固，未付尾款挂 2202 应付。
- 折旧：直线法，月折旧额 = round(原值 ×(1−残值率)/年限月数, 2)，最后一个月补尾差；
  从起折月（默认取得次月）开始。「计提至某月」按月补齐所有未计提月份，每月一张凭证
  （source_type=depreciation，借 各资产折旧费用科目 / 贷 1602 累计折旧），重复点不会重复计提。
- 撤销只允许撤最后一个已计提月（红冲该月凭证并删除该月折旧明细），避免中间断档。
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, inspect, select, text

from . import finance as F
from . import operations as O
from .models import Account, Asset, DepreciationLine, Voucher

ACC_ACCUM = "1602"
EPS = 0.005


class _In(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, str_strip_whitespace=True)


class CreditLine(_In):
    account: str
    amount: float = Field(gt=0)
    partner_id: Optional[int] = None
    summary: str = ""


class AssetIn(_In):
    name: str = Field(min_length=1, max_length=128)
    account: str
    expense_account: str = "660209"
    acquired_date: date
    cost: float = Field(gt=0)
    life_months: int = Field(gt=0, le=600)
    residual_rate: float = Field(default=0, ge=0, lt=1)
    start_month: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    partner_id: Optional[int] = None
    remark: str = ""
    credit_lines: list[CreditLine] = []   # 为空则只建卡片不出凭证（已另行入账的资产）


class RemarkIn(_In):
    remark: str = ""


class PeriodIn(_In):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")


def ensure_schema(engine):
    """v0.24 以前 asset 是空壳表；空表按新结构重建。"""
    cols = {c["name"] for c in inspect(engine).get_columns("asset")}
    if "life_months" not in cols:
        with engine.begin() as conn:
            if conn.execute(text("SELECT COUNT(*) FROM asset")).scalar():
                raise RuntimeError("asset 旧表有数据，需要人工迁移")
            conn.execute(text("DROP TABLE asset"))
        Asset.__table__.create(engine)
        DepreciationLine.__table__.create(engine, checkfirst=True)


def next_month(p: str) -> str:
    y, m = int(p[:4]), int(p[5:7])
    return f"{y + (m == 12)}-{m % 12 + 1:02d}"


def month_end(p: str) -> date:
    return date.fromisoformat(next_month(p) + "-01") - timedelta(days=1)


def monthly_amount(a: Asset) -> float:
    return round(a.cost * (1 - a.residual_rate) / a.life_months, 2)


def depreciable(a: Asset) -> float:
    return round(a.cost * (1 - a.residual_rate), 2)


def accumulated(db, a: Asset, upto: Optional[str] = None) -> float:
    q = select(func.coalesce(func.sum(DepreciationLine.amount), 0)).where(DepreciationLine.asset_id == a.id)
    if upto:
        q = q.where(DepreciationLine.period <= upto)
    return round(db.scalar(q) or 0, 2)


def _leaf(db, code: str):
    acc = db.get(Account, code)
    if not acc or not acc.active or db.scalar(select(Account.code).where(Account.parent_code == code).limit(1)):
        raise ValueError(f"科目 {code} 不存在或不是末级科目")


def create_asset(db, data: AssetIn) -> Asset:
    _leaf(db, data.account)
    _leaf(db, data.expense_account)
    start = data.start_month or next_month(str(data.acquired_date)[:7])
    if start < str(data.acquired_date)[:7]:
        raise ValueError("起折月不能早于取得月")
    n = (db.scalar(select(func.count(Asset.id))) or 0) + 1
    a = Asset(code=f"FA-{data.acquired_date.year}-{n:03d}", name=data.name, account=data.account,
              expense_account=data.expense_account, acquired_date=data.acquired_date, cost=round(data.cost, 2),
              life_months=data.life_months, residual_rate=data.residual_rate, start_month=start,
              partner_id=data.partner_id, remark=data.remark)
    db.add(a)
    db.flush()
    if data.credit_lines:
        total = round(sum(l.amount for l in data.credit_lines), 2)
        if abs(total - a.cost) > EPS:
            raise ValueError(f"贷方合计 {total:.2f} ≠ 原值 {a.cost:.2f}")
        summary = f"固定资产入账 {a.code} {a.name}"
        lines = [dict(account=a.account, debit=a.cost, partner_id=a.partner_id, summary=summary)]
        lines += [dict(account=l.account, credit=l.amount, partner_id=l.partner_id, summary=l.summary or summary)
                  for l in data.credit_lines]
        v = F.create_voucher(db, a.acquired_date, "asset", a.id, summary, lines)
        a.voucher_id = v.id
    O.audit(db, "登记固定资产", asset_id=a.id, voucher_id=a.voucher_id)
    return a


def _last_period(db) -> Optional[str]:
    return db.scalar(select(func.max(DepreciationLine.period)))


def depreciate_to(db, period: str) -> list[dict]:
    """把所有资产补提到 period（含）。返回每月生成的凭证。"""
    assets = list(db.scalars(select(Asset).where(Asset.status == "active")))
    if not assets:
        return []
    p = min(a.start_month for a in assets)   # 从最早起折月逐月检查，已提过的跳过（补登的旧资产也能补提）
    out = []
    while p <= period:
        lines, total = [], 0.0
        rows = []
        for a in assets:
            if a.start_month > p or db.scalar(select(DepreciationLine.id).where(
                    DepreciationLine.asset_id == a.id, DepreciationLine.period == p)):
                continue
            remaining = round(depreciable(a) - accumulated(db, a), 2)
            if remaining <= EPS:
                continue
            amt = min(monthly_amount(a), remaining)
            done = db.scalar(select(func.count(DepreciationLine.id)).where(DepreciationLine.asset_id == a.id)) or 0
            if done + 1 >= a.life_months:
                amt = remaining        # 最后一个月补尾差
            amt = round(amt, 2)
            lines.append(dict(account=a.expense_account, debit=amt, summary=f"折旧 {p} {a.code} {a.name}"))
            lines.append(dict(account=ACC_ACCUM, credit=amt, summary=f"折旧 {p} {a.code} {a.name}"))
            rows.append((a, amt))
            total += amt
        if rows:
            v = F.create_voucher(db, month_end(p), "depreciation", int(p.replace("-", "")), f"计提折旧 {p}", lines)
            for a, amt in rows:
                db.add(DepreciationLine(asset_id=a.id, period=p, amount=amt, voucher_id=v.id))
            db.flush()
            out.append({"period": p, "voucher_no": v.voucher_no, "amount": round(total, 2), "assets": len(rows)})
        p = next_month(p)
    if out:
        O.audit(db, "计提折旧", upto=period, months=[x["period"] for x in out])
    return out


def undo_last(db) -> str:
    last = _last_period(db)
    if not last:
        raise ValueError("还没有计提过折旧")
    rows = list(db.scalars(select(DepreciationLine).where(DepreciationLine.period == last)))
    for vid in {r.voucher_id for r in rows}:
        v = db.get(Voucher, vid)
        if v and v.status == "posted":
            F.reverse_voucher(db, v, f"撤销 {last} 折旧")
    for r in rows:
        db.delete(r)
    O.audit(db, "撤销折旧", period=last)
    db.flush()
    return last


def _row(db, a: Asset) -> dict:
    acc = accumulated(db, a)
    last = db.scalar(select(func.max(DepreciationLine.period)).where(DepreciationLine.asset_id == a.id))
    v = db.get(Voucher, a.voucher_id) if a.voucher_id else None
    return {"id": a.id, "code": a.code, "name": a.name, "account": a.account,
            "account_name": (db.get(Account, a.account).name if db.get(Account, a.account) else ""),
            "expense_account": a.expense_account, "acquired_date": str(a.acquired_date), "cost": a.cost,
            "life_months": a.life_months, "life_years": round(a.life_months / 12, 1), "residual_rate": a.residual_rate,
            "start_month": a.start_month, "monthly": monthly_amount(a), "accumulated": acc,
            "net": round(a.cost - acc, 2), "last_period": last or "", "status": a.status,
            "voucher_no": v.voucher_no if v else "", "partner_id": a.partner_id,
            "partner_short": (a.partner_id and _pshort(db, a.partner_id)) or "", "remark": a.remark}


def _pshort(db, pid):
    from .models import Partner
    p = db.get(Partner, pid)
    return (p.short_name or p.name) if p else ""


def install(app, get_db):
    @app.get("/api/assets")
    def list_assets(db=Depends(get_db)):
        rows = [_row(db, a) for a in db.scalars(select(Asset).order_by(Asset.acquired_date, Asset.id))]
        return {"items": rows, "last_period": _last_period(db) or ""}

    @app.post("/api/assets")
    def create_asset_api(data: AssetIn, db=Depends(get_db)):
        a = create_asset(db, data)
        db.commit()
        return {"id": a.id, "code": a.code}

    @app.put("/api/assets/{aid}/remark")
    def remark_api(aid: int, data: RemarkIn, db=Depends(get_db)):
        """只改备注（权证信息、税源口径等）；原值/年限动了会影响已提折旧，不在这里改"""
        a = db.get(Asset, aid)
        if not a:
            raise HTTPException(404, "资产不存在")
        a.remark = data.remark
        db.commit()
        return _row(db, a)

    @app.post("/api/assets/depreciate")
    def depreciate_api(data: PeriodIn, db=Depends(get_db)):
        out = depreciate_to(db, data.period)
        db.commit()
        return {"months": out}

    @app.post("/api/assets/depreciate/undo")
    def undo_api(db=Depends(get_db)):
        p = undo_last(db)
        db.commit()
        return {"period": p}

    @app.get("/api/assets/{aid}/schedule")
    def schedule(aid: int, db=Depends(get_db)):
        a = db.get(Asset, aid)
        if not a:
            raise HTTPException(404, "资产不存在")
        return [{"period": r.period, "amount": r.amount} for r in db.scalars(
            select(DepreciationLine).where(DepreciationLine.asset_id == aid).order_by(DepreciationLine.period))]
