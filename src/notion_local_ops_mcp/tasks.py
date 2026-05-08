from __future__ import annotations

import json
import shutil
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


# Statuses that indicate a task is not yet terminal from the perspective of
# the persisted meta.json. After a server restart these are de-facto stale
# and should be reaped to ``abandoned`` so consumers polling get_task /
# wait_task don't see a zombie status forever.
NON_TERMINAL_STATUSES = frozenset({"queued", "running"})


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

    def reap_stale_running_tasks(
        self,
        *,
        reason: str = "server_restart",
    ) -> dict[str, object]:
        """Mark every persisted ``queued`` / ``running`` task as ``abandoned``.

        Worker threads run as ``daemon=True`` and there is no ``try/finally``
        guarantee that ``meta.json`` is updated when the MCP process exits
        abruptly (launchd kill, OOM, supervisor reload, uncaught exception).
        Without this reaper the meta would remain stuck on ``running`` forever,
        so consumers polling ``get_task`` / ``wait_task`` see a zombie status.

        Call this once at server startup. It is also safe to call manually.
        """
        tasks_root = self.root / "tasks"
        reaped_ids: list[str] = []
        scanned = 0
        with self._lock:
            if not tasks_root.exists():
                return {
                    "success": True,
                    "scanned": 0,
                    "reaped": 0,
                    "task_ids": [],
                    "reason": reason,
                }
            for task_dir in sorted(tasks_root.iterdir()):
                if not task_dir.is_dir():
                    continue
                scanned += 1
                meta_path = task_dir / "meta.json"
                try:
                    payload = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    # Corrupt meta is dealt with by purge, not reap.
                    continue
                if payload.get("status") not in NON_TERMINAL_STATUSES:
                    continue
                payload["status"] = "abandoned"
                payload["abandoned_reason"] = reason
                payload["updated_at"] = _now()
                self._write_text(meta_path, json.dumps(payload, indent=2))
                reaped_ids.append(task_dir.name)

        return {
            "success": True,
            "scanned": scanned,
            "reaped": len(reaped_ids),
            "task_ids": reaped_ids,
            "reason": reason,
        }

    def purge_tasks(
        self,
        *,
        older_than_seconds: float,
        dry_run: bool = False,
        statuses: Iterable[str] | None = None,
    ) -> dict[str, object]:
        """Remove task directories whose ``updated_at`` is older than the threshold.

        When ``statuses`` is provided, only tasks whose ``status`` is in the
        whitelist are eligible. This lets callers narrow purges to e.g.
        ``cancelled`` / ``failed`` / ``abandoned`` so successful task records
        can be retained on a different schedule.
        """
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=max(float(older_than_seconds), 0.0))
        tasks_root = self.root / "tasks"
        scanned = 0
        purged = 0
        purged_ids: list[str] = []
        status_filter: frozenset[str] | None = (
            frozenset(statuses) if statuses else None
        )

        with self._lock:
            if not tasks_root.exists():
                return {
                    "success": True,
                    "scanned": 0,
                    "purged": 0,
                    "task_ids": [],
                    "dry_run": dry_run,
                    "cutoff": cutoff.isoformat(),
                    "statuses": sorted(status_filter) if status_filter else None,
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
                    age_ok = updated_at < cutoff
                    if status_filter is not None:
                        status_ok = str(payload.get("status") or "") in status_filter
                    else:
                        status_ok = True
                    should_purge = age_ok and status_ok
                except Exception:
                    # Corrupt/partial task directories can be cleaned up as stale,
                    # unless the caller has restricted purges to specific statuses.
                    should_purge = status_filter is None

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
            "statuses": sorted(status_filter) if status_filter else None,
        }
