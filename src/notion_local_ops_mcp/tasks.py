from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path


class TaskNotFoundError(FileNotFoundError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task not found: {task_id}")
        self.task_id = task_id


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks_root = self.root / "tasks"
        self.tasks_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _task_dir(self, task_id: str) -> Path:
        return self.tasks_root / task_id

    def _meta_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "meta.json"

    def _stdout_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "stdout.log"

    def _stderr_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "stderr.log"

    def _summary_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "summary.txt"

    def _write_text(self, path: Path, content: str) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)

    def _ensure_task_exists(self, task_id: str) -> None:
        if not self._meta_path(task_id).exists():
            raise TaskNotFoundError(task_id)

    def create(
        self,
        *,
        task: str,
        executor: str,
        cwd: str,
        timeout: int | None = None,
        context_files: list[str] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        with self._lock:
            task_id = uuid.uuid4().hex[:12]
            task_dir = self._task_dir(task_id)
            task_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "task_id": task_id,
                "task": task,
                "executor": executor,
                "cwd": cwd,
                "timeout": timeout,
                "context_files": context_files or [],
                "status": "queued",
                "created_at": _now(),
                "updated_at": _now(),
                "error": None,
            }
            if metadata:
                payload.update(metadata)
            self._write_text(self._meta_path(task_id), json.dumps(payload, indent=2))
            self._write_text(self._stdout_path(task_id), "")
            self._write_text(self._stderr_path(task_id), "")
            self._write_text(self._summary_path(task_id), "")
            return payload

    def get_optional(self, task_id: str) -> dict[str, object] | None:
        with self._lock:
            if not self._meta_path(task_id).exists():
                return None
            return json.loads(self._meta_path(task_id).read_text(encoding="utf-8"))

    def get(self, task_id: str) -> dict[str, object]:
        payload = self.get_optional(task_id)
        if payload is None:
            raise TaskNotFoundError(task_id)
        return payload

    def update(self, task_id: str, **fields: object) -> dict[str, object]:
        with self._lock:
            payload = self.get(task_id)
            payload.update(fields)
            payload["updated_at"] = _now()
            self._write_text(self._meta_path(task_id), json.dumps(payload, indent=2))
            return payload

    def write_logs(self, task_id: str, *, stdout: str, stderr: str) -> None:
        with self._lock:
            self._ensure_task_exists(task_id)
            self._write_text(self._stdout_path(task_id), stdout)
            self._write_text(self._stderr_path(task_id), stderr)

    def append_logs(self, task_id: str, *, stdout: str = "", stderr: str = "") -> None:
        with self._lock:
            self._ensure_task_exists(task_id)
            if stdout:
                with self._stdout_path(task_id).open("a", encoding="utf-8") as handle:
                    handle.write(stdout)
            if stderr:
                with self._stderr_path(task_id).open("a", encoding="utf-8") as handle:
                    handle.write(stderr)

    def write_summary(self, task_id: str, summary: str) -> None:
        with self._lock:
            self._ensure_task_exists(task_id)
            self._write_text(self._summary_path(task_id), summary)

    def read_stdout(self, task_id: str) -> str:
        with self._lock:
            self._ensure_task_exists(task_id)
            return self._stdout_path(task_id).read_text(encoding="utf-8")

    def read_stderr(self, task_id: str) -> str:
        with self._lock:
            self._ensure_task_exists(task_id)
            return self._stderr_path(task_id).read_text(encoding="utf-8")

    def read_summary(self, task_id: str) -> str:
        with self._lock:
            self._ensure_task_exists(task_id)
            return self._summary_path(task_id).read_text(encoding="utf-8").strip()

    def list_tasks(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        executor: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            items: list[dict[str, object]] = []
            if self.tasks_root.exists():
                for task_dir in self.tasks_root.iterdir():
                    meta_path = task_dir / "meta.json"
                    if not meta_path.is_file():
                        continue
                    payload = json.loads(meta_path.read_text(encoding="utf-8"))
                    if status and payload.get("status") != status:
                        continue
                    if executor and payload.get("executor") != executor:
                        continue
                    items.append(payload)

            items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
            normalized_offset = max(offset, 0)
            normalized_limit = max(limit, 0)
            paged = items[normalized_offset : normalized_offset + normalized_limit]
            return {
                "items": paged,
                "total": len(items),
                "offset": normalized_offset,
                "limit": normalized_limit,
                "has_more": normalized_offset + normalized_limit < len(items),
            }

    def recover_incomplete(self, summary_message: str = "server restarted before task completion") -> list[dict[str, object]]:
        recovered: list[dict[str, object]] = []
        with self._lock:
            if not self.tasks_root.exists():
                return recovered
            for task_dir in self.tasks_root.iterdir():
                meta_path = task_dir / "meta.json"
                if not meta_path.is_file():
                    continue
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
                if payload.get("status") not in {"queued", "running"}:
                    continue
                payload["status"] = "interrupted"
                payload["error"] = {
                    "code": "task_interrupted",
                    "message": summary_message,
                }
                payload["updated_at"] = _now()
                self._write_text(meta_path, json.dumps(payload, indent=2))
                summary_path = task_dir / "summary.txt"
                existing_summary = summary_path.read_text(encoding="utf-8").strip() if summary_path.exists() else ""
                merged_summary = summary_message if not existing_summary else f"{existing_summary}\n{summary_message}"
                self._write_text(summary_path, merged_summary)
                recovered.append(payload)
        return recovered
