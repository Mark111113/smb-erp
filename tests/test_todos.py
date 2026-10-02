"""看板手工待办"""
from conftest import PeerApp


def test_todo_lifecycle(client, seeded):
    c = client.post("/api/contracts", json={"contract_no": "S-TODO-1", "contract_type": "sales", "partner_id": 2,
                                            "amount_tax": 0, "lines": []}).json()
    r = client.post("/api/todos", json={"title": "DEMO04A 账实差 1 台", "detail": "供应商多用 1 台，待补合同",
                                        "level": "red", "contract_id": c["id"]})
    assert r.status_code == 200
    tid = r.json()["id"]
    client.post("/api/todos", json={"title": "要发票", "level": "info"})
    rows = client.get("/api/todos").json()
    assert [x["title"] for x in rows] == ["DEMO04A 账实差 1 台", "要发票"]          # 按紧急度排
    assert rows[0]["contract_no"] == "S-TODO-1" and rows[0]["created_by"] == "测试用户"
    assert client.put(f"/api/todos/{tid}", json={"title": "DEMO04A 账实差 1 台（已问唐工）", "level": "warn"}).status_code == 200
    assert client.post(f"/api/todos/{tid}/done").status_code == 200
    assert [x["title"] for x in client.get("/api/todos").json()] == ["要发票"]
    done = client.get("/api/todos?status=done").json()
    assert done[0]["title"].endswith("（已问唐工）") and done[0]["done_at"]
    assert client.post(f"/api/todos/{tid}/reopen").status_code == 200
    assert client.delete(f"/api/todos/{tid}").status_code == 200
    assert len(client.get("/api/todos?status=all").json()) == 1
    assert client.post("/api/todos", json={"title": ""}).status_code == 422
    assert client.post("/api/todos", json={"title": "x", "contract_id": 99999}).status_code == 400


def test_viewer_reads_but_cannot_write_todos(client, app_ctx):
    from fastapi.testclient import TestClient
    client.post("/api/users", json={"username": "rov", "role": "viewer", "password": "ro-pass-2026x"})
    v = TestClient(PeerApp(app_ctx.app))
    v.post("/api/auth/login", json={"username": "rov", "password": "ro-pass-2026x"})
    assert v.get("/api/todos").status_code == 200
    assert v.post("/api/todos", json={"title": "x"}).status_code == 403
