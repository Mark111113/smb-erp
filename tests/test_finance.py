"""财务核心：凭证平衡、自动过账、往来净额、红冲、账表勾稽"""
from datetime import date

from app.models import Invoice, Payment
import app.finance as F


def _mk_partner(db, app_ctx):
    app_ctx.Partner = app_ctx.Partner  # noqa
    from app.models import Partner
    p = Partner(code="F01", name="财务测试单位", is_customer=True, is_supplier=True)
    db.add(p)
    db.flush()
    return p.id


def test_voucher_balance_enforced(db, app_ctx):
    try:
        F.create_voucher(db, date(2026, 1, 1), "manual", None, "不平衡",
                         [{"account": "100201", "debit": 100}, {"account": "6001", "credit": 90}])
        assert False, "应抛不平衡"
    except ValueError:
        pass


def test_sales_invoice_posting(db, app_ctx):
    pid = _mk_partner(db, app_ctx)
    inv = Invoice(direction="output", invoice_no="FI-001", invoice_date=date(2026, 1, 5),
                  partner_id=pid, amount_ex_tax=800, tax_amount=104, amount_tax=904)
    db.add(inv); db.flush()
    v = F.post_invoice(db, inv)
    db.commit()
    lines = {(l.account_code, l.debit, l.credit) for l in v.lines}
    assert ("1122", 904, 0) in lines           # 应收 价税合计
    assert ("6001", 0, 800) in lines           # 收入 不含税
    assert ("222101", 0, 104) in lines         # 销项税
    assert F.account_balance(db, "1122", pid) == 904


def test_purchase_invoice_deductive_mode(db, app_ctx):
    """价税分离：进项入账抵扣，库存按不含税"""
    pid = _mk_partner(db, app_ctx)
    inv = Invoice(direction="input", invoice_no="FI-002", invoice_date=date(2026, 1, 6),
                  partner_id=pid, amount_ex_tax=500, tax_amount=65, amount_tax=565)
    db.add(inv); db.flush()
    F.post_invoice(db, inv)
    db.commit()
    assert F.account_balance(db, "1234", pid) == 500          # 库存不含税
    assert F.account_balance(db, "222104") == 65         # 进项税先记待认证（v0.26，勾选后才转 222102）
    assert F.account_balance(db, "2202", pid) == -565    # 应付价税合计


def test_receipt_netting_ar_then_advance(db, app_ctx):
    pid = _mk_partner(db, app_ctx)
    inv = Invoice(direction="output", invoice_no="FI-003", invoice_date=date(2026, 1, 1),
                  partner_id=pid, amount_ex_tax=100, tax_amount=13, amount_tax=113)
    db.add(inv); db.flush()
    F.post_invoice(db, inv)
    # 先收 50（冲应收），再收 100（应收余额 63 + 预收 37）
    pm1 = Payment(direction="receive", partner_id=pid, amount=50, pay_date=date(2026, 1, 10))
    db.add(pm1); db.flush()
    F.post_payment(db, pm1)
    pm2 = Payment(direction="receive", partner_id=pid, amount=100, pay_date=date(2026, 1, 11))
    db.add(pm2); db.flush()
    F.post_payment(db, pm2)
    from app import operations as O
    O.allocate(db, pm1.id, inv.id, 50, pm1.pay_date)
    O.allocate(db, pm2.id, inv.id, 63, pm2.pay_date)
    O.synchronize(db)
    db.commit()
    assert F.account_balance(db, "1122", pid) == 0       # 应收清零
    assert F.account_balance(db, "2203", pid) == -37     # 预收余额（贷方为正、余额函数给负）
    assert F.account_balance(db, "100201") == 150


def test_payment_overpay_goes_prepay(db, app_ctx):
    pid = _mk_partner(db, app_ctx)
    pm = Payment(direction="pay", partner_id=pid, amount=300, pay_date=date(2026, 1, 2))
    db.add(pm); db.flush()
    F.post_payment(db, pm)
    db.commit()
    assert F.account_balance(db, "1123", pid) == 300     # 无应付 → 全额预付
    assert F.account_balance(db, "100201") == -300


def test_payment_settles_ap_before_prepay(db, app_ctx):
    """有应付余额时付款先冲应付，超付部分才进预付（回归：AP 贷方符号）"""
    pid = _mk_partner(db, app_ctx)
    inv = Invoice(direction="input", invoice_no="FI-P1", invoice_date=date(2026, 1, 1),
                  partner_id=pid, amount_ex_tax=500, tax_amount=65, amount_tax=565)
    db.add(inv); db.flush()
    F.post_invoice(db, inv)
    pm = Payment(direction="pay", partner_id=pid, amount=600, pay_date=date(2026, 1, 5))
    db.add(pm); db.flush()
    F.post_payment(db, pm)
    from app import operations as O
    O.allocate(db, pm.id, inv.id, 565, pm.pay_date)
    O.synchronize(db)
    db.commit()
    assert F.account_balance(db, "2202", pid) == 0       # 应付清零
    assert F.account_balance(db, "1123", pid) == 35      # 超付 35 进预付
    assert F.account_balance(db, "100201") == -600


def test_reversal_flips_and_marks(db, app_ctx):
    pid = _mk_partner(db, app_ctx)
    inv = Invoice(direction="output", invoice_no="FI-004", invoice_date=date(2026, 1, 3),
                  partner_id=pid, amount_ex_tax=10, tax_amount=1.3, amount_tax=11.3)
    db.add(inv); db.flush()
    v = F.post_invoice(db, inv)
    db.commit()
    ar0 = F.account_balance(db, "1122", pid)
    rv = F.reverse_voucher(db, v, "测试红冲")
    db.commit()
    assert v.status == "reversed"
    assert F.account_balance(db, "1122", pid) == 0       # 红冲后应收归零
    assert rv.voucher_no.startswith("记-")
    # 已冲销凭证不再计入余额
    assert abs(F.account_balance(db, "6001")) < 0.005


def test_goods_flow_postings(db, app_ctx):
    from app.models import StockMovement, Material
    db.add(Material(id=1, code="F-M", name="Test", mat_group="01"))
    db.flush()
    pid = _mk_partner(db, app_ctx)
    gin = StockMovement(doc_no="IN-T1", move_type="in", material_id=1, qty=10, unit_cost=100.0,
                        move_date=date(2026, 1, 1), status="confirmed", partner_id=pid)
    gout = StockMovement(doc_no="OUT-T1", move_type="out", material_id=1, qty=4, unit_cost=100.0,
                         move_date=date(2026, 1, 2), status="confirmed", partner_id=pid)
    db.add_all([gin, gout]); db.flush()
    v1 = F.post_movement_confirm(db, gin)
    v2 = F.post_movement_confirm(db, gout)
    db.commit()
    assert F.account_balance(db, "1234", pid) == -1000  # GR/IR 贷方=货到票未到（出库不动此科目）
    assert F.account_balance(db, "1405") == 600          # 库存净增
    assert F.account_balance(db, "6401") == 400          # 出库成本


def test_trial_balance_and_bs_agree(db, app_ctx):
    tb = F.trial_balance(db)
    assert abs(sum(r["debit"] for r in tb) - sum(r["credit"] for r in tb)) < 0.005
    bs = F.balance_sheet(db, date.today())
    assert bs["balanced"], "资产负债表必须配平"
