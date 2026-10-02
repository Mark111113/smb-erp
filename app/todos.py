"""看板手工待办（v0.18）：自动风险只覆盖负库存/收付差额，账实差异、待补单等要人记。"""
from datetime import date, datetime
from typing import Literal, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, select

from . import audit
from .models import Contract, TodoItem


class TodoIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)
    detail: str = ""
    level: Literal["red", "warn", "info"] = "warn"
    contract_id: Optional[int] = None
    due_date: Optional[date] = None


def todo_json(t: TodoItem) -> dict:
    return {"id": t.id, "title": t.title, "detail": t.detail, "level": t.level, "contract_id": t.contract_id,
            "contract_no": t.contract.contract_no if t.contract else "", "due_date": str(t.due_date) if t.due_date else "",
            "status": t.status, "done_at": t.done_at.isoformat(sep=" ", timespec="minutes") if t.done_at else "",
            "created_at": t.created_at.isoformat(sep=" ", timespec="minutes") if t.created_at else ""}


def create_todo(db, data: TodoIn) -> TodoItem:
    if data.contract_id and not db.get(Contract, data.contract_id):
        raise ValueError("合同不存在")
    t = TodoItem(**data.model_dump())
    db.add(t)
    db.flush()
    return t


def install(app, get_db):
    def _get(db, tid):
        t = db.get(TodoItem, tid)
        if not t:
            raise HTTPException(404, "待办不存在")
        return t

    @app.get("/api/todos")
    def list_todos(status: str = "open", db=Depends(get_db)):
        q = select(TodoItem)
        if status in ("open", "done"):
            q = q.where(TodoItem.status == status)
        rank = case({"red": 0, "warn": 1, "info": 2}, value=TodoItem.level, else_=3)
        q = q.order_by(TodoItem.status.desc(), rank, TodoItem.due_date.is_(None), TodoItem.due_date, TodoItem.id.desc())
        rows = [todo_json(t) for t in db.scalars(q)]
        return audit.attach_creators(db, "todo_item", rows)

    @app.post("/api/todos")
    def add_todo(data: TodoIn, db=Depends(get_db)):
        t = create_todo(db, data)
        db.commit()
        return {"id": t.id}

    @app.put("/api/todos/{tid}")
    def edit_todo(tid: int, data: TodoIn, db=Depends(get_db)):
        t = _get(db, tid)
        if data.contract_id and not db.get(Contract, data.contract_id):
            raise ValueError("合同不存在")
        for k, v in data.model_dump().items():
            setattr(t, k, v)
        db.commit()
        return {"ok": True}

    @app.post("/api/todos/{tid}/done")
    def done_todo(tid: int, db=Depends(get_db)):
        t = _get(db, tid)
        t.status, t.done_at = "done", datetime.now()
        db.commit()
        return {"ok": True}

    @app.post("/api/todos/{tid}/reopen")
    def reopen_todo(tid: int, db=Depends(get_db)):
        t = _get(db, tid)
        t.status, t.done_at = "open", None
        db.commit()
        return {"ok": True}

    @app.delete("/api/todos/{tid}")
    def delete_todo(tid: int, db=Depends(get_db)):
        db.delete(_get(db, tid))   # 待办不是业务单据，删除即可（change_log 留痕）
        db.commit()
        return {"ok": True}
