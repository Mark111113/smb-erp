"""列表视图设置（v0.20）：跟账号走，存在服务器上，换电脑/浏览器不丢。

prefs 结构由前端 DataTable 定义：{"columns": [可见列 key，按顺序], "known": [保存时存在的全部列 key],
"sort": {"key":..., "dir": "asc"|"desc"} | null}。known 用来识别之后新增的列（按默认可见性处理）。
"""
import re

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from .models import UserViewPref

VIEW_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,47}$")
MAX_BYTES = 8000


def install(app, get_db):
    def _key(view_key: str) -> str:
        if not VIEW_KEY.match(view_key):
            raise HTTPException(400, "视图标识无效")
        return view_key

    @app.get("/api/prefs/{view_key}")
    def get_pref(view_key: str, request: Request, db=Depends(get_db)):
        row = db.scalar(select(UserViewPref).where(UserViewPref.user_id == request.state.auth_user_id,
                                                   UserViewPref.view_key == _key(view_key)))
        return row.prefs if row else {}

    @app.put("/api/prefs/{view_key}")
    async def put_pref(view_key: str, request: Request, db=Depends(get_db)):
        body = await request.body()
        if len(body) > MAX_BYTES:
            raise HTTPException(413, "设置过大")
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(400, "设置格式无效")
        uid = request.state.auth_user_id
        row = db.scalar(select(UserViewPref).where(UserViewPref.user_id == uid, UserViewPref.view_key == _key(view_key)))
        if row:
            row.prefs = data
        else:
            db.add(UserViewPref(user_id=uid, view_key=view_key, prefs=data))
        db.commit()
        return {"ok": True}

    @app.delete("/api/prefs/{view_key}")
    def reset_pref(view_key: str, request: Request, db=Depends(get_db)):
        row = db.scalar(select(UserViewPref).where(UserViewPref.user_id == request.state.auth_user_id,
                                                   UserViewPref.view_key == _key(view_key)))
        if row:
            db.delete(row)
            db.commit()
        return {"ok": True}
