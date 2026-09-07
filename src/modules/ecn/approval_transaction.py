"""复用通用流程引擎，将待办写入延迟到 ECN 自身的 SQLite 事务中执行。"""

import uuid
from datetime import (
    datetime,
)


class ApprovalTransaction:
    """只代理流程引擎的两个写方法；权限、组织与流程配置仍由 UserService 读取。"""

    def __init__(self, service):
        self.service = service
        self.pending = {}
        self.commands = []

    def __getattr__(self, name):
        return getattr(self.service, name)

    def list_pending_assignment_usernames(self, *, module, entity_id, task_key):
        key = (module, entity_id, task_key)
        if key not in self.pending:
            self.pending[key] = self.service.list_pending_assignment_usernames(
                module=module,
                entity_id=entity_id,
                task_key=task_key,
            )
        return list(self.pending[key])

    def replace_work_assignments(self, *, module, entity_id, task_key, assignee_usernames, source_policy_code):
        key = (module, entity_id, task_key)
        usernames = list(dict.fromkeys(assignee_usernames))
        self.pending[key] = usernames
        self.commands.append(("replace", key, usernames, source_policy_code))
        return usernames

    def complete_work_assignment(self, *, module, entity_id, task_key, username, approval_mode="any"):
        key = (module, entity_id, task_key)
        pending = self.list_pending_assignment_usernames(module=module, entity_id=entity_id, task_key=task_key)
        if username.casefold() not in {value.casefold() for value in pending}:
            return False
        self.pending[key] = (
            [] if approval_mode == "any" else [value for value in pending if value.casefold() != username.casefold()]
        )
        self.commands.append(("complete", key, username, approval_mode))
        return True

    async def flush(self, connection):
        """与单据 JSON 共用连接；任何一条 SQL 失败都会回滚整次业务操作。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for action, key, value, option in self.commands:
            if action == "complete":
                cursor = await connection.execute(
                    "UPDATE work_assignments SET status='completed', completed_at=?, updated_at=? "
                    "WHERE module=? AND entity_id=? AND task_key=? AND status='pending' "
                    "AND assignee_user_id IN (SELECT user_id FROM iam_users WHERE username=? COLLATE NOCASE)",
                    (now, now, *key, value),
                )
                if not cursor.rowcount:
                    raise ValueError("审批待办已变化，本次操作已回滚")
                if option != "any":
                    continue
            await connection.execute(
                "UPDATE work_assignments SET status='superseded', updated_at=? "
                "WHERE module=? AND entity_id=? AND task_key=? AND status='pending'",
                (now, *key),
            )
            if action != "replace":
                continue
            for username in value:
                async with connection.execute(
                    "SELECT user_id FROM iam_users WHERE username=? COLLATE NOCASE AND status='active'",
                    (username,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise ValueError(f"审批人不存在或已停用：{username}")
                await connection.execute(
                    "INSERT INTO work_assignments(assignment_id, module, entity_id, task_key, "
                    "assignment_type, assignee_user_id, status, source_policy_code, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, 'approval', ?, 'pending', ?, ?, ?) "
                    "ON CONFLICT(module, entity_id, task_key, assignee_user_id) DO UPDATE SET "
                    "status='pending', source_policy_code=excluded.source_policy_code, "
                    "updated_at=excluded.updated_at, completed_at=NULL",
                    (str(uuid.uuid4()), *key, row[0], option, now, now),
                )
