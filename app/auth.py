"""Local accounts, three-level roles and an API session gate.

The first account on an empty database is created from the browser as admin;
after that only admins create accounts. No default password is embedded.
Sessions are HttpOnly cookies and password hashes use PBKDF2-SHA256.

Roles (checked centrally in the middleware, not per endpoint):
  viewer  只读   — GET only
  editor  可读写 — all business and finance writes
  admin   管理员 — editor + user management + account/period unlock
"""
from datetime import datetime, timedelta
import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import struct
import time
from urllib.parse import quote
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, inspect, select, text

from . import audit
from .models import AuthIpBlock, AuthSession, AuthUser


COOKIE_NAME = "owe_session"
SESSION_HOURS = 12
ADMIN_LOCK_MINUTES = 10
MAX_FAILED_LOGINS = 5
PBKDF2_ITERATIONS = 240_000
# 非管理员账号锁定后不自动解除，须管理员解锁；管理员账号仍按时自动解除，避免无人可解
LOCKED_UNTIL_ADMIN = datetime(9999, 12, 31)

# ---- 外网登录（v0.38，2026-10-01 用户定：外网要验证器动态码，内网不要；admin 不区别对待）----
# 外网入口是反向代理（如 NAS 自带的反代：公网域名 → 本机 8186），程序看到的来源都是代理，
# 真实地址取代理加的 X-Forwarded-For / X-Real-IP。经代理却没带真实地址 → 按外网处理（宁严勿松）。
TRUSTED_PROXIES = {x.strip() for x in os.environ.get("OWE_TRUSTED_PROXIES", "172.17.0.1").split(",") if x.strip()}
IP_FAIL_WINDOW = timedelta(minutes=15)
IP_FAIL_LIMIT = 10
IP_BLOCK_FOR = timedelta(hours=1)

ROLES = {"admin": "管理员", "editor": "可读写", "viewer": "只读"}
WRITE_ROLES = {"admin", "editor"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PATHS = {"/api/health", "/api/auth/status", "/api/auth/setup", "/api/auth/login"}
SELF_SERVICE_PATHS = {"/api/auth/logout", "/api/auth/password"}
SELF_SERVICE_PREFIX = ("/api/prefs/", "/api/auth/totp")   # 只动自己的界面设置 / 自己的动态码，只读账号也可写
ADMIN_PATH = re.compile(r"^/api/(users(/.*)?|periods/[^/]+/unlock)$")
ADMIN_WRITE_PATH = re.compile(r"^/api/seals$")   # 换章图只限管理员（看章、盖章可读写账号就行）

Role = Literal["admin", "editor", "viewer"]
Username = Annotated[str, Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")]
NewPassword = Annotated[str, Field(min_length=10, max_length=128)]


class AuthInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, str_strip_whitespace=True)


class SetupIn(AuthInput):
    username: Username
    display_name: str = Field(default="", max_length=64)
    password: NewPassword


class LoginIn(AuthInput):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)
    totp: str = Field(default="", max_length=8)      # 外网登录的验证器动态码


class PasswordIn(AuthInput):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: NewPassword


class UserCreateIn(AuthInput):
    username: Username
    display_name: str = Field(default="", max_length=64)
    role: Role = "editor"
    password: NewPassword


class UserUpdateIn(AuthInput):
    display_name: str | None = Field(default=None, max_length=64)
    role: Role | None = None
    active: bool | None = None
    allow_external: bool | None = None
    totp_exempt_until: str | None = Field(default=None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")   # 空串＝取消放行


class TotpCodeIn(AuthInput):
    code: str = Field(min_length=6, max_length=8)


class TotpOffIn(AuthInput):
    password: str = Field(min_length=1, max_length=128)


class PasswordResetIn(AuthInput):
    new_password: NewPassword


def ensure_schema(engine):
    """v0.10 首版 auth_user 无 role 列：补列，已有账号按可读写处理；v0.38 补外网登录相关列。"""
    cols = {c["name"] for c in inspect(engine).get_columns("auth_user")}
    adds = [("role", "VARCHAR(16) NOT NULL DEFAULT 'editor'"), ("totp_secret", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ("totp_enabled", "BOOLEAN NOT NULL DEFAULT 0"), ("totp_last_step", "INTEGER NOT NULL DEFAULT 0"),
            ("allow_external", "BOOLEAN NOT NULL DEFAULT 1"), ("totp_exempt_until", "VARCHAR(10) NOT NULL DEFAULT ''")]
    with engine.begin() as conn:
        for name, ddl in adds:
            if name not in cols:
                conn.execute(text(f"ALTER TABLE auth_user ADD COLUMN {name} {ddl}"))


# ---------- 来源地址 ----------
def client_ip(request: Request) -> str | None:
    """真实来源 IP；经可信代理但没带转发头时返回 None（按外网处理）。"""
    peer = request.client.host if request.client else None
    if peer not in TRUSTED_PROXIES:
        return peer
    xff = [x.strip() for x in (request.headers.get("x-forwarded-for") or "").split(",") if x.strip()]
    for ip in reversed(xff):                       # 从右往左取第一个不是代理的：左边的可以被客户端伪造
        if ip not in TRUSTED_PROXIES:
            return ip
    return (request.headers.get("x-real-ip") or "").strip() or None


INTERNAL_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
                                                   "::1/128", "fc00::/7", "fe80::/10")]


_office = {"at": 0.0, "ips": set()}


def office_ips(db) -> set[str]:
    """办公室公网出口地址：在公司里经域名访问，绕外网回来，来源是公司公网 IP，也算内网。
    管理员在 用户管理 → 办公室公网地址 维护（存 company_setting.office_ips）；宽带换了 IP 就更新。60 秒缓存。"""
    if db is None:
        return _office["ips"]
    if time.time() - _office["at"] > 60:
        from .company import get as company
        _office["ips"] = {x.strip() for x in (company(db).get("office_ips") or []) if str(x).strip()}
        _office["at"] = time.time()
    return _office["ips"]


def is_internal(ip: str | None, db=None) -> bool:
    """局域网段、本机、办公室公网出口算内网（不用 is_private：它把文档保留段等也算私有）"""
    if not ip:
        return False
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in INTERNAL_NETS if n.version == a.version) or ip in office_ips(db)


# ---------- 验证器动态码（RFC 6238，30 秒、6 位、SHA1；前后各容一个时间步）----------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    h = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_verify(secret: str, code: str, last_step: int = 0, now: float | None = None) -> int | None:
    """对上了返回时间步（调用方存起来防重放），对不上返回 None"""
    code = re.sub(r"\s", "", code or "")
    if not secret or not re.fullmatch(r"\d{6}", code):
        return None
    step = int((now if now is not None else time.time()) // 30)
    for s in (step - 1, step, step + 1):
        if s > last_step and secrets.compare_digest(_hotp(secret, s), code):
            return s
    return None


def brand(db) -> str:
    """界面与验证器上显示的名字＝本单位简称（系统 → 本单位设置）+ ERP"""
    from .company import get as company
    c = company(db)
    return f"{c.get('short_name') or c.get('name') or ''} ERP".strip()


def totp_uri(user: AuthUser, issuer: str = "ERP") -> str:
    issuer = issuer.replace(" ", "")
    label = quote(f"{issuer}:{user.username}")
    return f"otpauth://totp/{label}?secret={user.totp_secret}&issuer={quote(issuer)}&digits=6&period=30"


def _qr_svg(text_: str) -> str:
    import qrcode
    import qrcode.image.svg
    return qrcode.make(text_, image_factory=qrcode.image.svg.SvgPathImage, box_size=8).to_string().decode()


# ---------- 外网 IP 失败计数 ----------
def _ip_blocked(db, ip: str, now: datetime) -> datetime | None:
    b = db.get(AuthIpBlock, ip)
    return b.blocked_until if b and b.blocked_until and b.blocked_until > now else None


def _ip_fail(db, ip: str, username: str, now: datetime):
    b = db.get(AuthIpBlock, ip) or AuthIpBlock(ip=ip, fail_count=0, total_fails=0)
    if b.first_fail_at is None or now - b.first_fail_at > IP_FAIL_WINDOW:
        b.first_fail_at, b.fail_count = now, 0
    b.fail_count += 1
    b.total_fails = (b.total_fails or 0) + 1
    b.last_username = username[:32]
    if b.fail_count >= IP_FAIL_LIMIT:
        b.blocked_until, b.fail_count, b.first_fail_at = now + IP_BLOCK_FOR, 0, None
        _audit_event(db, "封禁外网IP", {"ip": ip, "until": str(b.blocked_until), "last_username": b.last_username})
    db.add(b)


def _ip_ok(db, ip: str):
    b = db.get(AuthIpBlock, ip)
    if b:
        b.fail_count, b.first_fail_at = 0, None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "$".join(("pbkdf2_sha256", str(PBKDF2_ITERATIONS), salt.hex(), digest.hex()))


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return secrets.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def _audit_event(db, action: str, detail: dict):
    from .models import AuditEvent
    uid, name = audit.current_actor()
    db.add(AuditEvent(action=action, detail=detail, user_id=uid, user_name=name))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_context(db, request: Request):
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        from .models import ApiToken
        t = db.scalar(select(ApiToken).where(ApiToken.token_hash == _token_hash(auth[7:].strip())))
        if not t or t.revoked or not t.user.active:
            return None, None
        return t, t.user
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None, None
    session = db.get(AuthSession, _token_hash(token))
    if not session or session.expires_at <= datetime.now() or not session.user.active:
        return None, None
    return session, session.user


def _user_json(user: AuthUser):
    return {"id": user.id, "username": user.username, "display_name": user.display_name or user.username,
            "role": user.role, "role_label": ROLES.get(user.role, user.role), "totp_enabled": bool(user.totp_enabled)}


def _admin_user_json(user: AuthUser, now: datetime):
    locked = bool(user.locked_until and user.locked_until > now)
    return {**_user_json(user), "display_name": user.display_name or "", "active": user.active,
            "locked": locked, "locked_until": str(user.locked_until) if locked else None,
            "last_login_at": str(user.last_login_at) if user.last_login_at else None,
            "created_at": str(user.created_at) if user.created_at else None,
            "allow_external": user.allow_external is not False, "totp_exempt_until": _exempt(user)}


def _exempt(user) -> str:
    """临时免动态码的截止日（含）；过期或没设返回空串"""
    d = user.totp_exempt_until or ""
    return d if d and d >= datetime.now().strftime("%Y-%m-%d") else ""


def _forbidden(role: str, path: str, method: str):
    """返回拒绝原因；None 表示放行。"""
    if ADMIN_PATH.match(path) or (method not in SAFE_METHODS and ADMIN_WRITE_PATH.match(path)):
        return None if role == "admin" else "仅管理员可执行此操作"
    if method in SAFE_METHODS or path in SELF_SERVICE_PATHS or path.startswith(SELF_SERVICE_PREFIX):
        return None
    return None if role in WRITE_ROLES else "只读账号不能修改数据"


def _issue_session(db, response: Response, user: AuthUser):
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    db.add(AuthSession(token_hash=_token_hash(token), user_id=user.id,
                       expires_at=now + timedelta(hours=SESSION_HOURS), last_seen_at=now))
    user.failed_count = 0
    user.locked_until = None
    user.last_login_at = now
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS * 3600, httponly=True,
                        samesite="strict", path="/")


def _drop_sessions(db, user_id: int, keep: str | None = None):
    for s in db.scalars(select(AuthSession).where(AuthSession.user_id == user_id)):
        if s.token_hash != keep:
            db.delete(s)


def _other_active_admins(db, user_id: int) -> int:
    return db.scalar(select(func.count()).select_from(AuthUser).where(
        AuthUser.role == "admin", AuthUser.active.is_(True), AuthUser.id != user_id))


def install(app: FastAPI, get_db, session_factory):
    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        user_json = None
        if path.startswith("/api/") and path not in PUBLIC_PATHS:
            with session_factory() as db:
                session, user = _session_context(db, request)
                if user:
                    session.last_seen_at = datetime.now()
                    db.commit()
                    request.state.auth_user_id = user.id
                    request.state.auth_session_hash = session.token_hash
                    user_json = _user_json(user) | {"allow_external": user.allow_external is not False}
            if not user:
                return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})
            with session_factory() as db:
                external = not is_internal(client_ip(request), db)
            if external:
                if (request.headers.get("authorization") or "").lower().startswith("bearer "):
                    return JSONResponse(status_code=403, content={"detail": "API 令牌只能在内网使用"})
                if user_json.get("allow_external") is False:
                    return JSONResponse(status_code=403, content={"detail": "此账号不允许从外网使用，请在内网访问"})
            denied = _forbidden(user_json["role"], path, request.method)
            if denied:
                return JSONResponse(status_code=403, content={"detail": denied})
            request.state.auth_user = user_json
        token = audit.begin_request(user_json, request.method, path) if path.startswith("/api/") else None
        try:
            return await call_next(request)
        finally:
            if token is not None:
                audit.end_request(token)

    # ---- API 令牌（v0.31，管理员）----
    @app.get("/api/users/{uid}/tokens")
    def list_tokens(uid: int, db=Depends(get_db)):
        from .models import ApiToken
        return [{"id": t.id, "name": t.name, "prefix": t.prefix, "revoked": t.revoked,
                 "created_at": str(t.created_at)[:16], "last_seen_at": str(t.last_seen_at or "")[:16]}
                for t in db.scalars(select(ApiToken).where(ApiToken.user_id == uid).order_by(ApiToken.id.desc()))]

    @app.post("/api/users/{uid}/tokens")
    def create_token(uid: int, data: dict, db=Depends(get_db)):
        from .models import ApiToken
        u = db.get(AuthUser, uid)
        if not u or not u.active:
            raise HTTPException(404, "用户不存在或已停用")
        raw = "owe_" + secrets.token_urlsafe(32)
        t = ApiToken(user_id=uid, name=str(data.get("name") or "agent")[:64], token_hash=_token_hash(raw), prefix=raw[:10])
        db.add(t)
        db.flush()
        _audit_event(db, "签发 API 令牌", {"user_id": uid, "token_id": t.id, "name": t.name, "prefix": t.prefix})
        db.commit()
        return {"id": t.id, "token": raw, "note": "令牌只显示这一次，请妥善保存；泄露了就吊销重发"}

    @app.post("/api/users/tokens/{tid}/revoke")
    def revoke_token(tid: int, db=Depends(get_db)):
        from .models import ApiToken
        t = db.get(ApiToken, tid)
        if not t:
            raise HTTPException(404, "令牌不存在")
        t.revoked = True
        _audit_event(db, "吊销 API 令牌", {"user_id": t.user_id, "token_id": t.id, "prefix": t.prefix})
        db.commit()
        return {"ok": True}

    @app.get("/api/auth/status")
    def auth_status(request: Request, db=Depends(get_db)):
        _, user = _session_context(db, request)
        needs_setup = db.scalar(select(AuthUser.id).limit(1)) is None
        return {"needs_setup": needs_setup, "authenticated": user is not None,
                "user": _user_json(user) if user else None, "external": not is_internal(client_ip(request), db),
                "ip": client_ip(request), "brand": brand(db)}      # ip＝访问者自己的来源地址（核对反向代理有没有转发真实 IP 用）

    @app.post("/api/auth/setup")
    def auth_setup(data: SetupIn, db=Depends(get_db)):
        if db.scalar(select(AuthUser.id).limit(1)) is not None:
            raise HTTPException(409, "初始化账号已创建，请直接登录")
        user = AuthUser(username=data.username.lower(), display_name=data.display_name, role="admin",
                        password_hash=hash_password(data.password))
        db.add(user)
        db.flush()
        response = JSONResponse({"authenticated": True, "user": _user_json(user)})
        _issue_session(db, response, user)
        db.commit()
        return response

    @app.post("/api/auth/login")
    def auth_login(data: LoginIn, request: Request, db=Depends(get_db)):
        now = datetime.now()
        ip = client_ip(request)
        external = not is_internal(ip, db)
        ipk = ip or "（代理未转发真实地址）"
        if external:
            until = _ip_blocked(db, ipk, now)
            if until:
                minutes = int((until - now).total_seconds() // 60) + 1
                raise HTTPException(429, f"这个网络地址登录失败次数过多，已暂停约 {minutes} 分钟")
        user = db.scalar(select(AuthUser).where(AuthUser.username == data.username.lower()))
        if not user or not user.active:
            if external:
                _ip_fail(db, ipk, data.username, now)
                db.commit()
            raise HTTPException(401, "用户名或密码不正确")
        if user.locked_until and user.locked_until > now:
            if user.locked_until >= LOCKED_UNTIL_ADMIN:
                raise HTTPException(429, "账号因多次密码错误已锁定，请联系管理员解锁")
            minutes = int((user.locked_until - now).total_seconds() // 60) + 1
            raise HTTPException(429, f"失败次数过多，请约 {minutes} 分钟后再试")
        def fail(msg):
            user.failed_count += 1
            if user.failed_count >= MAX_FAILED_LOGINS:
                user.locked_until = (now + timedelta(minutes=ADMIN_LOCK_MINUTES)
                                     if user.role == "admin" else LOCKED_UNTIL_ADMIN)
                user.failed_count = 0
            if external:
                _ip_fail(db, ipk, data.username, now)
            db.commit()
            raise HTTPException(401, msg)

        if not verify_password(data.password, user.password_hash):
            fail("用户名或密码不正确")
        if external:
            if user.allow_external is False:
                raise HTTPException(403, "此账号不允许从外网登录，请在内网访问")
            if not user.totp_enabled and _exempt(user):
                _ip_ok(db, ipk)
                _audit_event(db, "外网登录（临时免动态码）", {"username": user.username, "ip": ip, "until": user.totp_exempt_until})
            elif not user.totp_enabled:
                raise HTTPException(403, "从外网登录需要验证器动态码：请先在内网登录，到右上角「改密码 / 动态码」里开通")
            elif not data.totp:
                return JSONResponse(status_code=401, content={"detail": "请输入验证器上的 6 位动态码", "need_totp": True})
            else:
                step = totp_verify(user.totp_secret, data.totp, user.totp_last_step or 0)
                if step is None:
                    fail("动态码不正确或已过期")
                user.totp_last_step = step
                _ip_ok(db, ipk)
                _audit_event(db, "外网登录", {"username": user.username, "ip": ip})
        response = JSONResponse({"authenticated": True, "user": _user_json(user)})
        _issue_session(db, response, user)
        db.commit()
        return response

    # ---- 验证器动态码：自己开通 / 关闭（v0.38）----
    @app.get("/api/auth/totp")
    def totp_status(request: Request, db=Depends(get_db)):
        _, user = _session_context(db, request)
        return {"enabled": bool(user.totp_enabled), "allow_external": user.allow_external is not False,
                "external": not is_internal(client_ip(request), db)}

    @app.post("/api/auth/totp/setup")
    def totp_setup(request: Request, db=Depends(get_db)):
        _, user = _session_context(db, request)
        if user.totp_enabled:
            raise HTTPException(400, "已经开通了；要换手机先关闭再重新开通")
        user.totp_secret = new_totp_secret()
        uri = totp_uri(user, brand(db))
        db.commit()
        return {"secret": user.totp_secret, "uri": uri, "svg": _qr_svg(uri)}

    @app.post("/api/auth/totp/enable")
    def totp_enable(data: TotpCodeIn, request: Request, db=Depends(get_db)):
        _, user = _session_context(db, request)
        step = totp_verify(user.totp_secret, data.code, 0)
        if step is None:
            raise HTTPException(400, "动态码不对：确认扫的是刚才的二维码、手机时间准确，再输一次当前的 6 位数")
        user.totp_enabled, user.totp_last_step = True, step
        _audit_event(db, "开通动态码", {"username": user.username})
        db.commit()
        return {"ok": True}

    @app.post("/api/auth/totp/disable")
    def totp_disable(data: TotpOffIn, request: Request, db=Depends(get_db)):
        _, user = _session_context(db, request)
        if not verify_password(data.password, user.password_hash):
            raise HTTPException(400, "密码不正确")
        user.totp_enabled, user.totp_secret, user.totp_last_step = False, "", 0
        _audit_event(db, "关闭动态码", {"username": user.username})
        db.commit()
        return {"ok": True}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, db=Depends(get_db)):
        session, _ = _session_context(db, request)
        if session:
            db.delete(session)
            db.commit()
        response = JSONResponse({"authenticated": False, "user": None})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.put("/api/auth/password")
    def change_password(data: PasswordIn, request: Request, db=Depends(get_db)):
        session, user = _session_context(db, request)
        if not user:
            raise HTTPException(401, "未登录或会话已过期")
        if not verify_password(data.old_password, user.password_hash):
            raise HTTPException(400, "原密码不正确")
        user.password_hash = hash_password(data.new_password)
        _drop_sessions(db, user.id, keep=session.token_hash)
        db.commit()
        return {"ok": True}

    # ---------- 用户管理（仅管理员，中间件已拦截） ----------
    def _get_user(db, uid: int) -> AuthUser:
        user = db.get(AuthUser, uid)
        if not user:
            raise HTTPException(404, "用户不存在")
        return user

    @app.get("/api/users")
    def list_users(db=Depends(get_db)):
        now = datetime.now()
        return [_admin_user_json(u, now) for u in db.scalars(select(AuthUser).order_by(AuthUser.id))]

    @app.post("/api/users")
    def create_user(data: UserCreateIn, db=Depends(get_db)):
        username = data.username.lower()
        if db.scalar(select(AuthUser.id).where(AuthUser.username == username)):
            raise HTTPException(409, "账号已存在")
        user = AuthUser(username=username, display_name=data.display_name, role=data.role,
                        password_hash=hash_password(data.password))
        db.add(user)
        db.commit()
        return _admin_user_json(user, datetime.now())

    @app.get("/api/users/office-ips")
    def get_office_ips(request: Request, db=Depends(get_db)):
        from .company import get as company
        return {"ips": company(db).get("office_ips") or [], "current": client_ip(request)}

    @app.put("/api/users/office-ips")
    def put_office_ips(data: dict, db=Depends(get_db)):
        from .company import CompanySetting
        ips = []
        for x in data.get("ips") or []:
            x = str(x).strip()
            try:
                a = ipaddress.ip_address(x)
            except ValueError:
                raise HTTPException(400, f"不是有效的 IP 地址：{x}")
            if any(a in n for n in INTERNAL_NETS if n.version == a.version):
                raise HTTPException(400, f"{x} 是局域网地址，本来就算内网，不用加")
            ips.append(x)
        row = db.get(CompanySetting, 1) or CompanySetting(id=1, data={})
        row.data = {**(row.data or {}), "office_ips": sorted(set(ips))}
        db.add(row)
        _audit_event(db, "修改办公室公网地址", {"ips": sorted(set(ips))})
        db.commit()
        _office["at"] = 0.0                 # 立即生效
        return {"ips": sorted(set(ips))}

    @app.put("/api/users/{uid}")
    def update_user(uid: int, data: UserUpdateIn, db=Depends(get_db)):
        user = _get_user(db, uid)
        demoting = (data.role not in (None, "admin")) or data.active is False
        if user.role == "admin" and demoting and not _other_active_admins(db, user.id):
            raise HTTPException(400, "至少保留一个启用的管理员")
        if data.display_name is not None:
            user.display_name = data.display_name
        if data.role is not None:
            user.role = data.role
        if data.active is not None:
            user.active = data.active
            if not data.active:
                _drop_sessions(db, user.id)
        if data.allow_external is not None:
            user.allow_external = data.allow_external
        if data.totp_exempt_until is not None and data.totp_exempt_until != (user.totp_exempt_until or ""):
            user.totp_exempt_until = data.totp_exempt_until
            _audit_event(db, "外网临时免动态码" if data.totp_exempt_until else "取消外网免动态码",
                         {"username": user.username, "until": data.totp_exempt_until})
        db.commit()
        return _admin_user_json(user, datetime.now())

    @app.post("/api/users/{uid}/totp/reset")
    def reset_totp(uid: int, db=Depends(get_db)):
        """手机丢了 / 换了：管理员清掉动态码，本人在内网重新开通"""
        user = _get_user(db, uid)
        user.totp_enabled, user.totp_secret, user.totp_last_step = False, "", 0
        _audit_event(db, "重置动态码", {"user_id": uid, "username": user.username})
        db.commit()
        return {"ok": True}

    @app.get("/api/users/ip-blocks")
    def ip_blocks(db=Depends(get_db)):
        now = datetime.now()
        return [{"ip": b.ip, "blocked": bool(b.blocked_until and b.blocked_until > now), "blocked_until": str(b.blocked_until or "")[:19],
                 "fail_count": b.fail_count, "total_fails": b.total_fails, "last_username": b.last_username}
                for b in db.scalars(select(AuthIpBlock).order_by(AuthIpBlock.blocked_until.desc()))]

    @app.post("/api/users/ip-blocks/unblock")
    def ip_unblock(data: dict, db=Depends(get_db)):
        b = db.get(AuthIpBlock, str(data.get("ip") or ""))
        if not b:
            raise HTTPException(404, "没有这个地址的记录")
        b.blocked_until, b.fail_count, b.first_fail_at = None, 0, None
        _audit_event(db, "解封外网IP", {"ip": b.ip})
        db.commit()
        return {"ok": True}

    @app.post("/api/users/{uid}/password")
    def reset_password(uid: int, data: PasswordResetIn, request: Request, db=Depends(get_db)):
        user = _get_user(db, uid)
        user.password_hash = hash_password(data.new_password)
        user.failed_count = 0
        user.locked_until = None
        keep = request.state.auth_session_hash if uid == request.state.auth_user_id else None
        _drop_sessions(db, user.id, keep=keep)
        db.commit()
        return {"ok": True}

    @app.post("/api/users/{uid}/unlock")
    def unlock_user(uid: int, db=Depends(get_db)):
        user = _get_user(db, uid)
        user.failed_count = 0
        user.locked_until = None
        db.commit()
        return {"ok": True}
