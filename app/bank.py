"""银行流水（v0.24）：导入中行 HISQRY CSV、与收付款匹配、非贸易流水分类入账、月末余额核对。

- 导入按「交易流水号」去重，重复导入同一文件或区间重叠的多个文件都是幂等的。
- 贸易款：流水 ↔ 收付款（payment.bank_txn_id），一笔流水可对多笔收付款（如客户一次付两张合同）。
  自动匹配只认「同方向 + 对方户名一致 + 同日 + 金额相等（单笔或同日合计）」，其余在页面上人工关联。
- 非贸易：在页面上「入账」，银行存款一侧自动生成，另一侧由操作员选科目（可多行），
  生成 source_type=bank 的凭证（不属于 synchronize 管的自动凭证，撤销=红冲）。
- 冲正：同日同户名同额一正一负且其一业务类型为「冲正」的，两笔自动标为已对冲。
"""
import csv
import io
import re
from collections import defaultdict
from datetime import date
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect, select, text

from . import finance as F
from . import operations as O
from .models import Account, BankTxn, Partner, Payment, Voucher

EPS = 0.005
STATUS_LABELS = {"unmatched": "未处理", "matched": "已匹配", "posted": "已入账", "ignored": "已对冲/忽略"}


class _In(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, str_strip_whitespace=True)


class ImportIn(_In):
    filename: str = ""
    content: str = Field(min_length=1)


class LinkIn(_In):
    payment_ids: list[int] = Field(min_length=1)


class NoteIn(_In):
    note: str = ""


class PostLine(_In):
    account: str
    amount: float = Field(gt=0)
    partner_id: Optional[int] = None
    cost_center_id: Optional[int] = None
    summary: str = ""


class PostIn(_In):
    category: str = ""
    summary: str = ""
    lines: list[PostLine] = Field(min_length=1)


def ensure_schema(engine):
    """v0.23 以前 bank_txn 只是空壳表（字段不够、从未写入）；空表直接按新结构重建。"""
    cols = {c["name"] for c in inspect(engine).get_columns("bank_txn")}
    if "txn_no" not in cols:
        with engine.begin() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM bank_txn")).scalar()
            if n:
                raise RuntimeError("bank_txn 旧表有数据，需要人工迁移")
            conn.execute(text("DROP TABLE bank_txn"))
        BankTxn.__table__.create(engine)


# ---------------- 解析 ----------------

def _num(s: str) -> float:
    s = (s or "").replace(",", "").replace("\t", "").strip()
    return float(s) if s else 0.0


def parse_boc_csv(content: str) -> tuple[str, list[dict]]:
    """中行企业网银「历史交易查询」CSV：前 7 行汇总，第 8 行表头（中文[英文]），字段值带 \\t。"""
    content = content.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(content)))
    account = ""
    head_at = None
    for i, r in enumerate(rows[:20]):
        if r and r[0].startswith("查询账号") and len(r) > 1:
            account = r[1].replace("\t", "").strip()
        if r and r[0].startswith("交易类型"):
            head_at = i
            break
    if head_at is None:
        raise ValueError("不是中行历史交易查询 CSV（找不到「交易类型」表头行）")
    hdr = [h.split("[")[0].strip() for h in rows[head_at]]
    out = []
    for r in rows[head_at + 1:]:
        if len(r) < 18:
            continue
        d = {h: (v or "").replace("\t", "").strip() for h, v in zip(hdr, r) if h}
        if not d.get("交易流水号") or not d.get("交易日期"):
            continue
        amt = _num(d["交易金额"])
        mine_is_payer = amt < 0
        ds = d["交易日期"]
        tm = d.get("交易时间", "")
        out.append(dict(
            account_no=account or (d.get("付款人账号") if mine_is_payer else d.get("收款人账号")) or "",
            txn_no=d["交易流水号"], txn_date=date(int(ds[:4]), int(ds[4:6]), int(ds[6:8])),
            txn_time=tm[:8], amount=round(amt, 2),
            balance=round(_num(d.get("交易后余额", "")), 2) if d.get("交易后余额") else None,
            counterparty_name=d.get("收款人名称" if mine_is_payer else "付款人名称", ""),
            counterparty_account=d.get("收款人账号" if mine_is_payer else "付款人账号", ""),
            counterparty_bank=d.get("收款人开户行名" if mine_is_payer else "付款人开户行名", ""),
            biz_type=d.get("业务类型", ""), summary=d.get("摘要", ""), purpose=d.get("用途", ""),
            memo=d.get("交易附言", ""), raw=d))
    return account, out


def import_rows(db, rows: list[dict], source_file: str = "") -> dict:
    known = set(db.scalars(select(BankTxn.txn_no)))
    added = 0
    for r in rows:
        if r["txn_no"] in known:
            continue
        known.add(r["txn_no"])
        db.add(BankTxn(**r, source_file=source_file))
        added += 1
    db.flush()
    return {"parsed": len(rows), "added": added, "duplicate": len(rows) - added}


# ---------------- 匹配 ----------------

def norm_name(s: str) -> str:
    s = re.sub(r"[\s（）()·,，.。]", "", s or "")
    for suffix in ("有限责任公司", "股份有限公司", "有限公司", "公司"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s


def partner_for(db, t: BankTxn) -> Optional[Partner]:
    key = norm_name(t.counterparty_name)
    if not key:
        return None
    for p in db.scalars(select(Partner)):
        if norm_name(p.name) == key or (t.counterparty_account and p.bank_account
                                         and p.bank_account.replace(" ", "") == t.counterparty_account):
            return p
    return None


def _open_payments(db):
    return [p for p in db.scalars(O.active_documents(db, Payment).where(Payment.bank_txn_id.is_(None)))]


def _link(db, t: BankTxn, pays: list[Payment]):
    for p in pays:
        p.bank_txn_id = t.id
    t.status = "matched"


def linked_payments(db, t: BankTxn) -> list[Payment]:
    return [p for p in db.scalars(select(Payment).where(Payment.bank_txn_id == t.id)) if O.active(db, "payment", p.id)]


def _subset(pays: list[Payment], target: float) -> list[Payment]:
    """同日同单位的收付款里找合计等于流水额的组合（先单笔，再 2~4 笔；一笔银行款拆到几张合同的情形）。"""
    from itertools import combinations
    for k in range(1, min(4, len(pays)) + 1):
        for combo in combinations(pays, k):
            if abs(sum(p.amount for p in combo) - target) < EPS:
                return list(combo)
    return []


def auto_match(db) -> dict:
    matched = ignored = 0
    txns = list(db.scalars(select(BankTxn).where(BankTxn.status == "unmatched").order_by(BankTxn.txn_date, BankTxn.id)))
    # 冲正：同日同户名一正一负同额，其一业务类型为冲正
    for t in txns:
        if t.status != "unmatched" or "冲正" not in t.biz_type:
            continue
        mate = next((x for x in txns if x is not t and x.status == "unmatched" and x.txn_date == t.txn_date
                     and abs(x.amount + t.amount) < EPS and norm_name(x.counterparty_name) == norm_name(t.counterparty_name)), None)
        if mate:
            for x in (t, mate):
                x.status, x.note = "ignored", f"冲正对冲（{t.txn_no} ↔ {mate.txn_no}）"
            ignored += 2
    open_pays = _open_payments(db)
    for t in txns:
        if t.status != "unmatched":
            continue
        partner = partner_for(db, t)
        if not partner:
            continue
        direction = "receive" if t.amount > 0 else "pay"
        same = [p for p in open_pays if p.partner_id == partner.id and p.direction == direction and p.pay_date == t.txn_date]
        pick = _subset(same, abs(t.amount))
        if pick:
            _link(db, t, pick)
            open_pays = [p for p in open_pays if p not in pick]
            matched += 1
    db.flush()
    return {"matched": matched, "ignored": ignored}


def candidates(db, t: BankTxn, days: int = 20) -> list[Payment]:
    direction = "receive" if t.amount > 0 else "pay"
    partner = partner_for(db, t)
    res = []
    for p in _open_payments(db):
        if p.direction != direction:
            continue
        near = abs((p.pay_date - t.txn_date).days) <= days
        if (partner and p.partner_id == partner.id and near) or (near and abs(p.amount - abs(t.amount)) < EPS):
            res.append(p)
    return sorted(res, key=lambda p: (abs((p.pay_date - t.txn_date).days), p.id))


# ---------------- 月末核对 ----------------

def reconcile(db) -> list[dict]:
    """每个账户、每月：流水月末余额 vs 账面银行存款（该账户对应的 1002xx 科目月末余额）。"""
    from .company import bank_account_code
    out = []
    accounts = sorted({t.account_no for t in db.scalars(select(BankTxn))})
    for acc_no in accounts:
        for r in _reconcile_one(db, acc_no, bank_account_code(db, acc_no)):
            out.append({**r, "account_no": acc_no})
    return out


def _reconcile_one(db, acc_no: str, acc_code: str) -> list[dict]:
    last = {}
    for t in db.scalars(select(BankTxn).where(BankTxn.account_no == acc_no).order_by(BankTxn.txn_date, BankTxn.txn_time, BankTxn.id)):
        m = str(t.txn_date)[:7]
        last[m] = t
    months = sorted(last)
    out = []
    prev_bal = 0.0
    for m in months:
        t = last[m]
        y, mo = int(m[:4]), int(m[5:])
        end = date(y + (mo == 12), mo % 12 + 1, 1)
        stmt = t.balance if t.balance is not None else None
        # 只按日期取当月流水之和（交易后余额在同日多笔时顺序不可靠，以合计为准）
        flow = sum(x.amount for x in db.scalars(select(BankTxn).where(BankTxn.account_no == acc_no, BankTxn.txn_date >= date(y, mo, 1),
                                                                    BankTxn.txn_date < end)))
        calc = round(prev_bal + flow, 2)
        prev_bal = calc
        book = F.account_balance(db, acc_code, None, date.fromordinal(end.toordinal() - 1))
        counts = defaultdict(int)
        for x in db.scalars(select(BankTxn).where(BankTxn.account_no == acc_no, BankTxn.txn_date >= date(y, mo, 1), BankTxn.txn_date < end)):
            counts[x.status] += 1
        out.append({"month": m, "statement": calc, "last_balance": stmt, "book": book,
                    "gap": round(calc - book, 2),
                    "unmatched": counts["unmatched"], "total": sum(counts.values())})
    return out


# ---------------- 入账 ----------------

def post_txn(db, t: BankTxn, data: PostIn) -> Voucher:
    if t.status not in ("unmatched",):
        raise ValueError("只有未处理的流水可以入账（已匹配收付款的请先取消匹配）")
    total = round(sum(l.amount for l in data.lines), 2)
    if abs(total - abs(t.amount)) > EPS:
        raise ValueError(f"对方科目合计 {total:.2f} ≠ 流水金额 {abs(t.amount):.2f}")
    from .company import bank_account_code
    for l in data.lines:
        if l.account == bank_account_code(db, t.account_no):
            raise ValueError("对方科目不能是银行存款本身")
    receive = t.amount > 0
    summary = data.summary or f"{t.counterparty_name or t.biz_type} {t.purpose or t.memo}".strip()
    bank_acc = bank_account_code(db, t.account_no)
    lines = [dict(account=bank_acc, debit=abs(t.amount) if receive else 0, credit=0 if receive else abs(t.amount),
                  summary=summary)]
    for l in data.lines:
        lines.append(dict(account=l.account, debit=0 if receive else l.amount, credit=l.amount if receive else 0,
                          partner_id=l.partner_id, cost_center_id=l.cost_center_id, summary=l.summary or summary))
    v = F.create_voucher(db, t.txn_date, "bank", t.id, f"银行流水 {summary}", lines)
    t.voucher_id, t.status, t.category = v.id, "posted", data.category
    O.audit(db, "银行流水入账", bank_txn_id=t.id, voucher_id=v.id, category=data.category)
    return v


def unpost_txn(db, t: BankTxn, reason: str):
    if t.status != "posted" or not t.voucher_id:
        raise ValueError("流水未入账")
    v = db.get(Voucher, t.voucher_id)
    if v and v.status == "posted":
        F.reverse_voucher(db, v, reason or "撤销流水入账")
    t.voucher_id, t.status, t.category = None, "unmatched", ""
    O.audit(db, "撤销流水入账", bank_txn_id=t.id, voucher_id=v.id if v else None, reason=reason)


# ---------------- API ----------------

def _row(db, t: BankTxn, pays=None, vno=None) -> dict:
    pays = pays if pays is not None else linked_payments(db, t)
    return {"id": t.id, "txn_no": t.txn_no, "txn_date": str(t.txn_date), "txn_time": t.txn_time,
            "amount": t.amount, "direction": "receive" if t.amount > 0 else "pay",
            "income": t.amount if t.amount > 0 else None, "expense": -t.amount if t.amount < 0 else None,
            "balance": t.balance, "counterparty_name": t.counterparty_name,
            "counterparty_account": t.counterparty_account, "counterparty_bank": t.counterparty_bank,
            "biz_type": t.biz_type, "summary": t.summary, "purpose": t.purpose, "memo": t.memo,
            "status": t.status, "status_label": STATUS_LABELS.get(t.status, t.status), "category": t.category,
            "voucher_id": t.voucher_id, "voucher_no": vno, "note": t.note, "source_file": t.source_file,
            "payments": [{"id": p.id, "amount": p.amount, "pay_date": str(p.pay_date),
                          "contract_id": p.contract_id, "contract_no": p.contract.contract_no if p.contract else ""}
                         for p in pays]}


def install(app, get_db):
    def _get(db, id_):
        t = db.get(BankTxn, id_)
        if not t:
            raise HTTPException(404, "流水不存在")
        return t

    @app.get("/api/bank/txns")
    def list_txns(status: str = "", db=Depends(get_db)):
        sel = select(BankTxn).order_by(BankTxn.txn_date.desc(), BankTxn.txn_time.desc(), BankTxn.id.desc())
        if status:
            sel = sel.where(BankTxn.status == status)
        pays = defaultdict(list)
        for p in db.scalars(select(Payment).where(Payment.bank_txn_id.is_not(None))):
            if O.active(db, "payment", p.id):
                pays[p.bank_txn_id].append(p)
        vnos = dict(db.execute(select(Voucher.id, Voucher.voucher_no).where(Voucher.source_type == "bank")).all())
        return [_row(db, t, pays.get(t.id, []), vnos.get(t.voucher_id)) for t in db.scalars(sel)]

    @app.post("/api/bank/import")
    def import_api(data: ImportIn, db=Depends(get_db)):
        account, rows = parse_boc_csv(data.content)
        from .company import get as company
        accs = [a.get("account_no", "").replace(" ", "") for a in company(db).get("bank_accounts") or []]
        if accs and account and account not in accs:
            raise ValueError(f"流水账号 {account} 不在「本单位设置」的开户账户里（{', '.join(accs)}），先去系统→本单位设置添加")
        res = import_rows(db, rows, data.filename)
        res.update(auto_match(db))
        O.audit(db, "导入银行流水", file=data.filename, **{k: v for k, v in res.items()})
        db.commit()
        return res

    @app.post("/api/bank/auto-match")
    def auto_match_api(db=Depends(get_db)):
        res = auto_match(db)
        db.commit()
        return res

    @app.get("/api/bank/txns/{id_}/candidates")
    def candidates_api(id_: int, db=Depends(get_db)):
        t = _get(db, id_)
        return [{"id": p.id, "pay_date": str(p.pay_date), "amount": p.amount, "direction": p.direction,
                 "partner_short": (p.partner.short_name or p.partner.name) if p.partner else "",
                 "contract_no": p.contract.contract_no if p.contract else "", "remark": p.remark}
                for p in candidates(db, t)]

    @app.post("/api/bank/txns/{id_}/link")
    def link_api(id_: int, data: LinkIn, db=Depends(get_db)):
        t = _get(db, id_)
        if t.status not in ("unmatched", "matched"):
            raise ValueError("已入账或已忽略的流水不能再关联收付款")
        pays = linked_payments(db, t)
        for pid in data.payment_ids:
            p = db.get(Payment, pid)
            if not p or not O.active(db, "payment", pid) or (p.bank_txn_id and p.bank_txn_id != t.id):
                raise ValueError(f"收付款 #{pid} 无效或已关联其他流水")
            if p.direction != ("receive" if t.amount > 0 else "pay"):
                raise ValueError(f"收付款 #{pid} 方向与流水不一致")
            if p not in pays:
                pays.append(p)
        total = round(sum(p.amount for p in pays), 2)
        if abs(total - abs(t.amount)) > EPS:
            raise ValueError(f"关联收付款合计 {total:.2f} ≠ 流水金额 {abs(t.amount):.2f}；金额不符的请先在收付款页拆分或补登")
        _link(db, t, pays)
        O.audit(db, "流水关联收付款", bank_txn_id=t.id, payment_ids=[p.id for p in pays])
        db.commit()
        return {"ok": True}

    @app.post("/api/bank/txns/{id_}/unlink")
    def unlink_api(id_: int, db=Depends(get_db)):
        t = _get(db, id_)
        if t.status != "matched":
            raise ValueError("流水未关联收付款")
        for p in linked_payments(db, t):
            p.bank_txn_id = None
        t.status = "unmatched"
        db.commit()
        return {"ok": True}

    @app.post("/api/bank/txns/{id_}/ignore")
    def ignore_api(id_: int, data: NoteIn, db=Depends(get_db)):
        t = _get(db, id_)
        if t.status != "unmatched":
            raise ValueError("只有未处理的流水可以忽略")
        if not data.note:
            raise ValueError("请写明忽略原因")
        t.status, t.note = "ignored", data.note
        db.commit()
        return {"ok": True}

    @app.post("/api/bank/txns/{id_}/reopen")
    def reopen_api(id_: int, db=Depends(get_db)):
        t = _get(db, id_)
        if t.status != "ignored":
            raise ValueError("只有已忽略的流水可以恢复")
        t.status, t.note = "unmatched", ""
        db.commit()
        return {"ok": True}

    @app.post("/api/bank/txns/{id_}/post")
    def post_api(id_: int, data: PostIn, db=Depends(get_db)):
        t = _get(db, id_)
        v = post_txn(db, t, data)
        db.commit()
        return {"voucher_id": v.id, "voucher_no": v.voucher_no}

    @app.post("/api/bank/txns/{id_}/unpost")
    def unpost_api(id_: int, data: NoteIn, db=Depends(get_db)):
        t = _get(db, id_)
        unpost_txn(db, t, data.note)
        db.commit()
        return {"ok": True}

    @app.get("/api/bank/reconcile")
    def reconcile_api(db=Depends(get_db)):
        return reconcile(db)
