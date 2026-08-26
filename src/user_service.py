"""Compatibility facade for legacy Excel users and the new IAM database."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from .identity_store import IdentityStore, UserMigrationResult
from .identity_matching import build_wecom_user_match_plan, suggest_contact_for_user

logger = logging.getLogger(__name__)


class UserService:
    """Serve users from SQLite after migration, otherwise from ``users.xlsx``.

    Deploying the code alone does not lock a server out: its workbook remains
    authoritative until an administrator runs migration on that server.
    """

    def __init__(
        self,
        *,
        excel_path: Path | str | None = None,
        db_path: Path | str | None = None,
        backup_dir: Path | str | None = None,
        password_iterations: int | None = None,
    ):
        base_dir = Path(__file__).parent.parent
        self.excel_path = Path(excel_path) if excel_path else base_dir / "data" / "users.xlsx"
        resolved_db_path = Path(db_path) if db_path else base_dir / "db" / "nicegui_storage.db"
        self.backup_dir = Path(backup_dir) if backup_dir else base_dir / "backups" / "user_migration"
        store_kwargs = {}
        if password_iterations is not None:
            store_kwargs["password_iterations"] = password_iterations
        self.identity_store = IdentityStore(resolved_db_path, **store_kwargs)
        self._lock = threading.RLock()

    @property
    def storage_mode(self) -> str:
        return "database" if self.identity_store.has_database_users() else "legacy_excel"

    @staticmethod
    def _safe_str_convert(value: Any) -> str:
        if pd.isna(value) or value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _format_password(raw_value: str) -> str:
        s = raw_value.strip()
        if "e" in s.lower():
            try:
                num = float(s)
                return f"{int(num)}" if num.is_integer() else f"{num}"
            except Exception:
                return s
        if "." in s:
            left, right = s.split(".", 1)
            if right == "0" or set(right) == {"0"}:
                return left
        return s

    def _load_excel_users(self) -> Dict[str, dict]:
        try:
            frame = pd.read_excel(
                self.excel_path,
                engine="openpyxl",
                dtype={"用户名": "string", "密码": "string", "角色": "string"},
            )
            return {
                str(row["用户名"]): {
                    "username": str(row["用户名"]),
                    "display_name": str(row["用户名"]),
                    "password": str(row["密码"]) if pd.notna(row["密码"]) else None,
                    "password_set": pd.notna(row["密码"]) and bool(str(row["密码"])),
                    "role": str(row["角色"]) if pd.notna(row["角色"]) else "匿名用户",
                    "status": "active",
                    "user_id": None,
                    "must_change_password": False,
                }
                for _, row in frame.iterrows()
                if pd.notna(row.get("用户名")) and str(row.get("用户名")).strip()
            }
        except Exception as exc:
            raise RuntimeError(f"用户数据加载失败: {exc}") from exc

    def load_users(self) -> Dict[str, dict]:
        if self.storage_mode == "database":
            return self.identity_store.list_users()
        return self._load_excel_users()

    def get_user(self, username: str) -> dict:
        if self.storage_mode == "database":
            return self.identity_store.get_user(username)
        user_info = self._load_excel_users().get(username, {})
        return {key: value if pd.notna(value) else None for key, value in user_info.items()}

    def authenticate(self, username: str, password: str) -> bool:
        if self.storage_mode == "database":
            return self.identity_store.authenticate(username, password)
        user = self.get_user(username)
        return bool(
            user
            and user.get("status", "active") == "active"
            and str(user.get("password", "")) == str(password)
        )

    def needs_password_setup(self, username: str) -> bool:
        if self.storage_mode == "database":
            return self.identity_store.needs_password_setup(username)
        user = self.get_user(username)
        return bool(user and user.get("password") is None)

    def _update_excel_password(self, username: str, new_password: str) -> bool:
        with self._lock:
            try:
                frame = pd.read_excel(self.excel_path, dtype=str)
                frame["密码"] = frame["密码"].astype("string")
                frame.loc[frame["用户名"] == username, "密码"] = str(new_password)
                frame.to_excel(self.excel_path, index=False, engine="openpyxl")
                return True
            except Exception:
                logger.error("Excel更新失败", exc_info=True)
                return False

    def update_password(self, username: str, new_password: str) -> bool:
        if not isinstance(new_password, str):
            raise TypeError("密码必须是字符串")
        normalized = new_password.strip()
        if len(normalized) < 6:
            raise ValueError("密码至少需要6位")
        if self.storage_mode == "database":
            return self.identity_store.update_password(username, normalized)
        return self._update_excel_password(username, normalized)

    def modify_user(self, action: str, username: str, password: str = "", role: str = "") -> bool:
        """Create, edit, or change user status in the active source."""
        if self.storage_mode == "database":
            if action == "add":
                return self.identity_store.create_user(username, password or "", role or "普通用户")
            if action in {"update", "edit"}:
                return self.identity_store.update_user(username, password or None, role)
            if action in {"delete", "deactivate"}:
                return self.identity_store.set_user_status(username, "disabled")
            if action == "depart":
                return self.identity_store.set_user_status(username, "departed")
            if action in {"activate", "restore"}:
                return self.identity_store.set_user_status(username, "active")
            raise ValueError(f"未知的操作指令: {action}")

        # Legacy mode remains intentionally compatible until migration.
        with self._lock:
            try:
                frame = pd.read_excel(self.excel_path, dtype=str)
                for column in ["用户名", "密码", "角色"]:
                    if column not in frame.columns:
                        frame[column] = pd.Series(dtype="string")
                if action == "add":
                    if username in frame["用户名"].values:
                        raise ValueError(f"用户 {username} 已存在")
                    frame = pd.concat(
                        [
                            frame,
                            pd.DataFrame(
                                [
                                    {
                                        "用户名": str(username),
                                        "密码": str(password) if password else "",
                                        "角色": str(role) if role else "普通用户",
                                    }
                                ]
                            ),
                        ],
                        ignore_index=True,
                    )
                elif action in {"update", "edit"}:
                    if username not in frame["用户名"].values:
                        raise ValueError(f"用户 {username} 不存在")
                    if password:  # empty means keep the current password
                        frame.loc[frame["用户名"] == username, "密码"] = str(password)
                    if role is not None:
                        frame.loc[frame["用户名"] == username, "角色"] = str(role)
                elif action == "delete":
                    if username not in frame["用户名"].values:
                        raise ValueError(f"用户 {username} 不存在")
                    frame = frame[frame["用户名"] != username]
                else:
                    raise ValueError("迁移前的 Excel 模式不支持停用/离职，请先执行一键迁移")
                frame.to_excel(self.excel_path, index=False, engine="openpyxl")
                return True
            except Exception:
                logger.error("Excel用户数据 %s 操作失败", action, exc_info=True)
                raise

    def migrate_legacy_users(self, *, refresh_existing_passwords: bool = False) -> UserMigrationResult:
        return self.identity_store.migrate_legacy_users(
            self.excel_path,
            backup_dir=self.backup_dir,
            refresh_existing_passwords=refresh_existing_passwords,
        )

    def get_wecom_binding(self, username: str) -> dict[str, Any]:
        if self.storage_mode != "database":
            return {}
        return self.identity_store.get_external_identity(username, "wecom")

    def list_wecom_bindings(self) -> dict[str, dict[str, Any]]:
        if self.storage_mode != "database":
            return {}
        return self.identity_store.list_external_identities("wecom")

    def bind_wecom_user(self, username: str, contact: dict[str, Any]) -> bool:
        if self.storage_mode != "database":
            raise RuntimeError("请先将用户迁移到身份数据库，再绑定企业微信")
        return self.identity_store.bind_external_identity(
            username,
            contact.get("userid", ""),
            provider="wecom",
            display_name=contact.get("name", ""),
            metadata={
                "departments": contact.get("departments", []),
                "department_ids": contact.get("department_ids", []),
                "position": contact.get("position", ""),
                "is_active": contact.get("is_active", True),
            },
        )

    def unbind_wecom_user(self, username: str) -> bool:
        if self.storage_mode != "database":
            return False
        return self.identity_store.unbind_external_identity(username, "wecom")

    def build_wecom_match_plan(self, contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return build_wecom_user_match_plan(
            self.load_users(),
            contacts,
            self.list_wecom_bindings(),
        )

    def suggest_wecom_contact(
        self,
        username: str,
        contacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        user = self.get_user(username)
        if not user:
            return {}
        return suggest_contact_for_user(
            username,
            user,
            contacts,
            self.list_wecom_bindings(),
        )

    def suggest_org_membership(self, contact: dict[str, Any]) -> dict[str, Any]:
        """Resolve an imported department and position for one WeCom contact."""
        units = self.list_org_units()
        units_by_wecom_id = {
            str(item.get("wecom_department_id", "")): item
            for item in units
            if item.get("wecom_department_id")
        }
        department_ids = [str(value) for value in contact.get("department_ids", []) if value]
        main_department_id = str(contact.get("main_department_id", "") or "")
        ordered_department_ids = [
            department_id
            for department_id in [main_department_id, *department_ids]
            if department_id
        ]
        org_unit = next(
            (
                units_by_wecom_id[department_id]
                for department_id in ordered_department_ids
                if department_id in units_by_wecom_id
            ),
            None,
        )

        position_text = str(contact.get("position", "")).strip()
        matching_positions = [
            item
            for item in self.list_positions()
            if position_text
            and position_text
            in {
                str(item.get("external_name_snapshot", "")).strip(),
                str(item.get("name", "")).strip(),
            }
        ]
        position = matching_positions[0] if len(matching_positions) == 1 else None
        return {
            "org_unit_id": org_unit.get("org_unit_id") if org_unit else None,
            "org_name": org_unit.get("name") if org_unit else "",
            "position_id": position.get("position_id") if position else None,
            "position_name": position.get("name") if position else "",
            "department_matched": org_unit is not None,
            "position_matched": position is not None,
        }

    def apply_suggested_org_membership(self, username: str, contact: dict[str, Any]) -> bool:
        """Fill a missing membership but never replace an existing assignment."""
        if self.get_primary_membership(username):
            return False
        suggestion = self.suggest_org_membership(contact)
        if not suggestion.get("org_unit_id"):
            return False
        self.set_primary_membership(
            username,
            org_unit_id=suggestion["org_unit_id"],
            position_id=suggestion.get("position_id"),
            manager_username=None,
        )
        return True

    def apply_wecom_match_plan(self, plan: list[dict[str, Any]]) -> tuple[int, int]:
        bound_count = 0
        org_assigned_count = 0
        for item in plan:
            contact = item.get("contact")
            if item.get("status") != "matched" or not isinstance(contact, dict):
                continue
            self.bind_wecom_user(str(item.get("username", "")), contact)
            bound_count += 1
            if self.apply_suggested_org_membership(str(item.get("username", "")), contact):
                org_assigned_count += 1
        return bound_count, org_assigned_count

    def list_org_units(self) -> list[dict[str, Any]]:
        return self.identity_store.list_org_units() if self.storage_mode == "database" else []

    def save_org_unit(self, **values) -> str:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再维护组织架构")
        return self.identity_store.save_org_unit(**values)

    def import_wecom_departments(self, departments) -> tuple[int, int]:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再导入组织架构")
        return self.identity_store.import_wecom_departments(departments)

    def import_wecom_positions(self, contacts) -> tuple[int, int]:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再导入岗位字典")
        return self.identity_store.import_wecom_positions(contacts)

    def list_positions(self) -> list[dict[str, Any]]:
        return self.identity_store.list_positions() if self.storage_mode == "database" else []

    def save_position(self, **values) -> str:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再维护岗位")
        return self.identity_store.save_position(**values)

    def get_primary_membership(self, username: str) -> dict[str, Any]:
        if self.storage_mode != "database":
            return {}
        return self.identity_store.get_primary_membership(username)

    def set_primary_membership(self, username: str, **values) -> bool:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再分配组织和岗位")
        return self.identity_store.set_primary_membership(username, **values)
