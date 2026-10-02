"""测试基座：每个测试独立临时 SQLite 库，重载 app.main 使 OWE_DB 生效"""
import importlib
import os

import pytest


@pytest.fixture()
def app_ctx(tmp_path):
    os.environ["OWE_DB"] = str(tmp_path / "test.db")
    import app.main as main
    main = importlib.reload(main)
    main.init_db()
    return main


class PeerApp:
    """测试用：来源地址默认 127.0.0.1（内网）；请求头 x-test-peer 可冒充任意来源（外网、或经 NAS 反向代理）"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            peer = dict(scope.get("headers", [])).get(b"x-test-peer")
            scope = dict(scope, client=(peer.decode() if peer else "127.0.0.1", 50000))
        await self.app(scope, receive, send)


@pytest.fixture()
def client(app_ctx):
    from fastapi.testclient import TestClient
    from app import auth
    auth._office.update(at=0.0, ips=set())          # 办公室地址缓存是进程级的，别串到下一个用例
    with TestClient(PeerApp(app_ctx.app)) as c:
        response = c.post("/api/auth/setup", json={
            "username": "tester",
            "display_name": "测试用户",
            "password": "unit-test-2026",
        })
        assert response.status_code == 200, response.text
        # 本单位设置（v0.29 起税号/开户账户读这里，不再写死）
        r = c.put("/api/company", json={"name": "示例智能科技有限公司", "short_name": "示例智能", "tax_no": "91320000MA1EXAMPL1",
                                        "bank_accounts": [{"bank": "示例银行城东支行", "account_no": "600000000001",
                                                           "account_code": "100201", "default": True}]})
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture()
def db(app_ctx):
    s = app_ctx.SessionLocal()
    yield s
    s.rollback()
    s.close()


@pytest.fixture()
def seeded(client):
    """最小主数据：一家供应商 + 一家客户 + 两个物料（01组/02组）"""
    client.post("/api/partners", json={"code": "T01", "name": "测试供应商", "is_supplier": True})
    client.post("/api/partners", json={"code": "T02", "name": "测试客户", "is_customer": True})
    m1 = client.post("/api/materials", json={"name": "测试驱动器", "spec": "T-A", "mat_group": "01"}).json()
    m2 = client.post("/api/materials", json={"name": "测试控制板", "spec": "T-B", "mat_group": "02"}).json()
    return {"mat1": m1["id"], "mat2": m2["id"]}
