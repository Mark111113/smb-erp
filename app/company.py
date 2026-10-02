"""本单位设置（v0.29）：名称、税号、地址、开户行（可多个，各对应一个 1002xx 银行存款科目）、执行的会计准则。

原先写死在代码里的税号、名称、100201 都改读这里（发票方向判断、报表抬头、银行流水账号 → 科目）。
首次启动从「本单位」往来单位（代码见 partner_code）和科目 100201 带出初值。页面：系统 → 本单位设置。
"""
from typing import Optional

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Integer, select
from sqlalchemy.orm import Mapped, mapped_column

from . import operations as O
from .models import Account, Base, Partner

DEFAULT = {"name": "", "short_name": "", "tax_no": "", "address": "", "phone": "", "legal_rep": "",
           "accounting_standard": "小企业会计准则", "partner_code": "SELF", "bank_accounts": []}


class CompanySetting(Base):
    __tablename__ = "company_setting"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class BankAccountIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    bank: str = Field(min_length=1)
    account_no: str = Field(min_length=4)
    account_code: str = Field(min_length=4)      # 对应的银行存款末级科目，如 100201
    default: bool = False


class CompanyIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=2)
    short_name: str = ""
    tax_no: str = Field(min_length=15, max_length=20)
    address: str = ""
    phone: str = ""
    legal_rep: str = ""
    accounting_standard: str = "小企业会计准则"
    partner_code: str = "SELF"
    bank_accounts: list[BankAccountIn] = Field(min_length=1)


def get(db) -> dict:
    row = db.get(CompanySetting, 1)
    return {**DEFAULT, **(row.data if row else {})}


def ensure(session_factory):
    with session_factory() as db:
        if db.get(CompanySetting, 1):
            return
        p = db.scalar(select(Partner).where(Partner.code == DEFAULT["partner_code"]))
        data = dict(DEFAULT)
        if p:
            data.update(name=p.name, short_name=p.short_name or "", tax_no=p.tax_no or "", address=p.address or "",
                        phone=p.phone or "")
            if p.bank_account:
                acc = db.get(Account, "100201")
                data["bank_accounts"] = [{"bank": p.bank_name or (acc.name if acc else ""), "account_no": p.bank_account.replace(" ", ""),
                                          "account_code": "100201", "default": True}]
        db.add(CompanySetting(id=1, data=data))
        db.commit()


def own_tax_no(db) -> str:
    return get(db).get("tax_no") or ""


def own_names(db) -> list[str]:
    c = get(db)
    return [x for x in (c.get("name"), c.get("short_name")) if x]


def bank_account_code(db, account_no: str = "") -> str:
    """流水账号 → 银行存款科目；找不到就用默认账户。"""
    from . import finance as F
    accs = get(db).get("bank_accounts") or []
    no = (account_no or "").replace(" ", "")
    for a in accs:
        if no and a.get("account_no", "").replace(" ", "") == no:
            return a["account_code"]
    d = next((a for a in accs if a.get("default")), accs[0] if accs else None)
    return d["account_code"] if d else F.ACC_BANK


def install(app, get_db):
    @app.get("/api/company")
    def get_company(db=Depends(get_db)):
        return get(db)

    @app.put("/api/company")
    def put_company(data: CompanyIn, db=Depends(get_db)):
        for a in data.bank_accounts:
            acc = db.get(Account, a.account_code)
            if not acc or not acc.active or not a.account_code.startswith("1002"):
                raise ValueError(f"银行账户 {a.account_no} 对应的科目 {a.account_code} 须为有效的 1002xx 银行存款科目")
            if db.scalar(select(Account.code).where(Account.parent_code == a.account_code).limit(1)):
                raise ValueError(f"{a.account_code} 不是末级科目")
        body = data.model_dump()
        if not any(a["default"] for a in body["bank_accounts"]):
            body["bank_accounts"][0]["default"] = True
        row = db.get(CompanySetting, 1) or CompanySetting(id=1)
        body["seals"] = get(db).get("seals") or {}      # 印章档案在「印章」里维护，这里不覆盖
        body["office_ips"] = get(db).get("office_ips") or []   # 办公室公网地址在「用户管理」里维护（安全设置，仅管理员）
        row.data = body
        db.add(row)
        O.audit(db, "修改本单位设置")
        db.commit()
        return {"ok": True}
