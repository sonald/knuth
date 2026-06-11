from __future__ import annotations

import html
import json
import os
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = REPO_ROOT / ".scratch" / "ai-shell-e2e" / datetime.now(UTC).strftime(
    "%Y%m%dT%H%M%SZ"
)
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def main() -> int:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    workspace = RUN_ROOT / "workspace"
    home = RUN_ROOT / "home"
    workspace.mkdir()
    home.mkdir()

    server = _start_stub_server(RUN_ROOT)
    port = server.server_address[1]
    proc, master_fd = _start_interactive_cli(workspace, home, port)
    transcript = _drive_interactive(proc, master_fd)
    clean_transcript = _strip_ansi(transcript)
    terminal_view = _terminal_snapshot(transcript)

    (RUN_ROOT / "transcript.ansi.txt").write_text(transcript, encoding="utf-8")
    (RUN_ROOT / "transcript.txt").write_text(clean_transcript, encoding="utf-8")
    (RUN_ROOT / "terminal-view.txt").write_text(terminal_view, encoding="utf-8")
    screenshot_path = _write_terminal_screenshot(terminal_view)
    inspection = _inspect_results(workspace, home, clean_transcript, screenshot_path)
    (RUN_ROOT / "inspection.json").write_text(
        json.dumps(inspection, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    server.shutdown()

    print(json.dumps({"run_root": str(RUN_ROOT), **inspection}, ensure_ascii=False, indent=2))
    return 0 if inspection["ok"] else 1


def _free_port() -> int:
    with socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_stub_server(run_root: Path) -> ThreadingHTTPServer:
    requests_path = run_root / "server_requests.jsonl"
    state = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            if not self.path.endswith("/chat/completions"):
                self.send_error(404)
                return
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            state["count"] += 1
            request_index = state["count"]
            with requests_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "index": request_index,
                            "path": self.path,
                            "message_count": len(body.get("messages", [])),
                            "tools": [
                                item.get("function", {}).get("name")
                                for item in body.get("tools", [])
                            ],
                            "last_message": body.get("messages", [])[-1]
                            if body.get("messages")
                            else None,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            chunks = _scripted_chunks(request_index)
            payload = "".join(
                f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks
            )
            payload += "data: [DONE]\n\n"
            raw = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", _free_port()), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _scripted_chunks(index: int) -> list[dict]:
    if index == 1:
        return [
            _content_chunk("<think>准备写入端到端测试文件</think>"),
            _tool_chunk(
                "call_write",
                "write_file",
                {"path": "e2e-output.txt", "content": "alpha\nbeta\n"},
            ),
            _finish_chunk("tool_calls"),
        ]
    if index == 2:
        return [
            _tool_chunk(
                "call_read",
                "read_file",
                {"path": "e2e-output.txt", "offset": 1, "limit": 10},
            ),
            _finish_chunk("tool_calls"),
        ]
    if index == 3:
        command = "printf 'shell-ok\\n'; python -c \"print('x'*5000)\""
        return [
            _tool_chunk("call_shell", "shell", {"command": command}),
            _finish_chunk("tool_calls"),
        ]
    return [
        _content_chunk(
            "E2E 完成：文件已写入并读取，shell 已执行，长 stdout 已保存到 offload。"
        ),
        _finish_chunk("stop"),
    ]


def _content_chunk(content: str) -> dict:
    return {
        "id": "chatcmpl-e2e",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "e2e-model",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


def _tool_chunk(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": "chatcmpl-e2e",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "e2e-model",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }


def _finish_chunk(reason: str) -> dict:
    return {
        "id": "chatcmpl-e2e",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "e2e-model",
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
    }


def _start_interactive_cli(workspace: Path, home: Path, port: int) -> tuple[subprocess.Popen, int]:
    master_fd, slave_fd = os.openpty()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "KNUTH_API_KEY": "test-key",
            "KNUTH_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "KNUTH_MODEL": "e2e-model",
            "NO_COLOR": "1",
            "TERM": "xterm-256color",
        }
    )
    proc = subprocess.Popen(
        ["uv", "--project", str(REPO_ROOT), "run", "knuth", "run"],
        cwd=workspace,
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        text=False,
    )
    os.close(slave_fd)
    return proc, master_fd


def _drive_interactive(proc: subprocess.Popen, master_fd: int) -> str:
    selector = selectors.DefaultSelector()
    selector.register(master_fd, selectors.EVENT_READ)
    transcript = bytearray()
    prompt_sent = False
    write_approved = False
    shell_approved = False
    exit_sent = False
    deadline = time.monotonic() + 45

    def send(text: str) -> None:
        os.write(master_fd, text.encode("utf-8"))

    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            for key, _ in selector.select(timeout=0.1):
                try:
                    data = os.read(key.fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    continue
                transcript.extend(data)
                clean = _strip_ansi(transcript.decode("utf-8", errors="replace"))
                if "knuth ❯" in clean and not prompt_sent:
                    send("请执行完整端到端工具验证：写文件、读文件、执行 shell 长输出\n")
                    prompt_sent = True
                if "approve write_file? [y/N/a]" in clean and not write_approved:
                    send("y\n")
                    write_approved = True
                if "approve shell? [y/N/a]" in clean and not shell_approved:
                    send("y\n")
                    shell_approved = True
                if (
                    "E2E 完成" in clean
                    and clean.rstrip().endswith("knuth ❯")
                    and not exit_sent
                ):
                    send("/exit\n")
                    exit_sent = True
            if exit_sent and proc.poll() is not None:
                break
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)
    finally:
        selector.close()
        os.close(master_fd)
    return transcript.decode("utf-8", errors="replace")


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def _terminal_snapshot(text: str) -> str:
    lines = [""]
    row = 0
    col = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\x1b" and index + 1 < len(text) and text[index + 1] == "[":
            end = index + 2
            while end < len(text) and text[end] not in "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~":
                end += 1
            if end >= len(text):
                break
            command = text[end]
            params = text[index + 2 : end]
            if command == "A":
                amount = _first_csi_int(params, default=1)
                row = max(0, row - amount)
                col = min(col, len(lines[row]))
            elif command == "K" and (params in {"", "0", "2"}):
                lines[row] = ""
                col = 0
            index = end + 1
            continue
        if char == "\r":
            col = 0
        elif char == "\n":
            row += 1
            col = 0
            while len(lines) <= row:
                lines.append("")
        else:
            line = lines[row]
            if col > len(line):
                line += " " * (col - len(line))
            if col == len(line):
                line += char
            else:
                line = line[:col] + char + line[col + 1 :]
            lines[row] = line
            col += 1
        index += 1
    return "\n".join(line.rstrip() for line in lines).rstrip() + "\n"


def _first_csi_int(params: str, *, default: int) -> int:
    cleaned = params.lstrip("?")
    first = cleaned.split(";", 1)[0]
    try:
        return int(first)
    except ValueError:
        return default


def _write_terminal_screenshot(transcript: str) -> str:
    lines = transcript.splitlines()[-80:]
    wrapped: list[str] = []
    for line in lines:
        while len(line) > 150:
            wrapped.append(line[:150])
            line = line[150:]
        wrapped.append(line)
    width = 1320
    line_height = 20
    height = max(360, 80 + line_height * len(wrapped))
    rows = []
    for idx, line in enumerate(wrapped, start=1):
        y = 50 + idx * line_height
        rows.append(
            f'<text x="28" y="{y}" fill="#d7e0ea" font-family="Menlo, Consolas, monospace" '
            f'font-size="14">{html.escape(line)}</text>'
        )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" fill="#0b1020"/>'
        '<rect x="16" y="16" width="1288" height="36" rx="10" fill="#151b2e"/>'
        '<circle cx="40" cy="34" r="6" fill="#ff5f57"/>'
        '<circle cx="62" cy="34" r="6" fill="#ffbd2e"/>'
        '<circle cx="84" cy="34" r="6" fill="#28c840"/>'
        '<text x="112" y="39" fill="#8da2bd" font-family="Menlo, Consolas, monospace" '
        'font-size="13">knuth interactive E2E transcript</text>'
        + "".join(rows)
        + "</svg>"
    )
    svg_path = RUN_ROOT / "terminal-screenshot.svg"
    svg_path.write_text(svg, encoding="utf-8")
    png_path = RUN_ROOT / "terminal-screenshot.png"
    qlmanage = shutil.which("qlmanage")
    if qlmanage:
        subprocess.run(
            [qlmanage, "-t", "-s", "1600", "-o", str(RUN_ROOT), str(svg_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        generated = RUN_ROOT / (svg_path.name + ".png")
        if generated.exists():
            generated.replace(png_path)
            return str(png_path)
    return str(svg_path)


def _inspect_results(
    workspace: Path, home: Path, transcript: str, screenshot_path: str
) -> dict:
    db_path = home / ".knuth" / "knuth.db"
    output_path = workspace / "e2e-output.txt"
    run_summary: dict = {}
    event_types: list[str] = []
    tool_events: list[dict] = []
    offload_paths: list[str] = []
    if db_path.exists():
        with sqlite3.connect(db_path) as conn:
            run_row = conn.execute(
                "select id, status, last_seq from runs order by created_at desc limit 1"
            ).fetchone()
            if run_row:
                run_summary = {
                    "run_id": run_row[0],
                    "status": run_row[1],
                    "last_seq": run_row[2],
                }
                rows = conn.execute(
                    "select type, event_json from events where run_id = ? order by seq",
                    (run_row[0],),
                ).fetchall()
                for event_type, event_json in rows:
                    event_types.append(event_type)
                    payload = json.loads(event_json)
                    if event_type == "tool.invocation_completed":
                        entry = {
                            "tool_name": payload.get("tool_name"),
                            "outcome": payload.get("outcome"),
                        }
                        observation = payload.get("observation") or ""
                        if "<offload>" in observation:
                            match = re.search(r"<offload>(.*?)</offload>", observation, re.S)
                            if match:
                                try:
                                    offload = json.loads(html.unescape(match.group(1)))
                                    entry["offload_status"] = offload.get("status")
                                    if offload.get("result_path"):
                                        offload_paths.append(offload["result_path"])
                                except json.JSONDecodeError:
                                    entry["offload_status"] = "parse_failed"
                        tool_events.append(entry)

    checks = {
        "process_exited": "E2E 完成" in transcript,
        "file_written": output_path.exists()
        and output_path.read_text(encoding="utf-8") == "alpha\nbeta\n",
        "renderer_showed_read_file": "✔ read_file" in transcript,
        "renderer_showed_shell_exit": "✔ shell exit 0" in transcript,
        "renderer_showed_offload": "offload:" in transcript,
        "run_succeeded": run_summary.get("status") == "succeeded",
        "tools_completed": {"write_file", "read_file", "shell"}.issubset(
            {item.get("tool_name") for item in tool_events if item.get("outcome") == "succeeded"}
        ),
        "offload_file_exists": bool(offload_paths)
        and all(Path(path).exists() for path in offload_paths),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "workspace": str(workspace),
        "home": str(home),
        "db_path": str(db_path),
        "screenshot_path": screenshot_path,
        "output_file": str(output_path),
        "run": run_summary,
        "event_types": event_types,
        "tool_events": tool_events,
        "offload_paths": offload_paths,
    }


if __name__ == "__main__":
    raise SystemExit(main())
