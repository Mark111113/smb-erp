"""命令行账号管理：建管理员、忘记密码、管理员被锁时的兜底入口。

用法（生产，在 NAS 上交互执行，密码经 getpass 输入不回显）：
  docker exec -it owe-erp python scripts/manage_users.py /data/owe.db create admin --role admin --name 管理员
  ... manage_users.py /data/owe.db list
  ... manage_users.py /data/owe.db set-role alice editor
  ... manage_users.py /data/owe.db reset-password admin
  ... manage_users.py /data/owe.db unlock admin
非交互场景可加 --password-stdin 从标准输入读一行密码。
"""
import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.auth import ROLES, ensure_schema, hash_password  # noqa: E402
from app.models import AuthSession, AuthUser, Base  # noqa: E402


def read_password(args) -> str:
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("新密码（至少10位）：")
        if pw != getpass.getpass("再输一次："):
            raise SystemExit("两次输入不一致")
    if not 10 <= len(pw) <= 128:
        raise SystemExit("密码长度须为 10–128 位")
    return pw


def find(db, username) -> AuthUser:
    user = db.scalar(select(AuthUser).where(AuthUser.username == username.lower()))
    if not user:
        raise SystemExit(f"账号不存在：{username}")
    return user


def main():
    ap = argparse.ArgumentParser(description="owe-erp 账号管理")
    ap.add_argument("db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    c = sub.add_parser("create")
    c.add_argument("username")
    c.add_argument("--role", choices=ROLES, default="editor")
    c.add_argument("--name", default="")
    c.add_argument("--password-stdin", action="store_true")
    r = sub.add_parser("reset-password")
    r.add_argument("username")
    r.add_argument("--password-stdin", action="store_true")
    u = sub.add_parser("unlock")
    u.add_argument("username")
    s = sub.add_parser("set-role")
    s.add_argument("username")
    s.add_argument("role", choices=ROLES)
    t = sub.add_parser("reset-totp", help="清掉验证器动态码（手机丢了）；本人在内网重新开通")
    t.add_argument("username")
    args = ap.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"数据库不存在：{args.db}")
    engine = create_engine(f"sqlite:///{args.db}", connect_args={"timeout": 15})
    Base.metadata.create_all(engine, tables=[AuthUser.__table__, AuthSession.__table__])
    ensure_schema(engine)

    with Session(engine) as db:
        if args.cmd == "list":
            now = datetime.now()
            for x in db.scalars(select(AuthUser).order_by(AuthUser.id)):
                locked = "锁定" if x.locked_until and x.locked_until > now else ""
                otp = "动态码" if x.totp_enabled else ""
                print(f"{x.id:>3}  {x.username:<16} {x.display_name or '':<10} {ROLES.get(x.role, x.role):<4} "
                      f"{'启用' if x.active else '停用'} {locked} {otp}")
            return
        if args.cmd == "create":
            if db.scalar(select(AuthUser.id).where(AuthUser.username == args.username.lower())):
                raise SystemExit(f"账号已存在：{args.username}")
            db.add(AuthUser(username=args.username.lower(), display_name=args.name, role=args.role,
                            password_hash=hash_password(read_password(args))))
        else:
            user = find(db, args.username)
            if args.cmd == "reset-password":
                user.password_hash = hash_password(read_password(args))
                for sess in db.scalars(select(AuthSession).where(AuthSession.user_id == user.id)):
                    db.delete(sess)
            if args.cmd in ("reset-password", "unlock"):
                user.failed_count = 0
                user.locked_until = None
            if args.cmd == "set-role":
                user.role = args.role
            if args.cmd == "reset-totp":
                user.totp_enabled, user.totp_secret, user.totp_last_step = False, "", 0
        db.commit()
        print(f"ok: {args.cmd} {args.username}")


if __name__ == "__main__":
    main()
