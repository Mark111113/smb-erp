"""列表视图设置跟账号走"""
from conftest import PeerApp


def test_prefs_per_user_and_viewer_can_save(client, app_ctx):
    from fastapi.testclient import TestClient
    assert client.get("/api/prefs/movements").json() == {}
    body = {"columns": ["doc_no", "move_date"], "known": ["doc_no", "move_date", "spec"], "sort": {"key": "move_date", "dir": "desc"}}
    assert client.put("/api/prefs/movements", json=body).status_code == 200
    assert client.get("/api/prefs/movements").json() == body
    client.post("/api/users", json={"username": "rov", "role": "viewer", "password": "ro-pass-2026x"})
    v = TestClient(PeerApp(app_ctx.app))
    v.post("/api/auth/login", json={"username": "rov", "password": "ro-pass-2026x"})
    assert v.get("/api/prefs/movements").json() == {}                      # 各人各自的
    assert v.put("/api/prefs/movements", json={"columns": ["doc_no"]}).status_code == 200   # 只读账号也能存自己的
    assert v.post("/api/partners", json={"code": "X", "name": "x"}).status_code == 403     # 业务写仍被拦
    assert client.get("/api/prefs/movements").json() == body
    assert v.delete("/api/prefs/movements").status_code == 200 and v.get("/api/prefs/movements").json() == {}
    assert client.put("/api/prefs/Bad Key!", json={}).status_code in (400, 404)
    assert client.put("/api/prefs/movements", json={"x": "y" * 9000}).status_code == 413
    assert not [g for g in client.get("/api/change-log").json() if any(i["table"] == "user_view_pref" for i in g["items"])]
