from __future__ import annotations

import json
import shutil
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.RLock()

    def _task_dir(self, task_id: str) -> Path:
        return self.root / "tasks" / task_id

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
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        temp_path.replace(path)

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
            try:
                task_dir.parent.chmod(0o700)
                task_dir.chmod(0o700)
            except OSError:
                pass
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
            }
            if metadata:
                payload.update(metadata)
            self._write_text(self._meta_path(task_id), json.dumps(payload, indent=2))
            self._write_text(self._stdout_path(task_id), "")
            self._write_text(self._stderr_path(task_id), "")
            self._write_text(self._summary_path(task_id), "")
            return payload

    def get(self, task_id: str) -> dict[str, object]:
        with self._lock:
            return json.loads(self._meta_path(task_id).read_text(encoding="utf-8"))

    def update(self, task_id: str, **fields: object) -> dict[str, object]:
        with self._lock:
            payload = self.get(task_id)
            payload.update(fields)
            payload["updated_at"] = _now()
            self._write_text(self._meta_path(task_id), json.dumps(payload, indent=2))
            return payload

    def write_logs(self, task_id: str, *, stdout: str, stderr: str) -> None:
        with self._lock:
            self._write_text(self._stdout_path(task_id), stdout)
            self._write_text(self._stderr_path(task_id), stderr)

    def append_logs(self, task_id: str, *, stdout: str = "", stderr: str = "") -> None:
        """Append incremental output to log files (no temp-file dance)."""
        with self._lock:
            if stdout:
                with self._stdout_path(task_id).open("a", encoding="utf-8") as f:
                    f.write(stdout)
            if stderr:
                with self._stderr_path(task_id).open("a", encoding="utf-8") as f:
                    f.write(stderr)

    def write_summary(self, task_id: str, summary: str) -> None:
        with self._lock:
            self._write_text(self._summary_path(task_id), summary)

    def read_stdout(self, task_id: str) -> str:
        with self._lock:
            return self._stdout_path(task_id).read_text(encoding="utf-8")

    def read_stderr(self, task_id: str) -> str:
        with self._lock:
            return self._stderr_path(task_id).read_text(encoding="utf-8")

    def read_summary(self, task_id: str) -> str:
        with self._lock:
            return self._summary_path(task_id).read_text(encoding="utf-8").strip()

    def purge_tasks(self, *, older_than_seconds: float, dry_run: bool = False) -> dict[str, object]:
        """Remove task directories whose ``updated_at`` is older than the threshold."""
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=max(float(older_than_seconds), 0.0))
        tasks_root = self.root / "tasks"
        scanned = 0
        purged = 0
        purged_ids: list[str] = []

        with self._lock:
            if not tasks_root.exists():
                return {
                    "success": True,
                    "scanned": 0,
                    "purged": 0,
                    "task_ids": [],
                    "dry_run": dry_run,
                    "cutoff": cutoff.isoformat(),
                }
            for task_dir in sorted(tasks_root.iterdir()):
                if not task_dir.is_dir():
                    continue
                scanned += 1
                task_id = task_dir.name
                meta_path = task_dir / "meta.json"
                should_purge = False
                try:
                    payload = json.loads(meta_path.read_text(encoding="utf-8"))
                    updated_raw = str(payload.get("updated_at") or payload.get("created_at") or "")
                    updated_at = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=UTC)
                    should_purge = updated_at < cutoff
                except Exception:
                    # Corrupt/partial task directories can be cleaned up as stale.
                    should_purge = True

                if not should_purge:
                    continue
                purged += 1
                purged_ids.append(task_id)
                if not dry_run:
                    shutil.rmtree(task_dir, ignore_errors=True)

        return {
            "success": True,
            "scanned": scanned,
            "purged": purged,
            "task_ids": purged_ids,
            "dry_run": dry_run,
            "cutoff": cutoff.isoformat(),
        }
