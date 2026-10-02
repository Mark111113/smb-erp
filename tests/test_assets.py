"""固定资产：登记（可带入账凭证）、按月折旧补提、尾差、撤销最后一月、资产负债表备抵"""


def _asset(client, **kw):
    body = {"name": "测试设备", "account": "160102", "acquired_date": "2026-09-30", "cost": 1000,
            "life_months": 3, "credit_lines": [{"account": "2202", "amount": 1000, "partner_id": 1}]}
    body.update(kw)
    r = client.post("/api/assets", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_asset_depreciation_cycle(client, seeded):
    bad = client.post("/api/assets", json={"name": "x", "account": "160102", "acquired_date": "2026-09-30", "cost": 1000,
                                           "life_months": 3, "credit_lines": [{"account": "2202", "amount": 999}]})
    assert bad.status_code == 400 and "≠" in bad.json()["detail"]
    _asset(client)
    rows = client.get("/api/assets").json()["items"]
    assert rows[0]["start_month"] == "2026-10" and rows[0]["monthly"] == 333.33 and rows[0]["voucher_no"]
    r = client.post("/api/assets/depreciate", json={"period": "2026-11"}).json()["months"]
    assert [m["period"] for m in r] == ["2026-10", "2026-11"]
    assert client.post("/api/assets/depreciate", json={"period": "2026-11"}).json()["months"] == []   # 幂等
    r = client.post("/api/assets/depreciate", json={"period": "2027-06"}).json()["months"]
    assert [m["amount"] for m in r] == [333.34]                                                      # 尾差，满额后停
    a = client.get("/api/assets").json()["items"][0]
    assert a["accumulated"] == 1000 and a["net"] == 0
    bs = client.get("/api/finance/balance-sheet?asof=2027-01-31").json()
    acc = {x["code"]: x["balance"] for x in bs["assets"]}
    assert acc["160102"] == 1000 and acc["1602"] == -1000 and bs["balanced"]
    assert client.post("/api/assets/depreciate/undo").json()["period"] == "2026-12"
    assert client.get("/api/assets").json()["items"][0]["accumulated"] == 666.66
    assert client.get("/api/change-log?table=depreciation_line").json()[0]["action"] == "撤销折旧"


def test_asset_without_voucher_and_start_month(client, seeded):
    _asset(client, credit_lines=[], start_month="2026-09", acquired_date="2026-09-01", life_months=240, cost=4822799.63)
    a = client.get("/api/assets").json()["items"][0]
    assert a["voucher_no"] == "" and a["monthly"] == 20095.0
    r = client.post("/api/assets/depreciate", json={"period": "2026-09"}).json()["months"]
    assert r[0]["amount"] == 20095.0


def test_asset_remark_edit(client, seeded):
    r = client.post("/api/assets", json={"name": "厂房", "account": "160101", "acquired_date": "2026-02-28", "cost": 1000, "life_months": 240})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    r = client.put(f"/api/assets/{aid}/remark", json={"remark": "土地使用权面积 260.18㎡"})
    assert r.status_code == 200 and r.json()["remark"] == "土地使用权面积 260.18㎡"
    assert client.put("/api/assets/9999/remark", json={"remark": "x"}).status_code == 404
