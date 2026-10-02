"""发票档案（v0.26）：导入发票文件与税务系统导出、归档存储、关联入账、抵扣标记、增值税台账。

归档：文件存 `<DB 目录>/invoice-archive/{in|out}/{YYYY-MM}/{发票号}_{对方}.{ext}`（可用 OWE_ARCHIVE 覆盖根目录），
库里 invoice_file 记相对路径与 sha256（同一文件重复上传不重复存）。一张票可以有 PDF+OFD+XML 多个文件。

解析优先级：数电 XML（最准）> PDF 文字层（数电版式固定）> 文件名里的 20 位票号（OFD、图片只归档不解析）。
税务导出：
- 「全量发票查询导出结果」：按票号汇总明细行，补齐/新建档案（红冲关系、状态）；
- 「用途确认信息 / 抵扣勾选」：按「税款所属期」给已勾选的进项票打 deducted，写有效抵扣税额。
抵扣口径（与增值税申报一致）：进项专票入账时记 222104 待认证进项税额；档案标记已勾选后，synchronize
在所属期末出凭证 借 222102 / 贷 222104；进项转出 借 费用 / 贷 222105。
"""
import base64
import hashlib
import io
import os
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from . import finance as F
from . import operations as O
from .models import Invoice, InvoiceDoc, InvoiceFile, Partner, Voucher, VoucherLine

def own_info(db) -> dict:
    """本单位名称/税号（v0.29 起读 本单位设置，不再写死）。"""
    from .company import get
    c = get(db)
    return {"name": c.get("name") or "", "tax": c.get("tax_no") or "",
            "keys": [x for x in (c.get("name"), c.get("short_name")) if x]}


NO_OWN = {"name": "", "tax": "", "keys": []}
ACC_VAT_PENDING = "222104"
ACC_VAT_TRANSFER_OUT = "222105"
CATEGORIES = {"trade": "贸易", "expense": "费用", "prepay": "工程/预付", "asset": "固定资产", "none": "不入账"}
DEDUCT_LABELS = {"": "—", "pending": "未勾选", "deducted": "已勾选", "nondeductible": "不可抵扣", "transferred": "已转出"}
STATUS_LABELS = {"normal": "正常", "reversed": "已红冲", "red": "红字"}


class _In(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, str_strip_whitespace=True)


class UploadIn(_In):
    name: str = Field(min_length=1)
    content_b64: str = Field(min_length=1)
    direction: str = Field(default="in", pattern=r"^(in|out)$")   # 收到的票 / 开出的票


class RegisterIn(_In):
    contract_id: Optional[int] = None     # 贸易票挂的合同（同单位同方向的有效合同）
    partner_id: Optional[int] = None      # 档案没认出单位时手选


class DocPatch(_In):
    voucher_no: Optional[str] = None          # 页面上按凭证号关联（空串=取消）
    category: Optional[str] = None
    partner_id: Optional[int] = None
    voucher_id: Optional[int] = None
    invoice_id: Optional[int] = None
    booked_month: Optional[str] = Field(default=None, pattern=r"^(\d{4}-\d{2})?$")
    deduct_status: Optional[str] = None
    deduct_period: Optional[str] = Field(default=None, pattern=r"^(\d{4}-\d{2})?$")
    deduct_tax: Optional[float] = None
    transfer_period: Optional[str] = Field(default=None, pattern=r"^(\d{4}-\d{2})?$")
    transfer_account: Optional[str] = None
    note: Optional[str] = None


def archive_root() -> Path:
    from .paths import invoice_archive
    return invoice_archive()


# ---------------- 解析 ----------------

def _f(s) -> float:
    try:
        return round(float(str(s).replace(",", "").replace("¥", "").strip()), 2)
    except (TypeError, ValueError):
        return 0.0


def parse_xml(data: bytes) -> Optional[dict]:
    """数电发票 XML（EInvoice）。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    g = lambda tag: (root.findtext(f".//{tag}") or "").strip()
    no = g("EIid") or g("InvoiceNumber")
    if not re.fullmatch(r"\d{20}", no or ""):
        return None
    kind_name = g("GeneralOrSpecialVAT/LabelName") or ""
    t = g("RequestTime") or g("IssueTime")
    rates = sorted({f"{round(float(r) * 100):g}%" for r in re.findall(r"<TaxRate>([\d.]+)</TaxRate>", data.decode("utf-8", "ignore"))})
    items = [e.text for e in root.iter("ItemName") if e.text]
    blue = g("InIssuType/LabelCode")
    return dict(invoice_no=no, kind="数电" + ("专票" if "专用" in kind_name else "普票"),
                issue_date=t[:10] if t else None,
                seller_name=g("SellerName"), seller_tax_no=g("SellerIdNum"),
                buyer_name=g("BuyerName"), buyer_tax_no=g("BuyerIdNum"),
                amount_ex_tax=_f(g("TotalAmWithoutTax")), tax_amount=_f(g("TotalTaxAm")),
                amount_tax=_f(g("TotalTax-includedAmount")), tax_rates=",".join(rates),
                items="；".join(dict.fromkeys(items))[:500], face_remark=g("Remark")[:500],
                red=(blue == "N"))


NAME_RE = re.compile(r"[\u4e00-\u9fa5][\u4e00-\u9fa5（）()·]{1,40}?(?:有限责任公司|股份有限公司|有限公司|分公司|公司|经营部|超市|商行|门市部|事务所|中心|店)")


def parse_pdf_text(text: str, direction: str = "in", own: dict = NO_OWN) -> Optional[dict]:
    """数电发票 PDF 文字层。版式有好几种（文字顺序不固定），所以不靠位置：
    本单位税号定「我方」，另一个 18 位税号和非本单位的公司名是对方。方向无法从版式可靠判断
    （文件名里有时是购方有时是销方），由调用方给（页面上传时选「收到的票/开出的票」），
    导入后再由同号 ERP 发票或税务导出纠正。"""
    flat = re.sub(r"\s+", "", text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    no = next((l for l in lines if re.fullmatch(r"\d{20}", l)), None)
    if not no:
        m = re.search(r"(?<!\d)(\d{20})(?!\d)", flat)
        no = m.group(1) if m else None
    if not no:
        return None
    dm = re.search(r"(\d{4})年(\d{2})月(\d{2})日", flat)
    ids = [x for x in re.findall(r"(?<![0-9A-Z])([0-9A-Z]{18})(?![0-9A-Z])", text) if not x.isdigit() or len(x) != 18 or x != no[:18]]
    other_id = next((x for x in ids if x != own["tax"]), "")
    def ok(n):
        return not any(k and (k in n or n in k) for k in own["keys"]) and "税务" not in n and "监制" not in n and "银行" not in n
    names = []
    for l in lines:                                    # 先按行：整行是名称，或「名称：xxx」
        l2 = re.sub(r"^.*?名称[：:]\s*", "", l).strip()
        if NAME_RE.fullmatch(l2) and ok(l2):
            names.append(l2)
    if not names:
        names = [n.lstrip("年月日") for n in NAME_RE.findall(flat) if ok(n)]
    other = names[0] if names else ""
    money = [_f(x) for x in re.findall(r"¥\s*(-?[\d,]+\.\d{2})", text)]
    total = max(money, key=abs) if money else 0.0
    rest = [x for x in money if x != total]
    net, tax = (rest[0], rest[1]) if len(rest) >= 2 else (total, 0.0)
    if abs(net + tax - total) > 0.02 and len(rest) >= 2:       # 顺序不对时按「两数之和=合计」重配
        pairs = [(a, b) for a in rest for b in rest if a is not b and abs(a + b - total) < 0.02 and abs(a) >= abs(b)]
        if pairs:
            net, tax = pairs[0]
    rates = sorted(set(re.findall(r"(\d{1,2})%", text)), key=int)
    items = list(dict.fromkeys(m.group(0) for m in re.finditer(r"\*[^*\s]{1,20}\*[^\s\d¥]{1,30}", flat)))
    kind = "数电专票" if "专用发票" in flat else ("数电普票" if "普通发票" in flat else "")
    remark = " ".join(l for l in lines if re.search(r"合同|OYL|CF\d{4}|BDR\d{4}", l) and not l.startswith("*"))[:300]
    ours = dict(name=own["name"], tax=own["tax"])
    them = dict(name=other, tax=other_id)
    buyer, seller = (ours, them) if direction == "in" else (them, ours)
    return dict(invoice_no=no, kind=kind, issue_date=f"{dm[1]}-{dm[2]}-{dm[3]}" if dm else None, direction=direction,
                buyer_name=buyer["name"], buyer_tax_no=buyer["tax"], seller_name=seller["name"], seller_tax_no=seller["tax"],
                amount_ex_tax=round(net, 2), tax_amount=round(tax, 2), amount_tax=round(total, 2),
                tax_rates=",".join(f"{r}%" for r in rates), items="；".join(items)[:500],
                face_remark=remark, red=total < 0)


def parse_pdf(data: bytes, direction: str = "in", own: dict = NO_OWN) -> Optional[dict]:
    try:
        from pypdf import PdfReader
        text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None
    return parse_pdf_text(text, direction, own)


def no_from_name(name: str) -> Optional[str]:
    m = re.search(r"(?<!\d)(\d{20})(?!\d)", name)
    return m.group(1) if m else None


# ---------------- 存储与合并 ----------------

def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "", s or "")[:40]


def _store(doc: InvoiceDoc, name: str, data: bytes) -> str:
    ext = (Path(name).suffix.lower().lstrip(".") or "bin")[:8]
    month = str(doc.issue_date)[:7] if doc.issue_date else "unknown"
    other = doc.seller_name if doc.direction == "in" else doc.buyer_name
    rel = Path(doc.direction or "unknown") / month / f"{doc.invoice_no}_{_safe(other)}.{ext}"
    root = archive_root()
    target = root / rel
    n = 1
    while target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
        n += 1
        rel = rel.with_name(f"{doc.invoice_no}_{_safe(other)}_{n}.{ext}")
        target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    return rel.as_posix()


def _direction(p: dict, own: dict = NO_OWN) -> str:
    if own["tax"] and p.get("buyer_tax_no") == own["tax"]:
        return "in"
    if own["tax"] and p.get("seller_tax_no") == own["tax"]:
        return "out"
    if any(k in (p.get("buyer_name") or "") for k in own["keys"]):
        return "in"
    if any(k in (p.get("seller_name") or "") for k in own["keys"]):
        return "out"
    return ""


def upsert_doc(db, parsed: dict, source: str, prefer: bool = False) -> InvoiceDoc:
    """按票号新建或补齐档案。prefer=True（XML/税务导出）时覆盖已有票面字段。"""
    doc = db.scalar(select(InvoiceDoc).where(InvoiceDoc.invoice_no == parsed["invoice_no"]))
    new = doc is None
    if new:
        doc = InvoiceDoc(invoice_no=parsed["invoice_no"], direction="", source=source)
        db.add(doc)
    for k in ("kind", "seller_name", "seller_tax_no", "buyer_name", "buyer_tax_no", "tax_rates", "items", "face_remark"):
        v = parsed.get(k)
        if v and (prefer or not getattr(doc, k)):
            setattr(doc, k, v)
    if parsed.get("issue_date") and (prefer or not doc.issue_date):
        doc.issue_date = date.fromisoformat(str(parsed["issue_date"])[:10])
    for k in ("amount_ex_tax", "tax_amount", "amount_tax"):
        v = parsed.get(k)
        if v is not None and (prefer or not getattr(doc, k)):
            setattr(doc, k, round(v, 2))
    if parsed.get("red"):
        doc.status = "red"
    if parsed.get("red_of"):
        doc.red_of = parsed["red_of"]
    if parsed.get("status") and parsed["status"] != "normal":
        doc.status = parsed["status"]
    by_tax = _direction({"buyer_tax_no": doc.buyer_tax_no, "seller_tax_no": doc.seller_tax_no,
                         "buyer_name": doc.buyer_name, "seller_name": doc.seller_name}, own_info(db))
    if prefer and by_tax and doc.direction and doc.direction != by_tax:
        doc.direction = by_tax                 # 税务导出的购销方税号是准的，已按导出覆盖
    elif prefer and by_tax:
        doc.direction = by_tax
    doc.direction = doc.direction or parsed.get("direction") or by_tax
    if doc.direction == "in" and not doc.deduct_status:
        doc.deduct_status = "pending" if "专" in (doc.kind or "") else "nondeductible"
    db.flush()
    return doc


def add_file(db, doc: InvoiceDoc, name: str, data: bytes) -> bool:
    sha = hashlib.sha256(data).hexdigest()
    if any(f.sha256 == sha for f in doc.files):
        return False
    rel = _store(doc, name, data)
    doc.files.append(InvoiceFile(file_name=name, stored_path=rel, file_type=(Path(name).suffix.lower().lstrip(".") or "bin")[:8],
                                 size=len(data), sha256=sha))
    db.flush()
    return True


def import_file(db, name: str, data: bytes, direction: str = "in") -> list[dict]:
    """一个上传文件 → 一条或多条结果（zip 展开）。"""
    ext = Path(name).suffix.lower()
    if ext == ".zip":
        out = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for m in z.namelist():
                if not m.endswith("/"):
                    out += import_file(db, Path(m).name, z.read(m), direction)
        return out
    parsed = parse_xml(data) if ext == ".xml" else parse_pdf(data, direction, own_info(db)) if ext == ".pdf" else None
    prefer = ext == ".xml"
    if not parsed:
        no = no_from_name(name)
        if not no:
            return [{"file": name, "ok": False, "msg": "识别不出发票号（文件名里也没有 20 位票号）"}]
        doc = db.scalar(select(InvoiceDoc).where(InvoiceDoc.invoice_no == no))
        if not doc:
            return [{"file": name, "ok": False, "msg": f"{ext} 文件只归档不解析；请先导入同号的 PDF/XML（{no}）"}]
    else:
        doc = upsert_doc(db, parsed, "file", prefer=prefer)
    added = add_file(db, doc, name, data)
    auto_link(db, doc)
    return [{"file": name, "ok": True, "invoice_no": doc.invoice_no, "id": doc.id, "new_file": added,
             "direction": doc.direction, "amount_tax": doc.amount_tax}]


# ---------------- 关联入账 ----------------

def _partner(db, doc: InvoiceDoc) -> Optional[Partner]:
    tax_no, name = (doc.seller_tax_no, doc.seller_name) if doc.direction == "in" else (doc.buyer_tax_no, doc.buyer_name)
    if tax_no:
        p = db.scalar(select(Partner).where(Partner.tax_no == tax_no))
        if p:
            return p
    if name:
        from .bank import norm_name
        key = norm_name(name)
        for p in db.scalars(select(Partner)):
            if norm_name(p.name) == key:
                return p
    return None


def _flip(doc: InvoiceDoc, direction: str):
    """方向判错时对调购销方（PDF 解析按调用方给的方向填了购销方）。"""
    doc.direction = direction
    doc.seller_name, doc.buyer_name = doc.buyer_name, doc.seller_name
    doc.seller_tax_no, doc.buyer_tax_no = doc.buyer_tax_no, doc.seller_tax_no
    if direction == "out":
        doc.deduct_status = ""
    elif not doc.deduct_status:
        doc.deduct_status = "pending" if "专" in (doc.kind or "") else "nondeductible"


def _guess_category(v: Voucher) -> str:
    accs = {l.account_code for l in v.lines}
    if any(a.startswith("1601") for a in accs):
        return "asset"
    if "1123" in accs:
        return "prepay"
    return "expense"


def auto_link(db, doc: InvoiceDoc):
    if doc.status in ("reversed", "red") and not doc.invoice_id:
        doc.category = doc.category or "none"
    if not doc.partner_id:
        p = _partner(db, doc)
        if p:
            doc.partner_id = p.id
    if not doc.invoice_id:
        inv = db.scalar(select(Invoice).where(Invoice.invoice_no == doc.invoice_no))
        if inv and O.active(db, "invoice", inv.id) and doc.direction != ("in" if inv.direction == "input" else "out"):
            _flip(doc, "in" if inv.direction == "input" else "out")
        if inv and O.active(db, "invoice", inv.id):
            doc.invoice_id, doc.category = inv.id, "trade"
            doc.booked_month = str(inv.invoice_date)[:7] if inv.invoice_date else doc.booked_month
            doc.partner_id = doc.partner_id or inv.partner_id
    if not doc.invoice_id and not doc.voucher_id:
        v = db.scalar(select(Voucher).join(VoucherLine, VoucherLine.voucher_id == Voucher.id)
                      .where(Voucher.status == "posted",
                             or_(Voucher.summary.contains(doc.invoice_no), VoucherLine.summary.contains(doc.invoice_no)))
                      .order_by(Voucher.voucher_date).limit(1))
        if v:
            doc.voucher_id, doc.booked_month = v.id, str(v.voucher_date)[:7]
            doc.category = doc.category or _guess_category(v)
    db.flush()


# ---------------- 税务导出 ----------------

def _xlsx_rows(data: bytes):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)   # 别用 read_only：税局导出的 dimension 元数据不对，只读模式只读到表头
    return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb}


def import_tax_export(db, name: str, data: bytes) -> dict:
    sheets = _xlsx_rows(data)
    # 用途确认 / 抵扣勾选：sheet「发票」或第一张，含「勾选状态」「数电发票号码」表头；「税款所属期」在表头上方
    for title, rows in sheets.items():
        rows = [[(str(v).rstrip("*") if isinstance(v, str) else v) for v in r] for r in rows]   # 表头带必填星号
        hi = next((i for i, r in enumerate(rows[:10]) if r and ("勾选状态" in r or "是否勾选" in r) and "数电发票号码" in r), None)
        if hi is None:
            continue
        period = ""
        for r in rows[:hi]:
            if r and "税款所属期" in r:
                v = str(r[r.index("税款所属期") + 1] or "")
                period = f"{v[:4]}-{v[4:6]}" if re.fullmatch(r"\d{6}", v) else ""
        h = rows[hi]
        n = created = 0
        listed = []
        for r in rows[hi + 1:]:
            d = dict(zip(h, r))
            no = str(d.get("数电发票号码") or "").strip()
            if not re.fullmatch(r"\d{20}", no):
                continue
            doc = db.scalar(select(InvoiceDoc).where(InvoiceDoc.invoice_no == no))
            if not doc:
                doc = upsert_doc(db, dict(invoice_no=no, issue_date=str(d.get("开票日期") or "")[:10] or None,
                                          seller_name=d.get("销售方纳税人名称") or "", seller_tax_no=d.get("销售方纳税人识别号") or "",
                                          buyer_tax_no=own_info(db)["tax"], buyer_name=own_info(db)["name"],
                                          amount_ex_tax=_f(d.get("金额")), tax_amount=_f(d.get("税额") or d.get("票面税额")),
                                          amount_tax=round(_f(d.get("金额")) + _f(d.get("税额") or d.get("票面税额")), 2),
                                          kind="数电专票" if "专用" in str(d.get("票种") or "") else "数电普票"), "tax_export")
                created += 1
            doc.direction = "in"                          # 勾选清单里的都是进项
            if doc.deduct_status in ("", "nondeductible") and "专" in (doc.kind or ""):
                doc.deduct_status = "pending"
            picked = str(d.get("勾选状态") or "").startswith("已勾选") or str(d.get("是否勾选") or "") == "是"
            if picked and period:
                doc.deduct_status, doc.deduct_period = "deducted", period
                doc.deduct_time = str(d.get("勾选时间") or "")[:19]
                doc.deduct_tax = _f(d.get("有效抵扣税额") or d.get("税额"))
                n += 1
            elif not picked and doc.deduct_status == "":
                doc.deduct_status = "pending"
            listed.append(no)
            auto_link(db, doc)
        db.flush()
        pending_here = [x for x in listed if (db.scalar(select(InvoiceDoc).where(InvoiceDoc.invoice_no == x)) or InvoiceDoc()).deduct_status == "pending"]
        return {"type": "勾选/用途确认" if period else "抵扣勾选清单（未勾选）", "period": period, "deducted": n,
                "created": created, "listed": len(listed), "pending": len(pending_here)}
    # 全量发票查询导出：明细行，按票号汇总
    for title, rows in sheets.items():
        if not rows or not rows[0] or "数电发票号码" not in rows[0] or "销方识别号" not in rows[0]:
            continue
        h = rows[0]
        agg = {}
        for r in rows[1:]:
            d = dict(zip(h, r))
            no = str(d.get("数电发票号码") or d.get("发票号码") or "").strip()
            if not re.fullmatch(r"\d{20}", no):
                continue
            x = agg.setdefault(no, dict(invoice_no=no, issue_date=str(d.get("开票日期") or "")[:10] or None,
                                        seller_name=d.get("销方名称") or "", seller_tax_no=d.get("销方识别号") or "",
                                        buyer_name=d.get("购买方名称") or "", buyer_tax_no=d.get("购方识别号") or "",
                                        amount_ex_tax=0.0, tax_amount=0.0, amount_tax=0.0, items=[], rates=set(),
                                        kind="数电专票" if "专用" in str(d.get("发票票种") or "") else "数电普票",
                                        status="reversed" if "红冲" in str(d.get("发票状态") or "") else "normal",
                                        red=str(d.get("是否正数发票") or "") == "否", face_remark=str(d.get("备注") or "")))
            x["amount_ex_tax"] += _f(d.get("金额"))
            x["tax_amount"] += _f(d.get("税额"))
            x["amount_tax"] += _f(d.get("价税合计"))
            x["items"].append(str(d.get("货物或应税劳务名称") or ""))
            x["rates"].add(str(d.get("税率") or ""))
            m = re.search(r"被红冲蓝字数电发票号码：(\d{20})", x["face_remark"])
            if m:
                x["red_of"] = m.group(1)
        for x in agg.values():
            x["items"] = "；".join(dict.fromkeys(i for i in x["items"] if i))[:500]
            x["tax_rates"] = ",".join(sorted(r for r in x.pop("rates") if r))
            for k in ("amount_ex_tax", "tax_amount", "amount_tax"):
                x[k] = round(x[k], 2)
            doc = upsert_doc(db, x, "tax_export", prefer=True)
            auto_link(db, doc)
        return {"type": "全量发票查询", "invoices": len(agg)}
    if all(sum(1 for r in rows if any(v not in (None, "") for v in r)) <= 1 for rows in sheets.values()):
        return {"type": "空表（只有表头）", "invoices": 0}
    raise ValueError("认不出的 xlsx：支持「全量发票查询导出结果」和「用途确认信息 / 抵扣勾选」")


# ---------------- 增值税台账 ----------------

def vat_ledger(db) -> list[dict]:
    """按月：销项（222101 贷方净额）、进项（222102 借方净额=已勾选）、进项转出（222105）、留抵、应纳、实缴（222103）。"""
    sums = defaultdict(lambda: defaultdict(float))
    for code, d, dr, cr in db.execute(
            select(VoucherLine.account_code, Voucher.voucher_date, VoucherLine.debit, VoucherLine.credit)
            .join(Voucher, VoucherLine.voucher_id == Voucher.id)
            .where(VoucherLine.account_code.in_(["222101", "222102", "222103", ACC_VAT_PENDING, ACC_VAT_TRANSFER_OUT]),
                   Voucher.status.in_(("posted", "reversed")))):
        sums[str(d)[:7]][code] += (dr or 0) - (cr or 0)
    out, carry = [], 0.0
    for m in sorted(sums):
        s = sums[m]
        output = round(-s["222101"], 2)
        inp = round(s["222102"], 2)
        tout = round(-s[ACC_VAT_TRANSFER_OUT], 2)
        due = round(output - inp + tout - carry, 2)
        payable = max(due, 0.0)
        new_carry = round(max(-due, 0.0), 2)
        z = lambda x: round(x, 2) + 0.0          # 去掉 -0.0
        out.append({"month": m, "output": z(output), "input": z(inp), "transfer_out": z(tout), "carry_in": z(carry),
                    "payable": z(payable), "carry_out": z(new_carry), "paid_in_month": z(s["222103"]),
                    "pending_change": z(s[ACC_VAT_PENDING])})
        carry = new_carry
    return out


def deduct_postings(db) -> list[tuple]:
    """供 synchronize 使用：每张已勾选/已转出的进项票应有的凭证 (kind, id, 日期, 摘要, 行)。"""
    from .assets import month_end
    res = []
    for doc in db.scalars(select(InvoiceDoc).where(InvoiceDoc.direction == "in",
                                                   InvoiceDoc.deduct_status.in_(("deducted", "transferred")))):
        if doc.deduct_period and doc.deduct_tax:
            amt = round(doc.deduct_tax, 2)
            res.append(("vat_deduct", doc.id, month_end(doc.deduct_period), f"进项勾选 {doc.deduct_period} {doc.invoice_no} {doc.seller_name}",
                        [dict(account=F.ACC_VAT_IN, debit=amt, partner_id=doc.partner_id),
                         dict(account=ACC_VAT_PENDING, credit=amt, partner_id=doc.partner_id)]))
        if doc.deduct_status == "transferred" and doc.transfer_period and doc.deduct_tax:
            amt = round(doc.deduct_tax, 2)
            res.append(("vat_transfer", doc.id, month_end(doc.transfer_period), f"进项转出 {doc.transfer_period} {doc.invoice_no} {doc.seller_name}",
                        [dict(account=doc.transfer_account or "660210", debit=amt),
                         dict(account=ACC_VAT_TRANSFER_OUT, credit=amt)]))
    return res


# ---------------- API ----------------

def _row(db, d: InvoiceDoc, vnos: dict, invs: dict) -> dict:
    counter = d.seller_name if d.direction == "in" else d.buyer_name
    return {"id": d.id, "invoice_no": d.invoice_no, "direction": d.direction, "kind": d.kind,
            "issue_date": str(d.issue_date) if d.issue_date else "", "issue_month": str(d.issue_date)[:7] if d.issue_date else "",
            "counterparty": counter, "partner_id": d.partner_id,
            "partner_short": (d.partner.short_name or d.partner.name) if d.partner else "",
            "seller_name": d.seller_name, "buyer_name": d.buyer_name,
            "amount_ex_tax": d.amount_ex_tax, "tax_amount": d.tax_amount, "amount_tax": d.amount_tax,
            "tax_rates": d.tax_rates, "items": d.items, "face_remark": d.face_remark,
            "status": d.status, "status_label": STATUS_LABELS.get(d.status, d.status), "red_of": d.red_of,
            "category": d.category, "category_label": CATEGORIES.get(d.category, "未分类"),
            "invoice_id": d.invoice_id, "invoice_contract": invs.get(d.invoice_id, ""),
            "voucher_id": d.voucher_id, "voucher_no": vnos.get(d.voucher_id, ""),
            "booked": bool(d.invoice_id or d.voucher_id or d.category == "none"), "booked_month": d.booked_month,
            "deduct_status": d.deduct_status, "deduct_label": DEDUCT_LABELS.get(d.deduct_status, d.deduct_status),
            "deduct_period": d.deduct_period, "deduct_time": d.deduct_time, "deduct_tax": d.deduct_tax,
            "transfer_period": d.transfer_period, "transfer_account": d.transfer_account, "note": d.note,
            "files": [{"id": f.id, "name": f.file_name, "type": f.file_type} for f in d.files],
            "has_file": bool(d.files), "source": d.source}


def install(app, get_db):
    def _get(db, id_):
        d = db.get(InvoiceDoc, id_)
        if not d:
            raise HTTPException(404, "发票档案不存在")
        return d

    @app.get("/api/invoice-docs")
    def list_docs(direction: str = "", db=Depends(get_db)):
        sel = select(InvoiceDoc).order_by(InvoiceDoc.issue_date.desc(), InvoiceDoc.id.desc())
        if direction:
            sel = sel.where(InvoiceDoc.direction == direction)
        vnos = dict(db.execute(select(Voucher.id, Voucher.voucher_no)).all())
        from .models import Contract
        invs = {i.id: (i.contract.contract_no if i.contract else "") for i in db.scalars(select(Invoice))}
        return [_row(db, d, vnos, invs) for d in db.scalars(sel)]

    @app.post("/api/invoice-docs/upload")
    def upload(data: UploadIn, db=Depends(get_db)):
        raw = base64.b64decode(data.content_b64)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("文件超过 20MB")
        if data.name.lower().endswith(".xlsx"):
            res = [dict(file=data.name, ok=True, **import_tax_export(db, data.name, raw))]
        else:
            res = import_file(db, data.name, raw, data.direction)
        O.synchronize(db, "发票档案导入")
        O.audit(db, "导入发票档案", file=data.name, results=len(res))
        db.commit()
        return res

    @app.put("/api/invoice-docs/{id_}")
    def patch(id_: int, data: DocPatch, db=Depends(get_db)):
        d = _get(db, id_)
        fields = data.model_dump(exclude_unset=True)
        if "voucher_no" in fields:
            vno = fields.pop("voucher_no") or ""
            v = db.scalar(select(Voucher).where(Voucher.voucher_no == vno)) if vno else None
            if vno and not v:
                raise ValueError(f"凭证 {vno} 不存在")
            d.voucher_id = v.id if v else None
            if v and not fields.get("booked_month"):
                d.booked_month = str(v.voucher_date)[:7]
        for k, v in fields.items():
            if k == "category" and v not in ("", *CATEGORIES):
                raise ValueError("类别无效")
            if k == "deduct_status" and v not in DEDUCT_LABELS:
                raise ValueError("抵扣状态无效")
            if k == "voucher_id" and v and not db.get(Voucher, v):
                raise ValueError("凭证不存在")
            if k == "invoice_id" and v and not db.get(Invoice, v):
                raise ValueError("ERP 发票不存在")
            setattr(d, k, v if v is not None else ("" if k not in ("partner_id", "voucher_id", "invoice_id", "deduct_tax") else None))
        if d.deduct_status == "deducted" and (not d.deduct_period or not d.deduct_tax):
            raise ValueError("已勾选需填所属期和有效抵扣税额")
        if d.deduct_status == "transferred" and not d.transfer_period:
            raise ValueError("进项转出需填转出所属期")
        O.synchronize(db, "发票档案更新")
        db.commit()
        return {"ok": True}

    @app.post("/api/invoice-docs/{id_}/register")
    def register(id_: int, data: RegisterIn, db=Depends(get_db)):
        """档案里的贸易票 → 登记为 ERP 采购/销售发票（=发票页「登记发票」，凭证由 synchronize 自动生成）。"""
        from .models import Contract
        from .validation import validate_document
        d = _get(db, id_)
        if d.invoice_id:
            raise ValueError("已登记为 ERP 发票")
        if d.status != "normal":
            raise ValueError("红冲/被红冲的票不登记")
        if db.scalar(select(Invoice).where(Invoice.invoice_no == d.invoice_no)):
            raise ValueError("ERP 已有同号发票，用「重新自动关联」")
        pid = data.partner_id or d.partner_id
        if not pid:
            raise ValueError("请先选往来单位")
        if data.contract_id:
            c = db.get(Contract, data.contract_id)
            want = "purchase" if d.direction == "in" else "sales"
            if not c or c.status != "active" or c.contract_type != want or c.partner_id != pid:
                raise ValueError("合同须为同一往来单位、同方向的有效合同")
        inv = Invoice(direction="input" if d.direction == "in" else "output", invoice_no=d.invoice_no,
                      invoice_date=d.issue_date, partner_id=pid, contract_id=data.contract_id,
                      amount_ex_tax=d.amount_ex_tax, tax_amount=d.tax_amount, amount_tax=d.amount_tax,
                      file_path=d.files[0].stored_path if d.files else "", remark=f"由发票档案登记（{d.kind}）")
        validate_document(db, inv, "invoice")
        db.add(inv)
        db.flush()
        d.invoice_id, d.partner_id, d.category = inv.id, pid, "trade"
        d.booked_month = str(d.issue_date)[:7] if d.issue_date else d.booked_month
        O.synchronize(db, "发票档案登记贸易票")
        O.audit(db, "发票档案登记贸易票", doc_id=d.id, invoice_id=inv.id)
        db.commit()
        return {"invoice_id": inv.id}

    @app.post("/api/invoice-docs/{id_}/relink")
    def relink(id_: int, db=Depends(get_db)):
        d = _get(db, id_)
        auto_link(db, d)
        db.commit()
        return {"ok": True}

    @app.get("/api/invoice-docs/files/{fid}")
    def get_file(fid: int, db=Depends(get_db)):
        f = db.get(InvoiceFile, fid)
        if not f:
            raise HTTPException(404, "文件不存在")
        path = (archive_root() / f.stored_path).resolve()
        if archive_root().resolve() not in path.parents or not path.exists():
            raise HTTPException(404, "归档文件缺失")
        media = {"pdf": "application/pdf", "xml": "application/xml", "png": "image/png", "jpg": "image/jpeg",
                 "ofd": "application/ofd"}.get(f.file_type, "application/octet-stream")
        return FileResponse(path, media_type=media, filename=f.file_name,
                            content_disposition_type="inline" if f.file_type in ("pdf", "png", "jpg", "xml") else "attachment")

    @app.get("/api/vat-ledger")
    def vat_ledger_api(db=Depends(get_db)):
        pending = [d for d in db.scalars(select(InvoiceDoc).where(InvoiceDoc.direction == "in",
                                                                  InvoiceDoc.deduct_status == "pending"))]
        return {"months": vat_ledger(db),
                "pending_count": len(pending), "pending_tax": round(sum(d.tax_amount for d in pending), 2)}
