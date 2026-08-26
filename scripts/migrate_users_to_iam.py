"""One-click server migration from data/users.xlsx to the IAM SQLite tables.

Run from the project root with the same Python environment as the application:

    python scripts/migrate_users_to_iam.py

The default operation is idempotent and never replaces an existing database
password hash.  ``--refresh-existing-passwords`` is intentionally explicit and
should only be used when the workbook must become authoritative again.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.user_service import UserService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移当前服务器 users.xlsx 到 IAM 数据库")
    parser.add_argument(
        "--refresh-existing-passwords",
        action="store_true",
        help="用当前 Excel 密码覆盖已迁移密码；普通上线和重复执行不要使用",
    )
    args = parser.parse_args()

    service = UserService()
    result = service.migrate_legacy_users(
        refresh_existing_passwords=args.refresh_existing_passwords,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
