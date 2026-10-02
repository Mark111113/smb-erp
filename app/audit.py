"""操作人追溯：请求上下文 + ORM flush 钩子自动写 change_log。

- 中间件在每个 /api 请求开始时 begin_request()，把当前用户放进 contextvar；
  同步接口跑在线程池里，contextvar 会随调用复制过去。
- SessionLocal 的 after_flush 钩子把本次 flush 的新增/修改/删除逐行写入 change_log，
  走同一连接同一事务：业务回滚则日志一起回滚，业务提交则日志必在。
- 没有请求上下文的改动（命令行脚本等）记为「系统」。
"""
from contextvars import ContextVar
from datetime import date, datetime
import re
import uuid

from sqlalchemy import event, inspect, select, text

from .models import ChangeLog


_ctx: ContextVar[dict | None] = ContextVar("owe_request_ctx", default=None)

SYSTEM_NAME = "系统"
EXCLUDED_TABLES = {"change_log", "audit_event", "auth_session", "auth_ip_block", "migration_record", "user_view_pref",
                   "api_token"}   # 令牌每次调用都更新 last_seen，不逐行记；签发/吊销单独记 audit_event  # 界面设置不算业务操作
# 登录过程的计数/时间戳不是「操作」，不记；密码只记「已修改」
IGNORED_FIELDS = {"auth_user": {"last_login_at", "failed_count"}}
REDACTED_FIELDS = {"password_hash", "totp_secret"}

TABLE_LABELS = {
    "partner": "往来单位", "material": "物料", "material_alias": "物料别名", "material_bom": "BOM",
    "contract": "合同", "contract_line": "合同明细", "stock_movement": "出入库单", "invoice": "发票",
    "payment": "收付款", "document_state": "单据状态", "period_lock": "期间锁定", "allocation": "票款核销",
    "stock_conversion": "组拆套", "conversion_movement": "组拆套明细", "auth_user": "用户",
    "document_link": "单据关联", "bank_txn": "银行流水", "expense": "费用", "asset": "固定资产", "depreciation_line": "折旧明细", "invoice_doc": "发票档案", "tax_filing": "申报登记", "invoice_file": "发票文件",
    "archive": "档案", "account": "科目", "cost_center": "成本中心", "voucher": "凭证",
    "voucher_line": "凭证行", "stock_location": "存放地点", "stock_transfer": "调拨单", "todo_item": "待办",
}

# (方法, 路径正则, 动作名)；按顺序匹配，未命中时页面显示原始路径
ACTION_LABELS = [
    ("POST", r"/api/auth/setup", "创建首个管理员"),
    ("POST", r"/api/auth/login", "登录"),
    ("PUT", r"/api/auth/password", "修改本人密码"),
    ("POST", r"/api/users", "新建用户"),
    ("PUT", r"/api/users/\d+", "修改用户"),
    ("POST", r"/api/users/\d+/password", "重置用户密码"),
    ("POST", r"/api/users/\d+/unlock", "解锁账号"),
    ("POST", r"/api/partners", "新建往来单位"),
    ("PUT", r"/api/partners/\d+", "修改往来单位"),
    ("POST", r"/api/materials", "新建物料"),
    ("PUT", r"/api/materials/\d+", "修改物料"),
    ("POST", r"/api/material-aliases", "新增物料别名"),
    ("DELETE", r"/api/material-aliases/\d+", "删除物料别名"),
    ("POST", r"/api/material-bom", "新增BOM构成"),
    ("DELETE", r"/api/material-bom/\d+", "删除BOM构成"),
    ("DELETE", r"/api/materials/\d+/bom", "清空BOM"),
    ("POST", r"/api/contracts", "新建合同"),
    ("PUT", r"/api/contracts/\d+", "修改合同"),
    ("POST", r"/api/contracts/\d+/settle-cost", "合同成本结算"),
    ("POST", r"/api/contracts/\d+/fulfill", "按合同收发货"),
    ("POST", r"/api/contracts/\d+/revise", "合同改版"),
    ("POST", r"/api/movements/batch-confirm", "批量确认出入库"),
    ("POST", r"/api/invoices/\d+/relink", "发票改挂合同"),
    ("POST", r"/api/todos", "新增待办"),
    ("PUT", r"/api/todos/\d+", "修改待办"),
    ("POST", r"/api/todos/\d+/done", "完成待办"),
    ("POST", r"/api/todos/\d+/reopen", "重开待办"),
    ("DELETE", r"/api/todos/\d+", "删除待办"),
    ("POST", r"/api/locations", "新建存放地点"),
    ("PUT", r"/api/locations/\d+", "修改存放地点"),
    ("POST", r"/api/transfers", "新建调拨单"),
    ("POST", r"/api/transfers/\d+/confirm", "确认调拨"),
    ("POST", r"/api/transfers/\d+/void", "作废调拨"),
    ("POST", r"/api/movements/\d+/location", "修改出入库地点"),
    ("POST", r"/api/payments/\d+/relink", "收付款改挂合同"),
    ("POST", r"/api/payments/\d+/split", "收付款拆分"),
    ("PUT", r"/api/tax-filings", "登记申报数"),
    ("POST", r"/api/movements/\d+/redate", "出入库改日期"),
    ("POST", r"/api/allocations/auto", "按合同自动核销"),
    ("POST", r"/api/contract-files/\d+/stamp", "合同盖章"),
    ("POST", r"/api/contract-files/\d+/stamp-preview", "盖章预览"),
    ("POST", r"/api/seals", "上传印章"),
    ("POST", r"/api/contracts/\d+/invoice-export", "导出开票 Excel"),
    ("POST", r"/api/users/\d+/tokens", "签发 API 令牌"),
    ("POST", r"/api/users/tokens/\d+/revoke", "吊销 API 令牌"),
    ("POST", r"/api/invoice-docs/upload", "导入发票档案"),
    ("PUT", r"/api/invoice-docs/\d+", "修改发票档案"),
    ("POST", r"/api/invoice-docs/\d+/relink", "发票档案重新关联"),
    ("POST", r"/api/invoice-docs/\d+/register", "发票档案登记贸易票"),
    ("POST", r"/api/assets/depreciate/undo", "撤销折旧"),
    ("POST", r"/api/assets/depreciate", "计提折旧"),
    ("POST", r"/api/assets", "登记固定资产"),
    ("PUT", r"/api/assets/\d+/remark", "修改固定资产备注"),
    ("POST", r"/api/boms/import", "导入BOM"),
    ("POST", r"/api/materials/import", "批量导入物料"),
    ("POST", r"/api/supplier-prices", "新增供应商价目"),
    ("PUT", r"/api/supplier-prices/\d+", "修改供应商价目"),
    ("POST", r"/api/boms/\d+/activate", "启用BOM版本"),
    ("PUT", r"/api/boms/\d+/lines", "修改BOM明细"),
    ("DELETE", r"/api/boms/\d+", "删除BOM草稿"),
    ("POST", r"/api/boms", "新建BOM版本"),
    ("POST", r"/api/work-orders/reports/\d+/void", "作废报工"),
    ("POST", r"/api/work-orders/\d+/issue", "工单领料"),
    ("POST", r"/api/work-orders/\d+/return", "工单退料"),
    ("POST", r"/api/work-orders/\d+/receipt", "完工入库"),
    ("POST", r"/api/work-orders/\d+/report", "报工"),
    ("POST", r"/api/work-orders/\d+/close", "关闭工单"),
    ("POST", r"/api/work-orders/\d+/cancel", "取消工单"),
    ("POST", r"/api/work-orders/\d+/reopen", "重开工单"),
    ("POST", r"/api/work-orders", "新建工单"),
    ("POST", r"/api/mfg/close/undo", "撤销成本结转"),
    ("POST", r"/api/mfg/close", "月末成本结转"),
    ("POST", r"/api/bank/import", "导入银行流水"),
    ("POST", r"/api/bank/auto-match", "流水自动匹配"),
    ("POST", r"/api/bank/txns/\d+/link", "流水关联收付款"),
    ("POST", r"/api/bank/txns/\d+/unlink", "流水取消关联"),
    ("POST", r"/api/bank/txns/\d+/ignore", "流水忽略"),
    ("POST", r"/api/bank/txns/\d+/reopen", "流水恢复"),
    ("POST", r"/api/bank/txns/\d+/post", "流水入账"),
    ("POST", r"/api/bank/txns/\d+/unpost", "撤销流水入账"),
    ("POST", r"/api/movements", "新建出入库单"),
    ("POST", r"/api/movements/\d+/confirm", "确认出入库"),
    ("POST", r"/api/movements/\d+/void", "作废出入库单"),
    ("POST", r"/api/movements/\d+/return", "出入库退货"),
    ("POST", r"/api/movements/\d+/link-line", "关联合同明细"),
    ("POST", r"/api/invoices", "登记发票"),
    ("POST", r"/api/invoices/\d+/void", "作废发票"),
    ("POST", r"/api/invoices/\d+/credit", "红字发票"),
    ("POST", r"/api/payments", "登记收付款"),
    ("POST", r"/api/payments/\d+/void", "作废收付款"),
    ("POST", r"/api/payments/\d+/refund", "退款"),
    ("POST", r"/api/archives", "登记档案"),
    ("DELETE", r"/api/archives/\d+", "删除档案"),
    ("POST", r"/api/accounts", "新建科目"),
    ("POST", r"/api/cost-centers", "新建成本中心"),
    ("PUT", r"/api/cost-centers/\d+", "修改成本中心"),
    ("POST", r"/api/vouchers", "手工录入凭证"),
    ("POST", r"/api/vouchers/\d+/reverse", "红冲凭证"),
    ("POST", r"/api/allocations", "票款核销"),
    ("POST", r"/api/allocations/\d+/void", "取消核销"),
    ("POST", r"/api/finance/close-year/\d+", "年度结转"),
    ("POST", r"/api/periods/[^/]+/lock", "锁定期间"),
    ("POST", r"/api/periods/[^/]+/unlock", "解除期间锁定"),
    ("POST", r"/api/conversions", "组套/拆套"),
    ("POST", r"/api/conversions/\d+/void", "作废组拆套"),
]
_ACTION_RE = [(m, re.compile(p + "$"), label) for m, p, label in ACTION_LABELS]


def describe(method: str, path: str) -> str:
    if not path:
        return "脚本/系统任务"
    for m, rx, label in _ACTION_RE:
        if m == method and rx.match(path):
            return label
    return f"{method} {path}"


def begin_request(user: dict | None, method: str, path: str):
    name = (user.get("display_name") or user.get("username")) if user else ""
    return _ctx.set({"request_id": uuid.uuid4().hex, "user_id": user["id"] if user else None,
                     "user_name": name, "method": method, "path": path})


def end_request(token):
    _ctx.reset(token)


def current_actor() -> tuple[int | None, str]:
    c = _ctx.get()
    if not c:
        return None, SYSTEM_NAME
    return c["user_id"], c["user_name"] or SYSTEM_NAME


def ensure_schema(engine):
    """v0.11 之前 audit_event 没有操作人列：补列，历史事件留空。"""
    cols = {c["name"] for c in inspect(engine).get_columns("audit_event")}
    with engine.begin() as conn:
        if "user_id" not in cols:
            conn.execute(text("ALTER TABLE audit_event ADD COLUMN user_id INTEGER"))
        if "user_name" not in cols:
            conn.execute(text("ALTER TABLE audit_event ADD COLUMN user_name VARCHAR(64) NOT NULL DEFAULT ''"))


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if v is None or isinstance(v, (str, int, float, bool, list, dict)):
        return v
    return str(v)


def _value(key, v):
    return "***" if key in REDACTED_FIELDS and v is not None else _jsonable(v)


def _row_key(state, mapper) -> str:
    # 只读已在实例字典里的值：flush 过程中不能触发懒加载
    parts = [state.dict.get(mapper.get_property_by_column(c).key) for c in mapper.primary_key]
    return "/".join("" if p is None else str(p) for p in parts)


def _snapshot(state, mapper, table):
    # created_at 多为 SQLite 服务端默认值（UTC），且日志行自带 at，快照里不重复
    skip = IGNORED_FIELDS.get(table, set()) | {"created_at"}
    return {a.key: _value(a.key, state.dict.get(a.key)) for a in mapper.column_attrs
            if a.key not in skip and a.key in state.dict}


def _diff(state, mapper, table):
    skip = IGNORED_FIELDS.get(table, set())
    out = {}
    for a in mapper.column_attrs:
        if a.key in skip:
            continue
        hist = state.attrs[a.key].history
        if not hist.has_changes():
            continue
        old = hist.deleted[0] if hist.deleted else None
        new = hist.added[0] if hist.added else None
        if old != new:
            out[a.key] = [_value(a.key, old), _value(a.key, new)]
    return out


def _after_flush(session, flush_context):
    ctx = _ctx.get() or {}
    user_id, user_name = current_actor()
    base = {"request_id": ctx.get("request_id") or uuid.uuid4().hex, "at": datetime.now(),
            "user_id": user_id, "user_name": user_name,
            "method": ctx.get("method", ""), "path": (ctx.get("path") or "")[:160]}
    rows = []
    for op, objs in (("insert", session.new), ("update", session.dirty), ("delete", session.deleted)):
        for obj in objs:
            state = inspect(obj)
            mapper = state.mapper
            table = mapper.local_table.name
            if table in EXCLUDED_TABLES:
                continue
            if op == "update":
                changes = _diff(state, mapper, table)
                if not changes:
                    continue
            else:
                changes = _snapshot(state, mapper, table)
            rows.append({**base, "table_name": table, "row_key": _row_key(state, mapper),
                         "op": op, "changes": changes})
    if rows:
        session.connection().execute(ChangeLog.__table__.insert(), rows)


def install(session_factory):
    event.listen(session_factory, "after_flush", _after_flush)


def creators(db, table: str, keys) -> dict[str, str]:
    """每行首条 insert 的操作人；v0.11 之前的历史数据没有记录，不出现在结果里。"""
    keys = [str(k) for k in keys]
    if not keys:
        return {}
    out = {}
    q = (select(ChangeLog.row_key, ChangeLog.user_name).where(
        ChangeLog.table_name == table, ChangeLog.op == "insert", ChangeLog.row_key.in_(keys))
        .order_by(ChangeLog.id.desc()))
    for key, name in db.execute(q):
        out[key] = name   # 倒序遍历，最后留下的是最早一条
    return out


def status_actors(db, table: str, keys, status: str) -> dict[str, str]:
    """每行最近一次把 status 改成指定值的操作人（如出入库单的确认人）。"""
    keys = [str(k) for k in keys]
    if not keys:
        return {}
    out = {}
    q = (select(ChangeLog.row_key, ChangeLog.user_name, ChangeLog.changes).where(
        ChangeLog.table_name == table, ChangeLog.op == "update", ChangeLog.row_key.in_(keys))
        .order_by(ChangeLog.id))
    for key, name, changes in db.execute(q):
        if (changes or {}).get("status", [None, None])[1] == status:
            out[key] = name
    return out


def attach_creators(db, table: str, items: list[dict], field="created_by") -> list[dict]:
    names = creators(db, table, [x["id"] for x in items])
    for x in items:
        x[field] = names.get(str(x["id"]), "")
    return items


def install_api(app, get_db):
    from datetime import timedelta
    from fastapi import Depends, Query
    from sqlalchemy import func

    @app.get("/api/change-log")
    def change_log(user_id: int = 0, table: str = "", row_key: str = "", date_from: str = "", date_to: str = "",
                   limit: int = Query(100, ge=1, le=500), db=Depends(get_db)):
        """按请求分组的变更日志，最新在前；筛选命中某行即返回该请求的全部改动。"""
        conds = []
        if user_id:
            conds.append(ChangeLog.user_id == user_id)
        if table:
            conds.append(ChangeLog.table_name == table)
        if row_key:
            conds.append(ChangeLog.row_key == row_key)
        if date_from:
            conds.append(ChangeLog.at >= datetime.fromisoformat(date_from))
        if date_to:
            conds.append(ChangeLog.at < datetime.fromisoformat(date_to) + timedelta(days=1))
        req = (select(ChangeLog.request_id, func.max(ChangeLog.id).label("mid")).where(*conds)
               .group_by(ChangeLog.request_id).order_by(func.max(ChangeLog.id).desc()).limit(limit))
        order = [r.request_id for r in db.execute(req)]
        groups = {rid: None for rid in order}
        if order:
            for x in db.scalars(select(ChangeLog).where(ChangeLog.request_id.in_(order)).order_by(ChangeLog.id)):
                g = groups[x.request_id]
                if g is None:
                    g = groups[x.request_id] = {
                        "request_id": x.request_id, "at": x.at.isoformat(sep=" ", timespec="seconds"),
                        "user_id": x.user_id, "user_name": x.user_name or SYSTEM_NAME,
                        "action": describe(x.method, x.path), "method": x.method, "path": x.path, "items": []}
                g["items"].append({"table": x.table_name, "table_label": TABLE_LABELS.get(x.table_name, x.table_name),
                                   "row_key": x.row_key, "op": x.op, "changes": x.changes})
        return [groups[rid] for rid in order if groups[rid]]

    @app.get("/api/change-log/facets")
    def change_log_facets(db=Depends(get_db)):
        users = db.execute(select(ChangeLog.user_id, func.max(ChangeLog.user_name))
                           .group_by(ChangeLog.user_id)).all()
        tables = db.scalars(select(ChangeLog.table_name).distinct()).all()
        return {"users": [{"id": u or 0, "name": n or SYSTEM_NAME} for u, n in users if u],
                "tables": sorted(({"key": t, "label": TABLE_LABELS.get(t, t)} for t in tables),
                                 key=lambda t: t["label"])}
