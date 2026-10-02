"""不含税移动加权平均；负库存为暂估，后续来货按来货成本重估缺口。

重放顺序 = (日期, 档位, id)。同一天里采购入库/期初/盘盈（档位 0）排在其余单据（档位 1）前面：
单据只到「日」，同日先到货后发货是常态；若按 id，早建的出库草稿会排在后建的入库前取到 0 成本
（实际遇到过：同日先建出库草稿、后到货确认）。组拆套、销售退货仍在档位 1 按 id：前者须先耗后产，后者须在原出库之后。

制造（v0.35）：工单领料 wo_issue（出）、退料 wo_return（入，按该工单该物料的领料均价退回）、
完工入库 wo_receipt（入，成本 = 工单在制材料余额按数量摊；已关闭工单的最后一张入库单吃掉全部余额；
委外工单另加加工费单价 × 数量）、成本分摊 wo_cost（只加金额不加数量，月末把人工和制造费用加到在库产品上）。
都在档位 1 按 id，和组拆套一样「先耗后产」。
"""
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import StockMovement, ConversionMovement, StockConversion, DocumentLink, WorkOrder

INCOMING = ("in", "opening", "return_in", "wo_return", "wo_receipt", "wo_cost")


END_OF_DAY = (2, 0)   # order_key 档位之后，用于「截至某日日终」


def _rank(m, conversion_ids):
    if m.id in conversion_ids:
        return 1
    return 0 if m.move_type in ("in", "opening") or (m.move_type == "adjust" and m.qty > 0) else 1


def order_key(m, conversion_ids):
    return (m.move_date, _rank(m, conversion_ids), m.id)


def end_of_day(d):
    return (d, *END_OF_DAY)


def movement_key(session, m):
    """某张单据在重放序列中的位置，供 replay(before=...) 取「它之前」的库存状态。"""
    return order_key(m, {x.movement_id for x in session.scalars(select(ConversionMovement))})


def replay(session, material_id=None, before=None):
    """before：order_key 形式的截止点（不含），见 movement_key / end_of_day。"""
    q = select(StockMovement).where(StockMovement.status == "confirmed")
    links = {x.movement_id:x.conversion_id for x in session.scalars(select(ConversionMovement))}
    conversions = {x.id:x for x in session.scalars(select(StockConversion))}
    transfer_totals, transfer_assigned, transfer_index = {}, {}, {}
    returns = {x.child_id:x.original_id for x in session.scalars(select(DocumentLink).where(DocumentLink.kind == "stock_return"))}
    historical_costs = {}
    state, entries = {}, []
    wos = {w.id: w for w in session.scalars(select(WorkOrder))}
    moves = sorted(session.scalars(q), key=lambda m: order_key(m, links))
    last_receipt = {m.work_order_id: m.id for m in moves if m.move_type == "wo_receipt"}
    wo_mat, wo_recv, wo_issued = {}, {}, {}    # 工单在制材料余额、已入库数量、(工单, 物料) 领料 [数量, 金额]
    for m in moves:
        if before and order_key(m, links) >= before:
            continue
        s = state.setdefault(m.material_id, dict(qty=0.0, value=0.0, avg_cost=0.0, last_date=None, neg=False))
        old_value, old_qty = s["value"], s["qty"]
        conversion_id = links.get(m.id)
        incoming = m.move_type in INCOMING or (m.move_type == "adjust" and m.qty > 0)
        if m.move_type == "wo_cost":
            s["value"] += m.amount or 0
            if s["qty"] > 1e-9:
                s["avg_cost"] = s["value"] / s["qty"]
            s["last_date"] = m.move_date
            entries.append((m, 0.0, round(s["value"], 2) - round(old_value, 2)))
            continue
        if incoming:
            cost = m.unit_cost if m.unit_cost is not None else s["avg_cost"]
            if m.move_type == "wo_return":
                iq, iv = wo_issued.get((m.work_order_id, m.material_id), (0.0, 0.0))
                cost = iv / iq if iq > 1e-9 else s["avg_cost"]
                wo_mat[m.work_order_id] = wo_mat.get(m.work_order_id, 0.0) - round(m.qty * cost, 2)
                wo_issued[(m.work_order_id, m.material_id)] = (iq - m.qty, iv - round(m.qty * cost, 2))
            elif m.move_type == "wo_receipt":
                w = wos.get(m.work_order_id)
                bal, recv = wo_mat.get(m.work_order_id, 0.0), wo_recv.get(m.work_order_id, 0.0)
                if w and w.status == "closed" and last_receipt.get(w.id) == m.id:
                    share = bal
                else:
                    left = max((w.qty if w else 0) - recv, m.qty)
                    share = round(bal * m.qty / left, 2) if left > 1e-9 else 0.0
                fee = round((w.fee_price or 0) * m.qty, 2) if w and w.kind == "subcontract" else 0.0
                wo_mat[m.work_order_id] = bal - share
                wo_recv[m.work_order_id] = recv + m.qty
                cost = (share + fee) / m.qty
            if m.move_type == "return_in" and m.id in returns:
                cost = historical_costs[returns[m.id]]
            if conversion_id:
                weights = conversions[conversion_id].detail["weights"]
                index = transfer_index.get(conversion_id, 0)
                total = transfer_totals.get(conversion_id, 0)
                assigned = transfer_assigned.get(conversion_id, 0)
                share = round(total-assigned, 2) if index == len(weights)-1 else round(total*weights[index]/sum(weights), 2)
                cost = share/m.qty
                transfer_index[conversion_id] = index+1
                transfer_assigned[conversion_id] = assigned+share
            if old_qty < 0:
                # 负库存缺口按本次入库成本重估，差额由财务成本调整承接。
                s["value"] = old_qty * cost
            s["qty"] += m.qty
            s["value"] += m.qty * cost
            s["avg_cost"] = s["value"] / s["qty"] if s["qty"] > 1e-9 else cost
        else:
            cost = s["avg_cost"]
            qty = abs(m.qty)
            if conversion_id and old_qty+1e-9 < qty:
                raise ValueError("变更将导致后续组拆套来源库存不足，请先作废受影响组拆套单")
            s["qty"] -= qty
            s["value"] -= qty * cost
        if abs(s["qty"]) < 1e-9:
            s["qty"], s["value"] = 0.0, 0.0
        s["last_date"], s["neg"] = m.move_date, s["qty"] < -1e-9
        entries.append((m, cost, round(s["value"], 2) - round(old_value, 2)))
        historical_costs[m.id] = cost
        if conversion_id and not incoming:
            transfer_totals[conversion_id] = transfer_totals.get(conversion_id, 0) + round(old_value, 2)-round(s["value"], 2)
        if m.move_type == "wo_issue":
            out_value = round(old_value, 2) - round(s["value"], 2)
            wo_mat[m.work_order_id] = wo_mat.get(m.work_order_id, 0.0) + out_value
            iq, iv = wo_issued.get((m.work_order_id, m.material_id), (0.0, 0.0))
            wo_issued[(m.work_order_id, m.material_id)] = (iq + abs(m.qty), iv + out_value)
    return ({k:v for k,v in state.items() if material_id is None or k == material_id},
            [e for e in entries if material_id is None or e[0].material_id == material_id])


def compute_material_stock(session: Session, material_id=None):
    return replay(session, material_id)[0]


def confirm_movement(session: Session, m: StockMovement):
    if m.id is None:
        session.add(m)
        session.flush()
    if m.move_type not in ("in", "opening", "return_in", "wo_cost"):
        st = replay(session, m.material_id, before=movement_key(session, m))[0].get(m.material_id)
        m.unit_cost = st["avg_cost"] if st else 0.0
    elif m.unit_cost is None:
        m.unit_cost = 0.0
    m.status = "confirmed"
    m.confirmed_at = datetime.now()
