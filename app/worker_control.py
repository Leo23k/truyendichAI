"""Safe, local-only controls for worker processes started by this web app."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse

import httpx

from app.config import PROJECT_ROOT, settings
from sync.service import worker_process_lock


class WorkerProcessManager:
    """Never terminates a worker it did not start itself."""

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen | None] = {"crawler": None, "translator": None}
        self._last_errors: dict[str, str | None] = {"crawler": None, "translator": None}

    def _translator_port(self) -> int:
        parsed = urlparse(settings.model_service_url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)

    def _external_running(self, name: str) -> bool:
        if name == "crawler":
            with worker_process_lock() as acquired:
                return not acquired
        try:
            response = httpx.get(f"{settings.model_service_url.rstrip('/')}/health", timeout=1, trust_env=False)
            return response.is_success
        except httpx.HTTPError:
            return False

    def status(self, name: str) -> dict[str, object]:
        process = self._processes[name]
        if process and process.poll() is None:
            return {"name": name, "state": "running", "pid": process.pid, "managed": True, "error": None}
        if process and process.poll() is not None:
            self._last_errors[name] = f"Process exited with code {process.returncode}."
            self._processes[name] = None
        if self._external_running(name):
            return {"name": name, "state": "running", "pid": None, "managed": False, "error": None}
        error = self._last_errors[name]
        return {"name": name, "state": "error" if error else "stopped", "pid": None, "managed": False, "error": error}

    def statuses(self) -> dict[str, dict[str, object]]:
        return {name: self.status(name) for name in self._processes}

    def _command(self, name: str) -> list[str]:
        if name == "crawler":
            return [sys.executable, "sync/service.py", "watch", "--interval", "10"]
        return [sys.executable, "-m", "uvicorn", "infra.translator.service:app", "--host", "127.0.0.1", "--port", str(self._translator_port())]

    def start(self, name: str) -> tuple[bool, str]:
        current = self.status(name)
        if current["state"] == "running":
            return True, f"{name.title()} đang chạy; không tạo process trùng."
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        try:
            log = (log_dir / f"{name}-worker.log").open("ab")
            self._processes[name] = subprocess.Popen(
                self._command(name), cwd=PROJECT_ROOT, env=os.environ.copy(),
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
            self._last_errors[name] = None
            return True, f"Đã khởi động {name.title()}."
        except OSError as exc:
            self._last_errors[name] = str(exc)
            return False, f"Không thể khởi động {name.title()}: {exc}"

    def stop(self, name: str) -> tuple[bool, str]:
        process = self._processes[name]
        if not process or process.poll() is not None:
            if self._external_running(name):
                return False, f"{name.title()} hiện được mở ngoài giao diện; để tránh ảnh hưởng job đang chạy, web không dừng process này."
            return True, f"{name.title()} đã dừng."
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        self._processes[name] = None
        return True, f"Đã dừng {name.title()}."

    def restart(self, name: str) -> tuple[bool, str]:
        current = self.status(name)
        if current["state"] == "running" and not current["managed"]:
            return False, f"{name.title()} đang được mở ngoài giao diện; không restart để bảo toàn tiến trình hiện tại."
        stopped, message = self.stop(name)
        if not stopped:
            return False, message
        return self.start(name)

    def act(self, target: str, action: str) -> tuple[bool, str]:
        names = ("crawler", "translator") if target == "all" else (target,)
        results = [(getattr(self, action)(name)) for name in names]
        return all(ok for ok, _ in results), " ".join(message for _, message in results)


worker_manager = WorkerProcessManager()
