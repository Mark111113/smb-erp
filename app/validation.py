from sqlalchemy import select
from .models import Partner, Material, Contract, ContractLine
from .operations import assert_open


def validate_contract(db, data):
    if not data.contract_no.strip() or not db.get(Partner, data.partner_id):
        raise ValueError("合同号和有效往来单位必填")
    if data.amount_tax < 0:
        raise ValueError("合同额不能为负")
    if data.lines and abs(round(sum(round(l.qty*l.price_tax, 2) for l in data.lines), 2)-data.amount_tax) > .01:
        raise ValueError("合同总额与明细合计不一致")
    for l in data.lines:
        if not db.get(Material, l.material_id):
            raise ValueError("合同物料不存在")


def validate_movement(db, data, legacy=False):
    assert_open(db, data.move_date)
    m = db.get(Material, data.material_id)
    if not m or (not m.active and not legacy):
        raise ValueError("请选择有效物料")
    if data.qty == 0 or (data.qty < 0 and data.move_type != "adjust"):
        raise ValueError("数量必须为正；库存调整可填负数")
    if getattr(m, "lot_control", False) and data.move_type in ("in", "opening") and not getattr(data, "lot_no", "x"):
        raise ValueError(f"{m.code} 按批次管理，收货要填批号")
    if data.partner_id and not db.get(Partner, data.partner_id):
        raise ValueError("往来单位不存在")
    if data.contract_id:
        c = db.get(Contract, data.contract_id)
        if not c or c.status != "active":
            raise ValueError("合同不存在或非有效状态")
        expected = "purchase" if data.move_type in ("in", "return_out") else "sales"
        if data.move_type in ("in", "out", "return_in", "return_out") and c.contract_type != expected:
            raise ValueError("出入库方向与合同不一致")
        if data.partner_id and data.partner_id != c.partner_id:
            raise ValueError("单位与合同不一致")
        data.partner_id = c.partner_id
        if not data.contract_line_id:
            ls = list(db.scalars(select(ContractLine).where(ContractLine.contract_id == c.id, ContractLine.material_id == data.material_id)))
            if len(ls) == 1:
                data.contract_line_id = ls[0].id
            elif not legacy:
                raise ValueError("请选择对应合同明细行")
    if data.contract_line_id:
        l = db.get(ContractLine, data.contract_line_id)
        if not l or l.contract_id != data.contract_id or l.material_id != data.material_id:
            raise ValueError("合同明细、物料和合同不一致")


def validate_document(db, data, kind):
    d = data.invoice_date if kind == "invoice" else data.pay_date
    if d is None:
        raise ValueError("业务日期必填")
    assert_open(db, d)
    if data.contract_id:
        c = db.get(Contract, data.contract_id)
        if not c or c.status != "active":
            raise ValueError("合同不存在或非有效状态")
        expected = "purchase" if data.direction in ("input", "pay") else "sales"
        if c.contract_type != expected or (data.partner_id and data.partner_id != c.partner_id):
            raise ValueError("合同方向或单位不一致")
        data.partner_id = c.partner_id
    if not data.partner_id or not db.get(Partner, data.partner_id):
        raise ValueError("往来单位必填")
    if kind == "invoice":
        if not data.invoice_no.strip() or data.amount_tax <= 0 or data.amount_ex_tax < 0 or data.tax_amount < 0:
            raise ValueError("发票号必填且金额必须有效；红字请从原票发起")
        if not data.amount_ex_tax and not data.tax_amount:
            raise ValueError("请填写发票原始不含税金额和税额，不再默认按13%猜测")
        if abs(round(data.amount_ex_tax+data.tax_amount, 2)-round(data.amount_tax, 2)) > .005:
            raise ValueError("发票价税合计不一致")
