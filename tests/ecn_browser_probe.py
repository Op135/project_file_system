"""隔离的真实浏览器验收入口：python -m tests.ecn_browser_probe。

仅启动回环地址测试服务；所有身份、单据、会话和浏览器配置位于临时目录。
不启动主应用后台任务，不发送微信；业务页面与按钮使用生产实现。
"""

import asyncio
import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen


def serve(port: int) -> None:
    from nicegui import app, ui
    from src import db_storage
    from src.modules.ecn.detail import open_ecn_detail_dialog
    from tests.test_ecn_acceptance import ECNAcceptanceTests, ordinary_scheme

    # 与正式入口一致，显式导入页面以执行路由注册。
    importlib.import_module("src.pages.ecn_management")

    fixture = ECNAcceptanceTests()
    ids: dict[str, str] = {}

    async def startup():
        await fixture.asyncSetUp()
        # 页面模块可能以模块属性保存数据库引用，统一指向临时数据库。
        for name, module in list(sys.modules.items()):
            if name.startswith("src.") and getattr(module, "db_storage", None) is db_storage:
                setattr(module, "db_storage", fixture.storage)
        app.state.user_service = fixture.service
        app.storage.general.update(project_summary=fixture.summary, project_req_max_ver={"P1": "1.0"}, over_config_data={}, over_config_data_flat={}, project_sale={})
        await fixture.create()
        ids["draft"] = fixture.value["ecn_id"]
        await fixture.create()
        ids["collaboration"] = fixture.value["ecn_id"]
        await fixture.scheming()
        await fixture.save_scheme(ordinary_scheme())
        ids["scheme"] = fixture.value["ecn_id"]
        await fixture.ready()
        await fixture.create()
        fixture.accept(await fixture.action("submit_ecr"))
        ids["approval"] = fixture.value["ecn_id"]
        await fixture.scheming()
        await fixture.save_scheme(ordinary_scheme())
        await fixture.review()
        ids["execution"] = fixture.value["ecn_id"]

    @ui.page("/_audit/{kind}/{username}")
    async def audit_page(kind: str, username: str):
        if username not in {"张三", "李四", "王五"}:
            raise ValueError("非验收账号")
        app.storage.user.update(current_user=username, current_role="申请岗位")
        ui.label("ECN 隔离验收")
        if kind == "list":
            ui.navigate.to("/ecn_management")
        else:
            await open_ecn_detail_dialog(ids[kind], current_user=username, current_role="申请岗位", refresh_list=lambda: None)

    @app.get("/_audit/state")
    async def state():
        data = await fixture.storage.get_fresh_item("ecn_management_data", {})
        return {kind: data[value]["workflow"]["current_state"] for kind, value in ids.items()}

    @app.get("/_audit/pending")
    async def pending():
        data = await fixture.storage.get_fresh_item("ecn_management_data", {})
        return data[ids["approval"]]["workflow"]["pending_roles"]

    @app.get("/_audit/date")
    async def saved_date():
        data = await fixture.storage.get_fresh_item("ecn_management_data", {})
        return data[ids["collaboration"]]["basic_info"]["requirement_date"]

    app.on_startup(startup)
    app.on_shutdown(fixture.asyncTearDown)
    ui.run(host="127.0.0.1", port=port, show=False, reload=False, storage_secret="isolated-ecn-acceptance-only")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def read_json(url: str):
    with urlopen(url, timeout=2) as response:
        return json.load(response)


async def probe() -> None:
    from websockets.asyncio.client import connect

    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not edge.is_file():
        raise RuntimeError("未找到本机Edge，不能执行真实浏览器验收")
    server_port, browser_port = free_port(), free_port()
    base = f"http://127.0.0.1:{server_port}"
    with tempfile.TemporaryDirectory(prefix="ecn-browser-") as directory:
        root = Path(directory)
        env = dict(os.environ, NICEGUI_STORAGE_PATH=str(root / "sessions"), PYTHONIOENCODING="utf-8", TEMP=str(root), TMP=str(root))
        with (root / "server.log").open("w", encoding="utf-8") as log, (root / "browser.log").open("w", encoding="utf-8") as browser_log:
            server = subprocess.Popen([sys.executable, "-m", "tests.ecn_browser_probe", "serve", str(server_port)], env=env, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
            browser = None
            try:
                for _ in range(120):
                    if server.poll() is not None:
                        raise RuntimeError((root / "server.log").read_text(encoding="utf-8"))
                    try:
                        read_json(base + "/_audit/state")
                        break
                    except OSError:
                        await asyncio.sleep(0.5)
                else:
                    raise TimeoutError("测试服务未就绪")
                print("START browser: isolated Edge", flush=True)
                browser = subprocess.Popen([str(edge), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", f"--remote-debugging-port={browser_port}", f"--user-data-dir={root / 'edge'}", "about:blank"], stdout=browser_log, stderr=browser_log, creationflags=subprocess.CREATE_NO_WINDOW)
                pages = []
                for _ in range(60):
                    if browser.poll() is not None:
                        raise RuntimeError(f"Edge exited: {browser.returncode}")
                    try:
                        pages = read_json(f"http://127.0.0.1:{browser_port}/json")
                        if pages:
                            break
                    except OSError:
                        pass
                    await asyncio.sleep(0.5)
                # 使用明确新建的标签，避免Edge首次启动的临时标签被自动关闭。
                await asyncio.sleep(1)
                with urlopen(Request(f"http://127.0.0.1:{browser_port}/json/new?about:blank", method="PUT"), timeout=5) as response:
                    page = json.load(response)
                async with connect(page["webSocketDebuggerUrl"], max_size=8_000_000, proxy=None) as ws:
                    active_ws = ws
                    serial = 0
                    errors: list[dict] = []

                    async def command(method: str, params: dict | None = None) -> dict:
                        nonlocal serial
                        serial += 1
                        await active_ws.send(json.dumps({"id": serial, "method": method, "params": params or {}}))
                        while True:
                            result = json.loads(await asyncio.wait_for(active_ws.recv(), 15))
                            if result.get("method") == "Runtime.exceptionThrown":
                                errors.append(result["params"])
                            if result.get("id") == serial:
                                if "error" in result:
                                    raise RuntimeError(result["error"])
                                return result.get("result", {})

                    async def evaluate(expression: str) -> object:
                        result = await command("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
                        if "exceptionDetails" in result:
                            raise RuntimeError(result["exceptionDetails"])
                        return result.get("result", {}).get("value")

                    async def wait_for(expression: str) -> None:
                        for _ in range(80):
                            if await evaluate(expression):
                                return
                            await asyncio.sleep(0.25)
                        raise AssertionError("等待页面条件超时：" + expression + "\n" + str(await evaluate("document.body.innerText")))

                    async def navigate(path: str):
                        print("OPEN " + path, flush=True)
                        await command("Page.navigate", {"url": base + path})
                        await wait_for("document.body.innerText.includes('ECN') && !document.body.innerText.includes('Connection lost')")
                        await asyncio.sleep(0.8)

                    async def click_text(text: str, selector="button"):
                        expression = f"[...document.querySelectorAll({json.dumps(selector)})].find(e => e.innerText.includes({json.dumps(text)}) && e.getBoundingClientRect().width > 0)"
                        await wait_for(f"Boolean({expression})")
                        await evaluate(f"({expression}).click()")
                        await asyncio.sleep(0.5)

                    await command("Runtime.enable")
                    await navigate("/_audit/draft/张三")
                    await wait_for("Boolean(document.querySelector('input[type=date]'))")
                    await evaluate("(()=>{const e=document.querySelector('input[type=date]');e.value='2026-11-15';e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()")
                    await click_text("保存为草稿")
                    await navigate("/_audit/draft/张三")
                    await wait_for("document.querySelector('input[type=date]')?.value === '2026-11-15'")
                    print("PASS browser: native date input and draft persistence", flush=True)
                    await click_text("发起 ECR")
                    await asyncio.sleep(0.5)
                    state = read_json(base + "/_audit/state")
                    assert state["draft"] == "ECR 审批中", state
                    print("PASS browser: ECR submission", flush=True)
                    await navigate("/_audit/scheme/张三")
                    for title in ("ECN-影响", "ECN-方案", "审批记录", "ECR-申请"):
                        await click_text(title, "[role=tab]")
                    print("PASS browser: lazy tabs and scheme rendering", flush=True)
                    await navigate("/_audit/approval/李四")
                    await click_text("审批记录", "[role=tab]")
                    await click_text("同意")
                    await wait_for("fetch('/_audit/pending').then(r=>r.json()).then(x=>x.length===1 && x[0]==='王五')")
                    print("PASS browser: approval callback", flush=True)
                    await navigate("/_audit/execution/张三")
                    await click_text("ECN-执行", "[role=tab]")
                    await wait_for("document.querySelectorAll('[role=checkbox]').length > 0")
                    print("PASS browser: execution controls rendered", flush=True)
                    for _ in range(2):
                        unchecked = "[...document.querySelectorAll('[role=checkbox]')].find(e=>e.getBoundingClientRect().width>0 && e.getAttribute('aria-checked')==='false' && e.getAttribute('aria-disabled')!=='true')"
                        await wait_for(f"Boolean({unchecked})")
                        await evaluate(f"({unchecked}).click()")
                        await asyncio.sleep(0.8)
                    await click_text("执行系统内资料")
                    await wait_for("fetch('/_audit/state').then(r=>r.json()).then(x=>x.execution==='变更已完成')")
                    print("PASS browser: ordinary and ERP confirmation, execution and closure", flush=True)
                    await navigate("/_audit/list/张三")
                    await wait_for("Boolean(document.querySelector('.ag-root'))")
                    print("PASS browser: actual ECN list route", flush=True)
                    # 两个真实标签先打开同一快照，再分别保存同一字段，验证后保存者明确冲突。
                    await navigate("/_audit/collaboration/张三")
                    with urlopen(Request(f"http://127.0.0.1:{browser_port}/json/new?about:blank", method="PUT"), timeout=5) as response:
                        second_page = json.load(response)
                    async with connect(second_page["webSocketDebuggerUrl"], max_size=8_000_000, proxy=None) as second_ws:
                        active_ws = second_ws
                        await command("Runtime.enable")
                        await navigate("/_audit/collaboration/张三")
                        active_ws = ws
                        await evaluate("(()=>{const e=document.querySelector('input[type=date]');e.value='2026-11-16';e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()")
                        await click_text("保存为草稿")
                        active_ws = second_ws
                        await evaluate("(()=>{const e=document.querySelector('input[type=date]');e.value='2026-11-17';e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()")
                        await click_text("保存为草稿")
                        await wait_for("document.body.innerText.includes('已被其他') || document.body.innerText.includes('冲突')")
                        assert read_json(base + "/_audit/date") == "2026-11-16"
                        print("PASS browser: stale second tab cannot overwrite saved date", flush=True)
                    active_ws = ws
                    if errors:
                        raise AssertionError(errors)
                    print("PASS browser: no uncaught JavaScript exception", flush=True)
                log.flush()
                server_text = (root / "server.log").read_text(encoding="utf-8")
                if "Traceback (most recent call last)" in server_text:
                    raise AssertionError(server_text)
                print("PASS server: no Python traceback", flush=True)
            except Exception:
                log.flush()
                browser_log.flush()
                print((root / "browser.log").read_text(encoding="utf-8", errors="replace")[-4000:], flush=True)
                print((root / "server.log").read_text(encoding="utf-8")[-12000:], flush=True)
                raise
            finally:
                if browser is not None:
                    browser.terminate()
                    browser.wait(timeout=10)
                server.terminate()
                server.wait(timeout=10)
                # Edge子进程释放临时配置文件后再由TemporaryDirectory回收。
                await asyncio.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve(int(sys.argv[2]))
    else:
        asyncio.run(probe())
