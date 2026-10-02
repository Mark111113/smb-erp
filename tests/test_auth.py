from conftest import PeerApp


def test_api_requires_login(client):
    client.cookies.clear()
    assert client.get("/api/materials").status_code == 401
    assert client.post("/api/auth/setup", json={
        "username": "another", "password": "another-password"
    }).status_code == 409
    assert client.post("/api/auth/login", json={
        "username": "tester", "password": "wrong-password"
    }).status_code == 401
    response = client.post("/api/auth/login", json={
        "username": "tester", "password": "unit-test-2026"
    })
    assert response.status_code == 200
    assert client.get("/api/materials").status_code == 200


def test_password_change_rotates_other_sessions(client):
    assert client.put("/api/auth/password", json={
        "old_password": "wrong-password",
        "new_password": "new-password-2026",
    }).status_code == 400
    assert client.put("/api/auth/password", json={
        "old_password": "unit-test-2026",
        "new_password": "new-password-2026",
    }).status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.post("/api/auth/login", json={
        "username": "tester", "password": "unit-test-2026"
    }).status_code == 401
    assert client.post("/api/auth/login", json={
        "username": "tester", "password": "new-password-2026"
    }).status_code == 200


def _login(app_ctx, username, password):
    from fastapi.testclient import TestClient
    c = TestClient(PeerApp(app_ctx.app))
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def _create(client, username, role):
    r = client.post("/api/users", json={"username": username, "display_name": username,
                                        "role": role, "password": f"{username}-pass-2026"})
    assert r.status_code == 200, r.text
    return r.json()


def test_setup_account_is_admin(client):
    me = client.get("/api/auth/status").json()["user"]
    assert me["role"] == "admin" and me["role_label"] == "管理员"


def test_role_gate(client, app_ctx):
    _create(client, "editor1", "editor")
    _create(client, "viewer1", "viewer")
    viewer = _login(app_ctx, "viewer1", "viewer1-pass-2026")
    editor = _login(app_ctx, "editor1", "editor1-pass-2026")

    assert viewer.get("/api/partners").status_code == 200
    r = viewer.post("/api/partners", json={"code": "V1", "name": "只读写入", "is_customer": True})
    assert r.status_code == 403 and r.json()["detail"] == "只读账号不能修改数据"
    assert viewer.put("/api/auth/password", json={"old_password": "viewer1-pass-2026",
                                                  "new_password": "viewer1-pass-2027"}).status_code == 200

    assert editor.post("/api/partners", json={"code": "E1", "name": "可写", "is_customer": True}).status_code == 200
    for c in (editor, viewer):
        assert c.get("/api/users").status_code == 403
        assert c.post("/api/users", json={"username": "x-user", "role": "admin",
                                          "password": "x-user-pass-2026"}).status_code == 403
        assert c.post("/api/periods/2026-01/unlock", json={"reason": "t"}).status_code == 403
    # 管理员放行到业务层（期间未锁定 → 400 而不是 403）
    assert client.post("/api/periods/2026-01/unlock", json={"reason": "t"}).status_code == 400
    assert client.post("/api/partners", json={"code": "A1", "name": "管理员写", "is_customer": True}).status_code == 200


def test_setup_closed_and_duplicate_usernames(client):
    assert client.post("/api/auth/setup", json={"username": "second", "password": "second-pass-2026"}).status_code == 409
    _create(client, "Mixed.Case", "editor")
    assert client.post("/api/users", json={"username": "mixed.case", "role": "viewer",
                                           "password": "mixed-case-2026"}).status_code == 409


def test_non_admin_lock_needs_admin(client, app_ctx):
    from fastapi.testclient import TestClient
    u = _create(client, "locky", "editor")
    anon = TestClient(PeerApp(app_ctx.app))
    for _ in range(5):
        assert anon.post("/api/auth/login", json={"username": "locky", "password": "wrong-password"}).status_code == 401
    r = anon.post("/api/auth/login", json={"username": "locky", "password": "locky-pass-2026"})
    assert r.status_code == 429 and "管理员" in r.json()["detail"]
    assert [x for x in client.get("/api/users").json() if x["id"] == u["id"]][0]["locked"]
    assert client.post(f"/api/users/{u['id']}/unlock").status_code == 200
    assert anon.post("/api/auth/login", json={"username": "locky", "password": "locky-pass-2026"}).status_code == 200


def test_admin_lock_expires_by_itself(client, app_ctx, db):
    from datetime import datetime
    from fastapi.testclient import TestClient
    from app.models import AuthUser
    anon = TestClient(PeerApp(app_ctx.app))
    for _ in range(5):
        anon.post("/api/auth/login", json={"username": "tester", "password": "wrong-password"})
    admin = db.query(AuthUser).filter_by(username="tester").one()
    assert admin.locked_until is not None and (admin.locked_until - datetime.now()).total_seconds() < 11 * 60


def test_last_admin_guard_and_deactivate(client, app_ctx):
    me = client.get("/api/auth/status").json()["user"]
    assert client.put(f"/api/users/{me['id']}", json={"role": "editor"}).status_code == 400
    assert client.put(f"/api/users/{me['id']}", json={"active": False}).status_code == 400
    u = _create(client, "leaver", "editor")
    leaver = _login(app_ctx, "leaver", "leaver-pass-2026")
    assert client.put(f"/api/users/{u['id']}", json={"active": False}).status_code == 200
    assert leaver.get("/api/partners").status_code == 401
    # 角色变更实时生效（中间件每次请求读库）
    v = _create(client, "promo", "viewer")
    promo = _login(app_ctx, "promo", "promo-pass-2026")
    assert promo.post("/api/partners", json={"code": "P1", "name": "x", "is_customer": True}).status_code == 403
    assert client.put(f"/api/users/{v['id']}", json={"role": "editor"}).status_code == 200
    assert promo.post("/api/partners", json={"code": "P1", "name": "x", "is_customer": True}).status_code == 200


def test_admin_reset_password_drops_sessions(client, app_ctx):
    u = _create(client, "forgetful", "editor")
    old = _login(app_ctx, "forgetful", "forgetful-pass-2026")
    assert client.post(f"/api/users/{u['id']}/password", json={"new_password": "brand-new-2026"}).status_code == 200
    assert old.get("/api/partners").status_code == 401
    _login(app_ctx, "forgetful", "brand-new-2026")


def test_ensure_schema_upgrades_v010_table(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    from app.auth import ensure_schema
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(32), password_hash VARCHAR(256))")
    con.execute("INSERT INTO auth_user (username, password_hash) VALUES ('alice', 'x')")
    con.commit(); con.close()
    engine = create_engine(f"sqlite:///{path}")
    ensure_schema(engine)
    ensure_schema(engine)  # 幂等
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT role FROM auth_user").scalar() == "editor"


def test_manage_users_cli(tmp_path, app_ctx):
    import os, subprocess, sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    db_path = Path(app_ctx.DB_PATH)
    run = lambda *a, stdin="": subprocess.run([sys.executable, str(root / "scripts/manage_users.py"), str(db_path), *a],
                                              input=stdin, capture_output=True, text=True, encoding="utf-8",
                                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    r = run("create", "admin", "--role", "admin", "--name", "管理员", "--password-stdin", stdin="cli-admin-2026\n")
    assert r.returncode == 0, r.stderr
    assert run("create", "admin", "--password-stdin", stdin="cli-admin-2026\n").returncode != 0
    assert run("create", "shorty", "--password-stdin", stdin="short\n").returncode != 0
    _login(app_ctx, "admin", "cli-admin-2026")
    assert "管理员" in run("list").stdout


def test_api_token_bearer_auth(client):
    me = client.get("/api/auth/status").json()["user"]
    r = client.post(f"/api/users/{me['id']}/tokens", json={"name": "agent"})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    from fastapi.testclient import TestClient
    anon = TestClient(client.app)
    assert anon.get("/api/contracts").status_code == 401
    ok = anon.get("/api/contracts", headers={"Authorization": f"Bearer {tok}"})
    assert ok.status_code == 200
    tid = r.json()["id"]
    assert client.post(f"/api/users/tokens/{tid}/revoke").status_code == 200
    assert anon.get("/api/contracts", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
    assert anon.get("/api/contracts", headers={"Authorization": "Bearer nope"}).status_code == 401
