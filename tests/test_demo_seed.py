"""演示数据脚本（scripts/demo_seed.py）跟着代码一起能跑：四张合同、库存、凭证都由系统算出"""
import importlib.util
from pathlib import Path


def _load():
    spec = importlib.util.spec_from_file_location("demo_seed", Path(__file__).resolve().parent.parent / "scripts" / "demo_seed.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_demo_seed_runs(client):
    out = _load().seed(client)
    assert len(out["partners"]) == 4
    stock = {s["material_id"]: s["qty"] for s in client.get("/api/stock").json()["items"]}
    assert stock[out["materials"]["drv"]] == 10            # 进 50，出 30 + 10
    assert client.get("/api/auth/status").json()["brand"] == "示例智能 ERP"
    assert len(client.get("/api/contracts").json()) == 4
