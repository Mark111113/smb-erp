from datetime import date
from sqlalchemy import select
from app.models import Voucher, VoucherLine, StockMovement
from app import finance as F, operations as O


def post(c, url, body):
    r = c.post(url, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def move(c, mid, kind, qty, cost=None, d="2026-01-01", **extra):
    return post(c, "/api/movements", dict(material_id=mid, move_type=kind, qty=qty, unit_cost=cost, move_date=d, status="confirmed", **extra))


def invoice(c, no="X", total=113, net=100, **extra):
    return post(c, "/api/invoices", dict(direction="input", invoice_no=no, invoice_date="2026-01-02", partner_id=1, amount_ex_tax=net, tax_amount=total-net, amount_tax=total, **extra))


def payment(c, amount=113, **extra):
    return post(c, "/api/payments", dict(direction="pay", partner_id=1, amount=amount, pay_date="2026-01-03", **extra))


def check(c):
    r = c.get("/api/reconciliation")
    assert r.status_code == 200, r.text
    r = r.json()
    assert abs(r["inventory_gap"]) < .01
    assert abs(r["trial_gap"]) < .01


def test_average_and_historical_revaluation(client, seeded, app_ctx):
    mid = seeded["mat1"]
    move(client, mid, "in", 10, 100)
    out = move(client, mid, "out", 4, d="2026-01-02")
    move(client, mid, "in", 6, 200, d="2026-01-03")
    st = client.get("/api/stock").json()["items"][0]
    assert st["value"] == 1800 and st["avg_cost"] == 150
    check(client)
    move(client, mid, "in", 10, 200, d="2025-12-31")
    with app_ctx.SessionLocal() as db:
        assert db.get(StockMovement, out["id"]).unit_cost == 150
        assert F.account_balance(db,"6401") == 600
        assert O.synchronize(db) == {"created_ids":[],"reversed_ids":[]}
    check(client)


def test_receipt_invoice_settle_and_void(client, seeded, app_ctx):
    m = move(client,seeded["mat1"],"in",1,100,partner_id=1)
    i = invoice(client)
    p = payment(client)
    a = post(client,"/api/allocations",dict(payment_id=p["id"],invoice_id=i["id"],amount=113,allocation_date="2026-01-03"))
    with app_ctx.SessionLocal() as db:
        assert F.account_balance(db,"1405") == 100
        assert F.account_balance(db,"1234",1) == 0
        assert F.account_balance(db,"2202",1) == 0
        assert F.account_balance(db,"1123",1) == 0
    assert client.get("/api/payments").status_code == 200
    post(client,f'/api/movements/{m["id"]}/void',{"reason":"测试"})
    check(client)
    post(client,f'/api/invoices/{i["id"]}/void',{"reason":"wrong invoice"})
    assert client.get("/api/invoices").json() == []
    assert client.get("/api/allocations").json()[0]["status"] == "voided"
    with app_ctx.SessionLocal() as db:
        assert F.account_balance(db,"1405") == 0
        assert F.account_balance(db,"2202",1) == 0
        assert F.account_balance(db,"1123",1) == 113


def test_profit_and_period_boundaries(client, seeded, app_ctx):
    move(client,seeded["mat1"],"in",10,100)
    move(client,seeded["mat1"],"out",4)
    with app_ctx.SessionLocal() as db:
        assert F.income_statement(db,date(2026,1,1),date(2026,1,31))["profit"] == -400
        tb = next(x for x in F.trial_balance(db) if x["code"] == "1405")
        assert tb["opening"] == 0 and tb["closing"] == 600
        assert F.ledger(db,"1405",from_date=date(2026,1,1))[-1]["balance"] == 600


def test_negative_stock_cost_trueup_and_adjust(client, seeded, app_ctx):
    move(client,seeded["mat1"],"out",3)
    move(client,seeded["mat1"],"in",5,100,d="2026-01-02")
    check(client)
    with app_ctx.SessionLocal() as db:
        assert F.account_balance(db,"6401") == 300
    move(client,seeded["mat1"],"adjust",-1,d="2026-01-03")
    check(client)
    with app_ctx.SessionLocal() as db:
        assert F.account_balance(db,"671101") == 100


def test_closed_requires_fulfillment(client, seeded):
    cid = post(client,"/api/contracts",dict(contract_no="C",contract_type="purchase",partner_id=1,amount_tax=113,lines=[dict(material_id=seeded["mat1"],qty=1,price_tax=113)]))["id"]
    invoice(client,contract_id=cid)
    payment(client,contract_id=cid)
    assert client.get("/api/three-way-match").json()[0]["status"] == "ir_gap"
    move(client,seeded["mat1"],"in",1,100,contract_id=cid)
    assert client.get("/api/three-way-match").json()[0]["status"] == "closed"


def test_contract_line_identity_and_guard(client, seeded):
    body=dict(contract_no="C",contract_type="purchase",partner_id=1,amount_tax=113,lines=[dict(material_id=seeded["mat1"],qty=1,price_tax=113)])
    cid=post(client,"/api/contracts",body)["id"]
    move(client,seeded["mat1"],"in",1,100,contract_id=cid)
    lid=client.get(f"/api/contracts/{cid}").json()["lines"][0]["id"]
    body["lines"][0]["id"]=lid
    assert client.put(f"/api/contracts/{cid}",json=body).status_code == 200
    assert client.get(f"/api/contracts/{cid}").json()["lines"][0]["id"] == lid
    body["lines"]=[]
    assert client.put(f"/api/contracts/{cid}",json=body).status_code == 400


def test_validation_and_lock(client, seeded):
    bad=client.post("/api/vouchers",json={"voucher_date":"2026-01-01","lines":[{"account":"BAD","debit":1},{"account":"100201","credit":1}]})
    assert bad.status_code == 400
    post(client,"/api/periods/2026-01/lock",{"reason":"close"})
    assert client.post("/api/movements",json=dict(move_type="in",material_id=seeded["mat1"],qty=1,unit_cost=100,move_date="2026-01-01")).status_code == 400
    post(client,"/api/periods/2026-01/unlock",{"reason":"correct"})
    move(client,seeded["mat1"],"in",1,100)


def test_credit_refund_allocation_caps(client, seeded):
    i=invoice(client)
    p=payment(client)
    credit=post(client,f'/api/invoices/{i["id"]}/credit',dict(reason="return",document_date="2026-01-04",amount=56.5,net=50,tax=6.5,invoice_no="RED"))
    refund=post(client,f'/api/payments/{p["id"]}/refund',dict(reason="return",document_date="2026-01-04",amount=56.5))
    body=dict(payment_id=p["id"],invoice_id=i["id"],amount=113,allocation_date="2026-01-04")
    assert client.post("/api/allocations",json=body).status_code == 400
    body["amount"]=56.5
    post(client,"/api/allocations",body)
    rows=client.get("/api/settlement-open-items?asof=2026-01-05").json()
    assert all(x["remaining"] == 0 for x in rows)
    check(client)


def test_conversion_conserves_value_and_reprices(client,seeded):
    parent,child=seeded["mat1"],seeded["mat2"]
    post(client,"/api/material-bom",dict(parent_material_id=parent,child_material_id=child,qty_per=2))
    move(client,parent,"in",10,100)
    payload=dict(kind="disassemble",move_date="2026-01-03",parent_material_id=parent,qty=2,components=[dict(material_id=child,qty=4)])
    assert client.post("/api/conversions",json=payload).status_code == 400
    payload["components"][0]["weight"]=1
    cv=post(client,"/api/conversions",payload)
    stock=client.get("/api/stock").json()["items"]
    assert sum(x["value"] for x in stock)==1000
    check(client)
    move(client,parent,"in",10,200,d="2025-12-31")
    check(client)
    childstock=next(x for x in client.get("/api/stock").json()["items"] if x["material_id"]==child)
    assert childstock["value"]==300
    post(client,f'/api/conversions/{cv["id"]}/void',dict(reason="correction"))
    check(client)


def test_auto_allocate_same_contract(client, seeded):
    r = client.post("/api/contracts", json={"contract_no": "P-AUTO-1", "contract_type": "purchase", "partner_id": 1,
                                            "sign_date": "2026-03-01", "amount_tax": 113, "lines": [
                                                {"material_id": seeded["mat1"], "qty": 1, "price_tax": 113}]}).json()
    client.post("/api/payments", json={"direction": "pay", "partner_id": 1, "contract_id": r["id"], "amount": 100, "pay_date": "2026-03-02"})
    client.post("/api/payments", json={"direction": "pay", "partner_id": 1, "amount": 50, "pay_date": "2026-03-02"})   # 未挂合同：不动
    client.post("/api/invoices", json={"direction": "input", "invoice_no": "INV-AUTO-1", "invoice_date": "2026-03-10", "partner_id": 1,
                                       "contract_id": r["id"], "amount_tax": 113, "amount_ex_tax": 100, "tax_amount": 13})
    done = client.post("/api/allocations/auto").json()["allocated"]
    assert [(x["amount"], x["date"]) for x in done] == [(100, "2026-03-10")]
    assert client.post("/api/allocations/auto").json()["allocated"] == []         # 幂等


def test_draft_in_locked_month_confirms_with_new_date(client, seeded):
    """9 月已锁，挂在 9 月的暂估草稿仍可按 10 月实际日期确认（草稿不入账）；不改日期确认会被拒"""
    m = client.post("/api/movements", json={"move_type": "in", "material_id": seeded["mat1"], "qty": 1, "unit_cost": 10.0,
                                            "move_date": "2026-09-15"}).json()
    assert client.post("/api/periods/2026-09/lock", json={"reason": "close"}).status_code == 200
    assert client.post("/api/movements/batch-confirm", json={"ids": [m["id"]]}).status_code == 400
    r = client.post("/api/movements/batch-confirm", json={"ids": [m["id"]], "move_date": "2026-10-05"})
    assert r.status_code == 200, r.text
    assert next(x for x in client.get("/api/movements").json() if x["id"] == m["id"])["move_date"] == "2026-10-05"
