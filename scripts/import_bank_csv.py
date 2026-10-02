"""命令行导入中行流水 CSV（与页面「导入中行流水 CSV」同一套函数），默认 dry-run，--apply 才提交。

用法：python scripts/import_bank_csv.py DB file1.csv [file2.csv ...] [--apply]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    os.environ["OWE_DB"] = args.db

    from app import main as M, audit, bank as B, operations as O

    M.init_db()
    audit.begin_request({"id": None, "display_name": "导入脚本"}, "SCRIPT", "scripts/import_bank_csv.py")
    with M.SessionLocal() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        for f in sorted(args.files):
            _, rows = B.parse_boc_csv(open(f, encoding="utf-8-sig").read())
            res = B.import_rows(db, rows, os.path.basename(f))
            print(os.path.basename(f), res)
        res = B.auto_match(db)
        O.audit(db, "导入银行流水", file="命令行", **res)
        print("自动匹配", res)
        for r in B.reconcile(db):
            print(r["month"], "流水", r["statement"], "账面", r["book"], "差", r["gap"], "未处理", r["unmatched"], "/", r["total"])
        if args.apply:
            db.commit()
            print("已提交")
        else:
            db.rollback()
            print("dry-run，未提交（加 --apply）")


if __name__ == "__main__":
    main()
