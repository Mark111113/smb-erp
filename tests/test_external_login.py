"""v0.38 外网登录：真实来源地址、验证器动态码（外网必须、内网不要）、外网 IP 失败封禁、外网禁用 API 令牌、按账号关外网。"""
import time

from app import auth

PROXY = "172.17.0.1"
OUT = {"x-test-peer": PROXY, "x-forwarded-for": "114.114.114.114"}          # 经 NAS 反向代理进来的外网访客
LAN_VIA_PROXY = {"x-test-peer": PROXY, "x-forwarded-for": "192.168.1.20"}


def test_client_ip_and_internal():
    class R:
        def __init__(self, peer, headers):
            self.client = type("C", (), {"host": peer})()
            self.headers = headers
    assert auth.client_ip(R("192.168.1.30", {})) == "192.168.1.30"                               # 内网直连
    assert auth.client_ip(R(PROXY, {"x-forwarded-for": "1.2.3.4, 8.8.8.8"})) == "8.8.8.8"  # 左边可伪造，取最右
    assert auth.client_ip(R(PROXY, {})) is None                                                   # 代理没转发 → 未知
    assert auth.is_internal("192.168.1.20") and auth.is_internal("127.0.0.1")
    assert not auth.is_internal("8.8.8.8") and not auth.is_internal(None)


def test_totp_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"      # ASCII 12345678901234567890
    assert auth._hotp(secret, 59 // 30) == "287082"
    assert auth.totp_verify(secret, "287082", 0, now=59) == 1
    assert auth.totp_verify(secret, "287082", 1, now=59) is None            # 用过的码不能再用


def _user(client, name="worker"):
    r = client.post("/api/users", json={"username": name, "display_name": "测试", "role": "editor", "password": "worker-pass-2026"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _code(secret):
    return auth._hotp(secret, int(time.time() // 30))


def test_external_requires_totp_internal_does_not(client):
    _user(client)
    body = {"username": "worker", "password": "worker-pass-2026"}
    assert client.post("/api/auth/login", json=body).status_code == 200                       # 内网：密码就够
    assert client.post("/api/auth/login", json=body, headers=LAN_VIA_PROXY).status_code == 200  # 内网经代理也是内网
    r = client.post("/api/auth/login", json=body, headers=OUT)
    assert r.status_code == 403 and "开通" in r.text                                           # 外网：没开通动态码进不来
    assert client.post("/api/auth/login", json=body, headers={"x-test-peer": PROXY}).status_code == 403  # 代理没转发地址＝外网
    # 本人在内网开通
    client.post("/api/auth/login", json=body)
    s = client.post("/api/auth/totp/setup").json()
    assert s["uri"].startswith("otpauth://totp/") and "<svg" in s["svg"]
    assert client.post("/api/auth/totp/enable", json={"code": "000000"}).status_code == 400
    assert client.post("/api/auth/totp/enable", json={"code": _code(s["secret"])}).status_code == 200
    # 外网：先要动态码，再登进去；同一个码不能用第二次
    r = client.post("/api/auth/login", json=body, headers=OUT)
    assert r.status_code == 401 and r.json()["need_totp"] is True
    code = auth._hotp(s["secret"], int(time.time() // 30) + 1)    # 开通用掉了当前时间步的码，登录用下一个（允许前后各一步）
    assert client.post("/api/auth/login", json=body | {"totp": code}, headers=OUT).status_code == 200
    assert client.get("/api/partners", headers=OUT).status_code == 200
    assert client.post("/api/auth/login", json=body | {"totp": code}, headers=OUT).status_code == 401
    # 内网登录仍不用动态码
    assert client.post("/api/auth/login", json=body).status_code == 200


def test_external_ip_block_and_unblock(client):
    for i in range(10):
        r = client.post("/api/auth/login", json={"username": f"nobody{i}", "password": "x"}, headers=OUT)
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "tester", "password": "unit-test-2026"}, headers=OUT)
    assert r.status_code == 429 and "暂停" in r.text
    assert client.post("/api/auth/login", json={"username": "tester", "password": "unit-test-2026"}).status_code == 200  # 内网不受影响
    blocks = client.get("/api/users/ip-blocks").json()
    assert blocks[0]["ip"] == "114.114.114.114" and blocks[0]["blocked"]
    assert client.post("/api/users/ip-blocks/unblock", json={"ip": "114.114.114.114"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "tester", "password": "unit-test-2026"}, headers=OUT).status_code == 403  # 解封了，但 tester 没开通动态码


def test_api_token_lan_only_and_allow_external_switch(client):
    uid = _user(client)
    tok = client.post(f"/api/users/{uid}/tokens", json={"name": "agent"}).json()["token"]
    h = {"authorization": "Bearer " + tok}
    assert client.get("/api/partners", headers=h).status_code == 200
    r = client.get("/api/partners", headers=h | OUT)
    assert r.status_code == 403 and "内网" in r.text
    # 关掉外网：开了动态码也进不来
    client.post("/api/auth/login", json={"username": "worker", "password": "worker-pass-2026"})
    s = client.post("/api/auth/totp/setup").json()
    client.post("/api/auth/totp/enable", json={"code": _code(s["secret"])})
    client.post("/api/auth/login", json={"username": "tester", "password": "unit-test-2026"})
    assert client.put(f"/api/users/{uid}", json={"allow_external": False}).json()["allow_external"] is False
    r = client.post("/api/auth/login", json={"username": "worker", "password": "worker-pass-2026"}, headers=OUT)
    assert r.status_code == 403 and "不允许从外网" in r.text
    # 管理员重置动态码
    assert client.post(f"/api/users/{uid}/totp/reset").status_code == 200


def test_office_public_ip_counts_as_internal(client):
    _user(client)
    body = {"username": "worker", "password": "worker-pass-2026"}
    assert client.post("/api/auth/login", json=body, headers=OUT).status_code == 403          # 公司公网出口：默认按外网
    client.post("/api/auth/login", json={"username": "tester", "password": "unit-test-2026"})
    assert client.put("/api/users/office-ips", json={"ips": ["192.168.1.9"]}).status_code == 400   # 局域网不用加
    assert client.put("/api/users/office-ips", json={"ips": ["114.114.114.114"]}).json()["ips"] == ["114.114.114.114"]
    assert client.get("/api/users/office-ips", headers=OUT).json()["current"] == "114.114.114.114"
    assert client.post("/api/auth/login", json=body, headers=OUT).status_code == 200          # 加进办公室后＝内网，不要动态码
    client.post("/api/auth/login", json={"username": "tester", "password": "unit-test-2026"})
    tok = client.post("/api/users/1/tokens", json={"name": "a"}).json()["token"]
    assert client.get("/api/partners", headers={"authorization": "Bearer " + tok} | OUT).status_code == 200
    # 本单位设置保存不冲掉办公室地址
    c = client.get("/api/company").json()
    client.put("/api/company", json={k: c[k] for k in ("name", "short_name", "tax_no", "bank_accounts")})
    assert client.get("/api/users/office-ips").json()["ips"] == ["114.114.114.114"]


def test_temporary_totp_exemption(client):
    from datetime import date, timedelta
    uid = _user(client)
    body = {"username": "worker", "password": "worker-pass-2026"}
    assert client.post("/api/auth/login", json=body, headers=OUT).status_code == 403
    client.put(f"/api/users/{uid}", json={"totp_exempt_until": str(date.today() - timedelta(days=1))})
    assert client.post("/api/auth/login", json=body, headers=OUT).status_code == 403          # 过期不算
    u = client.put(f"/api/users/{uid}", json={"totp_exempt_until": str(date.today())}).json()
    assert u["totp_exempt_until"] == str(date.today())
    assert client.post("/api/auth/login", json=body, headers=OUT).status_code == 200          # 放行期内凭密码
    client.post("/api/auth/login", json={"username": "tester", "password": "unit-test-2026"})
    client.put(f"/api/users/{uid}", json={"totp_exempt_until": ""})
    assert client.post("/api/auth/login", json=body, headers=OUT).status_code == 403
