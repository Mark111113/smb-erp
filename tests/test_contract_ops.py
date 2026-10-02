"""合同执行类操作：按明细收发货、批量确认、改挂合同、合同改版"""
from conftest import PeerApp


def _contract(client, seeded, no, ctype="purchase", partner=1, lines=None):
    lines = lines or [{"material_id": seeded["mat1"], "qty": 10, "price_tax": 113.0},
                      {"material_id": seeded["mat2"], "qty": 4, "price_tax": 56.5}]
    r = client.post("/api/contracts", json={"contract_no": no, "contract_type": ctype, "partner_id": partner,
                                            "sign_date": "2026-03-01", "lines": lines,
                                            "amount_tax": round(sum(l["qty"] * l["price_tax"] for l in lines), 2)})
    assert r.status_code == 200, r.text
    return client.get(f"/api/contracts/{r.json()['id']}").json()


def _stock(client, mid):
    return next((s for s in client.get("/api/stock").json()["items"] if s["material_id"] == mid), {"qty": 0})


def test_fulfill_purchase_confirms_ex_tax_cost_and_partial(client, seeded):
    c = _contract(client, seeded, "P-FUL-1")
    l1, l2 = c["lines"]
    r = client.post(f"/api/contracts/{c['id']}/fulfill", json={
        "move_date": "2026-03-05", "lines": [{"line_id": l1["id"], "qty": 6}, {"line_id": l2["id"], "qty": 4}]})
    assert r.status_code == 200, r.text
    assert [x["status"] for x in r.json()["created"]] == ["confirmed", "confirmed"]
    s = _stock(client, seeded["mat1"])
    assert s["qty"] == 6 and abs(s["avg_cost"] - 100.0) < 1e-6          # 113 含税 → 100 不含税
    prog = {p["line_id"]: p for p in client.get(f"/api/contracts/{c['id']}/progress").json()}
    assert prog[l1["id"]]["open"] == 4 and prog[l2["id"]]["open"] == 0
    # 超交被拒
    r = client.post(f"/api/contracts/{c['id']}/fulfill", json={
        "move_date": "2026-03-06", "lines": [{"line_id": l1["id"], "qty": 5}]})
    assert r.status_code == 400 and "超过可交付数量 4" in r.json()["detail"]
    # 草稿占用可交付量
    r = client.post(f"/api/contracts/{c['id']}/fulfill", json={
        "move_date": "2026-03-06", "confirm": False, "lines": [{"line_id": l1["id"], "qty": 4}]})
    assert r.json()["created"][0]["status"] == "draft"
    assert client.get(f"/api/contracts/{c['id']}/progress").json()[0]["open"] == 0


def test_fulfill_sales_reports_negative_stock(client, seeded):
    c = _contract(client, seeded, "S-FUL-1", ctype="sales", partner=2)
    r = client.post(f"/api/contracts/{c['id']}/fulfill", json={
        "move_date": "2026-03-05", "lines": [{"line_id": c["lines"][0]["id"], "qty": 3}]})
    assert r.status_code == 200
    assert r.json()["negative"] == [{"material_id": seeded["mat1"], "qty": -3}]
    mv = client.get(f"/api/contracts/{c['id']}").json()["movements"]
    assert mv[0]["move_type"] == "out" and mv[0]["contract_line_id"] == c["lines"][0]["id"]


def test_batch_confirm_with_date_override(client, seeded):
    c = _contract(client, seeded, "P-BAT-1")
    r = client.post(f"/api/contracts/{c['id']}/fulfill", json={
        "move_date": "2026-03-01", "confirm": False,
        "lines": [{"line_id": l["id"], "qty": l["qty"]} for l in c["lines"]]}).json()
    ids = [x["id"] for x in r["created"]]
    assert client.post("/api/movements/batch-confirm", json={"ids": ids, "move_date": "2026-03-09"}).json()["confirmed"] == 2
    mv = client.get(f"/api/contracts/{c['id']}").json()["movements"]
    assert {m["status"] for m in mv} == {"confirmed"} and {m["move_date"] for m in mv} == {"2026-03-09"}
    assert client.post("/api/movements/batch-confirm", json={"ids": ids}).status_code == 400


def test_relink_invoice_and_payment(client, seeded):
    a = _contract(client, seeded, "P-REL-A")
    b = _contract(client, seeded, "P-REL-B")
    s = _contract(client, seeded, "S-REL-C", ctype="sales", partner=2)
    inv = client.post("/api/invoices", json={"direction": "input", "invoice_no": "INV-REL-1", "invoice_date": "2026-03-02",
                                             "partner_id": 1, "contract_id": a["id"], "amount_tax": 113,
                                             "amount_ex_tax": 100, "tax_amount": 13}).json()
    pay = client.post("/api/payments", json={"direction": "pay", "partner_id": 1, "contract_id": a["id"],
                                             "amount": 50, "pay_date": "2026-03-03"}).json()
    assert client.post(f"/api/invoices/{inv['id']}/relink", json={"contract_id": b["id"]}).status_code == 200
    assert client.post(f"/api/payments/{pay['id']}/relink", json={"contract_id": b["id"]}).status_code == 200
    assert client.get(f"/api/contracts/{b['id']}").json()["invoice_sum"] == 113
    assert client.get(f"/api/contracts/{a['id']}").json()["payment_sum"] == 0
    r = client.post(f"/api/invoices/{inv['id']}/relink", json={"contract_id": s["id"]})
    assert r.status_code == 400 and "方向" in r.json()["detail"]
    assert client.post(f"/api/invoices/{inv['id']}/relink", json={"contract_id": None}).status_code == 200
    g = client.get("/api/change-log?table=invoice").json()[0]
    assert g["action"] == "发票改挂合同"


def test_revise_moves_documents_and_marks_replaced(client, seeded):
    old = _contract(client, seeded, "P-REV-1")
    client.post(f"/api/contracts/{old['id']}/fulfill", json={
        "move_date": "2026-03-05", "lines": [{"line_id": old["lines"][0]["id"], "qty": 10}]})
    client.post("/api/invoices", json={"direction": "input", "invoice_no": "INV-REV-1", "invoice_date": "2026-03-06",
                                       "partner_id": 1, "contract_id": old["id"], "amount_tax": 1130,
                                       "amount_ex_tax": 1000, "tax_amount": 130})
    body = {"contract_no": "P-REV-1-新", "contract_type": "purchase", "partner_id": 1, "sign_date": "2026-03-10",
            "amount_tax": 1356.0, "lines": [{"material_id": seeded["mat1"], "qty": 12, "price_tax": 113.0}]}
    r = client.post(f"/api/contracts/{old['id']}/revise", json=body)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["moved"] == {"invoices": 1, "payments": 0, "movements": 1} and res["kept"] == []
    new = client.get(f"/api/contracts/{res['id']}").json()
    assert new["replaces_no"] == "P-REV-1" and new["invoice_sum"] == 1130
    assert new["fulfillment"]["lines"][0]["delivered"] == 10
    o = client.get(f"/api/contracts/{old['id']}").json()
    assert o["status"] == "replaced" and o["replaced_by_no"] == "P-REV-1-新"
    # 不能二次改版、不能改单位
    assert client.post(f"/api/contracts/{old['id']}/revise", json={**body, "contract_no": "X"}).status_code == 400
    assert client.post(f"/api/contracts/{res['id']}/revise",
                       json={**body, "contract_no": "P-REV-1-v3", "partner_id": 2}).status_code == 400


def test_viewer_cannot_use_contract_ops(client, app_ctx, seeded):
    from fastapi.testclient import TestClient
    c = _contract(client, seeded, "P-VIEW-1")
    client.post("/api/users", json={"username": "rov", "role": "viewer", "password": "ro-pass-2026x"})
    v = TestClient(PeerApp(app_ctx.app))
    v.post("/api/auth/login", json={"username": "rov", "password": "ro-pass-2026x"})
    assert v.get(f"/api/contracts/{c['id']}/progress").status_code == 200
    assert v.post(f"/api/contracts/{c['id']}/fulfill", json={
        "move_date": "2026-03-05", "lines": [{"line_id": c["lines"][0]["id"], "qty": 1}]}).status_code == 403


def test_edit_contract_lines_after_voiding_wrong_movements(client, seeded):
    """录错明细且已出库：作废出库单后可直接改合同明细（不必改版）"""
    c = _contract(client, seeded, "S-FIX-1", ctype="sales", partner=2,
                  lines=[{"material_id": seeded["mat1"], "qty": 1, "price_tax": 0.0},
                         {"material_id": seeded["mat2"], "qty": 1, "price_tax": 0.0}])
    r = client.post(f"/api/contracts/{c['id']}/fulfill", json={
        "move_date": "2026-03-05", "lines": [{"line_id": l["id"], "qty": 1} for l in c["lines"]]}).json()
    body = {"contract_no": "S-FIX-1", "contract_type": "sales", "partner_id": 2, "sign_date": "2026-03-01",
            "amount_tax": 0, "lines": [{"material_id": seeded["mat1"], "qty": 1, "price_tax": 800.0}]}
    assert client.put(f"/api/contracts/{c['id']}", json={**body, "amount_tax": 800}).status_code == 400
    for x in r["created"]:
        assert client.post(f"/api/movements/{x['id']}/void", json={"reason": "测试"}).status_code == 200
    body["amount_tax"] = 800
    assert client.put(f"/api/contracts/{c['id']}", json=body).status_code == 200, "作废后应可改明细"
    d = client.get(f"/api/contracts/{c['id']}").json()
    assert [(l["qty"], l["price_tax"]) for l in d["lines"]] == [(1, 800.0)]
    assert all(m["status"] == "voided" and m["contract_line_id"] is None for m in d["movements"])


def test_confirm_before_receipt_warns_short_on_date(client, seeded):
    """出库日早于到货日：最终库存不负，但返回当日库存不足提示（成本先按 0 暂估）"""
    p = _contract(client, seeded, "P-SHORT-1", lines=[{"material_id": seeded["mat1"], "qty": 2, "price_tax": 113.0}])
    s = _contract(client, seeded, "S-SHORT-1", ctype="sales", partner=2,
                  lines=[{"material_id": seeded["mat1"], "qty": 2, "price_tax": 150.0}])
    client.post(f"/api/contracts/{p['id']}/fulfill", json={"move_date": "2026-09-23",
                "lines": [{"line_id": p["lines"][0]["id"], "qty": 2}]})
    r = client.post(f"/api/contracts/{s['id']}/fulfill", json={"move_date": "2026-09-20",
                    "lines": [{"line_id": s["lines"][0]["id"], "qty": 2}]}).json()
    assert r["negative"] == [] and "moves" not in r and "material_ids" not in r
    assert r["short_on_date"][0]["qty_on_date"] == -2 and r["short_on_date"][0]["move_date"] == "2026-09-20"
    ok = client.post(f"/api/contracts/{s['id']}/fulfill", json={"move_date": "2026-09-20", "confirm": False,
                     "lines": [{"line_id": s["lines"][0]["id"], "qty": 0.0001}]})
    assert ok.status_code == 400   # 已交满


def test_split_payment(client, seeded):
    a = _contract(client, seeded, "P-SPL-A")
    b = _contract(client, seeded, "P-SPL-B")
    pay = client.post("/api/payments", json={"direction": "pay", "partner_id": 1, "contract_id": a["id"],
                                             "amount": 1000, "pay_date": "2026-03-03"}).json()
    r = client.post(f"/api/payments/{pay['id']}/split", json={"parts": [{"amount": 1000, "contract_id": b["id"]}]})
    assert r.status_code == 400 and "保留余额" in r.json()["detail"]
    r = client.post(f"/api/payments/{pay['id']}/split", json={"parts": [{"amount": 129.2, "contract_id": b["id"]}]})
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == 870.8 and len(r.json()["children"]) == 1
    assert client.get(f"/api/contracts/{a['id']}").json()["payment_sum"] == 870.8
    assert client.get(f"/api/contracts/{b['id']}").json()["payment_sum"] == 129.2
    rows = {p["id"]: p for p in client.get("/api/payments").json()}
    child = rows[r.json()["children"][0]]
    assert child["pay_date"] == "2026-03-03" and f"拆自收付款#{pay['id']}" in child["remark"]
    # 凭证重算后借贷仍平，预付合计不变
    assert client.get("/api/finance/trial-balance").status_code == 200
    assert client.get("/api/change-log?table=payment").json()[0]["action"] == "收付款拆分"
    # 目标合同方向不符被拒
    s = _contract(client, seeded, "S-SPL-C", ctype="sales", partner=2)
    r = client.post(f"/api/payments/{pay['id']}/split", json={"parts": [{"amount": 10, "contract_id": s["id"]}]})
    assert r.status_code == 400
    assert client.get(f"/api/contracts/{a['id']}").json()["payment_sum"] == 870.8   # 失败整体回滚


def test_void_requires_reason_and_redate(client, seeded):
    c = _contract(client, seeded, "P-RED-1")
    r = client.post(f"/api/contracts/{c['id']}/fulfill", json={
        "move_date": "2026-03-20", "lines": [{"line_id": c["lines"][0]["id"], "qty": 5}]}).json()
    mid = r["created"][0]["id"]
    assert client.post(f"/api/movements/{mid}/void").status_code == 422          # 没原因不让作废
    r = client.post(f"/api/movements/{mid}/redate", json={"move_date": "2026-03-05", "reason": "录错日期"})
    assert r.status_code == 200, r.text
    mv = client.get(f"/api/contracts/{c['id']}").json()["movements"][0]
    assert mv["move_date"] == "2026-03-05"
    assert client.get("/api/change-log?table=stock_movement").json()[0]["action"] == "出入库改日期"
    assert client.post(f"/api/movements/{mid}/void", json={"reason": "测试作废"}).status_code == 200
    assert client.post(f"/api/movements/{mid}/redate", json={"move_date": "2026-03-06", "reason": "x"}).status_code == 400
