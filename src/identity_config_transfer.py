"""身份、组织、权限和审批流程的跨环境配置迁移。

配置包只使用稳定编码和用户名表达关联关系，绝不包含密码哈希、业务记录、待办或
审计日志。导入前会先预检，正式导入在单个事务内完成，并自动生成 SQLite 备份。
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .identity_codes import normalize_stable_code, validate_stable_code
from .identity_store import IdentityStore


PACKAGE_KIND = "project_file_system.identity_configuration"
PACKAGE_VERSION = 1


@dataclass
class ConfigurationTransferPreview:
    """配置包在目标数据库上的预检结果。"""

    summary: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def can_import(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["can_import"] = self.can_import
        return result


@dataclass
class ConfigurationImportResult:
    """配置导入执行结果。"""

    summary: dict[str, int]
    warnings: list[str]
    backup_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_checksum(configuration: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(configuration).encode("utf-8")).hexdigest()


def _decode_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _replace_reference_fields(
    value: Any,
    *,
    field_maps: dict[str, tuple[str, dict[str, str]]],
    missing: list[str] | None = None,
    context: str = "",
) -> Any:
    """递归替换流程 JSON 中已知的 ID 列表字段。"""
    if isinstance(value, list):
        return [
            _replace_reference_fields(
                item,
                field_maps=field_maps,
                missing=missing,
                context=context,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return copy.deepcopy(value)

    result: dict[str, Any] = {}
    for key, item in value.items():
        mapping_rule = field_maps.get(str(key))
        if mapping_rule and isinstance(item, list):
            target_key, reference_map = mapping_rule
            resolved: list[str] = []
            for raw_reference in item:
                reference = str(raw_reference or "").strip()
                if not reference:
                    continue
                mapped = reference_map.get(reference.casefold())
                if mapped is None:
                    if missing is not None:
                        missing.append(f"{context}{key} 引用了不存在的值：{reference}")
                    continue
                resolved.append(mapped)
            result[target_key] = list(dict.fromkeys(resolved))
            continue
        result[str(key)] = _replace_reference_fields(
            item,
            field_maps=field_maps,
            missing=missing,
            context=context,
        )
    return result


def _rows_by_code(connection: sqlite3.Connection, table: str, id_column: str) -> dict[str, str]:
    rows = connection.execute(f"SELECT {id_column}, code FROM {table}").fetchall()
    return {str(row["code"]).casefold(): str(row[id_column]) for row in rows}


def _users_by_name(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT user_id, username FROM iam_users").fetchall()
    return {str(row["username"]).casefold(): str(row["user_id"]) for row in rows}


class IdentityConfigurationTransfer:
    """在两个部署环境之间安全搬运身份配置。"""

    def __init__(self, store: IdentityStore):
        self.store = store

    def export_package(self) -> dict[str, Any]:
        """导出不含密码和业务数据的配置包。"""
        with self.store._lock, self.store._connect() as connection:
            organization_units = self._export_org_units(connection)
            positions = self._export_positions(connection)
            permission_groups = self._export_permission_groups(connection)
            user_links = self._export_user_links(connection)
            workflows = self._export_workflows(connection)
            helper_settings = self._export_helper_settings(connection)

        configuration = {
            "organization_units": organization_units,
            "positions": positions,
            "permission_groups": permission_groups,
            "user_links": user_links,
            "approval_workflows": workflows,
            "helper_settings": helper_settings,
        }
        return {
            "package_kind": PACKAGE_KIND,
            "package_version": PACKAGE_VERSION,
            "exported_at": _now_text(),
            "checksum_sha256": _payload_checksum(configuration),
            "configuration": configuration,
        }

    @staticmethod
    def serialize_package(package: dict[str, Any]) -> bytes:
        """生成便于人工审阅的 UTF-8 JSON 文件。"""
        return json.dumps(package, ensure_ascii=False, indent=2).encode("utf-8")

    def preview_package(
        self,
        package: dict[str, Any],
        *,
        include_user_links: bool = True,
        include_wecom_bindings: bool = True,
    ) -> ConfigurationTransferPreview:
        """验证文件格式、稳定引用和目标环境冲突，不写入数据库。"""
        preview = ConfigurationTransferPreview()
        configuration = self._validate_envelope(package, preview)
        if configuration is None:
            return preview
        with self.store._lock, self.store._connect() as connection:
            self._validate_configuration(
                connection,
                configuration,
                preview,
                include_user_links=include_user_links,
                include_wecom_bindings=include_wecom_bindings,
            )
        return preview

    def import_package(
        self,
        package: dict[str, Any],
        *,
        actor_username: str | None = None,
        include_user_links: bool = True,
        include_wecom_bindings: bool = True,
        backup_dir: Path | str | None = None,
    ) -> ConfigurationImportResult:
        """备份目标数据库后，以单个事务合并配置包。"""
        preview = self.preview_package(
            package,
            include_user_links=include_user_links,
            include_wecom_bindings=include_wecom_bindings,
        )
        if not preview.can_import:
            raise ValueError("配置包预检失败：" + "；".join(preview.errors))
        configuration = package["configuration"]
        target_dir = Path(backup_dir) if backup_dir else self.store.db_path.parent.parent / "backups" / "config_import"
        target_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = target_dir / f"identity_config_before_import_{timestamp}.db"

        with self.store._lock:
            source = sqlite3.connect(self.store.db_path, timeout=30)
            try:
                destination = sqlite3.connect(backup_path)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()

            with self.store._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                summary = self._apply_configuration(
                    connection,
                    configuration,
                    actor_username=actor_username,
                    include_user_links=include_user_links,
                    include_wecom_bindings=include_wecom_bindings,
                )

        return ConfigurationImportResult(
            summary=summary,
            warnings=preview.warnings,
            backup_path=str(backup_path),
        )

    @staticmethod
    def _validate_envelope(
        package: dict[str, Any], preview: ConfigurationTransferPreview
    ) -> dict[str, Any] | None:
        if not isinstance(package, dict):
            preview.errors.append("配置包根节点必须是对象")
            return None
        if package.get("package_kind") != PACKAGE_KIND:
            preview.errors.append("这不是本系统生成的身份配置包")
        if package.get("package_version") != PACKAGE_VERSION:
            preview.errors.append(
                f"不支持的配置包版本：{package.get('package_version')}，当前仅支持 {PACKAGE_VERSION}"
            )
        configuration = package.get("configuration")
        if not isinstance(configuration, dict):
            preview.errors.append("配置包缺少 configuration 对象")
            return None
        expected_checksum = str(package.get("checksum_sha256", "")).strip().lower()
        actual_checksum = _payload_checksum(configuration)
        if not expected_checksum or expected_checksum != actual_checksum:
            preview.errors.append("配置包校验值不一致，文件可能被修改或传输不完整")
        return configuration

    def _validate_configuration(
        self,
        connection: sqlite3.Connection,
        configuration: dict[str, Any],
        preview: ConfigurationTransferPreview,
        *,
        include_user_links: bool,
        include_wecom_bindings: bool,
    ) -> None:
        sections = {
            "organization_units": "部门",
            "positions": "岗位",
            "permission_groups": "附加权限组",
            "user_links": "用户关联",
            "approval_workflows": "审批流程",
        }
        for key, label in sections.items():
            if not isinstance(configuration.get(key, []), list):
                preview.errors.append(f"{label}配置必须是列表")
        if preview.errors:
            return

        departments = configuration.get("organization_units", [])
        positions = configuration.get("positions", [])
        roles = configuration.get("permission_groups", [])
        users = configuration.get("user_links", [])
        workflows = configuration.get("approval_workflows", [])

        department_codes = self._validate_codes(departments, "部门", preview)
        position_codes = self._validate_codes(positions, "岗位", preview)
        role_codes = self._validate_codes(roles, "附加权限组", preview)
        self._validate_codes(workflows, "审批流程", preview)
        if preview.errors:
            return

        target_departments = _rows_by_code(connection, "org_units", "org_unit_id")
        target_positions = _rows_by_code(connection, "iam_positions", "position_id")
        target_roles = _rows_by_code(connection, "iam_security_roles", "role_id")
        target_workflows = _rows_by_code(connection, "approval_workflows", "workflow_id")
        target_permissions = {
            str(row["code"]).casefold()
            for row in connection.execute("SELECT code FROM iam_permissions").fetchall()
        }
        target_users = _users_by_name(connection)

        preview.summary.update(
            {
                "departments_total": len(departments),
                "departments_new": len(department_codes - set(target_departments)),
                "positions_total": len(positions),
                "positions_new": len(position_codes - set(target_positions)),
                "permission_groups_total": len(roles),
                "permission_groups_new": len(role_codes - set(target_roles)),
                "workflows_total": len(workflows),
                "workflows_new": len(
                    {
                        normalize_stable_code(item.get("code"))
                        for item in workflows
                        if isinstance(item, dict)
                    }
                    - set(target_workflows)
                ),
                "users_total": len(users) if include_user_links else 0,
                "users_matched": 0,
                "wecom_bindings_total": 0,
            }
        )

        package_wecom_departments: dict[str, str] = {}
        target_wecom_departments = {
            str(row["wecom_department_id"]): str(row["code"]).casefold()
            for row in connection.execute(
                "SELECT code, wecom_department_id FROM org_units "
                "WHERE wecom_department_id IS NOT NULL AND wecom_department_id<>''"
            ).fetchall()
        }
        for item in departments:
            code = normalize_stable_code(item.get("code"))
            if not str(item.get("name", "")).strip():
                preview.errors.append(f"部门 {code} 的名称不能为空")
            if str(item.get("status", "active")) not in {"active", "disabled"}:
                preview.errors.append(f"部门 {code} 的状态无效")
            parent_code = normalize_stable_code(item.get("parent_code"))
            if parent_code and parent_code not in department_codes:
                preview.errors.append(f"部门 {code} 的上级部门不存在于配置包：{parent_code}")
            if parent_code == code:
                preview.errors.append(f"部门 {code} 不能把自己设为上级部门")
            wecom_department_id = str(item.get("wecom_department_id") or "").strip()
            if wecom_department_id:
                previous_code = package_wecom_departments.get(wecom_department_id)
                if previous_code and previous_code != code:
                    preview.errors.append(
                        f"企业微信部门 ID {wecom_department_id} 同时对应多个部门编码"
                    )
                target_code = target_wecom_departments.get(wecom_department_id)
                if target_code and target_code != code:
                    preview.errors.append(
                        f"企业微信部门 ID {wecom_department_id} 已被服务器部门 {target_code} 使用"
                    )
                package_wecom_departments[wecom_department_id] = code
        self._validate_department_cycles(departments, preview)

        referenced_permissions: set[str] = set()
        for item in positions:
            code = normalize_stable_code(item.get("code"))
            if not str(item.get("name", "")).strip():
                preview.errors.append(f"岗位 {code} 的名称不能为空")
            if str(item.get("status", "active")) not in {"active", "disabled"}:
                preview.errors.append(f"岗位 {code} 的状态无效")
            for department_code in item.get("department_codes", []):
                normalized = normalize_stable_code(department_code)
                if normalized not in department_codes:
                    preview.errors.append(f"岗位 {code} 引用了不存在的部门：{normalized}")
            referenced_permissions.update(
                str(value).strip().casefold() for value in item.get("permission_codes", []) if str(value).strip()
            )
            target_position_id = target_positions.get(code)
            if target_position_id:
                source_scope_codes = {
                    normalize_stable_code(value) for value in item.get("department_codes", [])
                }
                occupied_scope_rows = connection.execute(
                    "SELECT DISTINCT unit.code FROM org_memberships membership "
                    "JOIN org_units unit ON unit.org_unit_id=membership.org_unit_id "
                    "WHERE membership.position_id=? AND membership.status='active' "
                    "AND membership.is_primary=1",
                    (target_position_id,),
                ).fetchall()
                retained_codes = sorted(
                    str(row["code"])
                    for row in occupied_scope_rows
                    if normalize_stable_code(row["code"]) not in source_scope_codes
                )
                if retained_codes:
                    preview.warnings.append(
                        f"岗位 {code} 在服务器仍有员工任职，以下额外适用部门将暂时保留："
                        + "、".join(retained_codes)
                    )
        package_role_names: dict[str, str] = {}
        target_role_names = {
            str(row["name"]): str(row["code"]).casefold()
            for row in connection.execute("SELECT code, name FROM iam_security_roles").fetchall()
        }
        for item in roles:
            code = normalize_stable_code(item.get("code"))
            name = str(item.get("name", "")).strip()
            if code.startswith("legacy."):
                preview.errors.append(f"配置包不能包含旧角色兼容组：{code}")
            if not name:
                preview.errors.append(f"附加权限组 {code} 的名称不能为空")
            elif name in package_role_names and package_role_names[name] != code:
                preview.errors.append(f"附加权限组名称重复：{name}")
            elif name in target_role_names and target_role_names[name] != code:
                preview.errors.append(
                    f"附加权限组名称 {name} 已被服务器编码 {target_role_names[name]} 使用"
                )
            package_role_names[name] = code
            if str(item.get("status", "active")) not in {"active", "disabled"}:
                preview.errors.append(f"附加权限组 {code} 的状态无效")
            referenced_permissions.update(
                str(value).strip().casefold() for value in item.get("permission_codes", []) if str(value).strip()
            )
        package_usernames = {
            str(user.get("username", "")).casefold()
            for user in users
            if isinstance(user, dict) and str(user.get("username", "")).strip()
        }
        for item in workflows:
            workflow_code = normalize_stable_code(item.get("code"))
            if not str(item.get("module", "")).strip() or not str(item.get("event", "")).strip():
                preview.errors.append(f"审批流程 {workflow_code} 缺少模块或业务事件")
            if not str(item.get("name", "")).strip():
                preview.errors.append(f"审批流程 {workflow_code} 的名称不能为空")
            if str(item.get("status", "draft")) not in {"draft", "active", "disabled"}:
                preview.errors.append(f"审批流程 {workflow_code} 的状态无效")
            for version_key in ("published_version", "draft_version"):
                version = item.get(version_key)
                if isinstance(version, dict):
                    referenced_permissions.update(self._workflow_permission_codes(version))
                    self._validate_workflow_references(
                        item,
                        version,
                        department_codes,
                        position_codes,
                        package_usernames,
                        set(target_users),
                        preview,
                    )
        missing_permissions = sorted(referenced_permissions - target_permissions)
        if missing_permissions:
            preview.errors.append(
                "目标服务器缺少以下权限编码，请先部署相同版本代码：" + "、".join(missing_permissions)
            )

        if include_user_links:
            seen_usernames: set[str] = set()
            package_external_owners: dict[tuple[str, str], str] = {}
            for item in users:
                username = str(item.get("username", "")).strip()
                folded = username.casefold()
                if not username or folded in seen_usernames:
                    preview.errors.append(f"用户关联包含空用户名或重复用户名：{username or '（空）'}")
                    continue
                seen_usernames.add(folded)
                if folded not in target_users:
                    preview.warnings.append(f"服务器不存在用户 {username}，其任职、授权和微信绑定将跳过")
                    continue
                preview.summary["users_matched"] += 1
                membership = item.get("membership")
                if membership is not None:
                    if not isinstance(membership, dict):
                        preview.errors.append(f"用户 {username} 的任职配置格式无效")
                    else:
                        org_code = normalize_stable_code(membership.get("org_unit_code"))
                        position_code = normalize_stable_code(membership.get("position_code"))
                        if org_code not in department_codes:
                            preview.errors.append(f"用户 {username} 的部门不存在：{org_code}")
                        if position_code and position_code not in position_codes:
                            preview.errors.append(f"用户 {username} 的岗位不存在：{position_code}")
                        manager = str(membership.get("direct_manager_username", "")).strip()
                        if manager and manager.casefold() not in target_users:
                            preview.warnings.append(
                                f"用户 {username} 的直属上级 {manager} 在服务器不存在，将保留为空"
                            )
                for role_code in item.get("additional_role_codes", []):
                    normalized_role = normalize_stable_code(role_code)
                    if normalized_role not in role_codes:
                        preview.errors.append(f"用户 {username} 引用了不存在的附加权限组：{normalized_role}")
                bindings = item.get("wecom_bindings", [])
                if include_wecom_bindings:
                    preview.summary["wecom_bindings_total"] += len(bindings)
                    for binding in bindings:
                        self._validate_wecom_binding(connection, username, binding, target_users[folded], preview)
                        external_key = (
                            str(binding.get("provider", "wecom")).strip().lower() or "wecom",
                            str(binding.get("external_userid", "")).strip(),
                        )
                        previous_owner = package_external_owners.get(external_key)
                        if external_key[1] and previous_owner and previous_owner.casefold() != folded:
                            preview.errors.append(
                                f"配置包中的企业微信账号 {external_key[1]} 同时绑定了多个用户"
                            )
                        package_external_owners[external_key] = username

        helper = configuration.get("helper_settings", {})
        if helper and not isinstance(helper, dict):
            preview.errors.append("权限整理辅助配置必须是对象")

    @staticmethod
    def _validate_codes(
        items: list[dict[str, Any]], label: str, preview: ConfigurationTransferPreview
    ) -> set[str]:
        codes: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                preview.errors.append(f"{label}配置项必须是对象")
                continue
            code = normalize_stable_code(item.get("code"))
            error = validate_stable_code(code)
            # 企业微信部门是历史自动编码，冒号格式由旧版本生成，需要兼容迁移。
            if label == "部门" and code.startswith("wecom:") and code[6:].isdigit():
                error = ""
            if error:
                preview.errors.append(f"{label}编码 {code or '（空）'}：{error}")
            elif code in codes:
                preview.errors.append(f"{label}编码重复：{code}")
            codes.add(code)
        return codes

    @staticmethod
    def _validate_department_cycles(
        departments: list[dict[str, Any]], preview: ConfigurationTransferPreview
    ) -> None:
        parent_by_code = {
            normalize_stable_code(item.get("code")): normalize_stable_code(item.get("parent_code"))
            for item in departments
            if isinstance(item, dict)
        }
        for code in parent_by_code:
            visited: set[str] = set()
            current = code
            while current:
                if current in visited:
                    preview.errors.append(f"部门层级存在循环引用：{code}")
                    break
                visited.add(current)
                current = parent_by_code.get(current, "")

    @staticmethod
    def _workflow_permission_codes(version: dict[str, Any]) -> set[str]:
        codes = {str(version.get("required_permission_code", "")).strip().casefold()}
        approver = version.get("approver", {})
        stack = [approver]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                code = str(current.get("required_permission_code", "")).strip().casefold()
                permission_code = str(current.get("permission_code", "")).strip().casefold()
                if code:
                    codes.add(code)
                if permission_code:
                    codes.add(permission_code)
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
        codes.discard("")
        return codes

    @staticmethod
    def _validate_workflow_references(
        workflow: dict[str, Any],
        version: dict[str, Any],
        department_codes: set[str],
        position_codes: set[str],
        package_usernames: set[str],
        target_usernames: set[str],
        preview: ConfigurationTransferPreview,
    ) -> None:
        code = normalize_stable_code(workflow.get("code"))
        stack = [version.get("condition", {}), version.get("approver", {})]
        while stack:
            current = stack.pop()
            if isinstance(current, list):
                stack.extend(current)
                continue
            if not isinstance(current, dict):
                continue
            for key, values in current.items():
                if key == "requester_org_unit_codes" or key == "org_unit_codes":
                    for value in values if isinstance(values, list) else []:
                        if normalize_stable_code(value) not in department_codes:
                            preview.errors.append(f"审批流程 {code} 引用了不存在的部门：{value}")
                elif key == "requester_position_codes" or key == "position_codes":
                    for value in values if isinstance(values, list) else []:
                        if normalize_stable_code(value) not in position_codes:
                            preview.errors.append(f"审批流程 {code} 引用了不存在的岗位：{value}")
                elif key == "usernames":
                    for value in values if isinstance(values, list) else []:
                        username = str(value).casefold()
                        if username not in package_usernames:
                            preview.errors.append(f"审批流程 {code} 指定的用户不在配置包中：{value}")
                        elif username not in target_usernames:
                            preview.errors.append(f"审批流程 {code} 指定的用户在服务器不存在：{value}")
                stack.append(values)

    @staticmethod
    def _validate_wecom_binding(
        connection: sqlite3.Connection,
        username: str,
        binding: dict[str, Any],
        target_user_id: str,
        preview: ConfigurationTransferPreview,
    ) -> None:
        external_userid = str(binding.get("external_userid", "")).strip()
        provider = str(binding.get("provider", "wecom")).strip().lower() or "wecom"
        if not external_userid:
            preview.errors.append(f"用户 {username} 的企业微信绑定缺少 external_userid")
            return
        conflict = connection.execute(
            "SELECT user_id FROM iam_external_identities WHERE provider=? AND external_userid=?",
            (provider, external_userid),
        ).fetchone()
        if conflict and str(conflict["user_id"]) != target_user_id:
            preview.errors.append(
                f"企业微信账号 {external_userid} 已绑定服务器上的其他用户，请先解除冲突绑定"
            )

    @staticmethod
    def _export_org_units(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT child.*, parent.code AS parent_code FROM org_units child "
            "LEFT JOIN org_units parent ON parent.org_unit_id=child.parent_org_unit_id "
            "ORDER BY child.sort_order, child.code"
        ).fetchall()
        return [
            {
                "code": row["code"],
                "name": row["name"],
                "parent_code": row["parent_code"],
                "wecom_department_id": row["wecom_department_id"],
                "source": row["source"],
                "manual_override": int(row["manual_override"]),
                "external_name_snapshot": row["external_name_snapshot"],
                "external_parent_snapshot": row["external_parent_snapshot"],
                "sort_order": int(row["sort_order"]),
                "status": row["status"],
            }
            for row in rows
        ]

    @staticmethod
    def _export_positions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM iam_positions ORDER BY code").fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            scopes = connection.execute(
                "SELECT unit.code FROM org_position_scopes scope "
                "JOIN org_units unit ON unit.org_unit_id=scope.org_unit_id "
                "WHERE scope.position_id=? ORDER BY unit.code",
                (row["position_id"],),
            ).fetchall()
            permissions = connection.execute(
                "SELECT permission.code FROM iam_position_permissions pp "
                "JOIN iam_permissions permission ON permission.permission_id=pp.permission_id "
                "WHERE pp.position_id=? ORDER BY permission.code",
                (row["position_id"],),
            ).fetchall()
            result.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "source": row["source"],
                    "manual_override": int(row["manual_override"]),
                    "external_name_snapshot": row["external_name_snapshot"],
                    "level": int(row["level"]),
                    "status": row["status"],
                    "department_codes": [item["code"] for item in scopes],
                    "permission_codes": [item["code"] for item in permissions],
                }
            )
        return result

    @staticmethod
    def _export_permission_groups(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM iam_security_roles WHERE code NOT LIKE 'legacy.%' ORDER BY code"
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            permissions = connection.execute(
                "SELECT permission.code FROM iam_role_permissions rp "
                "JOIN iam_permissions permission ON permission.permission_id=rp.permission_id "
                "WHERE rp.role_id=? ORDER BY permission.code",
                (row["role_id"],),
            ).fetchall()
            result.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "status": row["status"],
                    "permission_codes": [item["code"] for item in permissions],
                }
            )
        return result

    @staticmethod
    def _export_user_links(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        users = connection.execute(
            "SELECT user_id, username FROM iam_users ORDER BY username COLLATE NOCASE"
        ).fetchall()
        id_to_username = {str(row["user_id"]): str(row["username"]) for row in users}
        result: list[dict[str, Any]] = []
        for user in users:
            membership = connection.execute(
                "SELECT m.*, unit.code AS org_unit_code, position.code AS position_code "
                "FROM org_memberships m JOIN org_units unit ON unit.org_unit_id=m.org_unit_id "
                "LEFT JOIN iam_positions position ON position.position_id=m.position_id "
                "WHERE m.user_id=? AND m.status='active' AND m.is_primary=1 "
                "ORDER BY m.updated_at DESC LIMIT 1",
                (user["user_id"],),
            ).fetchone()
            role_rows = connection.execute(
                "SELECT role.code FROM iam_user_roles ur "
                "JOIN iam_security_roles role ON role.role_id=ur.role_id "
                "WHERE ur.user_id=? AND role.code NOT LIKE 'legacy.%' ORDER BY role.code",
                (user["user_id"],),
            ).fetchall()
            binding_rows = connection.execute(
                "SELECT provider, external_userid, external_display_name, binding_source, metadata_json "
                "FROM iam_external_identities WHERE user_id=? AND provider='wecom' ORDER BY provider",
                (user["user_id"],),
            ).fetchall()
            membership_data = None
            if membership:
                membership_data = {
                    "org_unit_code": membership["org_unit_code"],
                    "position_code": membership["position_code"],
                    "direct_manager_username": id_to_username.get(
                        str(membership["direct_manager_user_id"]), ""
                    ),
                    "started_at": membership["started_at"],
                }
            result.append(
                {
                    "username": user["username"],
                    "membership": membership_data,
                    "additional_role_codes": [row["code"] for row in role_rows],
                    "wecom_bindings": [
                        {
                            "provider": row["provider"],
                            "external_userid": row["external_userid"],
                            "external_display_name": row["external_display_name"],
                            "binding_source": row["binding_source"],
                            "metadata": _decode_json(row["metadata_json"]),
                        }
                        for row in binding_rows
                    ],
                }
            )
        return result

    @staticmethod
    def _export_workflows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        org_id_to_code = {
            str(row["org_unit_id"]).casefold(): str(row["code"])
            for row in connection.execute("SELECT org_unit_id, code FROM org_units").fetchall()
        }
        position_id_to_code = {
            str(row["position_id"]).casefold(): str(row["code"])
            for row in connection.execute("SELECT position_id, code FROM iam_positions").fetchall()
        }
        user_id_to_name = {
            str(row["user_id"]).casefold(): str(row["username"])
            for row in connection.execute("SELECT user_id, username FROM iam_users").fetchall()
        }
        export_maps = {
            "requester_org_unit_ids": ("requester_org_unit_codes", org_id_to_code),
            "org_unit_ids": ("org_unit_codes", org_id_to_code),
            "requester_position_ids": ("requester_position_codes", position_id_to_code),
            "position_ids": ("position_codes", position_id_to_code),
            "user_ids": ("usernames", user_id_to_name),
        }
        workflows = connection.execute("SELECT * FROM approval_workflows ORDER BY code").fetchall()
        result: list[dict[str, Any]] = []
        for workflow in workflows:
            def export_version(row: sqlite3.Row | None) -> dict[str, Any] | None:
                if row is None:
                    return None
                return {
                    "priority": int(row["priority"]),
                    "condition": _replace_reference_fields(
                        _decode_json(row["condition_json"]), field_maps=export_maps
                    ),
                    "approver": _replace_reference_fields(
                        _decode_json(row["approver_json"]), field_maps=export_maps
                    ),
                    "required_permission_code": row["required_permission_code"],
                    "approval_mode": row["approval_mode"],
                    "notification": _decode_json(row["notification_json"]),
                }

            published = None
            if workflow["active_version_id"]:
                published = connection.execute(
                    "SELECT * FROM approval_workflow_versions WHERE version_id=?",
                    (workflow["active_version_id"],),
                ).fetchone()
            draft = connection.execute(
                "SELECT * FROM approval_workflow_versions WHERE workflow_id=? AND state='draft' "
                "ORDER BY version_number DESC LIMIT 1",
                (workflow["workflow_id"],),
            ).fetchone()
            result.append(
                {
                    "code": workflow["code"],
                    "module": workflow["module"],
                    "event": workflow["event"],
                    "name": workflow["name"],
                    "status": workflow["status"],
                    "published_version": export_version(published),
                    "draft_version": export_version(draft),
                }
            )
        return result

    @staticmethod
    def _export_helper_settings(connection: sqlite3.Connection) -> dict[str, Any]:
        position_id_to_code = {
            str(row["position_id"]): str(row["code"])
            for row in connection.execute("SELECT position_id, code FROM iam_positions").fetchall()
        }
        result: dict[str, Any] = {}
        mapping_row = connection.execute(
            "SELECT value FROM iam_meta WHERE key='project_overview_role_position_mapping'"
        ).fetchone()
        if mapping_row:
            raw_mapping = _decode_json(mapping_row["value"])
            result["project_overview_role_position_mapping"] = {
                str(role): [position_id_to_code[value] for value in values if value in position_id_to_code]
                for role, values in raw_mapping.items()
                if isinstance(values, list)
            }
        managed_row = connection.execute(
            "SELECT value FROM iam_meta WHERE key='project_overview_role_position_managed_permissions'"
        ).fetchone()
        if managed_row:
            raw_managed = _decode_json(managed_row["value"])
            result["project_overview_role_position_managed_permissions"] = {
                position_id_to_code[position_id]: list(codes)
                for position_id, codes in raw_managed.items()
                if position_id in position_id_to_code and isinstance(codes, list)
            }
        return result

    def _apply_configuration(
        self,
        connection: sqlite3.Connection,
        configuration: dict[str, Any],
        *,
        actor_username: str | None,
        include_user_links: bool,
        include_wecom_bindings: bool,
    ) -> dict[str, int]:
        now = _now_text()
        summary = {
            "departments": 0,
            "positions": 0,
            "permission_groups": 0,
            "users": 0,
            "wecom_bindings": 0,
            "workflows": 0,
        }
        self._apply_departments(connection, configuration["organization_units"], now)
        summary["departments"] = len(configuration["organization_units"])
        self._apply_positions_base(connection, configuration["positions"], now)
        self._apply_permission_groups(connection, configuration["permission_groups"], now)
        summary["permission_groups"] = len(configuration["permission_groups"])
        if include_user_links:
            users_count, bindings_count = self._apply_user_links(
                connection,
                configuration["user_links"],
                now,
                include_wecom_bindings=include_wecom_bindings,
            )
            summary["users"] = users_count
            summary["wecom_bindings"] = bindings_count
        self._apply_position_scopes_and_permissions(connection, configuration["positions"], now)
        summary["positions"] = len(configuration["positions"])
        self._apply_helper_settings(connection, configuration.get("helper_settings", {}), now)
        self._apply_workflows(connection, configuration["approval_workflows"], now)
        summary["workflows"] = len(configuration["approval_workflows"])

        actor = connection.execute(
            "SELECT user_id FROM iam_users WHERE username=? COLLATE NOCASE",
            (str(actor_username or ""),),
        ).fetchone()
        connection.execute(
            "INSERT INTO iam_audit_logs(audit_id, actor_user_id, action, target_type, target_id, "
            "detail_json, created_at) VALUES(?, ?, 'identity_configuration_imported', "
            "'identity_configuration', ?, ?, ?)",
            (
                str(uuid.uuid4()),
                actor["user_id"] if actor else None,
                str(uuid.uuid4()),
                json.dumps(summary, ensure_ascii=False),
                now,
            ),
        )
        return summary

    @staticmethod
    def _apply_departments(
        connection: sqlite3.Connection, items: list[dict[str, Any]], now: str
    ) -> None:
        for item in items:
            code = normalize_stable_code(item["code"])
            row = connection.execute(
                "SELECT org_unit_id, wecom_department_id FROM org_units WHERE code=? COLLATE NOCASE",
                (code,),
            ).fetchone()
            incoming_wecom_id = str(item.get("wecom_department_id") or "").strip() or None
            if row:
                org_unit_id = row["org_unit_id"]
                wecom_id = incoming_wecom_id or row["wecom_department_id"]
                connection.execute(
                    "UPDATE org_units SET name=?, wecom_department_id=?, source=?, manual_override=?, "
                    "external_name_snapshot=?, external_parent_snapshot=?, sort_order=?, status=?, "
                    "parent_org_unit_id=NULL, updated_at=? WHERE org_unit_id=?",
                    (
                        str(item.get("name", "")).strip(),
                        wecom_id,
                        str(item.get("source", "manual")),
                        int(bool(item.get("manual_override", 0))),
                        str(item.get("external_name_snapshot", "")),
                        str(item.get("external_parent_snapshot", "")),
                        int(item.get("sort_order", 0)),
                        str(item.get("status", "active")),
                        now,
                        org_unit_id,
                    ),
                )
            else:
                org_unit_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO org_units(org_unit_id, code, name, parent_org_unit_id, "
                    "wecom_department_id, source, manual_override, external_name_snapshot, "
                    "external_parent_snapshot, sort_order, status, created_at, updated_at) "
                    "VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        org_unit_id,
                        code,
                        str(item.get("name", "")).strip(),
                        incoming_wecom_id,
                        str(item.get("source", "manual")),
                        int(bool(item.get("manual_override", 0))),
                        str(item.get("external_name_snapshot", "")),
                        str(item.get("external_parent_snapshot", "")),
                        int(item.get("sort_order", 0)),
                        str(item.get("status", "active")),
                        now,
                        now,
                    ),
                )
        org_map = _rows_by_code(connection, "org_units", "org_unit_id")
        for item in items:
            code = normalize_stable_code(item["code"])
            parent_code = normalize_stable_code(item.get("parent_code"))
            connection.execute(
                "UPDATE org_units SET parent_org_unit_id=? WHERE org_unit_id=?",
                (org_map.get(parent_code), org_map[code]),
            )

    @staticmethod
    def _apply_positions_base(
        connection: sqlite3.Connection, items: list[dict[str, Any]], now: str
    ) -> None:
        for item in items:
            code = normalize_stable_code(item["code"])
            row = connection.execute(
                "SELECT position_id FROM iam_positions WHERE code=? COLLATE NOCASE", (code,)
            ).fetchone()
            values = (
                str(item.get("name", "")).strip(),
                str(item.get("source", "manual")),
                int(bool(item.get("manual_override", 0))),
                str(item.get("external_name_snapshot", "")),
                int(item.get("level", 0)),
                str(item.get("status", "active")),
                now,
            )
            if row:
                connection.execute(
                    "UPDATE iam_positions SET name=?, source=?, manual_override=?, "
                    "external_name_snapshot=?, level=?, status=?, updated_at=? WHERE position_id=?",
                    (*values, row["position_id"]),
                )
            else:
                connection.execute(
                    "INSERT INTO iam_positions(position_id, code, name, source, manual_override, "
                    "external_name_snapshot, level, status, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), code, *values[:-1], now, now),
                )

    @staticmethod
    def _permission_ids(
        connection: sqlite3.Connection, permission_codes: list[str]
    ) -> list[str]:
        normalized = [str(code).strip().casefold() for code in permission_codes if str(code).strip()]
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        rows = connection.execute(
            f"SELECT permission_id, code FROM iam_permissions WHERE code IN ({placeholders})", normalized
        ).fetchall()
        by_code = {str(row["code"]).casefold(): str(row["permission_id"]) for row in rows}
        return [by_code[code] for code in normalized]

    def _apply_permission_groups(
        self, connection: sqlite3.Connection, items: list[dict[str, Any]], now: str
    ) -> None:
        for item in items:
            code = normalize_stable_code(item["code"])
            role = connection.execute(
                "SELECT role_id FROM iam_security_roles WHERE code=? COLLATE NOCASE", (code,)
            ).fetchone()
            if role:
                role_id = str(role["role_id"])
                connection.execute(
                    "UPDATE iam_security_roles SET name=?, status=?, updated_at=? WHERE role_id=?",
                    (str(item.get("name", "")).strip(), str(item.get("status", "active")), now, role_id),
                )
            else:
                role_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO iam_security_roles(role_id, code, name, is_system, status, created_at, "
                    "updated_at) VALUES(?, ?, ?, 0, ?, ?, ?)",
                    (role_id, code, str(item.get("name", "")).strip(), str(item.get("status", "active")), now, now),
                )
            connection.execute("DELETE FROM iam_role_permissions WHERE role_id=?", (role_id,))
            permission_ids = self._permission_ids(connection, list(item.get("permission_codes", [])))
            connection.executemany(
                "INSERT INTO iam_role_permissions(role_id, permission_id, created_at) VALUES(?, ?, ?)",
                [(role_id, permission_id, now) for permission_id in permission_ids],
            )

    @staticmethod
    def _apply_user_links(
        connection: sqlite3.Connection,
        items: list[dict[str, Any]],
        now: str,
        *,
        include_wecom_bindings: bool,
    ) -> tuple[int, int]:
        user_map = _users_by_name(connection)
        org_map = _rows_by_code(connection, "org_units", "org_unit_id")
        position_map = _rows_by_code(connection, "iam_positions", "position_id")
        role_map = _rows_by_code(connection, "iam_security_roles", "role_id")
        matched = 0
        bindings = 0
        for item in items:
            username = str(item.get("username", "")).strip()
            user_id = user_map.get(username.casefold())
            if not user_id:
                continue
            matched += 1
            membership = item.get("membership")
            connection.execute(
                "UPDATE org_memberships SET is_primary=0, status='ended', ended_at=?, updated_at=? "
                "WHERE user_id=? AND status='active' AND is_primary=1",
                (now, now, user_id),
            )
            if isinstance(membership, dict):
                org_id = org_map[normalize_stable_code(membership.get("org_unit_code"))]
                position_code = normalize_stable_code(membership.get("position_code"))
                position_id = position_map.get(position_code) if position_code else None
                manager_name = str(membership.get("direct_manager_username", "")).strip()
                manager_id = user_map.get(manager_name.casefold()) if manager_name else None
                existing = connection.execute(
                    "SELECT membership_id FROM org_memberships WHERE user_id=? AND org_unit_id=? "
                    "AND position_id IS ?",
                    (user_id, org_id, position_id),
                ).fetchone()
                if existing:
                    connection.execute(
                        "UPDATE org_memberships SET direct_manager_user_id=?, is_primary=1, status='active', "
                        "started_at=?, ended_at=NULL, updated_at=? WHERE membership_id=?",
                        (manager_id, membership.get("started_at") or now, now, existing["membership_id"]),
                    )
                else:
                    connection.execute(
                        "INSERT INTO org_memberships(membership_id, user_id, org_unit_id, position_id, "
                        "direct_manager_user_id, is_primary, status, started_at, created_at, updated_at) "
                        "VALUES(?, ?, ?, ?, ?, 1, 'active', ?, ?, ?)",
                        (
                            str(uuid.uuid4()), user_id, org_id, position_id, manager_id,
                            membership.get("started_at") or now, now, now,
                        ),
                    )
            connection.execute(
                "DELETE FROM iam_user_roles WHERE user_id=? AND role_id IN "
                "(SELECT role_id FROM iam_security_roles WHERE code NOT LIKE 'legacy.%')",
                (user_id,),
            )
            role_ids = [
                role_map[normalize_stable_code(code)]
                for code in item.get("additional_role_codes", [])
            ]
            connection.executemany(
                "INSERT INTO iam_user_roles(user_id, role_id, created_at) VALUES(?, ?, ?)",
                [(user_id, role_id, now) for role_id in role_ids],
            )
            if include_wecom_bindings:
                for binding in item.get("wecom_bindings", []):
                    provider = str(binding.get("provider", "wecom")).strip().lower() or "wecom"
                    connection.execute(
                        "DELETE FROM iam_external_identities WHERE user_id=? AND provider=?",
                        (user_id, provider),
                    )
                    connection.execute(
                        "INSERT INTO iam_external_identities(external_identity_id, user_id, provider, "
                        "external_userid, external_display_name, binding_source, metadata_json, "
                        "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()), user_id, provider,
                            str(binding.get("external_userid", "")).strip(),
                            str(binding.get("external_display_name", "")),
                            str(binding.get("binding_source", "manual")),
                            json.dumps(binding.get("metadata", {}), ensure_ascii=False),
                            now, now,
                        ),
                    )
                    bindings += 1
        return matched, bindings

    def _apply_position_scopes_and_permissions(
        self, connection: sqlite3.Connection, items: list[dict[str, Any]], now: str
    ) -> None:
        org_map = _rows_by_code(connection, "org_units", "org_unit_id")
        position_map = _rows_by_code(connection, "iam_positions", "position_id")
        for item in items:
            position_id = position_map[normalize_stable_code(item["code"])]
            intended_org_ids = {
                org_map[normalize_stable_code(code)] for code in item.get("department_codes", [])
            }
            occupied_org_ids = {
                str(row["org_unit_id"])
                for row in connection.execute(
                    "SELECT DISTINCT org_unit_id FROM org_memberships WHERE position_id=? "
                    "AND status='active' AND is_primary=1",
                    (position_id,),
                ).fetchall()
            }
            scope_ids = sorted(intended_org_ids | occupied_org_ids)
            connection.execute("DELETE FROM org_position_scopes WHERE position_id=?", (position_id,))
            connection.executemany(
                "INSERT INTO org_position_scopes(position_id, org_unit_id, created_at) VALUES(?, ?, ?)",
                [(position_id, org_id, now) for org_id in scope_ids],
            )
            connection.execute("DELETE FROM iam_position_permissions WHERE position_id=?", (position_id,))
            permission_ids = self._permission_ids(connection, list(item.get("permission_codes", [])))
            connection.executemany(
                "INSERT INTO iam_position_permissions(position_id, permission_id, created_at) VALUES(?, ?, ?)",
                [(position_id, permission_id, now) for permission_id in permission_ids],
            )

    @staticmethod
    def _apply_helper_settings(
        connection: sqlite3.Connection, helper: dict[str, Any], now: str
    ) -> None:
        if not isinstance(helper, dict):
            return
        position_map = _rows_by_code(connection, "iam_positions", "position_id")
        role_mapping = helper.get("project_overview_role_position_mapping")
        if isinstance(role_mapping, dict):
            encoded = {
                str(role): [
                    position_map[normalize_stable_code(code)]
                    for code in codes
                    if normalize_stable_code(code) in position_map
                ]
                for role, codes in role_mapping.items()
                if isinstance(codes, list)
            }
            connection.execute(
                "INSERT INTO iam_meta(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("project_overview_role_position_mapping", json.dumps(encoded, ensure_ascii=False), now),
            )
        managed = helper.get("project_overview_role_position_managed_permissions")
        if isinstance(managed, dict):
            encoded_managed = {
                position_map[normalize_stable_code(code)]: list(permission_codes)
                for code, permission_codes in managed.items()
                if normalize_stable_code(code) in position_map and isinstance(permission_codes, list)
            }
            connection.execute(
                "INSERT INTO iam_meta(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (
                    "project_overview_role_position_managed_permissions",
                    json.dumps(encoded_managed, ensure_ascii=False),
                    now,
                ),
            )

    def _apply_workflows(
        self, connection: sqlite3.Connection, items: list[dict[str, Any]], now: str
    ) -> None:
        org_map = _rows_by_code(connection, "org_units", "org_unit_id")
        position_map = _rows_by_code(connection, "iam_positions", "position_id")
        user_map = _users_by_name(connection)
        import_maps = {
            "requester_org_unit_codes": ("requester_org_unit_ids", org_map),
            "org_unit_codes": ("org_unit_ids", org_map),
            "requester_position_codes": ("requester_position_ids", position_map),
            "position_codes": ("position_ids", position_map),
            "usernames": ("user_ids", user_map),
        }
        for item in items:
            code = normalize_stable_code(item["code"])
            workflow = connection.execute(
                "SELECT * FROM approval_workflows WHERE code=? COLLATE NOCASE", (code,)
            ).fetchone()
            if workflow:
                workflow_id = str(workflow["workflow_id"])
                connection.execute(
                    "UPDATE approval_workflows SET module=?, event=?, name=?, updated_at=? "
                    "WHERE workflow_id=?",
                    (
                        str(item.get("module", "")).strip().lower(),
                        str(item.get("event", "")).strip().lower(),
                        str(item.get("name", "")).strip(),
                        now,
                        workflow_id,
                    ),
                )
            else:
                workflow_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO approval_workflows(workflow_id, code, module, event, name, status, "
                    "created_at, updated_at) VALUES(?, ?, ?, ?, ?, 'draft', ?, ?)",
                    (
                        workflow_id, code, str(item.get("module", "")).strip().lower(),
                        str(item.get("event", "")).strip().lower(),
                        str(item.get("name", "")).strip(), now, now,
                    ),
                )
            max_row = connection.execute(
                "SELECT COALESCE(MAX(version_number), 0) AS value FROM approval_workflow_versions "
                "WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            next_version = int(max_row["value"] or 0) + 1
            published_id = None
            published = item.get("published_version")
            if isinstance(published, dict):
                decoded = self._decode_import_workflow_version(published, import_maps, code)
                current = connection.execute(
                    "SELECT * FROM approval_workflow_versions WHERE version_id=("
                    "SELECT active_version_id FROM approval_workflows WHERE workflow_id=?)",
                    (workflow_id,),
                ).fetchone()
                if current and self._stored_workflow_version_matches(current, decoded):
                    published_id = str(current["version_id"])
                else:
                    published_id = str(uuid.uuid4())
                    connection.execute(
                        "UPDATE approval_workflow_versions SET state='retired' "
                        "WHERE workflow_id=? AND state='published'",
                        (workflow_id,),
                    )
                    self._insert_workflow_version(
                        connection, workflow_id, published_id, next_version, decoded, "published", now
                    )
                    next_version += 1
            draft = item.get("draft_version")
            if isinstance(draft, dict):
                decoded_draft = self._decode_import_workflow_version(draft, import_maps, code)
                existing_draft = connection.execute(
                    "SELECT version_id, version_number FROM approval_workflow_versions "
                    "WHERE workflow_id=? AND state='draft' ORDER BY version_number DESC LIMIT 1",
                    (workflow_id,),
                ).fetchone()
                if existing_draft:
                    self._update_workflow_version(
                        connection, str(existing_draft["version_id"]), decoded_draft
                    )
                else:
                    self._insert_workflow_version(
                        connection,
                        workflow_id,
                        str(uuid.uuid4()),
                        next_version,
                        decoded_draft,
                        "draft",
                        now,
                    )
            status = str(item.get("status", "draft"))
            if published_id:
                target_status = status if status in {"active", "disabled"} else "active"
                connection.execute(
                    "UPDATE approval_workflows SET active_version_id=?, status=?, updated_at=? "
                    "WHERE workflow_id=?",
                    (published_id, target_status, now, workflow_id),
                )
            elif not workflow or not workflow["active_version_id"]:
                connection.execute(
                    "UPDATE approval_workflows SET active_version_id=NULL, status='draft', updated_at=? "
                    "WHERE workflow_id=?",
                    (now, workflow_id),
                )

    @staticmethod
    def _decode_import_workflow_version(
        version: dict[str, Any], import_maps: dict[str, tuple[str, dict[str, str]]], code: str
    ) -> dict[str, Any]:
        missing: list[str] = []
        condition = _replace_reference_fields(
            version.get("condition", {}),
            field_maps=import_maps,
            missing=missing,
            context=f"审批流程 {code}：",
        )
        approver = _replace_reference_fields(
            version.get("approver", {}),
            field_maps=import_maps,
            missing=missing,
            context=f"审批流程 {code}：",
        )
        if missing:
            raise ValueError("；".join(missing))
        return {
            "priority": int(version.get("priority", 100)),
            "condition": condition,
            "approver": approver,
            "required_permission_code": str(version.get("required_permission_code", "")).strip().lower(),
            "approval_mode": str(version.get("approval_mode", "any")).strip().lower(),
            "notification": copy.deepcopy(version.get("notification", {})),
        }

    @staticmethod
    def _stored_workflow_version_matches(row: sqlite3.Row, value: dict[str, Any]) -> bool:
        return (
            int(row["priority"]) == value["priority"]
            and _decode_json(row["condition_json"]) == value["condition"]
            and _decode_json(row["approver_json"]) == value["approver"]
            and str(row["required_permission_code"]) == value["required_permission_code"]
            and str(row["approval_mode"]) == value["approval_mode"]
            and _decode_json(row["notification_json"]) == value["notification"]
        )

    @staticmethod
    def _insert_workflow_version(
        connection: sqlite3.Connection,
        workflow_id: str,
        version_id: str,
        version_number: int,
        value: dict[str, Any],
        state: str,
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO approval_workflow_versions(version_id, workflow_id, version_number, priority, "
            "condition_json, approver_json, required_permission_code, approval_mode, notification_json, "
            "state, created_at, published_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id, workflow_id, version_number, value["priority"],
                json.dumps(value["condition"], ensure_ascii=False),
                json.dumps(value["approver"], ensure_ascii=False),
                value["required_permission_code"], value["approval_mode"],
                json.dumps(value["notification"], ensure_ascii=False), state, now,
                now if state == "published" else None,
            ),
        )

    @staticmethod
    def _update_workflow_version(
        connection: sqlite3.Connection, version_id: str, value: dict[str, Any]
    ) -> None:
        connection.execute(
            "UPDATE approval_workflow_versions SET priority=?, condition_json=?, approver_json=?, "
            "required_permission_code=?, approval_mode=?, notification_json=? WHERE version_id=?",
            (
                value["priority"], json.dumps(value["condition"], ensure_ascii=False),
                json.dumps(value["approver"], ensure_ascii=False), value["required_permission_code"],
                value["approval_mode"], json.dumps(value["notification"], ensure_ascii=False), version_id,
            ),
        )
