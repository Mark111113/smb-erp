"""银行流水：导入去重、自动匹配（单笔/合计/冲正）、人工关联、非贸易入账与撤销、月末核对"""

HEAD = ",".join(["交易类型[ a ]", "业务类型[ b ]", "付款人开户行号[ c ]", "付款人开户行名[ d ]", "付款人账号[ e ]",
                 "付款人名称[ f ]", "收款人开户行行号[ g ]", "收款人开户行名[ h ]", "收款人账号[ i ]", "收款人名称[ j ]",
                 "交易日期[ k ]", "交易时间[ l ]", "交易货币[ m ]", "交易金额[ n ]", "交易后余额[ o ]", "起息日期[ p ]",
                 "汇率[ q ]", "交易流水号[ r ]", "客户申请号[ s ]", "摘要[ t ]", "用途[ u ]", "交易附言[ v ]"])
ME = "测试本公司"


def _row(no, d, amt, cp, bal, biz="网上支付", memo=""):
    pay = amt.startswith("-")
    payer, payee = (ME, cp) if pay else (cp, ME)
    vals = ["往账", biz, "1\t", "行\t", "1\t", payer + "\t", "2\t", "行\t", "2\t", payee + "\t", d + "\t", "10:00:00\t",
            "CNY\t", amt, bal, d, "1", no + "\t", "x", "", "", memo]
    return ",".join(f'"{v}"' for v in vals)


def _csv(rows):
    top = ['﻿查询账号[ Inquirer account number ],"600000000001\t"'] + ["x,1"] * 6
    return "\n".join(top + [HEAD] + rows) + "\n"


def _setup(client, seeded):
    pay = client.post("/api/payments", json={"direction": "pay", "partner_id": 1, "amount": 100,
                                             "pay_date": "2026-03-03"}).json()
    r1 = client.post("/api/payments", json={"direction": "receive", "partner_id": 2, "amount": 30,
                                            "pay_date": "2026-03-05"}).json()
    r2 = client.post("/api/payments", json={"direction": "receive", "partner_id": 2, "amount": 20,
                                            "pay_date": "2026-03-05"}).json()
    # 同日另一笔不相干的收款，不能被凑进 N3
    client.post("/api/payments", json={"direction": "receive", "partner_id": 2, "amount": 7, "pay_date": "2026-03-05"})
    content = _csv([
        _row("N1", "20260301", "1,000.00", "某股东", "1,000.00", memo="投资款"),
        _row("N2", "20260303", "-100.00", "测试供应商有限公司", "900.00"),
        _row("N3", "20260305", "50.00", "测试客户", "950.00"),
        _row("N4", "20260306", "-80.00", "装修公司", "870.00"),
        _row("N5", "20260306", "80.00", "装修公司", "950.00", biz="冲正"),
        _row("N6", "20260402", "-2.50", "", "947.50", biz="代收费"),
    ])
    return pay, r1, r2, content


def test_import_dedupe_and_auto_match(client, seeded):
    pay, r1, r2, content = _setup(client, seeded)
    r = client.post("/api/bank/import", json={"filename": "a.csv", "content": content})
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 6 and r.json()["matched"] == 2 and r.json()["ignored"] == 2
    again = client.post("/api/bank/import", json={"filename": "b.csv", "content": content}).json()
    assert again["added"] == 0 and again["duplicate"] == 6
    rows = {t["txn_no"]: t for t in client.get("/api/bank/txns").json()}
    assert [p["id"] for p in rows["N2"]["payments"]] == [pay["id"]]
    assert sorted(p["id"] for p in rows["N3"]["payments"]) == sorted([r1["id"], r2["id"]])   # 同日合计
    assert rows["N4"]["status"] == rows["N5"]["status"] == "ignored"
    assert rows["N1"]["status"] == "unmatched"


def test_post_unpost_and_reconcile(client, seeded):
    _, _, _, content = _setup(client, seeded)
    client.post("/api/bank/import", json={"filename": "a.csv", "content": content})
    rows = {t["txn_no"]: t for t in client.get("/api/bank/txns").json()}
    n1, n6 = rows["N1"], rows["N6"]
    bad = client.post(f"/api/bank/txns/{n1['id']}/post", json={"lines": [{"account": "2241", "amount": 999}]})
    assert bad.status_code == 400 and "≠" in bad.json()["detail"]
    ok = client.post(f"/api/bank/txns/{n1['id']}/post", json={"category": "股东投资",
                                                               "lines": [{"account": "2241", "amount": 1000}]})
    assert ok.status_code == 200, ok.text
    assert client.post(f"/api/bank/txns/{n6['id']}/post", json={"lines": [{"account": "660301", "amount": 2.5}]}).status_code == 200
    rec = {r["month"]: r for r in client.get("/api/bank/reconcile").json()}
    assert rec["2026-03"]["statement"] == 950 and rec["2026-03"]["gap"] == -7     # 多出的 7 元 ERP 有、银行没有 → 差额 -7
    assert rec["2026-04"]["statement"] == 947.5 and rec["2026-04"]["gap"] == -7
    assert client.post(f"/api/bank/txns/{n1['id']}/unpost", json={"note": "测试"}).status_code == 200
    rec = {r["month"]: r for r in client.get("/api/bank/reconcile").json()}
    assert rec["2026-03"]["gap"] == 993
    assert client.get("/api/change-log?table=bank_txn").json()[0]["action"] == "撤销流水入账"


def test_manual_link_requires_exact_total(client, seeded):
    _, r1, r2, content = _setup(client, seeded)
    client.post("/api/bank/import", json={"filename": "a.csv", "content": content})
    n3 = next(t for t in client.get("/api/bank/txns").json() if t["txn_no"] == "N3")
    assert client.post(f"/api/bank/txns/{n3['id']}/unlink").status_code == 200
    r = client.post(f"/api/bank/txns/{n3['id']}/link", json={"payment_ids": [r1["id"]]})
    assert r.status_code == 400 and "≠" in r.json()["detail"]
    cands = client.get(f"/api/bank/txns/{n3['id']}/candidates").json()
    assert len(cands) == 3
    ids = [c["id"] for c in cands if c["amount"] in (30, 20)]
    assert client.post(f"/api/bank/txns/{n3['id']}/link", json={"payment_ids": ids}).status_code == 200


def test_close_check_suggestions(client, seeded):
    """月结检查给未处理流水的线索：发工资按未发月份先进先出；供应商款 = 未付 − 其他合同多付"""
    mk = lambda no, amt: client.post("/api/contracts", json={
        "contract_no": no, "contract_type": "purchase", "partner_id": 1, "amount_tax": amt, "sign_date": "2026-08-01",
        "lines": [{"material_id": seeded["mat1"], "qty": 1, "price_tax": amt, "amount_tax": amt}]}).json()
    a, b = mk("T-PA", 100.0), mk("T-PB", 200.0)
    client.post("/api/payments", json={"direction": "pay", "partner_id": 1, "contract_id": a["id"], "amount": 150,
                                       "pay_date": "2026-08-02"})
    for d in ("2026-08-31", "2026-09-30"):
        r = client.post("/api/vouchers", json={"voucher_date": d, "summary": "计提工资", "lines": [
            {"account": "660201", "debit": 8500}, {"account": "221101", "credit": 8500},
            {"account": "221101", "debit": 1917.5}, {"account": "221102", "credit": 1917.5}]})
        assert r.status_code == 200, r.text
    client.post("/api/bank/import", json={"filename": "s.csv", "content": _csv([
        _row("E1", "20260905", "-6,582.50", "某员工", "1.00"),
        _row("E2", "20260928", "-6,582.50", "某员工", "1.00", memo="稿费、演出费等劳务收入"),
        _row("S1", "20260910", "-150.00", "测试供应商有限公司", "1.00"),
    ])})
    rows = {t["txn_no"]: t for t in client.get("/api/bank/txns").json()}
    assert client.post(f"/api/bank/txns/{rows['E1']['id']}/post", json={"category": "工资社保", "lines": [
        {"account": "221101", "amount": 6582.5}]}).status_code == 200
    client.post("/api/todos", json={"title": "T-PA 多付 50 待抵下次货款", "level": "warn"})
    client.post("/api/todos", json={"title": "无关的事", "level": "info"})
    items = {x["key"]: x for x in client.get("/api/close-check?month=2026-09").json()["items"]}
    assert [r["title"] for r in items["todos"]["refs"]] == ["T-PA 多付 50 待抵下次货款"]
    item = items["bank_unmatched"]
    refs = {r["id"]: r for r in item["refs"]}
    assert item["count"] == 2
    wage = " ".join(s["text"] for s in refs[rows["E2"]["id"]]["suggestions"])
    assert "221101" in wage and "2026-09～2026-09 共 1 个月" in wage
    sup = refs[rows["S1"]["id"]]["suggestions"]
    hit = [s for s in sup if s["type"] == "contract_with_credit"]
    assert hit and hit[0]["contract_no"] == "T-PB" and hit[0]["credit_contract_no"] == "T-PA"
