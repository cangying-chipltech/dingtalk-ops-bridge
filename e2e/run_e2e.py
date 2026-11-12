#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROUTER = PROJECT_ROOT / "scripts" / "agent-router.sh"


class CaseFailure(AssertionError):
    pass


class WebhookHandler(BaseHTTPRequestHandler):
    payloads: list[dict[str, Any]] = []
    lock = threading.Lock()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"invalid_body": body.decode("utf-8", errors="replace")}
        with self.lock:
            self.payloads.append(payload)
        response = json.dumps({"errcode": 0, "errmsg": "ok"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.payloads.clear()

    @classmethod
    def snapshot(cls) -> list[dict[str, Any]]:
        with cls.lock:
            return list(cls.payloads)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), WebhookHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    webhook_url = f"http://127.0.0.1:{server.server_address[1]}/robot/send?access_token=e2e"

    try:
        with tempfile.TemporaryDirectory(prefix="dingtalk-ops-e2e-") as temp:
            root = Path(temp)
            run_cases(root, webhook_url)
    except CaseFailure as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    print("\nAll 7 end-to-end cases passed.")
    return 0


def run_cases(root: Path, webhook_url: str) -> None:
    actions_file = root / "actions.json"
    monitors_file = root / "monitors.json"
    state_file = root / "monitor-state.json"
    marker_file = root / "healthy"
    plugin_dir = root / "plugins"

    write_actions(actions_file)
    write_monitors(monitors_file, marker_file)
    write_plugin(plugin_dir)
    env = runtime_env(actions_file, monitors_file, webhook_url, plugin_dir)

    _case("1/7 钉钉命令 -> 动作 -> Markdown 反馈")
    result = run_agent(["run", "hello"], env)
    _expect(result.returncode == 0, _process_error(result))
    _expect("hello-e2e" in result.stdout, "动作输出没有返回给命令调用方")
    _expect("成功：**1**" in result.stdout, "动作结果摘要缺失")
    print("    PASS: hello-e2e 已返回")

    _case("2/7 多个动作并发执行")
    started = time.monotonic()
    result = run_agent(["run", "slow-a,slow-b"], env)
    elapsed = time.monotonic() - started
    _expect(result.returncode == 0, _process_error(result))
    _expect("成功：**2**" in result.stdout, "并发动作结果不完整")
    _expect(elapsed < 1.4, f"并发执行耗时过长：{elapsed:.2f}s")
    print(f"    PASS: 两个 0.8s 动作总耗时 {elapsed:.2f}s")

    _case("3/7 钉钉 check -> 即时监控检查")
    result = run_agent(["check"], env)
    _expect(result.returncode == 0, _process_error(result))
    _expect("监控检查结果" in result.stdout, "未返回监控检查结果")
    _expect("❌ e2e-marker" in result.stdout, "异常监控项未显示为失败")
    print("    PASS: check 返回 e2e-marker 异常")

    _case("4/7 监控异常 -> 钉钉 Webhook")
    WebhookHandler.reset()
    result = run_monitor(monitors_file, state_file, env)
    _expect(result.returncode == 0, _process_error(result))
    payloads = WebhookHandler.snapshot()
    _expect(len(payloads) == 1, f"预期 1 条异常消息，实际 {len(payloads)} 条")
    title = payloads[0].get("markdown", {}).get("title", "")
    _expect("监控异常" in title and "e2e-marker" in title, f"异常消息标题不正确：{title!r}")
    print("    PASS: Webhook 收到异常消息")

    _case("5/7 持续异常 -> 告警去重")
    WebhookHandler.reset()
    result = run_monitor(monitors_file, state_file, env)
    _expect(result.returncode == 0, _process_error(result))
    payloads = WebhookHandler.snapshot()
    _expect(len(payloads) == 0, f"重复检查不应再次告警，实际收到 {len(payloads)} 条")
    print("    PASS: repeatAlertSeconds=0 时未重复告警")

    _case("6/7 异常恢复 -> 钉钉 Webhook")
    marker_file.write_text("healthy\n", encoding="utf-8")
    WebhookHandler.reset()
    result = run_monitor(monitors_file, state_file, env)
    _expect(result.returncode == 0, _process_error(result))
    payloads = WebhookHandler.snapshot()
    _expect(len(payloads) == 1, f"预期 1 条恢复消息，实际 {len(payloads)} 条")
    title = payloads[0].get("markdown", {}).get("title", "")
    _expect("监控恢复" in title and "e2e-marker" in title, f"恢复消息标题不正确：{title!r}")
    state = json.loads(state_file.read_text(encoding="utf-8"))
    _expect(state["monitors"]["e2e-marker"]["status"] == "ok", "监控状态没有恢复为 ok")
    print("    PASS: Webhook 收到恢复消息")

    _case("7/7 外部插件自带 action/actionsFile + command")
    result = run_agent(["run", "plugin-echo"], env)
    _expect(result.returncode == 0, _process_error(result))
    _expect("plugin-action-ok" in result.stdout, "外部插件动作未执行")
    result = run_agent(["plugin-check"], env)
    _expect(result.returncode == 0, _process_error(result))
    _expect("plugin-command-ok" in result.stdout, "外部插件命令未接管")
    print("    PASS: plugins/ 目录插件已注册并生效")


def write_actions(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "settings": {"maxConcurrency": 4, "maxOutputChars": 2000, "maxReplyChars": 8000},
                "actions": {
                    "hello": {
                        "description": "返回固定字符串",
                        "command": ["printf", "hello-e2e"],
                    },
                    "slow-a": {
                        "description": "并发测试 A",
                        "command": ["sh", "-c", "sleep 0.8"],
                    },
                    "slow-b": {
                        "description": "并发测试 B",
                        "command": ["sh", "-c", "sleep 0.8"],
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def write_monitors(path: Path, marker_file: Path) -> None:
    command = ["sh", "-c", f"test -f {shlex.quote(str(marker_file))}"]
    path.write_text(
        json.dumps(
            {
                "settings": {"maxConcurrency": 2, "defaultIntervalSeconds": 60},
                "monitors": {
                    "e2e-marker": {
                        "description": "标记文件存在时正常，否则异常",
                        "check": {
                            "type": "command",
                            "command": command,
                            "expectedExitCodes": [0],
                        },
                        "intervalSeconds": 60,
                        "timeoutSeconds": 5,
                        "severity": "warning",
                        "repeatAlertSeconds": 0,
                        "notifyRecovery": True,
                        "enabled": True,
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def write_plugin(root: Path) -> None:
    plugin_dir = root / "e2e-plugin"
    source_dir = plugin_dir / "src"
    config_dir = plugin_dir / "config"
    source_dir.mkdir(parents=True)
    config_dir.mkdir()
    (source_dir / "e2e_action_plugin.py").write_text(
        "\n".join(
            [
                "from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION, PreparedAction",
                "",
                "class Provider:",
                "    api_version = PLUGIN_API_VERSION",
                "    type_name = 'e2e-plugin'",
                "",
                "    def validate(self, definition):",
                "        pass",
                "",
                "    def prepare(self, definition, values, context):",
                "        return PreparedAction(definition=definition, command=('printf', 'plugin-action-ok'))",
                "",
                "class Commands:",
                "    api_version = PLUGIN_API_VERSION",
                "",
                "    def handle(self, prompt, context):",
                "        if prompt.strip() == 'plugin-check':",
                "            return '### plugin-command-ok'",
                "        return None",
                "",
                "PROVIDER = Provider()",
                "COMMAND_PROVIDER = Commands()",
            ]
        ),
        encoding="utf-8",
    )
    (config_dir / "actions.json").write_text(
        json.dumps(
            {
                "actions": {
                    "plugin-echo": {
                        "description": "外部插件自带动作",
                        "type": "e2e-plugin",
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (plugin_dir / "action_plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 1,
                "module": "e2e_action_plugin",
                "provider": "PROVIDER",
                "commandProvider": "COMMAND_PROVIDER",
                "actionsFile": "config/actions.json",
                "pythonPath": "src",
            }
        ),
        encoding="utf-8",
    )


def runtime_env(
    actions_file: Path,
    monitors_file: Path,
    webhook_url: str,
    plugin_dir: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "ACTIONS_FILE": str(actions_file),
            "MONITORS_FILE": str(monitors_file),
            "ACTION_PLUGIN_DIR": str(plugin_dir),
            "DINGTALK_WEBHOOK_URL": webhook_url,
            "DINGTALK_WEBHOOK_TIMEOUT_SECONDS": "3",
            "LOG_LEVEL": "WARNING",
        }
    )
    for name in (
        "DINGTALK_WEBHOOK_SECRET",
        "DINGTALK_WEBHOOK_KEYWORD",
        "DINGTALK_ALERT_AT_MOBILES",
        "SWE_RUNNER_CONFIG",
    ):
        env.pop(name, None)
    return env


def run_agent(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(AGENT_ROUTER), *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )


def run_monitor(monitors_file: Path, state_file: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "dingtalk_ops_bridge",
            "monitor",
            "--once",
            "--monitors-file",
            str(monitors_file),
            "--state-file",
            str(state_file),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )


def _case(label: str) -> None:
    print(f"\n[{label}]")


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise CaseFailure(message)


def _process_error(result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"命令退出码 {result.returncode}\n"
        f"stdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-2000:]}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
