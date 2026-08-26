"""Relational identity, organization, permission, and external-account storage.

The application historically stores users in ``data/users.xlsx``.  This module
provides the target SQLite schema and deliberately keeps migration explicit and
idempotent: the workbook is read from the machine on which the migration runs,
so development and production passwords are never mixed.
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

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 390_000
ACTIVE_USER_STATUSES = {"active"}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hash_password(password: str, *, iterations: int = PASSWORD_ITERATIONS) -> str:
    """Return a versioned PBKDF2-SHA256 password hash."""
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
    """Verify a password without exposing the stored value to callers."""
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
    """Small synchronous repository used by login and NiceGUI callbacks.

    It shares the existing SQLite database file, but owns only ``iam_*``,
    ``org_*`` and ``work_assignments`` tables.  WAL and a process lock keep its
    short transactions compatible with the application's aiosqlite storage.
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
            "CREATE INDEX IF NOT EXISTS idx_work_assignments_assignee ON work_assignments(assignee_user_id, status)",
        ]
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            for statement in statements:
                connection.execute(statement)
            # Existing installations may already have the first IAM schema.
            # Add source/override metadata without rebuilding populated tables.
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
                # We cannot know whether an imported department was manually
                # edited before this metadata existed, so preserve it by default.
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
                "INSERT INTO iam_meta(key, value, updated_at) VALUES('schema_version', '1', ?) "
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
        """Import the current machine's workbook in one atomic, repeatable pass.

        Existing database password hashes are preserved unless
        ``refresh_existing_passwords`` is explicitly requested.  This makes the
        normal one-click action safe to repeat after deployment.
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
                    # A migrated user has exactly one compatibility role.  New
                    # permission roles can be added later without relying on it.
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
    ) -> str:
        code = str(code).strip()
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
        """Upsert the administrator-selected WeCom department snapshot."""
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
            rows = connection.execute("SELECT * FROM iam_positions ORDER BY level DESC, name").fetchall()
        return [dict(row) for row in rows]

    def save_position(self, *, code: str, name: str, level: int = 0) -> str:
        code = str(code).strip()
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
                position_id = row["position_id"]
                connection.execute(
                    "UPDATE iam_positions SET name=?, level=?, manual_override=?, status='active', "
                    "updated_at=? WHERE position_id=?",
                    (name, int(level), int(row["source"] == "wecom"), now, position_id),
                )
            else:
                position_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO iam_positions(position_id, code, name, source, manual_override, level, "
                    "status, created_at, updated_at) VALUES(?, ?, ?, 'manual', 0, ?, 'active', ?, ?)",
                    (position_id, code, name, int(level), now, now),
                )
        return position_id

    def import_wecom_positions(self, contacts: Iterable[dict[str, Any]]) -> tuple[int, int]:
        """Import distinct WeCom position text without replacing local overrides.

        WeCom exposes position as free text rather than a stable position ID.
        Consequently a renamed external position is imported as a new candidate
        instead of guessing that two different strings are the same job.
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
                    # An identical system-defined position already represents
                    # this text; do not create a visually duplicated candidate.
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
