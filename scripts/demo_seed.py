"""演示数据：在一个空数据目录里建一家虚构公司和一个月的业务，起来就能点着看。

    python scripts/demo_seed.py <数据目录> [--admin demo]      # 管理员密码交互输入（或 --password-stdin）

只认空库（已有账号就拒绝，不会碰真实数据）。全部走页面同一套接口，所以库存、凭证、四流都是系统真算出来的。
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

COMPANY = {"name": "示例智能科技有限公司", "short_name": "示例智能", "tax_no": "91320000MA1EXAMPL1",
           "address": "示例省示例市科技路 1 号", "phone": "0512-00000000", "legal_rep": "张三",
           "bank_accounts": [{"bank": "示例银行城东支行", "account_no": "600000000001", "account_code": "100201", "default": True}]}
PARTNERS = [
    {"code": "SUP-A", "name": "示例电子元件有限公司", "short_name": "元件A", "is_supplier": True},
    {"code": "SUP-B", "name": "示例磁性器件有限公司", "short_name": "磁件B", "is_supplier": True},
    {"code": "CUS-A", "name": "示例空调设备有限公司", "short_name": "空调A", "is_customer": True},
    {"code": "CUS-B", "name": "示例热泵科技有限公司", "short_name": "热泵B", "is_customer": True, "is_supplier": True},
]
MATERIALS = [
    {"key": "drv", "name": "变频驱动器", "spec": "DEMO-DRV-3KW", "mat_group": "01", "unit": "个"},
    {"key": "ctl", "name": "主控板", "spec": "DEMO-CTL-V2", "mat_group": "02", "unit": "个"},
    {"key": "rea", "name": "电抗器", "spec": "DEMO-L-10mH", "mat_group": "03", "unit": "个"},
    {"key": "thm", "name": "温控器", "spec": "DEMO-T100", "mat_group": "04", "unit": "个"},
]


def seed(client, month="2026-03"):
    def ok(r):
        assert r.status_code == 200, r.text
        return r.json()

    d = lambda day: f"{month}-{day:02d}"
    ok(client.put("/api/company", json=COMPANY))
    pid = {p["code"]: ok(client.post("/api/partners", json=p))["id"] for p in PARTNERS}
    mid = {m["key"]: ok(client.post("/api/materials", json={k: v for k, v in m.items() if k != "key"}))["id"] for m in MATERIALS}

    def contract(no, ctype, partner, day, lines):
        body = {"contract_no": no, "contract_type": ctype, "partner_id": pid[partner], "sign_date": d(day),
                "lines": [{"material_id": mid[k], "qty": q, "price_tax": p} for k, q, p in lines],
                "amount_tax": round(sum(q * p for _, q, p in lines), 2)}
        return ok(client.get(f"/api/contracts/{ok(client.post('/api/contracts', json=body))['id']}"))

    def deliver(c, day, qtys=None):
        lines = [{"line_id": l["id"], "qty": (qtys or {}).get(i, l["qty"])} for i, l in enumerate(c["lines"])]
        ok(client.post(f"/api/contracts/{c['id']}/fulfill", json={"move_date": d(day), "lines": lines}))

    def invoice(c, direction, no, day, rate=0.13):
        total = c["amount_tax"]
        net = round(total / (1 + rate), 2)
        ok(client.post("/api/invoices", json={"direction": direction, "invoice_no": no, "invoice_date": d(day),
                                               "partner_id": c["partner_id"], "contract_id": c["id"], "amount_tax": total,
                                               "amount_ex_tax": net, "tax_amount": round(total - net, 2)}))

    def pay(c, direction, day, amount=None):
        ok(client.post("/api/payments", json={"direction": direction, "partner_id": c["partner_id"], "contract_id": c["id"],
                                               "amount": amount if amount is not None else c["amount_tax"], "pay_date": d(day)}))

    # 采购两张：一张四流闭合，一张货到了票还没来
    p1 = contract("CG2603001", "purchase", "SUP-A", 2, [("drv", 50, 339.0), ("ctl", 50, 113.0)])
    deliver(p1, 6); invoice(p1, "input", "26320000000000000101", 8); pay(p1, "pay", 10)
    p2 = contract("CG2603002", "purchase", "SUP-B", 3, [("rea", 100, 45.2)])
    deliver(p2, 9); pay(p2, "pay", 4, 2260.0)
    # 销售两张：一张闭合，一张部分发货
    s1 = contract("XS2603001", "sales", "CUS-A", 12, [("drv", 30, 565.0), ("rea", 30, 79.1)])
    deliver(s1, 15); invoice(s1, "output", "26320000000000000201", 16); pay(s1, "receive", 25)
    s2 = contract("XS2603002", "sales", "CUS-B", 18, [("drv", 15, 559.35), ("ctl", 20, 180.8)])
    deliver(s2, 22, {0: 10, 1: 20})
    ok(client.post("/api/todos", json={"title": "热泵B 剩 5 台驱动器下月初发", "level": "warn"}))
    return {"partners": pid, "materials": mid}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dir")
    ap.add_argument("--admin", default="demo")
    ap.add_argument("--password-stdin", action="store_true")
    a = ap.parse_args()
    pwd = sys.stdin.readline().rstrip("\n") if a.password_stdin else getpass.getpass(f"给管理员 {a.admin} 设密码（至少 8 位）：")
    Path(a.data_dir).mkdir(parents=True, exist_ok=True)
    os.environ["OWE_DATA_DIR"] = a.data_dir
    from fastapi.testclient import TestClient
    import app.main as main_app
    main_app.init_db()
    with TestClient(main_app.app) as c:
        if not c.get("/api/auth/status").json()["needs_setup"]:
            sys.exit("这个数据目录里已经有账号了：演示数据只往空库里灌")
        r = c.post("/api/auth/setup", json={"username": a.admin, "display_name": "演示管理员", "password": pwd})
        if r.status_code != 200:
            sys.exit(r.text)
        seed(c)
    print(f"完成：{a.data_dir}，用 {a.admin} 登录")


if __name__ == "__main__":
    main()
