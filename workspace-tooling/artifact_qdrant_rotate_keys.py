#!/usr/bin/env python3
"""Rotate loopback Qdrant keys and prove immediate old-key rejection."""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

import artifact_runtime
import artifact_security as security


def _status(key: str) -> int:
    request = urllib.request.Request(
        "http://127.0.0.1:6343/collections",
        headers={"api-key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0


def main() -> int:
    runtime = artifact_runtime.load_runtime()
    root = runtime.qdrant_admin_key_file.parent
    old_admin = runtime.qdrant_admin_key()
    old_read = runtime.qdrant_read_key()
    new_admin = secrets.token_urlsafe(48)
    new_read = secrets.token_urlsafe(48)
    restore_admin = artifact_runtime.read_secret(
        root / "restore-admin-api-key",
        "restore admin key",
    )
    restore_read = artifact_runtime.read_secret(
        root / "restore-read-only-api-key",
        "restore read-only key",
    )
    security.atomic_write_bytes(
        runtime.qdrant_admin_key_file,
        (new_admin + "\n").encode(),
        replace=True,
    )
    security.atomic_write_bytes(
        runtime.qdrant_read_key_file,
        (new_read + "\n").encode(),
        replace=True,
    )
    security.atomic_write_bytes(
        root / ".env",
        (
            f"QDRANT_API_KEY={new_admin}\n"
            f"QDRANT_READ_ONLY_API_KEY={new_read}\n"
            f"QDRANT_RESTORE_API_KEY={restore_admin}\n"
            f"QDRANT_RESTORE_READ_ONLY_API_KEY={restore_read}\n"
        ).encode(),
        replace=True,
    )
    result = subprocess.run(
        [
            shutil.which("docker") or "/usr/local/bin/docker",
            "compose",
            "--env-file",
            str(root / ".env"),
            "-f",
            str(root / "compose.yaml"),
            "up",
            "-d",
            "--force-recreate",
            "qdrant",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Qdrant key rotation restart failed: "
            + (result.stderr or result.stdout)[-2000:]
        )
    new_status = 0
    for _attempt in range(60):
        new_status = _status(new_read)
        if new_status == 200:
            break
        time.sleep(1)
    evidence = {
        "schema_version": 1,
        "rotated_at": datetime.now().astimezone().isoformat(),
        "passed": (
            new_status == 200
            and _status(new_admin) == 200
            and _status(old_admin) == 401
            and _status(old_read) == 401
        ),
        "new_admin_status": _status(new_admin),
        "new_read_status": new_status,
        "old_admin_status": _status(old_admin),
        "old_read_status": _status(old_read),
        "secret_values_recorded": False,
        "canonical_mutation": "disabled",
    }
    security.atomic_write_json(root / "key-rotation-evidence.json", evidence, replace=True)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence["passed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
