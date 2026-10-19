"""概述审批业务的条件写入与同事务 JSON 辅助；不修改通用数据库规则。"""
import json
from datetime import datetime


async def read_json(connection, storage, key: str) -> dict:
    async with connection.execute(f"SELECT value FROM {storage.TABLE_NAME} WHERE key=?", (key,)) as cursor:
        row = await cursor.fetchone()
    return json.loads(row[0]) if row else {}


async def write_json(connection, storage, key: str, value: dict) -> None:
    await connection.execute(f"INSERT OR REPLACE INTO {storage.TABLE_NAME}(key,value) VALUES(?,?)",
                             (key, json.dumps(value)))


async def withdraw_request(storage, key: str, request_id: str, actor: str, expected: dict) -> bool:
    removed = False
    def remove(records):
        nonlocal removed
        current = (records or {}).get(request_id)
        if (not isinstance(current, dict) or current != expected or current.get("submitter") != actor
                or current.get("status") not in {"pending", "rejected", "failed", "withdrawn"}):
            return storage.ATOMIC_NO_UPDATE
        records.pop(request_id)
        removed = True
        return records
    saved = await storage.atomic_deep_update([key], remove)
    return saved and removed


def resubmission_matches(current: dict, expected: dict | None, actor: str) -> bool:
    return bool(expected is not None and current == expected and actor
                and current.get("submitter") == actor and current.get("status") in {"rejected", "failed"})


async def recover_processing(storage, key: str) -> bool:
    """启动时处理尚未完成的单项审批；不自动重放可能已发生的业务写入。"""
    def recover(records):
        for request in (records or {}).values():
            if request.get("status") == "processing":
                checkpoint = request.get("completion_checkpoint")
                if isinstance(checkpoint, dict):
                    request.update(checkpoint)
                    request.pop("completion_checkpoint", None)
                else:
                    request["status"] = "interrupted"
                    request["result"] = {"ok": False, "message": "执行中断，可能部分完成；请核对实际概述与关联影响后归档，不要重跑。"}
        return records or {}
    return await storage.atomic_deep_update([key], recover)


async def reconcile_interrupted(storage, key: str, request_id: str, actor: str, note: str, authorized) -> bool:
    changed = False
    def reconcile(request):
        nonlocal changed
        if not isinstance(request, dict) or not authorized(request):
            return storage.ATOMIC_NO_UPDATE
        checkpoint = request.get("completion_checkpoint")
        if request.get("status") in {"processing", "interrupted"} and isinstance(checkpoint, dict):
            request.update(checkpoint)
            request.pop("completion_checkpoint", None)
        elif request.get("status") == "interrupted" and note.strip():
            request.update(status="reconciled")
            request.setdefault("review_log", []).append({"action": "reconcile", "user": actor, "note": note.strip(),
                                                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        else:
            return storage.ATOMIC_NO_UPDATE
        changed = True
        return request
    saved = await storage.atomic_deep_update([key, request_id], reconcile)
    return saved and changed
