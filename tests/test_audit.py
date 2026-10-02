"""操作人追溯：change_log 自动记录、录入人/确认人、事务一致、只读拒绝不留痕"""
from conftest import PeerApp


def _login(app_ctx, username, password):
    from fastapi.testclient import TestClient
    c = TestClient(PeerApp(app_ctx.app))
    assert c.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
    return c


def _user(client, username, role):
    assert client.post("/api/users", json={"username": username, "display_name": username.upper(),
                                           "role": role, "password": f"{username}-pass-2026"}).status_code == 200


def test_insert_and_update_are_logged_with_operator(client):
    p = client.post("/api/partners", json={"code": "L1", "name": "日志单位", "is_customer": True}).json()
    client.put(f"/api/partners/{p['id']}", json={"code": "L1", "name": "日志单位改名", "is_customer": True})
    groups = client.get("/api/change-log?table=partner").json()
    assert [g["action"] for g in groups[:2]] == ["修改往来单位", "新建往来单位"]
    upd, ins = groups[0], groups[1]
    assert upd["user_name"] == "测试用户" and upd["path"] == f"/api/partners/{p['id']}"
    item = next(i for i in upd["items"] if i["table"] == "partner")
    assert item["op"] == "update" and item["changes"]["name"] == ["日志单位", "日志单位改名"]
    snap = next(i for i in ins["items"] if i["table"] == "partner")
    assert snap["op"] == "insert" and snap["changes"]["name"] == "日志单位" and snap["row_key"] == str(p["id"])


def test_movement_creator_confirmer_and_auto_voucher(client, app_ctx, seeded):
    _user(client, "clerk", "editor")
    _user(client, "keeper", "editor")
    clerk = _login(app_ctx, "clerk", "clerk-pass-2026")
    keeper = _login(app_ctx, "keeper", "keeper-pass-2026")
    m = clerk.post("/api/movements", json={"move_type": "in", "material_id": seeded["mat1"], "qty": 2,
                                           "unit_cost": 10.0, "move_date": "2026-02-01"}).json()
    assert keeper.post(f"/api/movements/{m['id']}/confirm").status_code == 200
    row = next(x for x in client.get("/api/movements").json() if x["id"] == m["id"])
    assert row["created_by"] == "CLERK" and row["confirmed_by"] == "KEEPER"
    # 确认过账自动生成的凭证记在确认人名下
    vouchers = client.get("/api/vouchers?source_type=goods_in").json()
    assert vouchers and vouchers[0]["created_by"] == "KEEPER"
    g = client.get("/api/change-log?table=stock_movement&row_key=" + str(m["id"])).json()[0]
    assert g["action"] == "确认出入库" and {"voucher", "voucher_line"} <= {i["table"] for i in g["items"]}


def test_list_creators_for_documents(client, seeded):
    body = {"direction": "input", "invoice_no": "INV-LOG-1", "amount_tax": 113, "amount_ex_tax": 100,
            "tax_amount": 13, "invoice_date": "2026-02-01", "partner_id": 1}
    inv = client.post("/api/invoices", json=body).json()
    assert next(x for x in client.get("/api/invoices").json() if x["id"] == inv["id"])["created_by"] == "测试用户"
    events = client.get("/api/audit-events").json()
    assert next(e for e in events if e["action"] == "新增发票")["user_name"] == "测试用户"


def test_failed_and_denied_writes_leave_no_log(client, app_ctx, seeded):
    before = len(client.get("/api/change-log?limit=500").json())
    body = {"direction": "input", "invoice_no": "INV-DUP", "amount_tax": 100, "amount_ex_tax": 100,
            "tax_amount": 0, "invoice_date": "2026-01-01", "partner_id": 1}
    assert client.post("/api/invoices", json=body).status_code == 200
    assert client.post("/api/invoices", json=body).status_code in (400, 409)
    _user(client, "watcher", "viewer")
    watcher = _login(app_ctx, "watcher", "watcher-pass-2026")
    assert watcher.post("/api/partners", json={"code": "W1", "name": "x", "is_customer": True}).status_code == 403
    after = client.get("/api/change-log?limit=500").json()
    # 新增：创建发票 1 次 + 新建用户 1 次；重复发票与只读写入都没有留痕
    assert len(after) - before == 2
    assert all(g["user_name"] != "WATCHER" for g in after)


def test_user_admin_changes_logged_with_password_redacted(client):
    _user(client, "temp", "viewer")
    uid = next(u["id"] for u in client.get("/api/users").json() if u["username"] == "temp")
    client.post(f"/api/users/{uid}/password", json={"new_password": "another-pass-2026"})
    g = client.get(f"/api/change-log?table=auth_user&row_key={uid}").json()
    assert [x["action"] for x in g] == ["重置用户密码", "新建用户"]
    reset = next(i for i in g[0]["items"] if i["table"] == "auth_user")
    assert reset["changes"]["password_hash"] == ["***", "***"]
    assert g[1]["items"][0]["changes"]["password_hash"] == "***"


def test_viewer_can_read_log(client, app_ctx):
    _user(client, "reader", "viewer")
    reader = _login(app_ctx, "reader", "reader-pass-2026")
    assert reader.get("/api/change-log").status_code == 200
    facets = reader.get("/api/change-log/facets").json()
    assert any(u["name"] == "测试用户" for u in facets["users"])
    assert any(t["key"] == "auth_user" and t["label"] == "用户" for t in facets["tables"])


def test_script_changes_logged_as_system(app_ctx, db):
    from app.models import Partner, ChangeLog
    db.add(Partner(code="S1", name="脚本单位"))
    db.commit()
    row = db.query(ChangeLog).filter_by(table_name="partner").order_by(ChangeLog.id.desc()).first()
    assert row.user_name == "系统" and row.user_id is None and row.path == ""


def test_audit_event_schema_upgrade(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    from app.audit import ensure_schema
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE audit_event (id INTEGER PRIMARY KEY, action VARCHAR(64), detail JSON, created_at DATETIME)")
    con.execute("INSERT INTO audit_event (action, detail) VALUES ('旧事件', '{}')")
    con.commit(); con.close()
    engine = create_engine(f"sqlite:///{path}")
    ensure_schema(engine)
    ensure_schema(engine)
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT user_id, user_name FROM audit_event").one() == (None, "")
