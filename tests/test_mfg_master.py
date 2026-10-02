"""v0.37 制造基础数据：物料批量导入、供应商价目表、产品标准工时、领料认仓库（含地点数量方向修正）。"""
import base64

from test_mfg import _bom, plant, receive  # noqa: F401  复用虚拟物料夹具


def _imp(client, text, **kw):
    body = {"name": "m.csv", "content_b64": base64.b64encode(text.encode("utf-8")).decode(), **kw}
    return client.post("/api/materials/import", json=body)


def test_material_import_alias_dup_and_errors(client, seeded):
    head = "物料编码,物料名称,规格型号,物料组,制造商料号,单位,最小包装,损耗率,批次管理\n"
    rows = ("BDR-001,贴片电容,100nF 0603 X7R,11,CL10B104KB8,个,4000,1%,否\n"
            "BDR-002,主控MCU,CY9AF114 LQFP64,10,CY9AF114LAPMC1,个,,,是\n")
    # 文件内重复 + 单位不认识 → 有错，不让导
    bad = _imp(client, head + rows + "BDR-001,贴片电容,100nF 0603 X7R,11,CL10B104KB8,个,,,\nBDR-003,螺丝,M3,16,,颗,,,\n",
               code_is="alias", partner_id=1).json()
    assert bad["summary"]["error"] == 2
    assert _imp(client, head + rows + "BDR-003,螺丝,M3,16,,颗,,,\n", code_is="alias", partner_id=1, apply=True).status_code == 400
    # 对方料号记成别名；新建按组生成编码；损耗率 1% → 0.01；批次管理「是」
    r = _imp(client, head + rows, code_is="alias", partner_id=1, apply=True).json()
    assert r["created"] == 2 and r["summary"] == {"create": 2, "exists": 0, "dup": 0, "error": 0}
    mats = {m["mpn"]: m for m in client.get("/api/materials").json() if m["mpn"]}
    cap, mcu = mats["CL10B104KB8"], mats["CY9AF114LAPMC1"]
    assert cap["code"] == "OZ-1100001" and cap["material_type"] == "raw" and cap["loss_rate"] == 0.01 and cap["min_pack"] == 4000
    assert mcu["lot_control"] is True and cap["aliases"][0]["alias_spec"] == "BDR-001" and cap["aliases"][0]["partner_id"] == 1
    # 再导一次：按对方料号别名认出是已有物料，不重复建；补空字段（封装）
    again = _imp(client, "物料编码,物料名称,规格型号,封装\nBDR-001,贴片电容,100nF 0603 X7R,0603\n", code_is="alias", partner_id=1, apply=True).json()
    assert again["summary"]["exists"] == 1 and again["created"] == 0
    assert next(m for m in client.get("/api/materials").json() if m["id"] == cap["id"])["package"] == "0603"
    # 规格相似、没有料号可对 → 疑似重复，默认不导；勾「疑似重复也新建」才建
    d = _imp(client, "物料名称,规格型号,物料组\n电容,100nF-0603 X7R,11\n").json()
    assert d["summary"]["dup"] == 1
    assert _imp(client, "物料名称,规格型号,物料组\n电容,100nF-0603 X7R,11\n", force_dup=True, apply=True).json()["created"] == 1
    # 我方编码不存在 → 报错（新物料编码要留空让系统生成）
    assert _imp(client, "物料编码,物料名称\nOZ-1199999,电阻\n", code_is="own").json()["summary"]["error"] == 1


def test_supplier_price_and_std_hours_in_quote(client, plant):
    bid = _bom(client, plant)
    p2 = client.post("/api/partners", json={"code": "T03", "name": "测试供应商二", "is_supplier": True}).json()["id"]
    a = client.post("/api/supplier-prices", json={"material_id": plant["ipm"], "partner_id": 1, "price": 40, "moq": 100, "lead_days": 56})
    assert a.status_code == 200, a.text
    client.post("/api/supplier-prices", json={"material_id": plant["ipm"], "partner_id": p2, "price": 38})
    # 产品标准工时 0.5 小时：报价不填工时就用它
    drv = next(m for m in client.get("/api/materials").json() if m["id"] == plant["drv"])
    upd = {k: drv[k] for k in ("name", "spec", "unit", "mat_group", "remark", "material_type")} | {"std_hours": 0.5, "smt_points": 320}
    assert client.put(f"/api/materials/{plant['drv']}", json=upd).status_code == 200
    q = client.post("/api/mfg/quote", json={"bom_id": bid, "labor_rate": 40}).json()
    ipm = next(l for l in q["lines"] if l["material_id"] == plant["ipm"])
    assert ipm["price"] == 38 and "最低" in ipm["price_source"]                 # 没有首选取最低价（压过库存均价 45）
    assert q["hours"] == 0.5 and q["labor"] == 20
    client.put(f"/api/supplier-prices/{a.json()['id']}", json={"material_id": plant["ipm"], "partner_id": 1, "price": 40, "preferred": True})
    q = client.post("/api/mfg/quote", json={"bom_id": bid, "hours_per_unit": 1, "labor_rate": 40}).json()
    ipm = next(l for l in q["lines"] if l["material_id"] == plant["ipm"])
    assert ipm["price"] == 40 and "首选" in ipm["price_source"] and q["labor"] == 40
    assert len(client.get(f"/api/supplier-prices?material_id={plant['ipm']}").json()) == 2


def test_issue_from_warehouse_and_location_quantities(client, plant):
    _bom(client, plant)
    raw = client.post("/api/locations", json={"code": "RAW", "name": "原材料仓", "purpose": "raw"}).json()["id"]
    fin = client.post("/api/locations", json={"code": "FIN", "name": "成品仓", "purpose": "finished"}).json()["id"]
    w = client.post("/api/work-orders", json={"kind": "production", "product_id": plant["drv"], "qty": 2, "plan_start": "2027-01-08"}).json()
    lines = [{"material_id": plant["ipm"], "qty": 2}, {"material_id": plant["pcb"], "qty": 2}]
    # 夹具的料收在默认地点；默认从原材料仓领 → 原材料仓没货，报错并说明货在哪
    r = client.post(f"/api/work-orders/{w['id']}/issue", json={"move_date": "2027-01-08", "lines": lines})
    assert r.status_code == 400 and "原材料仓 只有 0" in r.text and "主仓库" in r.text
    # 先调拨到原材料仓再领
    for mid in (plant["ipm"], plant["pcb"]):
        t = client.post("/api/transfers", json={"material_id": mid, "qty": 5, "from_location_id": 1, "to_location_id": raw, "move_date": "2027-01-07"})
        assert t.status_code == 200, t.text
    assert client.post(f"/api/work-orders/{w['id']}/issue", json={"move_date": "2027-01-08", "lines": lines}).status_code == 200
    client.post(f"/api/work-orders/{w['id']}/return", json={"move_date": "2027-01-09", "lines": [{"material_id": plant["pcb"], "qty": 1}]})
    client.post(f"/api/work-orders/{w['id']}/receipt", json={"move_date": "2027-01-10", "qty": 2})
    stock = {x["material_id"]: {l["name"]: l["qty"] for l in x["locations"]} for x in client.get("/api/stock").json()["items"]}
    assert stock[plant["drv"]] == {"成品仓": 2}                                # 完工入库进成品仓，且是正数
    assert stock[plant["pcb"]]["原材料仓"] == 5 - 2 + 1                         # 退料回原材料仓
    assert stock[plant["ipm"]]["原材料仓"] == 3


def test_material_import_duplicate_code_without_mpn(client, seeded):
    """同一对方料号出现两次，一行有制造商料号、一行没有，也要判重复"""
    t = "物料编码,物料名称,规格型号,物料组,制造商料号\nB-1,MCU,LQFP64,10,CY9AF\nB-1,重复行,X,10,\n"
    r = _imp(client, t, code_is="alias", partner_id=1).json()
    assert r["summary"]["error"] == 1 and "编码和第 2 行重复" in r["rows"][1]["errors"][0]


def test_production_receipt_requires_issue(client, plant):
    _bom(client, plant)
    w = client.post("/api/work-orders", json={"kind": "production", "product_id": plant["drv"], "qty": 1, "plan_start": "2027-01-08"}).json()
    r = client.post(f"/api/work-orders/{w['id']}/receipt", json={"move_date": "2027-01-10", "qty": 1})
    assert r.status_code == 400 and "还没领料" in r.text
