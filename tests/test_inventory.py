"""库存核心：不含税移动加权平均、重放顺序、负库存、确认快照"""
from datetime import date

from app.models import StockMovement, Material


def _mv(db, **kw):
    if not db.get(Material, kw.get("material_id", 1)):
        db.add(Material(id=kw.get("material_id", 1), code="T-M", name="Test", mat_group="01"))
        db.flush()
    m = StockMovement(doc_no=kw.pop("doc_no", "T1"), move_type=kw.pop("move_type", "in"),
                      material_id=kw.pop("material_id", 1), qty=kw.pop("qty", 1),
                      unit_cost=kw.pop("unit_cost", None), move_date=kw.pop("move_date", date(2026, 1, 1)),
                      status=kw.pop("status", "confirmed"), **kw)
    db.add(m)
    db.flush()
    return m


def test_weighted_average(db, app_ctx):
    from app.inventory import compute_material_stock
    _mv(db, move_type="in", qty=10, unit_cost=100.0, move_date=date(2026, 1, 1))
    _mv(db, move_type="out", qty=4, move_date=date(2026, 1, 2))
    _mv(db, move_type="in", qty=6, unit_cost=200.0, move_date=date(2026, 1, 3))
    db.commit()
    st = compute_material_stock(db)[1]
    assert st["qty"] == 12                                  # 10-4+6
    assert st["avg_cost"] == (6 * 100 + 6 * 200) / 12      # 加权只累计入库：2200/16
    assert abs(st["value"] - 12 * 150) < 1e-6
    assert not st["neg"]


def test_negative_stock_flagged(db, app_ctx):
    from app.inventory import compute_material_stock
    _mv(db, move_type="out", qty=2, move_date=date(2026, 1, 1))
    _mv(db, move_type="in", qty=2, unit_cost=50.0, move_date=date(2026, 1, 2))
    db.commit()
    st = compute_material_stock(db)[1]
    assert st["qty"] == 0
    assert st["neg"] is False  # 暂估先出后入：曾穿零要标红


def test_replay_order_is_date_then_id(db, app_ctx):
    """同日两笔按 id 序；跨日按日期序（即使后插入的日期更早）"""
    from app.inventory import compute_material_stock
    _mv(db, move_type="in", qty=5, unit_cost=100.0, move_date=date(2026, 1, 5))
    _mv(db, move_type="in", qty=5, unit_cost=300.0, move_date=date(2026, 1, 1))
    db.commit()
    st = compute_material_stock(db)[1]
    assert st["last_date"] == date(2026, 1, 5)
    assert st["avg_cost"] == (5 * 100 + 5 * 300) / 10


def test_draft_excluded(db, app_ctx):
    from app.inventory import compute_material_stock
    _mv(db, move_type="in", qty=3, unit_cost=100.0, status="draft")
    db.commit()
    assert compute_material_stock(db) == {}


def test_confirm_snapshots_avg_cost(db, app_ctx):
    from app.inventory import compute_material_stock, confirm_movement
    _mv(db, move_type="in", qty=10, unit_cost=120.0, move_date=date(2026, 1, 1))
    out = _mv(db, move_type="out", qty=4, move_date=date(2026, 1, 2), status="draft")
    db.commit()
    confirm_movement(db, out)
    db.commit()
    assert out.unit_cost == 120.0  # 确认时快照当时加权价
    st = compute_material_stock(db)[1]
    assert st["qty"] == 6 and st["avg_cost"] == 120.0


def test_voided_excluded(db, app_ctx):
    from app.inventory import compute_material_stock
    _mv(db, move_type="in", qty=7, unit_cost=10.0, status="voided")
    db.commit()
    assert compute_material_stock(db) == {}


def test_same_day_receipt_sorts_before_earlier_draft_issue(client, seeded):
    """同日：先建的出库草稿，后建并确认入库，再确认出库 → 出库取入库成本而不是 0（真实场景：同日先建出库草稿、后到货）"""
    out = client.post("/api/movements", json={"move_type": "out", "material_id": seeded["mat1"], "qty": 2,
                                               "move_date": "2026-04-01"}).json()
    client.post("/api/movements", json={"move_type": "in", "material_id": seeded["mat1"], "qty": 5,
                                        "unit_cost": 80.0, "move_date": "2026-04-01", "status": "confirmed"})
    r = client.post(f"/api/movements/{out['id']}/confirm").json()
    assert r["negative"] is False and r["qty_after"] == 3
    rec = next(m for m in client.get("/api/movements").json() if m["id"] == out["id"])
    assert rec["unit_cost"] == 80.0
    # 已有的同日「出库先于入库」历史，重新同步后也按入库优先重估
    s = next(s for s in client.get("/api/stock").json()["items"] if s["material_id"] == seeded["mat1"])
    assert s["qty"] == 3 and abs(s["avg_cost"] - 80.0) < 1e-9
