"""Real-browser acceptance with an isolated SQLite database and temporary local server.

Requires: pip install -r requirements-dev.txt; playwright install chromium.
Screenshots and a machine-readable report are written to artifacts/browser/.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    project = Path(__file__).resolve().parents[1]
    artifacts = project / "artifacts" / "browser"
    artifacts.mkdir(parents=True, exist_ok=True)
    scratch = project / ".test-runs"
    scratch.mkdir(exist_ok=True)
    errors, checks = [], []
    with tempfile.TemporaryDirectory(prefix="browser-", dir=scratch) as temp:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "SMARTVOYAGE_MODE": "demo",
            "SMARTVOYAGE_API_HOST": "127.0.0.1",
            "SMARTVOYAGE_API_PORT": str(port),
            "SMARTVOYAGE_APP_DB": str(Path(temp) / "browser.sqlite3"),
        }
        with (artifacts / "server.log").open("w", encoding="utf-8") as log:
            server = subprocess.Popen(
                [sys.executable, "-m", "ai01_SmartVoyage.api.server"],
                cwd=project.parent,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(80):
                    if server.poll() is not None:
                        raise RuntimeError("Acceptance server exited. See artifacts/browser/server.log")
                    try:
                        urllib.request.urlopen(url + "/api/health", timeout=0.5).close()
                        break
                    except OSError:
                        time.sleep(0.1)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(url)
                    expect(page.locator("#auth-title")).to_be_visible()
                    page.screenshot(path=str(artifacts / "01-login.png"), full_page=True)
                    page.locator("#auth-toggle").click()
                    page.locator("#auth-name").fill("行旅体验者")
                    email = f"browser-{uuid.uuid4().hex[:8]}@example.test"
                    page.locator("#auth-email").fill(email)
                    page.locator("#auth-password").fill("DemoPass123!")
                    page.locator("#auth-submit").click()
                    expect(page.locator("#app")).to_be_visible()
                    checks.append("register_and_cookie_session")
                    page.locator("#plan-submit").click()
                    expect(page.locator(".detail-head h2")).to_have_text("北京 → 成都 · 3天1人")
                    expect(page.locator(".cost-row.total")).to_contain_text("1610.00")
                    page.locator("#days").fill("4")
                    page.locator("#plan-submit").click()
                    expect(page.locator(".detail-head h2")).to_have_text("北京 → 成都 · 4天1人")
                    expect(page.locator(".detail-head .pill")).to_contain_text("v2")
                    page.screenshot(path=str(artifacts / "02-plan-desktop.png"), full_page=True)
                    checks.append("generate_edit_and_requote")
                    page.locator('input[aria-label="确认模拟预订说明"]').check()
                    page.get_by_role("button", name="确认方案，创建模拟订单").click()
                    expect(page.locator("#confirm-dialog")).to_be_visible()
                    page.locator('#confirm-dialog button[value="confirm"]').click()
                    expect(page.locator(".status")).to_have_text("待模拟支付")
                    page.get_by_role("button", name="模拟支付 ¥1850.00").click()
                    page.locator('#confirm-dialog button[value="confirm"]').click()
                    expect(page.locator(".status")).to_have_text("模拟支付成功")
                    page.screenshot(path=str(artifacts / "03-order-paid.png"), full_page=True)
                    page.get_by_role("button", name="取消并模拟退款").click()
                    page.locator('#confirm-dialog button[value="confirm"]').click()
                    expect(page.locator(".status")).to_have_text("已模拟退款")
                    checks.append("confirm_order_pay_and_refund")
                    page.locator('[data-tab="chat"]').click()
                    page.locator("#chat-input").fill("我的方案需要多少钱？")
                    page.locator("#chat-submit").click()
                    expect(page.locator(".message.assistant")).to_contain_text("1850.00")
                    first = page.locator(".message.user").inner_text()
                    page.locator("#new-conversation").click()
                    expect(page.locator(".message.user")).to_have_count(0)
                    page.locator(".conversation-item").last.click()
                    expect(page.locator(".message.user")).to_have_text(first)
                    page.locator("#chat details summary").click()
                    page.locator("#run-list button").first.click()
                    expect(page.locator("#trace")).to_contain_text("deterministic_rules")
                    page.screenshot(path=str(artifacts / "04-chat-trace.png"), full_page=True)
                    checks.append("chat_context_history_and_trace")
                    page.locator('[data-tab="activity"]').click()
                    expect(page.locator("#audit-list")).to_contain_text("完成模拟退款")
                    page.locator('[data-tab="planner"]').click()
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(artifacts / "05-plan-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
                        "Mobile horizontal overflow"
                    )
                    checks.append("mobile_layout_390px")
                    page.reload()
                    expect(page.locator("#app")).to_be_visible()
                    expect(page.locator(".plan-card")).to_have_count(1)
                    checks.append("reload_restores_login_and_plan")
                    page.locator("#logout").click()
                    expect(page.locator("#auth")).to_be_visible()
                    assert page.request.get(url + "/api/plans").status == 401
                    checks.append("logout_revokes_access")
                    browser.close()
                if errors:
                    raise AssertionError(errors)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                (artifacts / "report.json").write_text(
                    json.dumps(
                        {"checks": checks, "browser_errors": errors, "simulation": True}, ensure_ascii=False, indent=2
                    ),
                    encoding="utf-8",
                )
    print(
        json.dumps({"passed": len(checks), "browser_errors": errors, "artifacts": str(artifacts)}, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
