"""存放地点与调拨：地点只管数量，不影响成本和凭证"""
from conftest import PeerApp


def _loc(client, code, name, kind="own", partner_id=None, **kw):
    r = client.post("/api/locations", json={"code": code, "name": name, "kind": kind, "partner_id": partner_id, **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _stock(client, mid):
    return next(s for s in client.get("/api/stock").json()["items"] if s["material_id"] == mid)


def _default(client):
    return next(l for l in client.get("/api/locations").json() if l["is_default"])


def test_default_location_seeded_and_unique(client, seeded):
    locs = client.get("/api/locations").json()
    assert [l["name"] for l in locs if l["is_default"]] == ["主仓库"]
    bdr = _loc(client, "BDR", "供应商寄存", "third_party", 1)
    assert client.put(f"/api/locations/{bdr}", json={"code": "BDR", "name": "供应商寄存", "kind": "third_party",
                                                    "partner_id": 1, "is_default": True}).status_code == 200
    assert [l["code"] for l in client.get("/api/locations").json() if l["is_default"]] == ["BDR"]
    assert client.post("/api/locations", json={"code": "X", "name": "缺单位", "kind": "third_party"}).status_code == 400


def test_receipt_issue_transfer_by_location(client, seeded):
    mid = seeded["mat1"]
    home = _default(client)["id"]
    bdr = _loc(client, "BDR", "供应商寄存", "third_party", 1)
    client.post("/api/movements", json={"move_type": "in", "material_id": mid, "qty": 100, "unit_cost": 10,
                                        "move_date": "2026-07-30", "status": "confirmed", "location_id": bdr})
    client.post("/api/movements", json={"move_type": "out", "material_id": mid, "qty": 60,
                                        "move_date": "2026-07-30", "status": "confirmed", "location_id": bdr})
    before = client.get("/api/vouchers").json()
    r = client.post("/api/transfers", json={"material_id": mid, "qty": 25, "from_location_id": bdr,
                                            "to_location_id": home, "move_date": "2026-08-02", "remark": "拉回"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed" and r.json()["short_on_date"] is None
    s = _stock(client, mid)
    assert s["qty"] == 40 and abs(s["avg_cost"] - 10) < 1e-9                      # 总数与成本不变
    assert {l["name"]: l["qty"] for l in s["locations"]} == {"供应商寄存": 15, "主仓库": 25}
    assert len(client.get("/api/vouchers").json()) == len(before)                 # 调拨不出凭证
    # 调多了：提示调出地点当日为负
    r = client.post("/api/transfers", json={"material_id": mid, "qty": 20, "from_location_id": bdr,
                                            "to_location_id": home, "move_date": "2026-08-03"}).json()
    assert r["short_on_date"] == -5
    tid = r["id"]
    assert client.post(f"/api/transfers/{tid}/void").status_code == 200
    assert {l["name"]: l["qty"] for l in _stock(client, mid)["locations"]} == {"供应商寄存": 15, "主仓库": 25}


def test_draft_transfer_confirm_with_date_and_same_location_rejected(client, seeded):
    home = _default(client)["id"]
    other = _loc(client, "OFFICE", "办公室")
    body = {"material_id": seeded["mat2"], "qty": 1, "from_location_id": home, "to_location_id": home,
            "move_date": "2026-08-01"}
    assert client.post("/api/transfers", json=body).status_code == 400
    r = client.post("/api/transfers", json={**body, "to_location_id": other, "confirm": False}).json()
    assert r["status"] == "draft"
    assert client.post(f"/api/transfers/{r['id']}/confirm", json={"move_date": "2026-08-05"}).status_code == 200
    t = next(x for x in client.get("/api/transfers").json() if x["id"] == r["id"])
    assert t["status"] == "confirmed" and t["move_date"] == "2026-08-05" and t["created_by"] == "测试用户"


def test_fulfill_and_relocate_movement(client, seeded):
    bdr = _loc(client, "BDR", "供应商寄存", "third_party", 1)
    c = client.post("/api/contracts", json={"contract_no": "P-LOC-1", "contract_type": "purchase", "partner_id": 1,
                                            "amount_tax": 113, "lines": [{"material_id": seeded["mat1"], "qty": 1, "price_tax": 113}]}).json()
    line = client.get(f"/api/contracts/{c['id']}").json()["lines"][0]
    client.post(f"/api/contracts/{c['id']}/fulfill", json={"move_date": "2026-08-01", "location_id": bdr,
                                                          "lines": [{"line_id": line["id"], "qty": 1}]})
    mv = client.get(f"/api/contracts/{c['id']}").json()["movements"][0]
    assert mv["location_id"] == bdr
    home = _default(client)["id"]
    assert client.post(f"/api/movements/{mv['id']}/location", json={"location_id": home}).status_code == 200
    assert {l["name"] for l in _stock(client, seeded["mat1"])["locations"]} == {"主仓库"}
    # 有库存的地点不能停用
    assert client.put(f"/api/locations/{home}", json={"code": "SZ-FAC", "name": "主仓库", "is_default": True,
                                                     "active": False}).status_code == 400


def test_viewer_cannot_transfer(client, app_ctx, seeded):
    from fastapi.testclient import TestClient
    client.post("/api/users", json={"username": "rov", "role": "viewer", "password": "ro-pass-2026x"})
    v = TestClient(PeerApp(app_ctx.app))
    v.post("/api/auth/login", json={"username": "rov", "password": "ro-pass-2026x"})
    assert v.get("/api/locations").status_code == 200
    home = _default(client)["id"]
    assert v.post("/api/transfers", json={"material_id": seeded["mat1"], "qty": 1, "from_location_id": home,
                                          "to_location_id": home, "move_date": "2026-08-01"}).status_code == 403
