from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import ensure_dir


class StateStore:
    def __init__(self, data_root: Path) -> None:
        self.data_root = ensure_dir(data_root)
        self.state_dir = ensure_dir(self.data_root / "state")
        self.jobs_dir = ensure_dir(self.state_dir / "jobs")
        self.instances_path = self.state_dir / "instances.json"
        self._lock = threading.RLock()
        self._init_files()

    def _init_files(self) -> None:
        if not self.instances_path.exists():
            self._atomic_write_json(self.instances_path, {})

    def _atomic_write_json(self, path: Path, payload: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def list_instances(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return self._read_json(self.instances_path, {})

    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        instances = self.list_instances()
        return instances.get(instance_id)

    def upsert_instance(self, instance_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            instances = self._read_json(self.instances_path, {})
            instances[instance_id] = payload
            self._atomic_write_json(self.instances_path, instances)

    def create_job(self, job_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            payload = {**payload, "jobId": job_id}
            self._atomic_write_json(self.jobs_dir / f"{job_id}.json", payload)
            self.append_job_log(job_id, "job created")

    def update_job(self, job_id: str, patch: dict[str, Any]) -> None:
        with self._lock:
            path = self.jobs_dir / f"{job_id}.json"
            job = self._read_json(path, {})
            job.update(patch)
            self._atomic_write_json(path, job)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            path = self.jobs_dir / f"{job_id}.json"
            if not path.exists():
                return None
            job = self._read_json(path, {})
            log_path = self.jobs_dir / f"{job_id}.log"
            if log_path.exists():
                job["log"] = log_path.read_text(encoding="utf-8")
            return job

    def append_job_log(self, job_id: str, line: str) -> None:
        with self._lock:
            timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            path = self.jobs_dir / f"{job_id}.log"
            with path.open("a", encoding="utf-8") as fp:
                fp.write(f"[{timestamp}] {line}\n")
