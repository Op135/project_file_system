"""关系型身份、组织、权限与外部账号存储。

系统过去将用户保存在 ``data/users.xlsx``。本模块提供目标 SQLite 架构，并确保迁移
需要显式执行且可以安全重复：迁移读取执行机器上的工作簿，因此开发环境与生产环境的
密码不会混用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .permission_catalog import ignores_legacy_role_grants
from .identity_codes import normalize_stable_code, validate_stable_code

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 390_000
ACTIVE_USER_STATUSES = {"active"}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hash_password(password: str, *, iterations: int = PASSWORD_ITERATIONS) -> str:
    """生成带版本信息的 PBKDF2-SHA256 密码哈希。"""
    if not isinstance(password, str):
        raise TypeError("密码必须是字符串")
    salt = uuid.uuid4().bytes
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        [
            PASSWORD_SCHEME,
            str(iterations),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(derived).decode("ascii"),
        ]
    )


def verify_password(password: str, encoded: str | None) -> bool:
    """校验密码，同时不向调用方暴露存储值。"""
    if not encoded or not isinstance(password, str):
        return False
    try:
        scheme, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class UserMigrationResult:
    source_path: str
    backup_path: str | None
    imported: int
    updated: int
    unchanged: int
    password_refreshed: int
    total: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IdentityStore:
    """供登录逻辑和 NiceGUI 回调使用的小型同步数据仓库。

    本仓库与现有系统共用 SQLite 文件，但只维护 ``iam_*``、``org_*`` 和
    ``work_assignments`` 表。通过 WAL 与进程锁，让短事务能够和系统现有的 aiosqlite
    存储安全共存。
    """

    def __init__(self, db_path: Path | str, *, password_iterations: int = PASSWORD_ITERATIONS):
        self.db_path = Path(db_path)
        self.password_iterations = password_iterations
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS iam_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS iam_users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT,
                legacy_role TEXT NOT NULL DEFAULT '普通用户',
                employee_no TEXT,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'disabled', 'departed')),
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS org_units (
                org_unit_id TEXT PRIMARY KEY,
                code TEXT NOT NULL COLLATE NOCASE UNIQUE,
                name TEXT NOT NULL,
                parent_org_unit_id TEXT REFERENCES org_units(org_unit_id),
                wecom_department_id TEXT UNIQUE,
                source TEXT NOT NULL DEFAULT 'manual',
                manual_override INTEGER NOT NULL DEFAULT 0,
                external_name_snapshot TEXT NOT NULL DEFAULT '',
                external_parent_snapshot TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'disabled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS iam_positions (
                position_id TEXT PRIMARY KEY,
                code TEXT NOT NULL COLLATE NOCASE UNIQUE,
                name TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                manual_override INTEGER NOT NULL DEFAULT 0,
                external_name_snapshot TEXT NOT NULL DEFAULT '',
                level INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'disabled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS org_memberships (
                membership_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES iam_users(user_id),
                org_unit_id TEXT NOT NULL REFERENCES org_units(org_unit_id),
                position_id TEXT REFERENCES iam_positions(position_id),
                direct_manager_user_id TEXT REFERENCES iam_users(user_id),
                is_primary INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'ended')),
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, org_unit_id, position_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS iam_security_roles (
                role_id TEXT PRIMARY KEY,
                code TEXT NOT NULL COLLATE NOCASE UNIQUE,
                name TEXT NOT NULL UNIQUE,
                is_system INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS iam_permissions (
                permission_id TEXT PRIMARY KEY,
                code TEXT NOT NULL COLLATE NOCASE UNIQUE,
                name TEXT NOT NULL,
                module TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS iam_position_permissions (
                position_id TEXT NOT NULL REFERENCES iam_positions(position_id),
                permission_id TEXT NOT NULL REFERENCES iam_permissions(permission_id),
                created_at TEXT NOT NULL,
                PRIMARY KEY (position_id, permission_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS iam_user_roles (
                user_id TEXT NOT NULL REFERENCES iam_users(user_id),
                role_id TEXT NOT NULL REFERENCES iam_security_roles(role_id),
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, role_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS iam_role_permissions (
                role_id TEXT NOT NULL REFERENCES iam_security_roles(role_id),
                permission_id TEXT NOT NULL REFERENCES iam_permissions(permission_id),
                created_at TEXT NOT NULL,
                PRIMARY KEY (role_id, permission_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS iam_external_identities (
                external_identity_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES iam_users(user_id),
                provider TEXT NOT NULL,
                external_userid TEXT NOT NULL,
                external_display_name TEXT NOT NULL DEFAULT '',
                binding_source TEXT NOT NULL DEFAULT 'manual',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(provider, external_userid),
                UNIQUE(provider, user_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS work_assignments (
                assignment_id TEXT PRIMARY KEY,
                module TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                task_key TEXT NOT NULL,
                assignment_type TEXT NOT NULL,
                assignee_user_id TEXT NOT NULL REFERENCES iam_users(user_id),
                status TEXT NOT NULL DEFAULT 'pending',
                source_policy_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(module, entity_id, task_key, assignee_user_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS iam_audit_logs (
                audit_id TEXT PRIMARY KEY,
                actor_user_id TEXT,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_org_memberships_user ON org_memberships(user_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_position_permissions_position "
            "ON iam_position_permissions(position_id)",
            "CREATE INDEX IF NOT EXISTS idx_work_assignments_assignee ON work_assignments(assignee_user_id, status)",
        ]
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            for statement in statements:
                connection.execute(statement)
            # 现有安装可能已经包含第一版身份表，直接补充来源和覆盖元数据，避免重建数据表。
            org_columns = {row["name"] for row in connection.execute("PRAGMA table_info(org_units)").fetchall()}
            org_override_added = "manual_override" not in org_columns
            for column_name, definition in {
                "source": "TEXT NOT NULL DEFAULT 'manual'",
                "manual_override": "INTEGER NOT NULL DEFAULT 0",
                "external_name_snapshot": "TEXT NOT NULL DEFAULT ''",
                "external_parent_snapshot": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if column_name not in org_columns:
                    connection.execute(f"ALTER TABLE org_units ADD COLUMN {column_name} {definition}")
            if org_override_added:
                # 无法判断旧版导入部门是否被手工编辑过，因此默认保护其现有内容。
                connection.execute(
                    "UPDATE org_units SET source='wecom', manual_override=1 "
                    "WHERE wecom_department_id IS NOT NULL AND wecom_department_id<>''"
                )

            position_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(iam_positions)").fetchall()
            }
            for column_name, definition in {
                "source": "TEXT NOT NULL DEFAULT 'manual'",
                "manual_override": "INTEGER NOT NULL DEFAULT 0",
                "external_name_snapshot": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if column_name not in position_columns:
                    connection.execute(f"ALTER TABLE iam_positions ADD COLUMN {column_name} {definition}")
            connection.execute(
                "INSERT INTO iam_meta(key, value, updated_at) VALUES('schema_version', '3', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (_now_text(),),
            )

    def has_database_users(self) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT 1 FROM iam_users LIMIT 1").fetchone()
        return row is not None

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "password": None,
            "password_set": bool(row["password_hash"]),
            "role": row["legacy_role"],
            "status": row["status"],
            "must_change_password": bool(row["must_change_password"]),
        }

    def list_users(self) -> dict[str, dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM iam_users ORDER BY legacy_role, display_name, username").fetchall()
        return {row["username"]: self._public_user(row) for row in rows}

    def get_user(self, username: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM iam_users WHERE username = ? COLLATE NOCASE",
                (str(username).strip(),),
            ).fetchone()
        return self._public_user(row) if row else {}

    def authenticate(self, username: str, password: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT password_hash, status FROM iam_users WHERE username = ? COLLATE NOCASE",
                (str(username).strip(),),
            ).fetchone()
        return bool(row and row["status"] in ACTIVE_USER_STATUSES and verify_password(password, row["password_hash"]))

    def needs_password_setup(self, username: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT password_hash, status FROM iam_users WHERE username = ? COLLATE NOCASE",
                (str(username).strip(),),
            ).fetchone()
        return bool(row and row["status"] in ACTIVE_USER_STATUSES and not row["password_hash"])

    def update_password(self, username: str, password: str, *, must_change: bool = False) -> bool:
        encoded = hash_password(password, iterations=self.password_iterations)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE iam_users SET password_hash=?, must_change_password=?, updated_at=? "
                "WHERE username=? COLLATE NOCASE",
                (encoded, int(must_change), _now_text(), str(username).strip()),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _legacy_role_code(role_name: str) -> str:
        digest = hashlib.sha256(role_name.encode("utf-8")).hexdigest()[:16]
        return f"legacy.{digest}"

    def _ensure_legacy_role(self, connection: sqlite3.Connection, role_name: str) -> str:
        now = _now_text()
        code = self._legacy_role_code(role_name)
        row = connection.execute(
            "SELECT role_id FROM iam_security_roles WHERE code=?",
            (code,),
        ).fetchone()
        if row:
            connection.execute(
                "UPDATE iam_security_roles SET name=?, updated_at=? WHERE role_id=?",
                (role_name, now, row["role_id"]),
            )
            return row["role_id"]
        role_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO iam_security_roles(role_id, code, name, is_system, status, created_at, updated_at) "
            "VALUES(?, ?, ?, 0, 'active', ?, ?)",
            (role_id, code, role_name, now, now),
        )
        return role_id

    def migrate_legacy_users(
        self,
        excel_path: Path | str,
        *,
        backup_dir: Path | str | None = None,
        refresh_existing_passwords: bool = False,
    ) -> UserMigrationResult:
        """以原子且可重复执行的方式导入当前机器上的用户工作簿。

        除非明确传入 ``refresh_existing_passwords``，否则保留数据库中已有的密码哈希，
        确保部署后可以安全地重复执行普通一键迁移。
        """
        source = Path(excel_path)
        if not source.exists():
            raise FileNotFoundError(f"用户文件不存在：{source}")
        frame = pd.read_excel(source, engine="openpyxl", dtype=str)
        required_columns = {"用户名", "密码", "角色"}
        missing = required_columns - set(frame.columns)
        if missing:
            raise ValueError(f"用户文件缺少列：{'、'.join(sorted(missing))}")

        records: list[tuple[str, str, str]] = []
        seen_usernames: set[str] = set()
        for _, row in frame.iterrows():
            raw_username = row.get("用户名")
            username = "" if pd.isna(raw_username) else str(raw_username).strip()
            if not username:
                continue
            normalized = username.casefold()
            if normalized in seen_usernames:
                raise ValueError(f"用户文件存在重复用户名：{username}")
            seen_usernames.add(normalized)
            raw_password = row.get("密码")
            password = "" if pd.isna(raw_password) else str(raw_password).strip()
            raw_role = row.get("角色")
            role = "普通用户" if pd.isna(raw_role) or not str(raw_role).strip() else str(raw_role).strip()
            records.append((username, password, role))
        if not records:
            raise ValueError("用户文件中没有可迁移的用户")

        backup_path: Path | None = None
        if backup_dir is not None:
            target_dir = Path(backup_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = target_dir / f"users_before_iam_{timestamp}.xlsx"
            shutil.copy2(source, backup_path)

        imported = updated = unchanged = password_refreshed = 0
        now = _now_text()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for username, password, role in records:
                    existing = connection.execute(
                        "SELECT user_id, password_hash, legacy_role, display_name, status "
                        "FROM iam_users WHERE username=? COLLATE NOCASE",
                        (username,),
                    ).fetchone()
                    role_id = self._ensure_legacy_role(connection, role)
                    if existing is None:
                        user_id = str(uuid.uuid4())
                        encoded = hash_password(password, iterations=self.password_iterations) if password else None
                        connection.execute(
                            "INSERT INTO iam_users(user_id, username, display_name, password_hash, legacy_role, "
                            "status, must_change_password, created_at, updated_at) "
                            "VALUES(?, ?, ?, ?, ?, 'active', 0, ?, ?)",
                            (user_id, username, username, encoded, role, now, now),
                        )
                        imported += 1
                    else:
                        user_id = existing["user_id"]
                        fields_changed = existing["legacy_role"] != role
                        new_hash = existing["password_hash"]
                        if refresh_existing_passwords:
                            new_hash = (
                                hash_password(password, iterations=self.password_iterations) if password else None
                            )
                            password_refreshed += 1
                            fields_changed = True
                        if fields_changed:
                            connection.execute(
                                "UPDATE iam_users SET legacy_role=?, password_hash=?, updated_at=? WHERE user_id=?",
                                (role, new_hash, now, user_id),
                            )
                            updated += 1
                        else:
                            unchanged += 1
                    # 每个迁移用户只保留一个兼容角色，后续可另外叠加不依赖它的新权限角色。
                    legacy_role_ids = connection.execute(
                        "SELECT ur.role_id FROM iam_user_roles ur "
                        "JOIN iam_security_roles r ON r.role_id=ur.role_id "
                        "WHERE ur.user_id=? AND r.code LIKE 'legacy.%'",
                        (user_id,),
                    ).fetchall()
                    for old_role in legacy_role_ids:
                        if old_role["role_id"] != role_id:
                            connection.execute(
                                "DELETE FROM iam_user_roles WHERE user_id=? AND role_id=?",
                                (user_id, old_role["role_id"]),
                            )
                    connection.execute(
                        "INSERT OR IGNORE INTO iam_user_roles(user_id, role_id, created_at) VALUES(?, ?, ?)",
                        (user_id, role_id, now),
                    )
                detail = {
                    "source": str(source),
                    "imported": imported,
                    "updated": updated,
                    "unchanged": unchanged,
                    "password_refreshed": password_refreshed,
                }
                connection.execute(
                    "INSERT INTO iam_meta(key, value, updated_at) VALUES('users_storage_mode', 'database', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (now,),
                )
                connection.execute(
                    "INSERT INTO iam_audit_logs(audit_id, actor_user_id, action, target_type, target_id, "
                    "detail_json, created_at) VALUES(?, NULL, 'legacy_users_migrated', 'system', 'iam_users', ?, ?)",
                    (str(uuid.uuid4()), json.dumps(detail, ensure_ascii=False), now),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return UserMigrationResult(
            source_path=str(source),
            backup_path=str(backup_path) if backup_path else None,
            imported=imported,
            updated=updated,
            unchanged=unchanged,
            password_refreshed=password_refreshed,
            total=len(records),
        )

    def create_user(self, username: str, password: str, role: str) -> bool:
        username = str(username).strip()
        if not username:
            raise ValueError("用户名不能为空")
        if password and len(str(password).strip()) < 6:
            raise ValueError("密码至少需要6位")
        role = str(role).strip() or "普通用户"
        now = _now_text()
        encoded = hash_password(password, iterations=self.password_iterations) if password else None
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            role_id = self._ensure_legacy_role(connection, role)
            user_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO iam_users(user_id, username, display_name, password_hash, legacy_role, status, "
                "must_change_password, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 'active', 0, ?, ?)",
                (user_id, username, username, encoded, role, now, now),
            )
            connection.execute(
                "INSERT INTO iam_user_roles(user_id, role_id, created_at) VALUES(?, ?, ?)",
                (user_id, role_id, now),
            )
        return True

    def update_user(self, username: str, password: str | None, role: str | None) -> bool:
        current = self.get_user(username)
        if not current:
            raise ValueError(f"用户 {username} 不存在")
        if password and len(str(password).strip()) < 6:
            raise ValueError("密码至少需要6位")
        now = _now_text()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if role is not None and str(role).strip():
                role_name = str(role).strip()
                role_id = self._ensure_legacy_role(connection, role_name)
                connection.execute(
                    "UPDATE iam_users SET legacy_role=?, updated_at=? WHERE user_id=?",
                    (role_name, now, current["user_id"]),
                )
                rows = connection.execute(
                    "SELECT ur.role_id FROM iam_user_roles ur JOIN iam_security_roles r ON r.role_id=ur.role_id "
                    "WHERE ur.user_id=? AND r.code LIKE 'legacy.%'",
                    (current["user_id"],),
                ).fetchall()
                for row in rows:
                    if row["role_id"] != role_id:
                        connection.execute(
                            "DELETE FROM iam_user_roles WHERE user_id=? AND role_id=?",
                            (current["user_id"], row["role_id"]),
                        )
                connection.execute(
                    "INSERT OR IGNORE INTO iam_user_roles(user_id, role_id, created_at) VALUES(?, ?, ?)",
                    (current["user_id"], role_id, now),
                )
            if password:
                encoded = hash_password(password, iterations=self.password_iterations)
                connection.execute(
                    "UPDATE iam_users SET password_hash=?, must_change_password=0, updated_at=? WHERE user_id=?",
                    (encoded, now, current["user_id"]),
                )
        return True

    def set_user_status(self, username: str, status: str) -> bool:
        if status not in {"active", "disabled", "departed"}:
            raise ValueError(f"不支持的用户状态：{status}")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE iam_users SET status=?, updated_at=? WHERE username=? COLLATE NOCASE",
                (status, _now_text(), str(username).strip()),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"用户 {username} 不存在")
        return True

    def get_external_identity(self, username: str, provider: str = "wecom") -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT e.* FROM iam_external_identities e "
                "JOIN iam_users u ON u.user_id=e.user_id "
                "WHERE u.username=? COLLATE NOCASE AND e.provider=?",
                (str(username).strip(), provider),
            ).fetchone()
        return dict(row) if row else {}

    def list_external_identities(self, provider: str = "wecom") -> dict[str, dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT u.username, e.* FROM iam_external_identities e "
                "JOIN iam_users u ON u.user_id=e.user_id WHERE e.provider=?",
                (provider,),
            ).fetchall()
        return {row["username"]: dict(row) for row in rows}

    def bind_external_identity(
        self,
        username: str,
        external_userid: str,
        *,
        provider: str = "wecom",
        display_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        user = self.get_user(username)
        if not user:
            raise ValueError(f"用户 {username} 不存在")
        external_userid = str(external_userid).strip()
        if not external_userid:
            raise ValueError("外部账号不能为空")
        now = _now_text()
        with self._lock, self._connect() as connection:
            conflict = connection.execute(
                "SELECT u.username FROM iam_external_identities e "
                "JOIN iam_users u ON u.user_id=e.user_id "
                "WHERE e.provider=? AND e.external_userid=? AND e.user_id<>?",
                (provider, external_userid, user["user_id"]),
            ).fetchone()
            if conflict:
                raise ValueError(f"该外部账号已绑定系统用户：{conflict['username']}")
            connection.execute(
                "INSERT INTO iam_external_identities(external_identity_id, user_id, provider, external_userid, "
                "external_display_name, binding_source, metadata_json, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, 'manual', ?, ?, ?) "
                "ON CONFLICT(provider, user_id) DO UPDATE SET external_userid=excluded.external_userid, "
                "external_display_name=excluded.external_display_name, metadata_json=excluded.metadata_json, "
                "binding_source='manual', updated_at=excluded.updated_at",
                (
                    str(uuid.uuid4()),
                    user["user_id"],
                    provider,
                    external_userid,
                    display_name,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return True

    def unbind_external_identity(self, username: str, provider: str = "wecom") -> bool:
        user = self.get_user(username)
        if not user:
            raise ValueError(f"用户 {username} 不存在")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM iam_external_identities WHERE user_id=? AND provider=?",
                (user["user_id"], provider),
            )
        return cursor.rowcount > 0

    def list_org_units(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT child.*, parent.name AS parent_name FROM org_units child "
                "LEFT JOIN org_units parent ON parent.org_unit_id=child.parent_org_unit_id "
                "ORDER BY child.sort_order, child.name"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_org_unit(
        self,
        *,
        code: str,
        name: str,
        parent_org_unit_id: str | None = None,
        wecom_department_id: str | None = None,
        sort_order: int = 0,
        reject_existing: bool = False,
    ) -> str:
        code = normalize_stable_code(code)
        name = str(name).strip()
        if not code or not name:
            raise ValueError("部门编码和名称不能为空")
        now = _now_text()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT org_unit_id, source FROM org_units WHERE code=? COLLATE NOCASE",
                (code,),
            ).fetchone()
            if existing:
                if reject_existing:
                    raise ValueError(f"部门编码已存在：{code}")
                org_unit_id = existing["org_unit_id"]
                if parent_org_unit_id == org_unit_id:
                    raise ValueError("部门不能把自己设为上级部门")
                connection.execute(
                    "UPDATE org_units SET name=?, parent_org_unit_id=?, wecom_department_id=?, "
                    "sort_order=?, manual_override=?, status='active', updated_at=? WHERE org_unit_id=?",
                    (
                        name,
                        parent_org_unit_id or None,
                        wecom_department_id or None,
                        int(sort_order),
                        int(existing["source"] == "wecom"),
                        now,
                        org_unit_id,
                    ),
                )
            else:
                error = validate_stable_code(code)
                if error:
                    raise ValueError(error)
                org_unit_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO org_units(org_unit_id, code, name, parent_org_unit_id, wecom_department_id, "
                    "source, manual_override, sort_order, status, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, 'manual', 0, ?, 'active', ?, ?)",
                    (
                        org_unit_id,
                        code,
                        name,
                        parent_org_unit_id or None,
                        wecom_department_id or None,
                        int(sort_order),
                        now,
                        now,
                    ),
                )
        return org_unit_id

    def import_wecom_departments(self, departments: Iterable[dict[str, Any]]) -> tuple[int, int]:
        """新增或更新管理员选定的企业微信部门快照。"""
        normalized = [item for item in departments if str(item.get("id", "")).strip()]
        if not normalized:
            return 0, 0
        now = _now_text()
        inserted = 0
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            id_map: dict[str, str] = {}
            for item in normalized:
                wecom_id = str(item.get("id", "")).strip()
                row = connection.execute(
                    "SELECT org_unit_id FROM org_units WHERE wecom_department_id=?",
                    (wecom_id,),
                ).fetchone()
                if row:
                    id_map[wecom_id] = row["org_unit_id"]
                else:
                    org_unit_id = str(uuid.uuid4())
                    id_map[wecom_id] = org_unit_id
                    connection.execute(
                        "INSERT INTO org_units(org_unit_id, code, name, wecom_department_id, source, "
                        "manual_override, external_name_snapshot, external_parent_snapshot, sort_order, "
                        "status, created_at, updated_at) "
                        "VALUES(?, ?, ?, ?, 'wecom', 0, ?, ?, ?, 'active', ?, ?)",
                        (
                            org_unit_id,
                            f"wecom:{wecom_id}",
                            str(item.get("name", "")).strip() or f"企业微信部门 {wecom_id}",
                            wecom_id,
                            str(item.get("name", "")).strip(),
                            str(item.get("parentid", "")).strip(),
                            int(item.get("order", 0) or 0),
                            now,
                            now,
                        ),
                    )
                    inserted += 1
            for item in normalized:
                wecom_id = str(item.get("id", "")).strip()
                parent_wecom_id = str(item.get("parentid", "")).strip()
                parent_id = id_map.get(parent_wecom_id)
                connection.execute(
                    "UPDATE org_units SET "
                    "name=CASE WHEN manual_override=0 THEN ? ELSE name END, "
                    "parent_org_unit_id=CASE WHEN manual_override=0 THEN ? ELSE parent_org_unit_id END, "
                    "sort_order=CASE WHEN manual_override=0 THEN ? ELSE sort_order END, "
                    "source='wecom', external_name_snapshot=?, external_parent_snapshot=?, "
                    "status='active', updated_at=? WHERE org_unit_id=?",
                    (
                        str(item.get("name", "")).strip() or f"企业微信部门 {wecom_id}",
                        parent_id,
                        int(item.get("order", 0) or 0),
                        str(item.get("name", "")).strip(),
                        parent_wecom_id,
                        now,
                        id_map[wecom_id],
                    ),
                )
            connection.commit()
        return inserted, max(0, len(normalized) - inserted)

    def list_positions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT p.*, COUNT(DISTINCT CASE WHEN m.status='active' AND u.status='active' "
                "THEN m.user_id END) AS member_count "
                "FROM iam_positions p LEFT JOIN org_memberships m ON m.position_id=p.position_id "
                "LEFT JOIN iam_users u ON u.user_id=m.user_id "
                "GROUP BY p.position_id ORDER BY p.level DESC, p.name"
            ).fetchall()
            permission_rows = connection.execute(
                "SELECT pp.position_id, permission.code FROM iam_position_permissions pp "
                "JOIN iam_permissions permission ON permission.permission_id=pp.permission_id "
                "ORDER BY permission.code"
            ).fetchall()
        permission_map: dict[str, list[str]] = {}
        for row in permission_rows:
            permission_map.setdefault(row["position_id"], []).append(row["code"])
        result = []
        for row in rows:
            item = dict(row)
            item["permission_codes"] = permission_map.get(row["position_id"], [])
            result.append(item)
        return result

    def save_position(
        self,
        *,
        code: str,
        name: str,
        level: int = 0,
        reject_existing: bool = False,
    ) -> str:
        code = normalize_stable_code(code)
        name = str(name).strip()
        if not code or not name:
            raise ValueError("岗位编码和名称不能为空")
        now = _now_text()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT position_id, source FROM iam_positions WHERE code=? COLLATE NOCASE",
                (code,),
            ).fetchone()
            if row:
                if reject_existing:
                    raise ValueError(f"岗位编码已存在：{code}")
                position_id = row["position_id"]
                connection.execute(
                    "UPDATE iam_positions SET name=?, level=?, manual_override=?, status='active', "
                    "updated_at=? WHERE position_id=?",
                    (name, int(level), int(row["source"] == "wecom"), now, position_id),
                )
            else:
                error = validate_stable_code(code)
                if error:
                    raise ValueError(error)
                position_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO iam_positions(position_id, code, name, source, manual_override, level, "
                    "status, created_at, updated_at) VALUES(?, ?, ?, 'manual', 0, ?, 'active', ?, ?)",
                    (position_id, code, name, int(level), now, now),
                )
        return position_id

    def import_wecom_positions(self, contacts: Iterable[dict[str, Any]]) -> tuple[int, int]:
        """导入不重复的企业微信职务文本，同时保留本地覆盖内容。

        企业微信以自由文本而非稳定岗位 ID 提供职务，因此外部职务改名后会作为新的岗位
        候选导入，不猜测两个不同文本是否代表同一岗位。
        """
        names = sorted(
            {
                str(contact.get("position", "")).strip()
                for contact in contacts
                if str(contact.get("position", "")).strip() and contact.get("is_active", True)
            },
            key=lambda value: value.casefold(),
        )
        if not names:
            return 0, 0
        inserted = 0
        now = _now_text()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for external_name in names:
                row = connection.execute(
                    "SELECT position_id FROM iam_positions WHERE source='wecom' AND external_name_snapshot=?",
                    (external_name,),
                ).fetchone()
                if row:
                    connection.execute(
                        "UPDATE iam_positions SET name=CASE WHEN manual_override=0 THEN ? ELSE name END, "
                        "status='active', updated_at=? WHERE position_id=?",
                        (external_name, now, row["position_id"]),
                    )
                    continue
                same_name = connection.execute(
                    "SELECT position_id FROM iam_positions WHERE name=? AND status='active' LIMIT 1",
                    (external_name,),
                ).fetchone()
                if same_name:
                    # 系统中已有同名岗位可以表达该职务，不再建立视觉上重复的候选项。
                    continue
                digest = hashlib.sha256(external_name.encode("utf-8")).hexdigest()[:16]
                code = f"wecom.position.{digest}"
                position_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO iam_positions(position_id, code, name, source, manual_override, "
                    "external_name_snapshot, level, status, created_at, updated_at) "
                    "VALUES(?, ?, ?, 'wecom', 0, ?, 0, 'active', ?, ?)",
                    (position_id, code, external_name, external_name, now, now),
                )
                inserted += 1
            connection.commit()
        return inserted, len(names) - inserted

    def get_primary_membership(self, username: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT m.*, o.name AS org_name, p.name AS position_name, manager.username AS manager_username "
                "FROM org_memberships m "
                "JOIN iam_users u ON u.user_id=m.user_id "
                "JOIN org_units o ON o.org_unit_id=m.org_unit_id "
                "LEFT JOIN iam_positions p ON p.position_id=m.position_id "
                "LEFT JOIN iam_users manager ON manager.user_id=m.direct_manager_user_id "
                "WHERE u.username=? COLLATE NOCASE AND m.is_primary=1 AND m.status='active' "
                "ORDER BY m.updated_at DESC LIMIT 1",
                (str(username).strip(),),
            ).fetchone()
        return dict(row) if row else {}

    def set_primary_membership(
        self,
        username: str,
        *,
        org_unit_id: str,
        position_id: str | None = None,
        manager_username: str | None = None,
    ) -> bool:
        user = self.get_user(username)
        if not user:
            raise ValueError(f"用户 {username} 不存在")
        manager_id = None
        if manager_username:
            manager = self.get_user(manager_username)
            if not manager:
                raise ValueError(f"直属上级用户不存在：{manager_username}")
            if manager["user_id"] == user["user_id"]:
                raise ValueError("直属上级不能是本人")
            manager_id = manager["user_id"]
        now = _now_text()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE org_memberships SET is_primary=0, updated_at=? WHERE user_id=? AND status='active'",
                (now, user["user_id"]),
            )
            existing = connection.execute(
                "SELECT membership_id FROM org_memberships WHERE user_id=? AND org_unit_id=? "
                "AND ((position_id IS NULL AND ? IS NULL) OR position_id=?)",
                (user["user_id"], org_unit_id, position_id, position_id),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE org_memberships SET direct_manager_user_id=?, is_primary=1, status='active', "
                    "ended_at=NULL, updated_at=? WHERE membership_id=?",
                    (manager_id, now, existing["membership_id"]),
                )
            else:
                connection.execute(
                    "INSERT INTO org_memberships(membership_id, user_id, org_unit_id, position_id, "
                    "direct_manager_user_id, is_primary, status, started_at, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, 1, 'active', ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        user["user_id"],
                        org_unit_id,
                        position_id or None,
                        manager_id,
                        now,
                        now,
                        now,
                    ),
                )
        return True

    # ------------------------------------------------------------------
    # 稳定权限与安全角色

    @staticmethod
    def _validate_security_role_code(code: str) -> str:
        normalized = normalize_stable_code(code)
        error = validate_stable_code(normalized)
        if error:
            raise ValueError(error)
        if normalized.startswith("legacy."):
            raise ValueError("legacy. 前缀由系统兼容层保留")
        return normalized

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        actor_username: str | None,
        action: str,
        target_type: str,
        target_id: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        actor_user_id = None
        if actor_username:
            actor = connection.execute(
                "SELECT user_id FROM iam_users WHERE username=? COLLATE NOCASE",
                (str(actor_username).strip(),),
            ).fetchone()
            actor_user_id = actor["user_id"] if actor else None
        connection.execute(
            "INSERT INTO iam_audit_logs(audit_id, actor_user_id, action, target_type, target_id, "
            "detail_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                actor_user_id,
                action,
                target_type,
                str(target_id),
                json.dumps(detail or {}, ensure_ascii=False),
                _now_text(),
            ),
        )

    def seed_permission_catalog(
        self,
        permissions: Iterable[dict[str, Any]],
        default_grants: dict[str, set[str]] | None = None,
    ) -> tuple[int, int]:
        """新增或更新权限元数据，并只应用一次兼容默认授权。

        每一组默认角色与权限关系都会写入初始化标记。管理员之后移除授权时，重复启动或
        迁移不会悄悄将其重新添加。
        """
        normalized_permissions: list[dict[str, str]] = []
        for item in permissions:
            code = str(item.get("code", "")).strip().lower()
            name = str(item.get("name", "")).strip()
            module = str(item.get("module", "")).strip()
            if not code or not name or not module:
                raise ValueError("权限编码、名称和模块不能为空")
            normalized_permissions.append(
                {
                    "code": code,
                    "name": name,
                    "module": module,
                    "description": str(item.get("description", "")).strip(),
                }
            )

        inserted_permissions = inserted_grants = 0
        now = _now_text()
        grants_by_role = {
            str(role_name).strip().casefold(): {str(code).strip().lower() for code in codes}
            for role_name, codes in (default_grants or {}).items()
            if str(role_name).strip()
        }
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in normalized_permissions:
                existing = connection.execute(
                    "SELECT permission_id FROM iam_permissions WHERE code=? COLLATE NOCASE",
                    (item["code"],),
                ).fetchone()
                if existing:
                    connection.execute(
                        "UPDATE iam_permissions SET name=?, module=?, description=?, updated_at=? "
                        "WHERE permission_id=?",
                        (
                            item["name"],
                            item["module"],
                            item["description"],
                            now,
                            existing["permission_id"],
                        ),
                    )
                else:
                    connection.execute(
                        "INSERT INTO iam_permissions(permission_id, code, name, module, description, "
                        "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            item["code"],
                            item["name"],
                            item["module"],
                            item["description"],
                            now,
                            now,
                        ),
                    )
                    inserted_permissions += 1

            permission_rows = connection.execute(
                "SELECT permission_id, code FROM iam_permissions"
            ).fetchall()
            permission_ids = {row["code"].lower(): row["permission_id"] for row in permission_rows}
            role_rows = connection.execute(
                "SELECT role_id, name FROM iam_security_roles"
            ).fetchall()
            for role in role_rows:
                for permission_code in grants_by_role.get(str(role["name"]).casefold(), set()):
                    permission_id = permission_ids.get(permission_code)
                    if not permission_id:
                        continue
                    marker_key = f"permission_default:{role['role_id']}:{permission_id}"
                    marker = connection.execute(
                        "SELECT 1 FROM iam_meta WHERE key=?",
                        (marker_key,),
                    ).fetchone()
                    if marker:
                        continue
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO iam_role_permissions(role_id, permission_id, created_at) "
                        "VALUES(?, ?, ?)",
                        (role["role_id"], permission_id, now),
                    )
                    inserted_grants += max(0, cursor.rowcount)
                    connection.execute(
                        "INSERT INTO iam_meta(key, value, updated_at) VALUES(?, 'applied', ?)",
                        (marker_key, now),
                    )
        return inserted_permissions, inserted_grants

    def list_permissions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT p.*, COUNT(rp.role_id) AS role_count "
                "FROM iam_permissions p LEFT JOIN iam_role_permissions rp "
                "ON rp.permission_id=p.permission_id "
                "GROUP BY p.permission_id ORDER BY p.module, p.name, p.code"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_position_permission_codes(self, position_id: str) -> set[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT permission.code FROM iam_position_permissions pp "
                "JOIN iam_permissions permission ON permission.permission_id=pp.permission_id "
                "WHERE pp.position_id=? ORDER BY permission.code",
                (str(position_id),),
            ).fetchall()
        return {str(row["code"]) for row in rows}

    def set_position_permissions(
        self,
        position_id: str,
        permission_codes: Iterable[str],
        *,
        actor_username: str | None = None,
    ) -> bool:
        normalized_codes = list(
            dict.fromkeys(str(code).strip().lower() for code in permission_codes if str(code).strip())
        )
        now = _now_text()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            position = connection.execute(
                "SELECT position_id, name FROM iam_positions WHERE position_id=?",
                (str(position_id),),
            ).fetchone()
            if not position:
                raise ValueError("岗位不存在")
            permission_ids: list[str] = []
            if normalized_codes:
                placeholders = ",".join("?" for _ in normalized_codes)
                rows = connection.execute(
                    f"SELECT permission_id, code FROM iam_permissions WHERE code IN ({placeholders})",
                    normalized_codes,
                ).fetchall()
                found = {str(row["code"]).lower(): row["permission_id"] for row in rows}
                missing = [code for code in normalized_codes if code not in found]
                if missing:
                    raise ValueError(f"存在无效权限编码：{'、'.join(missing)}")
                permission_ids = [found[code] for code in normalized_codes]
            connection.execute(
                "DELETE FROM iam_position_permissions WHERE position_id=?",
                (position["position_id"],),
            )
            connection.executemany(
                "INSERT INTO iam_position_permissions(position_id, permission_id, created_at) "
                "VALUES(?, ?, ?)",
                [(position["position_id"], permission_id, now) for permission_id in permission_ids],
            )
            self._audit(
                connection,
                actor_username=actor_username,
                action="position_permissions_updated",
                target_type="position",
                target_id=position["position_id"],
                detail={"name": position["name"], "permissions": normalized_codes},
            )
        return True

    def list_security_roles(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        where_clause = "" if include_disabled else "WHERE r.status='active'"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT r.*, COUNT(DISTINCT ur.user_id) AS user_count "
                "FROM iam_security_roles r LEFT JOIN iam_user_roles ur ON ur.role_id=r.role_id "
                f"{where_clause} GROUP BY r.role_id "
                "ORDER BY CASE r.status WHEN 'active' THEN 0 ELSE 1 END, r.name, r.code"
            ).fetchall()
            permission_rows = connection.execute(
                "SELECT rp.role_id, p.code FROM iam_role_permissions rp "
                "JOIN iam_permissions p ON p.permission_id=rp.permission_id ORDER BY p.code"
            ).fetchall()
        permission_map: dict[str, list[str]] = {}
        for row in permission_rows:
            permission_map.setdefault(row["role_id"], []).append(row["code"])
        result = []
        for row in rows:
            item = dict(row)
            item["permission_codes"] = permission_map.get(row["role_id"], [])
            item["is_compatibility"] = str(row["code"]).startswith("legacy.")
            result.append(item)
        return result

    def create_security_role(
        self,
        *,
        code: str,
        name: str,
        permission_codes: Iterable[str] = (),
        actor_username: str | None = None,
    ) -> str:
        normalized_code = self._validate_security_role_code(code)
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("角色名称不能为空")
        role_id = str(uuid.uuid4())
        now = _now_text()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT 1 FROM iam_security_roles WHERE code=? COLLATE NOCASE",
                (normalized_code,),
            ).fetchone()
            if duplicate:
                raise ValueError(f"附加权限组编码已存在：{normalized_code}")
            connection.execute(
                "INSERT INTO iam_security_roles(role_id, code, name, is_system, status, created_at, updated_at) "
                "VALUES(?, ?, ?, 0, 'active', ?, ?)",
                (role_id, normalized_code, normalized_name, now, now),
            )
            self._replace_role_permissions(connection, role_id, permission_codes, now)
            self._audit(
                connection,
                actor_username=actor_username,
                action="security_role_created",
                target_type="security_role",
                target_id=role_id,
                detail={"code": normalized_code, "name": normalized_name},
            )
        return role_id

    @staticmethod
    def _replace_role_permissions(
        connection: sqlite3.Connection,
        role_id: str,
        permission_codes: Iterable[str],
        now: str,
    ) -> list[str]:
        normalized_codes = list(
            dict.fromkeys(str(code).strip().lower() for code in permission_codes if str(code).strip())
        )
        permission_ids: list[str] = []
        if normalized_codes:
            placeholders = ",".join("?" for _ in normalized_codes)
            rows = connection.execute(
                f"SELECT permission_id, code FROM iam_permissions WHERE code IN ({placeholders})",
                normalized_codes,
            ).fetchall()
            found = {str(row["code"]).lower(): row["permission_id"] for row in rows}
            missing = [code for code in normalized_codes if code not in found]
            if missing:
                raise ValueError(f"存在无效权限编码：{'、'.join(missing)}")
            permission_ids = [found[code] for code in normalized_codes]
        connection.execute("DELETE FROM iam_role_permissions WHERE role_id=?", (role_id,))
        connection.executemany(
            "INSERT INTO iam_role_permissions(role_id, permission_id, created_at) VALUES(?, ?, ?)",
            [(role_id, permission_id, now) for permission_id in permission_ids],
        )
        return normalized_codes

    def update_security_role(
        self,
        role_id: str,
        *,
        name: str,
        status: str = "active",
        permission_codes: Iterable[str] = (),
        actor_username: str | None = None,
    ) -> bool:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("角色名称不能为空")
        if status not in {"active", "disabled"}:
            raise ValueError("角色状态只支持 active 或 disabled")
        now = _now_text()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            role = connection.execute(
                "SELECT role_id, code, name FROM iam_security_roles WHERE role_id=?",
                (str(role_id),),
            ).fetchone()
            if not role:
                raise ValueError("安全角色不存在")
            if str(role["code"]).startswith("legacy.") and normalized_name != role["name"]:
                raise ValueError("兼容角色名称由旧角色字段维护，不能在此改名")
            connection.execute(
                "UPDATE iam_security_roles SET name=?, status=?, updated_at=? WHERE role_id=?",
                (normalized_name, status, now, role["role_id"]),
            )
            normalized_codes = self._replace_role_permissions(
                connection, role["role_id"], permission_codes, now
            )
            self._audit(
                connection,
                actor_username=actor_username,
                action="security_role_updated",
                target_type="security_role",
                target_id=role["role_id"],
                detail={"name": normalized_name, "status": status, "permissions": normalized_codes},
            )
        return True

    def get_user_security_roles(
        self,
        username: str,
        *,
        include_compatibility: bool = True,
    ) -> list[dict[str, Any]]:
        compatibility_filter = "" if include_compatibility else "AND r.code NOT LIKE 'legacy.%'"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT r.*, GROUP_CONCAT(p.code) AS permission_codes_text FROM iam_user_roles ur "
                "JOIN iam_users u ON u.user_id=ur.user_id "
                "JOIN iam_security_roles r ON r.role_id=ur.role_id "
                "LEFT JOIN iam_role_permissions rp ON rp.role_id=r.role_id "
                "LEFT JOIN iam_permissions p ON p.permission_id=rp.permission_id "
                f"WHERE u.username=? COLLATE NOCASE {compatibility_filter} "
                "GROUP BY r.role_id ORDER BY r.name, r.code",
                (str(username).strip(),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw_codes = item.pop("permission_codes_text", "") or ""
            item["permission_codes"] = sorted(code for code in raw_codes.split(",") if code)
            item["is_compatibility"] = str(item.get("code", "")).startswith("legacy.")
            result.append(item)
        return result

    def set_user_security_roles(
        self,
        username: str,
        role_ids: Iterable[str],
        *,
        preserve_compatibility: bool = True,
        actor_username: str | None = None,
    ) -> bool:
        user = self.get_user(username)
        if not user:
            raise ValueError(f"用户 {username} 不存在")
        normalized_role_ids = list(dict.fromkeys(str(value).strip() for value in role_ids if str(value).strip()))
        now = _now_text()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if normalized_role_ids:
                placeholders = ",".join("?" for _ in normalized_role_ids)
                rows = connection.execute(
                    f"SELECT role_id, code, status FROM iam_security_roles WHERE role_id IN ({placeholders})",
                    normalized_role_ids,
                ).fetchall()
                found = {row["role_id"]: row for row in rows}
                missing = [role_id for role_id in normalized_role_ids if role_id not in found]
                if missing:
                    raise ValueError("选择中包含不存在的安全角色")
                disabled = [
                    role_id
                    for role_id in normalized_role_ids
                    if found[role_id]["status"] != "active"
                ]
                if disabled:
                    raise ValueError("不能分配已停用的安全角色")
                if preserve_compatibility and any(
                    str(found[role_id]["code"]).startswith("legacy.")
                    for role_id in normalized_role_ids
                ):
                    raise ValueError("兼容角色由旧角色字段自动维护，不能作为附加角色分配")
            if preserve_compatibility:
                connection.execute(
                    "DELETE FROM iam_user_roles WHERE user_id=? AND role_id IN "
                    "(SELECT role_id FROM iam_security_roles WHERE code NOT LIKE 'legacy.%')",
                    (user["user_id"],),
                )
            else:
                connection.execute("DELETE FROM iam_user_roles WHERE user_id=?", (user["user_id"],))
            connection.executemany(
                "INSERT INTO iam_user_roles(user_id, role_id, created_at) VALUES(?, ?, ?)",
                [(user["user_id"], role_id, now) for role_id in normalized_role_ids],
            )
            self._audit(
                connection,
                actor_username=actor_username,
                action="user_security_roles_updated",
                target_type="user",
                target_id=user["user_id"],
                detail={"username": username, "role_ids": normalized_role_ids},
            )
        return True

    def get_user_permission_codes(self, username: str) -> set[str]:
        with self._lock, self._connect() as connection:
            user = connection.execute(
                "SELECT status FROM iam_users WHERE username=? COLLATE NOCASE",
                (str(username).strip(),),
            ).fetchone()
            if (
                str(username).strip().casefold() == "admin"
                and user
                and user["status"] == "active"
            ):
                rows = connection.execute(
                    "SELECT code FROM iam_permissions ORDER BY code"
                ).fetchall()
                return {str(row["code"]) for row in rows}
            role_rows = connection.execute(
                "SELECT DISTINCT p.code, r.code AS source_role_code FROM iam_users u "
                "JOIN iam_user_roles ur ON ur.user_id=u.user_id "
                "JOIN iam_security_roles r ON r.role_id=ur.role_id AND r.status='active' "
                "JOIN iam_role_permissions rp ON rp.role_id=r.role_id "
                "JOIN iam_permissions p ON p.permission_id=rp.permission_id "
                "WHERE u.username=? COLLATE NOCASE AND u.status='active'",
                (str(username).strip(),),
            ).fetchall()
            position_rows = connection.execute(
                "SELECT DISTINCT p.code FROM iam_users u "
                "JOIN org_memberships m ON m.user_id=u.user_id "
                "AND m.status='active' AND m.is_primary=1 "
                "JOIN iam_positions position ON position.position_id=m.position_id "
                "AND position.status='active' "
                "JOIN iam_position_permissions pp ON pp.position_id=position.position_id "
                "JOIN iam_permissions p ON p.permission_id=pp.permission_id "
                "WHERE u.username=? COLLATE NOCASE AND u.status='active'",
                (str(username).strip(),),
            ).fetchall()
        role_codes = {
            str(row["code"])
            for row in role_rows
            if not (
                str(row["source_role_code"]).startswith("legacy.")
                and ignores_legacy_role_grants(str(row["code"]))
            )
        }
        return role_codes | {str(row["code"]) for row in position_rows}

    def has_permission(self, username: str, permission_code: str) -> bool:
        permission_code = str(permission_code or "").strip().lower()
        if not permission_code:
            return False
        ignore_legacy_roles = int(ignores_legacy_role_grants(permission_code))
        with self._lock, self._connect() as connection:
            if str(username).strip().casefold() == "admin":
                admin_permission = connection.execute(
                    "SELECT 1 FROM iam_users u CROSS JOIN iam_permissions p "
                    "WHERE u.username='admin' COLLATE NOCASE AND u.status='active' "
                    "AND p.code=? COLLATE NOCASE LIMIT 1",
                    (permission_code,),
                ).fetchone()
                if admin_permission:
                    return True
            row = connection.execute(
                "SELECT 1 FROM iam_users u WHERE u.username=? COLLATE NOCASE AND u.status='active' AND ("
                "EXISTS (SELECT 1 FROM iam_user_roles ur "
                "JOIN iam_security_roles r ON r.role_id=ur.role_id AND r.status='active' "
                "JOIN iam_role_permissions rp ON rp.role_id=r.role_id "
                "JOIN iam_permissions p ON p.permission_id=rp.permission_id "
                "WHERE ur.user_id=u.user_id AND p.code=? COLLATE NOCASE "
                "AND (?=0 OR r.code NOT LIKE 'legacy.%')) "
                "OR EXISTS (SELECT 1 FROM org_memberships m "
                "JOIN iam_positions position ON position.position_id=m.position_id "
                "AND position.status='active' "
                "JOIN iam_position_permissions pp ON pp.position_id=position.position_id "
                "JOIN iam_permissions p ON p.permission_id=pp.permission_id "
                "WHERE m.user_id=u.user_id AND m.status='active' AND m.is_primary=1 "
                "AND p.code=? COLLATE NOCASE)) LIMIT 1",
                (
                    str(username).strip(),
                    permission_code,
                    ignore_legacy_roles,
                    permission_code,
                ),
            ).fetchone()
        return row is not None
