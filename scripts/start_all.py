"""按正确顺序启动 SmartVoyage 全部本地服务。

运行位置：
    必须从 ``04-代码`` 目录执行，保证 ``python -m ai01_SmartVoyage...`` 能找到包。

运行命令：
    python -m ai01_SmartVoyage.scripts.start_all

启动顺序：
    1. MCP 工具服务：先暴露数据库/API 工具。
    2. 天气爬虫：按城市列表拉取预报写入 MySQL，然后按调度常驻。
    3. A2A 专家服务：再加载 MCP 工具并对外提供 Agent 能力。
    4. FastAPI 主服务：最后启动页面和聊天接口。

脚本只负责本地开发/课堂演示，不做守护进程管理；按 Ctrl+C 会尝试停止已启动进程。
"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_PARENT = Path(__file__).resolve().parents[2]

# 每一项都是一个独立进程。顺序不能随意调整：A2A 启动时会连接 MCP，
# 主 API 启动后会通过 A2A 调用专家 Agent。
SERVICES = [
    ("Weather MCP", "ai01_SmartVoyage.mcp_server.mcp_weather_server"),
    ("Ticket MCP", "ai01_SmartVoyage.mcp_server.mcp_ticket_server"),
    ("Trip MCP", "ai01_SmartVoyage.mcp_server.mcp_trip_server"),
    ("Weather Spider", "ai01_SmartVoyage.utils.spider_weather"),
    ("Weather A2A", "ai01_SmartVoyage.a2a_server.weather_server"),
    ("Ticket A2A", "ai01_SmartVoyage.a2a_server.ticket_server"),
    ("Trip A2A", "ai01_SmartVoyage.a2a_server.trip_server"),
    ("Main API", "ai01_SmartVoyage.api.server"),
]

PORTS = [
    ("Weather MCP", "WEATHER_MCP_PORT", 8002),
    ("Ticket MCP", "TICKET_MCP_PORT", 8001),
    ("Trip MCP", "TRIP_MCP_PORT", 8003),
    ("Weather A2A", "WEATHER_A2A_PORT", 5005),
    ("Ticket A2A", "TICKET_A2A_PORT", 5006),
    ("Trip A2A", "TRIP_A2A_PORT", 5007),
    ("Main API", "SMARTVOYAGE_API_PORT", 8080),
]


def configured_port(env_name: str, default: int) -> int:
    """读取端口配置，保证预检查和实际服务启动使用同一组环境变量。"""
    return int(os.getenv(env_name, str(default)))


def is_port_open(host: str, port: int) -> bool:
    """检测本地端口是否已有进程监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def assert_ports_available():
    """启动前检查所有服务端口，避免启动到一半才发现端口冲突。"""
    occupied = []
    for service_name, env_name, default_port in PORTS:
        port = configured_port(env_name, default_port)
        if is_port_open("127.0.0.1", port):
            occupied.append((service_name, env_name, port))

    if not occupied:
        return

    print("[error] 以下端口已被占用，请先停止旧进程或修改 .env 端口：", flush=True)
    for service_name, env_name, port in occupied:
        print(f"  - {service_name}: {env_name}={port}", flush=True)
    print("", flush=True)
    print("PowerShell 查看占用进程：", flush=True)
    print("  netstat -ano | findstr :8080", flush=True)
    print("停止指定 PID：", flush=True)
    print("  taskkill /PID <PID> /F", flush=True)
    raise SystemExit(1)


def start_service(name: str, module: str) -> subprocess.Popen:
    """启动一个 Python 模块进程，并返回 Popen 句柄供后续监控/停止。"""
    env = os.environ.copy()
    command = [sys.executable, "-m", module]
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_PARENT),
        env=env,
        creationflags=creation_flags,
    )
    print(f"[start] {name}: pid={process.pid}, module={module}", flush=True)
    return process


def stop_process(process: subprocess.Popen):
    """跨平台停止子进程。

    Windows 下优先发送 CTRL_BREAK_EVENT，让 uvicorn/FastMCP 有机会正常退出；
    非 Windows 平台使用 terminate。
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.terminate()


def main():
    """启动全部服务并持续监控。

    如果任一子服务提前退出，脚本会抛出异常并进入 finally 清理阶段，避免主 API
    仍然运行但下游 Agent 已经不可用。
    """
    processes: list[tuple[str, subprocess.Popen]] = []
    try:
        assert_ports_available()

        for name, module in SERVICES:
            processes.append((name, start_service(name, module)))
            time.sleep(2)

        api_port = configured_port("SMARTVOYAGE_API_PORT", 8080)
        print(f"[ready] SmartVoyage API: http://127.0.0.1:{api_port}", flush=True)
        print("[hint] Press Ctrl+C to stop all services.", flush=True)

        while True:
            for name, process in processes:
                code = process.poll()
                if code is not None:
                    raise RuntimeError(f"{name} exited with code {code}")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[stop] stopping services...", flush=True)
    finally:
        for name, process in reversed(processes):
            stop_process(process)
            print(f"[stop] {name}", flush=True)
        for _, process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
