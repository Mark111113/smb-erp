"""制造模块（v0.35）：虚拟元器件 + 虚拟驱动器 BOM，跑通 自制工单 / 委外 / 研发领料 / 月末成本结转，逐科目核余额。"""
import base64

import pytest


def gl(client, code):
    """科目余额（借正）：直接查明细账汇总，口径稳定"""
    rows = client.get(f"/api/finance/ledger?account={code}").json()
    return round(sum((x.get("debit") or 0) - (x.get("credit") or 0) for x in rows), 2)


def mat(client, name, spec, group, **kw):
    r = client.post("/api/materials", json={"name": name, "spec": spec, "mat_group": group, "force": True, **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def receive(client, mid, qty, cost, d="2027-01-05", lot=""):
    r = client.post("/api/movements", json={"move_type": "in", "material_id": mid, "qty": qty, "unit_cost": cost,
                                            "move_date": d, "partner_id": 1, "status": "confirmed", "lot_no": lot})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture
def plant(client, seeded):
    m = dict(
        mcu=mat(client, "主控MCU", "TEST-MCU-001", "10", mpn="CY9AF114", manufacturer="Infineon", lot_control=True),
        ipm=mat(client, "IPM 模块", "TEST-IPM-15A", "12", mpn="IKCM15L60"),
        cap=mat(client, "贴片电容", "100nF 0603", "11", mpn="CL10B104KB8NNNC", loss_rate=0.02),
        pcb=mat(client, "驱动板 PCB", "TEST-PCB-V1", "14"),
        drv=mat(client, "直流变频驱动器（测试）", "TEST-DRV-3HP", "01", material_type="finished"),
    )
    receive(client, m["mcu"], 20, 12.0, lot="L2701")
    receive(client, m["ipm"], 20, 45.0)
    receive(client, m["cap"], 500, 0.02)
    receive(client, m["pcb"], 20, 6.0)
    return m


def test_material_types_codes_and_inventory_accounts(client, plant):
    mats = {x["id"]: x for x in client.get("/api/materials").json()}
    assert mats[plant["mcu"]]["code"] == "OZ-1000001" and mats[plant["mcu"]]["material_type"] == "raw"
    assert mats[plant["cap"]]["code"] == "OZ-1100001" and mats[plant["drv"]]["code"].startswith("OZ-01")
    # 原材料收货记 1403，不是 1405；老物料类型 goods 仍是 1405
    assert gl(client, "1403") == round(20 * 12 + 20 * 45 + 500 * 0.02 + 20 * 6, 2)
    assert gl(client, "1405") == 0
    # 批次管理物料收货不填批号被拒
    r = client.post("/api/movements", json={"move_type": "in", "material_id": plant["mcu"], "qty": 1, "unit_cost": 1,
                                            "move_date": "2027-01-06", "status": "confirmed"})
    assert r.status_code == 400 and "批号" in r.text
    lots = client.get(f"/api/stock/lots?material_id={plant['mcu']}").json()
    assert lots[0]["lot_no"] == "L2701" and lots[0]["qty"] == 20


def _bom(client, plant):
    r = client.post("/api/boms", json={"product_id": plant["drv"], "version": "V1", "lines": [
        {"material_id": plant["mcu"], "qty": 1, "ref_des": "U1"},
        {"material_id": plant["ipm"], "qty": 1, "ref_des": "U5"},
        {"material_id": plant["cap"], "qty": 10, "ref_des": "C1-C10"},     # 损耗率取物料默认 2%
        {"material_id": plant["pcb"], "qty": 1}]})
    assert r.status_code == 200, r.text
    bid = r.json()["id"]
    assert client.post(f"/api/boms/{bid}/activate").status_code == 200
    return bid


def test_bom_versions_and_import(client, plant):
    bid = _bom(client, plant)
    mats = {x["id"]: x for x in client.get("/api/materials").json()}
    assert {b["child_id"] for b in mats[plant["drv"]]["bom"]} == {plant["mcu"], plant["ipm"], plant["cap"], plant["pcb"]}  # 同步到旧 BOM
    # 生效版本不能直接改；复制成 V2 草稿改完再启用，V1 自动作废
    assert client.put(f"/api/boms/{bid}/lines", json={"lines": []}).status_code == 400
    v2 = client.post("/api/boms", json={"product_id": plant["drv"], "version": "V2", "copy_from": bid, "change_note": "ECN-001 电容改 12 个"}).json()
    lines = [{k: l[k] for k in ("material_id", "qty", "ref_des")} for l in v2["lines"]]
    lines[2]["qty"] = 12
    assert client.put(f"/api/boms/{v2['id']}/lines", json={"lines": lines}).status_code == 200
    client.post(f"/api/boms/{v2['id']}/activate")
    st = {b["version"]: b["status"] for b in client.get(f"/api/boms?product_id={plant['drv']}").json()}
    assert st == {"V1": "obsolete", "V2": "active"}
    # Excel/CSV 导入：按我方编码或制造商料号匹配；对不上的可以按组新建
    csv_text = "位号,物料编码,制造商料号,名称,用量\nU1,OZ-1000001,,MCU,1\nU5,,IKCM15L60,IPM,1\nR1-R4,,RC0603FR-0710KL,电阻 10k,4\n"
    body = {"product_id": plant["drv"], "version": "V3", "name": "drv.csv", "content_b64": base64.b64encode(csv_text.encode()).decode()}
    dry = client.post("/api/boms/import", json=body).json()
    assert dry["total"] == 3 and dry["missing"] == 1 and dry["rows"][1]["material_id"] == plant["ipm"]
    assert client.post("/api/boms/import", json=body | {"apply": True}).status_code == 400          # 缺料不建不让导
    ok = client.post("/api/boms/import", json=body | {"apply": True, "create_group": "11"}).json()
    v3 = client.get(f"/api/boms/{ok['bom_id']}").json()
    assert v3["status"] == "draft" and len(v3["lines"]) == 3 and v3["lines"][2]["code"].startswith("OZ-11")


def test_production_work_order_full_cycle(client, plant):
    _bom(client, plant)
    w = client.post("/api/work-orders", json={"kind": "production", "product_id": plant["drv"], "qty": 10,
                                              "plan_start": "2027-01-08"}).json()
    assert w["wo_no"].startswith("WO-202701-") and w["bom_version"] == "V1"
    # MCU 按批次管理，按 BOM 自动领料没批号会被拦；先手工领 MCU（带批号），其余按 BOM
    r = client.post(f"/api/work-orders/{w['id']}/issue", json={"move_date": "2027-01-08", "from_bom": True})
    assert r.status_code == 400 and "批号" in r.text
    client.post(f"/api/work-orders/{w['id']}/issue", json={"move_date": "2027-01-08", "lines": [
        {"material_id": plant["mcu"], "qty": 10, "lot_no": "L2701"}]})
    d = client.post(f"/api/work-orders/{w['id']}/issue", json={"move_date": "2027-01-08", "from_bom": True}).json()
    req = {r["material_id"]: r for r in d["requirements"]}
    assert req[plant["cap"]]["required"] == 102 and req[plant["cap"]]["remaining"] == 0      # 10 × 10 × 1.02
    issued = 10 * 12 + 10 * 45 + 102 * 0.02 + 10 * 6
    assert gl(client, "500101") == round(issued, 2) and d["wip"] == round(issued, 2)
    # 库存不够领不了
    r = client.post(f"/api/work-orders/{w['id']}/issue", json={"move_date": "2027-01-08", "lines": [{"material_id": plant["ipm"], "qty": 50}]})
    assert r.status_code == 400 and "库存不足" in r.text
    # 退 2 个电容（按领料价），报工 8 小时，分两次完工入库，关单后在制归零
    client.post(f"/api/work-orders/{w['id']}/return", json={"move_date": "2027-01-09", "lines": [{"material_id": plant["cap"], "qty": 2}]})
    issued -= 2 * 0.02
    client.post(f"/api/work-orders/{w['id']}/report", json={"report_date": "2027-01-09", "good_qty": 10, "hours": 8})
    client.post(f"/api/work-orders/{w['id']}/receipt", json={"move_date": "2027-01-10", "qty": 6})
    d = client.post(f"/api/work-orders/{w['id']}/receipt", json={"move_date": "2027-01-11", "qty": 4}).json()
    d = client.post(f"/api/work-orders/{w['id']}/close").json()
    assert d["status"] == "closed" and d["wip"] == 0 and gl(client, "500101") == 0
    assert d["receipt_value"] == round(issued, 2) and gl(client, "1405") == round(issued, 2)
    # 人工和制造费用：电费 600（SMT）、一线工资 2,000，卖出 4 台后做月末结转
    cc = {c["code"]: c["id"] for c in client.get("/api/cost-centers").json()}
    client.post("/api/vouchers", json={"voucher_date": "2027-01-20", "summary": "电费", "lines": [
        {"account": "510102", "debit": 600, "cost_center_id": cc["CC11"]}, {"account": "100201", "credit": 600}]})
    client.post("/api/vouchers", json={"voucher_date": "2027-01-31", "summary": "一线工资", "lines": [
        {"account": "500102", "debit": 2000, "cost_center_id": cc["CC11"]}, {"account": "221101", "credit": 2000}]})
    client.post("/api/movements", json={"move_type": "out", "material_id": plant["drv"], "qty": 4, "move_date": "2027-01-25",
                                        "partner_id": 2, "status": "confirmed"})
    cogs_before = gl(client, "6401")
    pv = client.get("/api/mfg/close?month=2027-01").json()
    assert pv["pool_total"] == 2600 and pv["basis"] == "hours" and pv["orders"][0]["on_hand_ratio"] == 0.6
    r = client.post("/api/mfg/close", json={"month": "2027-01"})
    assert r.status_code == 200, r.text
    assert gl(client, "510102") == 0 and gl(client, "500102") == 0
    assert round(gl(client, "6401") - cogs_before, 2) == 1040                 # 已卖 4/10 那部分直接进成本
    assert gl(client, "1405") == round(issued * 0.6 + 1560, 2)               # 在库 6 台带上 1,560
    stock = {x["material_id"]: x for x in client.get("/api/stock").json()["items"]}
    assert round(stock[plant["drv"]]["value"], 2) == gl(client, "1405")
    assert round(stock[plant["drv"]]["avg_cost"], 2) == round((issued * 0.6 + 1560) / 6, 2)
    detail = client.get(f"/api/work-orders/{w['id']}").json()
    assert detail["allocated"] == 2600 and detail["unit_cost"] == round((issued + 2600) / 10, 4)
    # 撤销结转：科目回到结转前；不能重复结
    assert client.post("/api/mfg/close", json={"month": "2027-01"}).status_code == 400
    assert client.post("/api/mfg/close/undo").status_code == 200
    assert gl(client, "510102") == 600 and gl(client, "500102") == 2000 and gl(client, "1405") == round(issued * 0.6, 2)
    assert client.post("/api/mfg/close", json={"month": "2027-01"}).status_code == 200
    # 关闭的工单不能再领料；重开后可以
    assert client.post(f"/api/work-orders/{w['id']}/issue", json={"move_date": "2027-01-12", "from_bom": True}).status_code == 400


def test_subcontract_and_fee_invoice(client, plant):
    w = client.post("/api/work-orders", json={"kind": "subcontract", "product_id": plant["drv"], "qty": 5, "partner_id": 1,
                                              "fee_price": 10, "plan_start": "2027-02-01"}).json()
    assert w["wo_no"].startswith("SC-")
    client.post(f"/api/work-orders/{w['id']}/issue", json={"move_date": "2027-02-01", "lines": [
        {"material_id": plant["mcu"], "qty": 5, "lot_no": "L2701"}, {"material_id": plant["ipm"], "qty": 5}]})
    mat_cost = 5 * 12 + 5 * 45
    assert gl(client, "1408") == mat_cost and gl(client, "500101") == 0
    grir0 = gl(client, "1234")
    client.post(f"/api/work-orders/{w['id']}/receipt", json={"move_date": "2027-02-05", "qty": 5})
    d = client.post(f"/api/work-orders/{w['id']}/close").json()
    assert gl(client, "1408") == 0 and d["receipt_value"] == mat_cost + 50
    assert round(gl(client, "1234") - grir0, 2) == -50                       # 加工费暂估在 GR/IR 贷方（挂委外厂）
    client.post("/api/invoices", json={"direction": "input", "invoice_no": "SC0001", "invoice_date": "2027-02-06", "partner_id": 1,
                                       "amount_ex_tax": 50, "tax_amount": 6.5, "amount_tax": 56.5})
    assert round(gl(client, "1234") - grir0, 2) == 0                         # 加工费发票冲平


def test_rd_issue_and_rd_ledger(client, plant):
    cc20 = next(c for c in client.get("/api/cost-centers").json() if c["code"] == "CC20")
    proj = client.post("/api/cost-centers", json={"code": "RD2701", "name": "三相 25HP 驱动器开发", "parent_id": cc20["id"]}).json()
    pid = proj["id"] if "id" in proj else next(c["id"] for c in client.get("/api/cost-centers").json() if c["code"] == "RD2701")
    assert client.post("/api/work-orders", json={"kind": "rd"}).status_code == 400                # 研发要选项目
    w = client.post("/api/work-orders", json={"kind": "rd", "cost_center_id": pid, "plan_start": "2027-03-01"}).json()
    client.post(f"/api/work-orders/{w['id']}/issue", json={"move_date": "2027-03-02", "lines": [{"material_id": plant["ipm"], "qty": 2}]})
    assert gl(client, "5301") == 90
    client.post("/api/vouchers", json={"voucher_date": "2027-03-31", "summary": "研发人员工资", "lines": [
        {"account": "5301", "debit": 5000, "cost_center_id": pid}, {"account": "221101", "credit": 5000}]})
    assert client.post("/api/mfg/close", json={"month": "2027-03"}).status_code == 200
    assert gl(client, "5301") == 0 and gl(client, "660211") == 5090
    led = client.get("/api/mfg/rd-ledger?year=2027").json()
    p = next(x for x in led["projects"] if x["code"] == "RD2701")
    assert p["直接投入费用"] == 90 and p["人员人工费用"] == 5000 and p["expensed"] == 5090
    st = client.get("/api/statements/small?period=2027-Q1").json()
    assert next(x for x in st["income_statement"] if x["row"] == 17)["ytd"] == 5090        # 利润表「研究费用」


def test_quote_and_related_party(client, plant):
    bid = _bom(client, plant)
    q = client.post("/api/mfg/quote", json={"bom_id": bid, "hours_per_unit": 0.5, "labor_rate": 40, "overhead_rate": 20, "margin": 0.25}).json()
    material = 12 + 45 + 10 * 1.02 * 0.02 + 6
    assert round(q["material"], 4) == round(material, 4) and q["labor"] == 20 and q["overhead"] == 10
    assert q["price_ex_tax"] == round((material + 30) / 0.75, 2) and not q["no_price"]
    p = client.get("/api/partners").json()[0]
    client.put(f"/api/partners/{p['id']}", json={**{k: p[k] for k in ("name", "code", "short_name", "is_customer", "is_supplier")}, "is_related": True})
    rel = client.get("/api/related-party?year=2027").json()
    assert rel["rows"] and rel["rows"][0]["partner_id"] == p["id"]


def test_legacy_goods_unchanged(client, seeded):
    """老物料（类型 goods）照旧记 1405；组套用的旧 BOM 端点仍可用并同步出 BOM 版本"""
    a = client.post("/api/movements", json={"move_type": "in", "material_id": seeded["mat1"], "qty": 2, "unit_cost": 100,
                                            "move_date": "2027-01-05", "status": "confirmed"})
    assert a.status_code == 200 and gl(client, "1405") == 200 and gl(client, "1403") == 0
    r = client.post("/api/material-bom", json={"parent_material_id": seeded["mat2"], "child_material_id": seeded["mat1"], "qty_per": 2})
    assert r.status_code == 200
    boms = client.get(f"/api/boms?product_id={seeded['mat2']}").json()
    assert boms[0]["status"] == "active" and boms[0]["line_count"] == 1


def _recon(client):
    r = client.get("/api/reconciliation")
    assert r.status_code == 200, r.text
    return r.json()


def test_retroactive_changes_stay_consistent(client, plant):
    """事后插入更早的采购（不同价格）、作废领料/入库、锁期——成本链路重放后，存货 = 总账，在制归零"""
    _bom(client, plant)
    w = client.post("/api/work-orders", json={"kind": "production", "product_id": plant["drv"], "qty": 4, "plan_start": "2027-01-10"}).json()
    client.post(f"/api/work-orders/{w['id']}/issue", json={"move_date": "2027-01-10", "lines": [
        {"material_id": plant["mcu"], "qty": 4, "lot_no": "L2701"}, {"material_id": plant["ipm"], "qty": 4},
        {"material_id": plant["cap"], "qty": 41}, {"material_id": plant["pcb"], "qty": 4}]})
    client.post(f"/api/work-orders/{w['id']}/receipt", json={"move_date": "2027-01-12", "qty": 4})
    client.post(f"/api/work-orders/{w['id']}/close")
    v0 = gl(client, "1405")
    # 补登一笔 01-03 的 IPM 采购，单价 75（比 45 贵）→ IPM 均价变、领料成本变、完工成本跟着变
    receive(client, plant["ipm"], 20, 75.0, d="2027-01-03")
    rec = _recon(client)
    assert rec["inventory_gap"] == 0 and rec["trial_gap"] == 0
    assert gl(client, "500101") == 0 and gl(client, "1405") == round(v0 + 4 * (60 - 45), 2)   # 均价 (20×75+20×45)/40 = 60
    # 重开工单、作废入库单 → 成本回到在制；再入库关单
    client.post(f"/api/work-orders/{w['id']}/reopen")
    d = client.get(f"/api/work-orders/{w['id']}").json()
    fg = next(m for m in d["movements"] if m["move_type"] == "wo_receipt")
    assert client.post(f"/api/movements/{fg['id']}/void", json={"reason": "测试"}).status_code == 200
    assert gl(client, "1405") == 0 and gl(client, "500101") == d["material_cost"]
    client.post(f"/api/work-orders/{w['id']}/receipt", json={"move_date": "2027-01-13", "qty": 4})
    client.post(f"/api/work-orders/{w['id']}/close")
    rec = _recon(client)
    assert rec["inventory_gap"] == 0 and gl(client, "500101") == 0
    # 锁期后不能再在该月领料、入库
    w2 = client.post("/api/work-orders", json={"kind": "production", "product_id": plant["drv"], "qty": 1, "plan_start": "2027-01-20"}).json()
    assert client.post("/api/periods/2027-01/lock", json={"reason": "t"}).status_code == 200
    r = client.post(f"/api/work-orders/{w2['id']}/issue", json={"move_date": "2027-01-20", "lines": [{"material_id": plant["pcb"], "qty": 1}]})
    assert r.status_code == 400 and "锁定" in r.text
    assert client.post(f"/api/work-orders/{w2['id']}/issue", json={"move_date": "2027-02-01", "lines": [{"material_id": plant["pcb"], "qty": 1}]}).status_code == 200
    assert client.post(f"/api/work-orders/{w2['id']}/cancel").status_code == 400          # 有领料不能取消
