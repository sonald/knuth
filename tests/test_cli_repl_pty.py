"""Real PTY smoke coverage for the interactive CLI input path."""

from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMPT_ENV = {
    "KNUTH_API_KEY": "test-key",
    "KNUTH_BASE_URL": "https://example.test/v1",
    "KNUTH_MODEL": "test-model",
    "UV_CACHE_DIR": "/private/tmp/knuth-uv-cache",
}


def _command() -> list[str]:
    return ["uv", "run", "knuth", "run"]


def _driver_command() -> list[str]:
    return ["uv", "run", "python", "-m", "tests.cli_repl_pty_driver"]


class CliReplPtyTests(unittest.TestCase):
    def test_pipe_exit_remains_line_oriented(self) -> None:
        completed = subprocess.run(
            _command(),
            cwd=ROOT,
            env={**os.environ, **PROMPT_ENV},
            input="/exit\n",
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Knuth agent ready", completed.stdout)

    def test_pty_prompt_ctrl_c_then_exit(self) -> None:
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            _command(),
            cwd=ROOT,
            env={**os.environ, **PROMPT_ENV, "TERM": "xterm-256color"},
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        try:
            output = _read_until(master_fd, b"knuth", timeout=15)
            self.assertIn(b"Knuth agent ready", output)

            os.write(master_fd, b"\x03")
            _read_until(master_fd, b"knuth", timeout=5)
            os.write(master_fd, b"/exit\r")

            try:
                exit_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.fail("knuth run did not exit after prompt Ctrl-C then /exit")
            self.assertEqual(exit_code, 0)
        finally:
            _terminate_process(process)
            os.close(master_fd)


@unittest.skipUnless(
    os.environ.get("KNUTH_PTY_SMOKE") == "1",
    "set KNUTH_PTY_SMOKE=1 to run opt-in interactive PTY smoke tests",
)
class CliReplOptInPtyTests(unittest.TestCase):
    def test_history_navigation_replays_previous_prompt(self) -> None:
        with _spawn_driver() as repl:
            repl.expect(b"knuth")
            repl.send(b"first prompt\r")
            repl.expect(b"fake answer: first prompt")
            repl.clear()
            repl.expect(b"knuth")
            repl.send(b"\x1b[A\r")
            output = repl.expect(b"fake answer: first prompt", occurrence=2)
            self.assertIn(b"fake answer: first prompt", output)
            repl.exit()

    def test_history_down_restores_current_draft(self) -> None:
        with _spawn_driver() as repl:
            repl.expect(b"knuth")
            repl.send(b"history item\r")
            repl.expect(b"fake answer: history item")
            repl.clear()
            repl.expect(b"knuth")
            repl.send(b"draft text\x1b[A\x1b[B\r")
            output = repl.expect(b"fake answer: draft text")
            self.assertIn(b"fake answer: draft text", output)
            repl.exit()

    def test_multiline_escape_enter_inserts_newline(self) -> None:
        with _spawn_driver() as repl:
            repl.expect(b"knuth")
            repl.send(b"line one\x1b\rline two\r")
            output = repl.expect(b"line two")
            self.assertIn(b"line one", output)
            repl.exit()

    def test_slash_completion_can_accept_resume_command(self) -> None:
        with _spawn_driver({"KNUTH_TEST_RUNS": "1"}) as repl:
            repl.expect(b"knuth")
            time.sleep(0.5)
            repl.send(b"/res\t")
            repl.expect(b"/resume")
            repl.send(b"\r")
            output = repl.expect(b"fake answer: resumed")
            self.assertIn(b"fake answer: resumed", output)
            repl.exit()

    def test_reverse_history_search_submits_match(self) -> None:
        with _spawn_driver() as repl:
            repl.expect(b"knuth")
            repl.send(b"alpha search\r")
            repl.expect(b"fake answer: alpha search")
            repl.clear()
            repl.expect(b"knuth")
            repl.send(b"draft\x12alpha\r")
            output = repl.expect(b"fake answer: alpha search", occurrence=2)
            self.assertIn(b"fake answer: alpha search", output)
            repl.exit()

    def test_reverse_history_search_escape_accepts_without_submitting(self) -> None:
        with _spawn_driver() as repl:
            repl.expect(b"knuth")
            repl.send(b"escape search\r")
            repl.expect(b"fake answer: escape search")
            repl.clear()
            repl.expect(b"knuth")
            repl.send(b"draft\x12escape")
            time.sleep(0.2)
            repl.send(b"\x1b")
            time.sleep(0.2)
            repl.send(b" edited\r")
            output = repl.expect(b"fake answer: escape search edited")
            self.assertIn(b"fake answer: escape search edited", output)
            repl.exit()

    def test_reverse_history_search_tab_accepts_without_submitting(self) -> None:
        with _spawn_driver() as repl:
            repl.expect(b"knuth")
            repl.send(b"tab search\r")
            repl.expect(b"fake answer: tab search")
            repl.clear()
            repl.expect(b"knuth")
            repl.send(b"draft\x12tab\t edited\r")
            output = repl.expect(b"fake answer: tab search edited")
            self.assertIn(b"fake answer: tab search edited", output)
            repl.exit()

    def test_reverse_history_search_ctrl_c_restores_draft(self) -> None:
        with _spawn_driver() as repl:
            repl.expect(b"knuth")
            repl.send(b"cancel search\r")
            repl.expect(b"fake answer: cancel search")
            repl.clear()
            repl.expect(b"knuth")
            repl.send(b"draft\x12cancel\x03 edited\r")
            output = repl.expect(b"fake answer: draft edited")
            self.assertIn(b"fake answer: draft edited", output)
            repl.exit()

    def test_approval_ctrl_c_leaves_run_waiting(self) -> None:
        with _spawn_driver() as repl:
            repl.expect(b"knuth")
            repl.send(b"approval\r")
            repl.expect(b"approve read_file?")
            repl.send(b"\x03")
            output = repl.expect(b"approval-1")
            self.assertIn(b"Input closed; leaving the run waiting", output)
            self.assertIn(b"approval-1", output)
            repl.exit()


class _PtyRepl:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        master_fd: int,
        home: tempfile.TemporaryDirectory[str] | None = None,
    ) -> None:
        self.process = process
        self.master_fd = master_fd
        self.home = home
        self.output = b""

    def __enter__(self) -> "_PtyRepl":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            _terminate_process(self.process)
            os.close(self.master_fd)
        finally:
            if self.home is not None:
                self.home.cleanup()

    def send(self, data: bytes) -> None:
        os.write(self.master_fd, data)

    def clear(self) -> None:
        self.output = b""

    def expect(
        self, needle: bytes, *, timeout: float = 10, occurrence: int = 1
    ) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.output.count(needle) >= occurrence:
                return self.output
            readable, _, _ = select.select([self.master_fd], [], [], 0.1)
            if not readable:
                continue
            try:
                chunk = os.read(self.master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.output += chunk
        raise AssertionError(
            f"did not see {needle!r}; tail={self.output[-2000:]!r}"
        )

    def exit(self) -> None:
        self.send(b"/exit\r")
        try:
            exit_code = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("driver did not exit after /exit") from exc
        if exit_code != 0:
            raise AssertionError(f"driver exited with {exit_code}")


def _spawn_driver(extra_env: dict[str, str] | None = None) -> _PtyRepl:
    home = tempfile.TemporaryDirectory()
    master_fd, slave_fd = pty.openpty()
    env = {
        **os.environ,
        **PROMPT_ENV,
        "TERM": "xterm-256color",
        "HOME": home.name,
        **(extra_env or {}),
    }
    process = subprocess.Popen(
        _driver_command(),
        cwd=ROOT,
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
    )
    os.close(slave_fd)
    return _PtyRepl(process, master_fd, home)


def _read_until(fd: int, needle: bytes, *, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.1)
        if not readable:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        output = b"".join(chunks)
        if needle in output:
            return output
    return b"".join(chunks)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
