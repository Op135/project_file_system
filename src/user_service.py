"""数据库身份、组织、权限与审批服务门面。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .identity_store import IdentityStore
from .identity_matching import build_wecom_user_match_plan, suggest_contact_for_user
from .permission_catalog import (
    DEPRECATED_PERMISSION_REPLACEMENTS,
    build_default_grants,
    permission_catalog_rows,
)

class UserService:
    """只从 SQLite 身份库提供用户、组织、权限和流程数据。"""

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        config_root: Path | str | None = None,
        password_iterations: int | None = None,
    ):
        base_dir = Path(config_root) if config_root else Path(__file__).parent.parent
        self.base_dir = base_dir
        resolved_db_path = Path(db_path) if db_path else base_dir / "db" / "nicegui_storage.db"
        store_kwargs = {}
        if password_iterations is not None:
            store_kwargs["password_iterations"] = password_iterations
        self.identity_store = IdentityStore(resolved_db_path, **store_kwargs)
        if self.identity_store.has_database_users():
            self.sync_permission_catalog()

    def load_users(self) -> Dict[str, dict]:
        return self.identity_store.list_users()

    def require_database_users(self) -> None:
        """在应用启动时拒绝空身份库，防止误连新建或错误的数据库文件。"""
        if not self.identity_store.has_database_users():
            raise RuntimeError(
                "身份数据库中没有用户。系统已停用旧版 Excel 身份兼容模式；"
                "请恢复已完成身份迁移的 db/nicegui_storage.db。"
            )

    def get_user(self, username: str) -> dict:
        return self.identity_store.get_user(username)

    def authenticate(self, username: str, password: str) -> bool:
        return self.identity_store.authenticate(username, password)

    def needs_password_setup(self, username: str) -> bool:
        return self.identity_store.needs_password_setup(username)

    def update_password(self, username: str, new_password: str) -> bool:
        if not isinstance(new_password, str):
            raise TypeError("密码必须是字符串")
        normalized = new_password.strip()
        if len(normalized) < 6:
            raise ValueError("密码至少需要6位")
        return self.identity_store.update_password(username, normalized)

    def modify_user(
        self, action: str, username: str, password: str = "", role: str = "",
        *, actor_username: str | None = None,
    ) -> bool:
        """在身份数据库中新增、编辑、删除用户或修改用户状态。"""
        if action == "add":
            result = self.identity_store.create_user(username, password or "", role or "普通用户")
            self.sync_permission_catalog()
            return result
        if action in {"update", "edit"}:
            result = self.identity_store.update_user(username, password or None, role)
            self.sync_permission_catalog()
            return result
        if action == "delete":
            return self.identity_store.delete_user(username, actor_username=actor_username)
        if action == "deactivate":
            return self.identity_store.set_user_status(username, "disabled")
        if action == "depart":
            return self.identity_store.set_user_status(username, "departed")
        if action in {"activate", "restore"}:
            return self.identity_store.set_user_status(username, "active")
        raise ValueError(f"未知的操作指令: {action}")

    def sync_permission_catalog(
        self,
        *,
        strict_overview: bool = False,
        overview_config: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        """以幂等方式注册稳定权限及必要的系统默认授权。"""
        result = self.identity_store.seed_permission_catalog(
            permission_catalog_rows(
                overview_config=overview_config,
                strict_overview=strict_overview,
            ),
            build_default_grants(),
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
        del legacy_role, legacy_allowed_roles
        return self.identity_store.has_permission(username, permission_code)

    def list_permissions(self) -> list[dict[str, Any]]:
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
        return self.identity_store.get_position_permission_codes(position_id)

    def get_overview_role_position_mapping(self) -> dict[str, list[str]]:
        return self.identity_store.get_overview_role_position_mapping()

    def get_overview_role_position_managed_permissions(self) -> dict[str, set[str]]:
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
        return self.identity_store.apply_overview_role_position_mapping(
            role_position_mapping,
            position_permission_codes,
            affected_position_ids=affected_position_ids,
            actor_username=actor_username,
        )

    def set_position_permissions(self, position_id: str, permission_codes, **values) -> bool:
        return self.identity_store.set_position_permissions(
            position_id,
            permission_codes,
            **values,
        )

    def list_security_roles(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        return self.identity_store.list_security_roles(include_disabled=include_disabled)

    def create_security_role(self, **values) -> str:
        return self.identity_store.create_security_role(**values)

    def update_security_role(self, role_id: str, **values) -> bool:
        return self.identity_store.update_security_role(role_id, **values)

    def get_user_security_roles(
        self,
        username: str,
        *,
        include_compatibility: bool = True,
    ) -> list[dict[str, Any]]:
        return self.identity_store.get_user_security_roles(
            username,
            include_compatibility=include_compatibility,
        )

    def set_user_security_roles(self, username: str, role_ids, **values) -> bool:
        return self.identity_store.set_user_security_roles(username, role_ids, **values)

    def get_user_permission_codes(self, username: str) -> set[str]:
        return self.identity_store.get_user_permission_codes(username)

    def list_active_user_permission_codes(self) -> dict[str, set[str]]:
        """批量返回在职用户权限，避免列表逐用户重复打开数据库连接。"""
        return self.identity_store.list_active_user_permission_codes()

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
        return self.identity_store.get_external_identity(username, "wecom")

    def list_wecom_bindings(self) -> dict[str, dict[str, Any]]:
        return self.identity_store.list_external_identities("wecom")

    def bind_wecom_user(self, username: str, contact: dict[str, Any]) -> bool:
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
        return self.identity_store.unbind_external_identity(username, "wecom")

    def build_wecom_match_plan(self, contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return build_wecom_user_match_plan(
            {name: user for name, user in self.load_users().items() if name.casefold() != "admin"},
            contacts,
            {name: binding for name, binding in self.list_wecom_bindings().items() if name.casefold() != "admin"},
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
            {name: binding for name, binding in self.list_wecom_bindings().items() if name.casefold() != "admin"},
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
        return self.identity_store.list_org_units()

    def save_org_unit(self, **values) -> str:
        return self.identity_store.save_org_unit(**values)

    def import_wecom_departments(self, departments) -> tuple[int, int]:
        return self.identity_store.import_wecom_departments(departments)

    def import_wecom_positions(self, contacts) -> tuple[int, int]:
        return self.identity_store.import_wecom_positions(contacts)

    def list_positions(self, org_unit_id: str | None = None) -> list[dict[str, Any]]:
        return self.identity_store.list_positions(org_unit_id=org_unit_id)

    def save_position(self, **values) -> str:
        return self.identity_store.save_position(**values)

    def delete_position(self, position_id: str, **values) -> bool:
        return self.identity_store.delete_position(position_id, **values)

    def get_primary_membership(self, username: str) -> dict[str, Any]:
        return self.identity_store.get_primary_membership(username)

    def get_overview_owner_position_policy(self) -> dict[str, dict[str, Any]]:
        return self.identity_store.get_overview_owner_position_policy()

    def set_overview_owner_position_policy(
        self, dimension: str, position_ids: list[str], *, actor_username: str | None = None,
    ) -> None:
        self.identity_store.set_overview_owner_position_policy(
            dimension, position_ids, actor_username=actor_username,
        )

    def is_overview_owner_eligible(self, username: str, position_ids: set[str]) -> bool:
        return self.identity_store.is_overview_owner_eligible(username, position_ids)

    def list_primary_memberships(self) -> dict[str, dict[str, Any]]:
        return self.identity_store.list_primary_memberships()

    def set_primary_membership(self, username: str, **values) -> bool:
        return self.identity_store.set_primary_membership(username, **values)

    def list_approval_workflows(self, **values) -> list[dict[str, Any]]:
        return self.identity_store.list_approval_workflows(**values)

    def export_identity_configuration(self) -> dict[str, Any]:
        """导出不含密码和业务数据的身份配置包。"""
        from .identity_config_transfer import IdentityConfigurationTransfer

        return IdentityConfigurationTransfer(self.identity_store).export_package()

    def preview_identity_configuration(self, package: dict[str, Any], **values):
        """在不写入数据库的前提下预检身份配置包。"""
        from .identity_config_transfer import IdentityConfigurationTransfer

        return IdentityConfigurationTransfer(self.identity_store).preview_package(package, **values)

    def import_identity_configuration(self, package: dict[str, Any], **values):
        """自动备份后，以事务方式合并身份配置包。"""
        from .identity_config_transfer import IdentityConfigurationTransfer

        return IdentityConfigurationTransfer(self.identity_store).import_package(package, **values)

    def save_approval_workflow_draft(self, **values) -> tuple[str, str]:
        return self.identity_store.save_approval_workflow_draft(**values)

    def publish_approval_workflow(self, workflow_id: str, **values) -> bool:
        return self.identity_store.publish_approval_workflow(workflow_id, **values)

    def set_approval_workflow_status(self, workflow_id: str, status: str, **values) -> bool:
        return self.identity_store.set_approval_workflow_status(workflow_id, status, **values)

    def delete_approval_workflow(self, workflow_id: str, **values) -> bool:
        return self.identity_store.delete_approval_workflow(workflow_id, **values)

    def replace_work_assignments(self, **values) -> list[str]:
        return self.identity_store.replace_work_assignments(**values)

    def list_pending_assignment_usernames(self, **values) -> list[str]:
        return self.identity_store.list_pending_assignment_usernames(**values)

    def list_pending_work_assignment_refs(self, **values) -> list[dict[str, Any]]:
        return self.identity_store.list_pending_work_assignment_refs(**values)

    def supersede_pending_work_assignments(self, **values) -> int:
        return self.identity_store.supersede_pending_work_assignments(**values)

    def complete_work_assignment(self, **values) -> bool:
        return self.identity_store.complete_work_assignment(**values)
