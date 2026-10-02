"""API 关键路径：物料分组自动编码、BOM 守卫、合同明细、出入库过账闭环"""


def test_health(client):
    r = client.get("/api/health").json()
    assert r["ok"] is True


def test_material_auto_code_by_group(client, seeded):
    ms = client.get("/api/materials").json()
    codes = {m["code"] for m in ms}
    assert "OZ-010001" in codes and "OZ-020001" in codes
    r = client.post("/api/materials", json={"name": "第二个驱动器", "mat_group": "01"}).json()
    m = next(m for m in client.get("/api/materials").json() if m["id"] == r["id"])
    assert m["code"] == "OZ-010002"  # 组内序号递增


def test_material_groups_registry(client):
    gs = client.get("/api/material-groups").json()
    assert {g["code"] for g in gs} >= {"01", "02", "09", "99"}


def test_bom_guards(client, seeded):
    p, c1, c2 = seeded["mat1"], seeded["mat1"], seeded["mat2"]
    assert client.post("/api/material-bom", json={"parent_material_id": p, "child_material_id": c1}).status_code == 400  # 父=子
    r = client.post("/api/material-bom", json={"parent_material_id": p, "child_material_id": c2, "qty_per": 2}).json()
    assert r["id"]
    assert client.post("/api/material-bom", json={"parent_material_id": p, "child_material_id": c2}).status_code == 409  # 重复
    m = next(m for m in client.get("/api/materials").json() if m["id"] == p)
    assert len(m["bom"]) == 1 and m["bom"][0]["qty_per"] == 2
    client.delete(f"/api/materials/{p}/bom")
    m = next(m for m in client.get("/api/materials").json() if m["id"] == p)
    assert m["bom"] == []


def test_contract_with_lines(client, seeded):
    r = client.post("/api/contracts", json={
        "contract_no": "T-P001", "contract_type": "purchase", "partner_id": 1, "amount_tax": 260.0,
        "lines": [
            {"material_id": seeded["mat1"], "qty": 2, "price_tax": 100.0, "amount_tax": 200.0},
            {"material_id": seeded["mat2"], "qty": 1, "price_tax": 60.0, "amount_tax": 60.0},
        ]}).json()
    d = client.get(f"/api/contracts/{r['id']}").json()
    assert len(d["lines"]) == 2
    assert abs(sum(l["amount_tax"] for l in d["lines"]) - d["amount_tax"]) < 0.01


def test_movement_flow_posting(client, seeded):
    """入草稿→确认→出草稿→确认→库存结转且出库快照成本"""
    cin = client.post("/api/movements", json={
        "move_type": "in", "material_id": seeded["mat1"], "qty": 10, "unit_cost": 100.0,
        "move_date": "2026-01-01"}).json()
    st = client.get("/api/stock").json()["items"]
    assert not st or all(s["material_id"] != seeded["mat1"] for s in st)  # 草稿不过账
    client.post(f"/api/movements/{cin['id']}/confirm")
    cout = client.post("/api/movements", json={
        "move_type": "out", "material_id": seeded["mat1"], "qty": 4, "move_date": "2026-01-02"}).json()
    client.post(f"/api/movements/{cout['id']}/confirm")
    s = next(s for s in client.get("/api/stock").json()["items"] if s["material_id"] == seeded["mat1"])
    assert s["qty"] == 6 and abs(s["avg_cost"] - 100.0) < 1e-6
    mv = client.get("/api/movements").json()
    out_rec = next(m for m in mv if m["id"] == cout["id"])
    assert out_rec["unit_cost"] == 100.0  # 确认时快照


def test_movement_void(client, seeded):
    cin = client.post("/api/movements", json={
        "move_type": "in", "material_id": seeded["mat2"], "qty": 5, "unit_cost": 10.0,
        "move_date": "2026-01-01"}).json()
    client.post(f"/api/movements/{cin['id']}/confirm")
    client.post(f"/api/movements/{cin['id']}/void", json={"reason": "测试"})
    st = client.get("/api/stock").json()["items"]
    assert all(s["material_id"] != seeded["mat2"] for s in st)  # 作废后不过账


def test_invoice_no_unique(client, seeded):
    body = {"direction": "input", "invoice_no": "INV-T-1", "amount_tax": 100, "amount_ex_tax": 100, "tax_amount": 0, "invoice_date": "2026-01-01", "partner_id": 1}
    assert client.post("/api/invoices", json=body).status_code == 200
    assert client.post("/api/invoices", json=body).status_code in (400, 409, 500)


def test_three_way_match_flow(client, seeded):
    """三单匹配：合同→收货→收票→付款全链路状态流转"""
    r = client.post("/api/contracts", json={
        "contract_no": "T-P002", "contract_type": "purchase", "partner_id": 1, "amount_tax": 565.0,
        "lines": [{"material_id": seeded["mat1"], "qty": 1, "price_tax": 565.0, "amount_tax": 565.0}]}).json()
    rows = client.get("/api/three-way-match?contract_type=purchase").json()
    row = next(x for x in rows if x["contract_no"] == "T-P002")
    assert row["status"] == "todo"
    m = client.post("/api/movements", json={"move_type": "in", "material_id": seeded["mat1"], "qty": 1,
        "unit_cost": 500.0, "tax_rate": 0.13, "move_date": "2026-01-01", "contract_id": r["id"], "partner_id": 1,
        "status": "confirmed"}).json()
    row = next(x for x in client.get("/api/three-way-match?contract_type=purchase").json() if x["contract_no"] == "T-P002")
    assert row["status"] == "gr_gap" and abs(row["gr_ir_gap"] - 500.0) < 0.01  # 货到票未到
    inv = client.post("/api/invoices", json={"direction": "input", "invoice_no": "INV-3W-1",
        "invoice_date": "2026-01-02", "partner_id": 1, "contract_id": r["id"],
        "amount_ex_tax": 500.0, "tax_amount": 65.0, "amount_tax": 565.0}).json()
    row = next(x for x in client.get("/api/three-way-match?contract_type=purchase").json() if x["contract_no"] == "T-P002")
    assert row["status"] == "pay_gap" and abs(row["gr_ir_gap"]) < 0.01        # 票到齐，未付款
    client.post("/api/payments", json={"direction": "pay", "partner_id": 1, "contract_id": r["id"],
        "amount": 565.0, "pay_date": "2026-01-03"})
    row = next(x for x in client.get("/api/three-way-match?contract_type=purchase").json() if x["contract_no"] == "T-P002")
    assert row["status"] == "closed"


def test_ledger_export(client, seeded):
    r = client.get("/api/ledger-export")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert len(r.content) > 5000  # xlsx 非空


def test_static_assets_revalidate(client):
    """前端多文件直载：必须 no-cache，避免升级后浏览器混用新旧 JS"""
    assert client.get("/assets/js/base.js").headers.get("cache-control") == "no-cache"
    assert client.get("/").headers.get("cache-control") == "no-cache"


def test_create_material_blocks_duplicate_by_alias(client, seeded):
    """同一实物只允许一条物料：新料号若与已有规格/别名料号相同，须先确认（应加别名而非新建）"""
    client.post("/api/material-aliases", json={"material_id": seeded["mat1"], "partner_id": 1,
                                               "alias_name": "驱动", "alias_spec": "QX9AB1BS050BSAAX / QX9AB1BS025BSAAX"})
    body = {"name": "压力传感器", "spec": "QX9AB1BS050BSAAX/QX9AB1BS025BSAAX", "mat_group": "05"}
    r = client.post("/api/materials", json=body)
    assert r.status_code == 409 and "测试驱动器" in r.json()["detail"] and "别名" in r.json()["detail"]
    sim = client.get("/api/materials/similar", params={"spec": "qx9ab1bs050bsaax"}).json()
    assert [x["id"] for x in sim] == [seeded["mat1"]]
    assert client.post("/api/materials", json={**body, "force": True}).status_code == 200
    assert client.post("/api/materials", json={"name": "新物料", "spec": "ZZ-9", "mat_group": "05"}).status_code == 200


def test_similar_ignores_plain_names(client, seeded):
    client.post("/api/material-aliases", json={"material_id": seeded["mat1"], "partner_id": 1,
                                               "alias_name": "板", "alias_spec": "EC风机转换板"})
    assert client.get("/api/materials/similar", params={"spec": "EC风机转换板+通讯线"}).json() == []


def test_dashboard_stage_follows_four_flows_and_skips_replaced(client, seeded):
    def mk(no, amount=113.0):
        c = client.post("/api/contracts", json={"contract_no": no, "contract_type": "sales", "partner_id": 2, "amount_tax": amount,
                                                "lines": [{"material_id": seeded["mat1"], "qty": 1, "price_tax": amount}]}).json()
        return client.get(f"/api/contracts/{c['id']}").json()
    a = mk("S-STG-A")
    client.post(f"/api/contracts/{a['id']}/fulfill", json={"move_date": "2026-03-01", "lines": [{"line_id": a["lines"][0]["id"], "qty": 1}]})
    b = mk("S-STG-B")
    client.post(f"/api/contracts/{b['id']}/fulfill", json={"move_date": "2026-03-01", "lines": [{"line_id": b["lines"][0]["id"], "qty": 1}]})
    client.post("/api/invoices", json={"direction": "output", "invoice_no": "INV-STG-B", "invoice_date": "2026-03-02", "partner_id": 2,
                                       "contract_id": b["id"], "amount_tax": 113, "amount_ex_tax": 100, "tax_amount": 13})
    c = mk("S-STG-C")
    body = {"contract_no": "S-STG-C2", "contract_type": "sales", "partner_id": 2, "amount_tax": 113.0,
            "lines": [{"material_id": seeded["mat1"], "qty": 1, "price_tax": 113.0}]}
    assert client.post(f"/api/contracts/{c['id']}/revise", json=body).status_code == 200
    d = client.get("/api/dashboard").json()
    st = {x["contract_no"]: x["stage"] for x in d["pipeline"]}
    assert st["S-STG-A"] == "待开票" and st["S-STG-B"] == "待收款" and st["S-STG-C2"] == "待执行"
    assert "S-STG-C" not in st                                   # 被替代的不上看板
    assert abs(d["kpi"]["ar"] - 113 * 3) < 0.01                  # 也不计入应收
