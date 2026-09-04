"""旧版 Excel 用户数据与新身份数据库之间的兼容服务。"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from .identity_store import IdentityStore, UserMigrationResult
from .identity_matching import build_wecom_user_match_plan, suggest_contact_for_user
from .legacy_compatibility import record_legacy_compatibility_hit
from .permission_catalog import (
    DEPRECATED_PERMISSION_REPLACEMENTS,
    PERMISSION_CODES,
    build_legacy_default_grants,
    load_tool_role_mapping,
    permission_catalog_rows,
)

logger = logging.getLogger(__name__)


class UserService:
    """迁移后从 SQLite 提供用户数据，否则继续读取 ``users.xlsx``。

    只部署新代码不会导致服务器用户无法登录；管理员在服务器执行迁移前，服务器上的
    Excel 用户文件仍是权威数据源。
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
        if self.identity_store.has_database_users():
            self.sync_permission_catalog()

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
            users: Dict[str, dict] = {}
            for _, row in frame.iterrows():
                # 先转为普通字典，避免 pandas 的 Series 联合类型污染标量判空判断。
                # pandas 将列名声明为 Hashable，这里显式收窄为系统实际使用的字符串列名。
                record = {str(key): value for key, value in row.to_dict().items()}
                username_value = record.get("用户名")
                if not bool(pd.notna(username_value)) or not str(username_value).strip():
                    continue
                username = str(username_value)
                password_value = record.get("密码")
                role_value = record.get("角色")
                password_present = bool(pd.notna(password_value))
                users[username] = {
                    "username": username,
                    "display_name": username,
                    "password": str(password_value) if password_present else None,
                    "password_set": password_present and bool(str(password_value)),
                    "role": str(role_value) if bool(pd.notna(role_value)) else "匿名用户",
                    "status": "active",
                    "user_id": None,
                    "must_change_password": False,
                }
            record_legacy_compatibility_hit(
                "excel_user_store",
                "load_users",
                detail=f"source={self.excel_path}",
            )
            return users
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
        authenticated = bool(
            user
            and user.get("status", "active") == "active"
            and str(user.get("password", "")) == str(password)
        )
        if authenticated:
            record_legacy_compatibility_hit(
                "excel_user_store",
                "authenticate",
                username=username,
                detail="使用 users.xlsx 明文兼容认证",
            )
        return authenticated

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
        """在当前启用的数据源中新增、编辑用户或修改用户状态。"""
        if self.storage_mode == "database":
            if action == "add":
                result = self.identity_store.create_user(username, password or "", role or "普通用户")
                self.sync_permission_catalog()
                return result
            if action in {"update", "edit"}:
                result = self.identity_store.update_user(username, password or None, role)
                self.sync_permission_catalog()
                return result
            if action in {"delete", "deactivate"}:
                return self.identity_store.set_user_status(username, "disabled")
            if action == "depart":
                return self.identity_store.set_user_status(username, "departed")
            if action in {"activate", "restore"}:
                return self.identity_store.set_user_status(username, "active")
            raise ValueError(f"未知的操作指令: {action}")

        # 在执行迁移前，旧版 Excel 模式继续保持原有兼容行为。
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
                    if password:  # 留空表示保持当前密码
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
        result = self.identity_store.migrate_legacy_users(
            self.excel_path,
            backup_dir=self.backup_dir,
            refresh_existing_passwords=refresh_existing_passwords,
        )
        self.sync_permission_catalog()
        return result

    def sync_permission_catalog(
        self,
        *,
        strict_overview: bool = False,
        overview_config: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        """以幂等方式注册稳定权限和旧角色初始授权。"""
        roles = self.identity_store.list_security_roles()
        role_names = [str(item.get("name", "")) for item in roles]
        tool_mapping = load_tool_role_mapping(self.excel_path.parent.parent / "tools_permission.json")
        grants = build_legacy_default_grants(tool_mapping, known_role_names=role_names)
        result = self.identity_store.seed_permission_catalog(
            permission_catalog_rows(
                overview_config=overview_config,
                strict_overview=strict_overview,
            ),
            grants,
        )
        self.identity_store.replace_permission_codes(DEPRECATED_PERMISSION_REPLACEMENTS)
        return result

    def has_permission(
        self,
        username: str,
        permission_code: str,
        *,
        legacy_role: str = "",
        legacy_allowed_roles=None,
    ) -> bool:
        if self.storage_mode == "database":
            return self.identity_store.has_permission(username, permission_code)
        if (
            str(username).strip().casefold() == "admin"
            and str(permission_code).strip().lower() in PERMISSION_CODES
        ):
            user = self.get_user(username)
            allowed = bool(user and user.get("status", "active") == "active")
            if allowed:
                record_legacy_compatibility_hit(
                    "legacy_role_grant",
                    str(permission_code).strip().lower(),
                    username=username,
                    detail="旧 Excel 模式 admin 紧急授权",
                )
            return allowed
        if legacy_allowed_roles is None:
            record_legacy_compatibility_hit(
                "legacy_role_grant",
                str(permission_code).strip().lower(),
                username=username,
                detail=f"role={str(legacy_role or '').strip() or '-'}; rule=unrestricted",
            )
            return True
        allowed = {str(role).strip() for role in legacy_allowed_roles if str(role).strip()}
        matched = str(legacy_role or "").strip() in allowed
        if matched:
            record_legacy_compatibility_hit(
                "legacy_role_grant",
                str(permission_code).strip().lower(),
                username=username,
                detail=f"role={str(legacy_role or '').strip() or '-'}; rule=role_match",
            )
        return matched

    def list_permissions(self) -> list[dict[str, Any]]:
        if self.storage_mode != "database":
            return []
        permissions = self.identity_store.list_permissions()
        current_codes = {
            str(item["code"]).strip().lower()
            for item in permission_catalog_rows(strict_overview=False)
        }
        # 已从 JSON 删除的 label 权限保留在数据库供审计和恢复，但不再显示在授权界面。
        return [
            permission
            for permission in permissions
            if not str(permission.get("code", "")).lower().startswith("project_overview.item.")
            or str(permission.get("code", "")).lower() in current_codes
        ]

    def get_position_permission_codes(self, position_id: str) -> set[str]:
        if self.storage_mode != "database":
            return set()
        return self.identity_store.get_position_permission_codes(position_id)

    def get_overview_role_position_mapping(self) -> dict[str, list[str]]:
        if self.storage_mode != "database":
            return {}
        return self.identity_store.get_overview_role_position_mapping()

    def get_overview_role_position_managed_permissions(self) -> dict[str, set[str]]:
        if self.storage_mode != "database":
            return {}
        return self.identity_store.get_overview_role_position_managed_permissions()

    def apply_overview_role_position_mapping(
        self,
        role_position_mapping: dict[str, list[str]],
        position_permission_codes: dict[str, set[str]],
        *,
        affected_position_ids,
        actor_username: str | None = None,
    ) -> int:
        """保存旧角色映射，并批量整理相关岗位的概述项权限。"""
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再整理项目概述权限")
        return self.identity_store.apply_overview_role_position_mapping(
            role_position_mapping,
            position_permission_codes,
            affected_position_ids=affected_position_ids,
            actor_username=actor_username,
        )

    def set_position_permissions(self, position_id: str, permission_codes, **values) -> bool:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再配置岗位默认权限")
        return self.identity_store.set_position_permissions(
            position_id,
            permission_codes,
            **values,
        )

    def list_security_roles(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        if self.storage_mode != "database":
            return []
        return self.identity_store.list_security_roles(include_disabled=include_disabled)

    def create_security_role(self, **values) -> str:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再维护安全角色")
        return self.identity_store.create_security_role(**values)

    def update_security_role(self, role_id: str, **values) -> bool:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再维护安全角色")
        return self.identity_store.update_security_role(role_id, **values)

    def get_user_security_roles(
        self,
        username: str,
        *,
        include_compatibility: bool = True,
    ) -> list[dict[str, Any]]:
        if self.storage_mode != "database":
            return []
        return self.identity_store.get_user_security_roles(
            username,
            include_compatibility=include_compatibility,
        )

    def set_user_security_roles(self, username: str, role_ids, **values) -> bool:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再分配安全角色")
        return self.identity_store.set_user_security_roles(username, role_ids, **values)

    def get_user_permission_codes(self, username: str) -> set[str]:
        if self.storage_mode != "database":
            return set()
        return self.identity_store.get_user_permission_codes(username)

    def list_usernames_with_permission(
        self,
        permission_code: str,
        *,
        include_system_admin: bool = False,
    ) -> list[str]:
        """列出拥有稳定权限的在职用户，供通知订阅等非页面场景使用。

        系统管理员会自动拥有全部已注册权限，但这不代表管理员希望订阅全部业务通知，
        因此默认排除 ``admin``；确有需要时应给实际业务账号或岗位分配通知接收权限。
        """
        if self.storage_mode != "database":
            return []
        usernames: list[str] = []
        for username, user in self.load_users().items():
            normalized_username = str(username).strip()
            if user.get("status", "active") != "active":
                continue
            if not include_system_admin and normalized_username.casefold() == "admin":
                continue
            if self.has_permission(normalized_username, permission_code):
                usernames.append(normalized_username)
        return usernames

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
        """为一名企业微信成员解析已导入的部门和岗位。"""
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
            and (
                not item.get("org_unit_ids")
                or org_unit is None
                or org_unit.get("org_unit_id") in item.get("org_unit_ids", [])
            )
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
        """补齐缺失的组织任职，但绝不替换已经存在的分配。"""
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

    def list_positions(self, org_unit_id: str | None = None) -> list[dict[str, Any]]:
        return (
            self.identity_store.list_positions(org_unit_id=org_unit_id)
            if self.storage_mode == "database"
            else []
        )

    def save_position(self, **values) -> str:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再维护岗位")
        return self.identity_store.save_position(**values)

    def delete_position(self, position_id: str, **values) -> bool:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再删除岗位")
        return self.identity_store.delete_position(position_id, **values)

    def get_primary_membership(self, username: str) -> dict[str, Any]:
        if self.storage_mode != "database":
            return {}
        return self.identity_store.get_primary_membership(username)

    def set_primary_membership(self, username: str, **values) -> bool:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再分配组织和岗位")
        return self.identity_store.set_primary_membership(username, **values)

    def list_approval_workflows(self, **values) -> list[dict[str, Any]]:
        if self.storage_mode != "database":
            return []
        return self.identity_store.list_approval_workflows(**values)

    def export_identity_configuration(self) -> dict[str, Any]:
        """导出不含密码和业务数据的身份配置包。"""
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再导出身份配置")
        from .identity_config_transfer import IdentityConfigurationTransfer

        return IdentityConfigurationTransfer(self.identity_store).export_package()

    def preview_identity_configuration(self, package: dict[str, Any], **values):
        """在不写入数据库的前提下预检身份配置包。"""
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移服务器用户，再预检身份配置")
        from .identity_config_transfer import IdentityConfigurationTransfer

        return IdentityConfigurationTransfer(self.identity_store).preview_package(package, **values)

    def import_identity_configuration(self, package: dict[str, Any], **values):
        """自动备份后，以事务方式合并身份配置包。"""
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移服务器用户，再导入身份配置")
        from .identity_config_transfer import IdentityConfigurationTransfer

        return IdentityConfigurationTransfer(self.identity_store).import_package(package, **values)

    def save_approval_workflow_draft(self, **values) -> tuple[str, str]:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再维护审批流程")
        return self.identity_store.save_approval_workflow_draft(**values)

    def publish_approval_workflow(self, workflow_id: str, **values) -> bool:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再发布审批流程")
        return self.identity_store.publish_approval_workflow(workflow_id, **values)

    def set_approval_workflow_status(self, workflow_id: str, status: str, **values) -> bool:
        if self.storage_mode != "database":
            raise RuntimeError("请先迁移用户，再修改审批流程")
        return self.identity_store.set_approval_workflow_status(workflow_id, status, **values)

    def replace_work_assignments(self, **values) -> list[str]:
        if self.storage_mode != "database":
            return []
        return self.identity_store.replace_work_assignments(**values)

    def list_pending_assignment_usernames(self, **values) -> list[str]:
        if self.storage_mode != "database":
            return []
        return self.identity_store.list_pending_assignment_usernames(**values)

    def list_pending_work_assignment_refs(self, **values) -> list[dict[str, Any]]:
        if self.storage_mode != "database":
            return []
        return self.identity_store.list_pending_work_assignment_refs(**values)

    def supersede_pending_work_assignments(self, **values) -> int:
        if self.storage_mode != "database":
            return 0
        return self.identity_store.supersede_pending_work_assignments(**values)

    def complete_work_assignment(self, **values) -> bool:
        if self.storage_mode != "database":
            return False
        return self.identity_store.complete_work_assignment(**values)
