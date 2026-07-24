#!/usr/bin/env python3
"""Exercise Qdrant outage recovery and atomic service-side rollback."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime
from typing import Any

import artifact_runtime
import artifact_security as security
from artifact_service_client import ArtifactServiceError, post_json


def main() -> int:
    runtime = artifact_runtime.load_runtime()
    root = runtime.qdrant_admin_key_file.parent

    def compose(*arguments: str) -> None:
        result = subprocess.run(
            [
                shutil.which("docker") or "/usr/local/bin/docker",
                "compose",
                "--env-file",
                str(root / ".env"),
                "-f",
                str(root / "compose.yaml"),
                *arguments,
            ],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "compose failed: " + (result.stderr or result.stdout)[-1000:]
            )

    def call(route: str, body: dict[str, Any]) -> dict[str, Any]:
        return post_json(
            socket_path=runtime.service_socket_path,
            route=route,
            payload=body,
            timeout=30,
        )

    baseline = call("/v1/status", {})
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "tested_at": datetime.now().astimezone().isoformat(),
        "baseline_points": baseline["qdrant"]["points"],
    }
    try:
        compose("stop", "qdrant")
        unavailable = None
        for _attempt in range(20):
            unavailable = call("/v1/status", {})
            if unavailable["qdrant"]["available"] is False:
                break
            time.sleep(0.25)
        search_failed_closed = False
        try:
            call("/v1/search", {"query": "artifact ownership", "limit": 3})
        except ArtifactServiceError:
            search_failed_closed = True
        evidence["outage"] = {
            "service_status_available": unavailable["service"]["available"],
            "qdrant_available": unavailable["qdrant"]["available"],
            "search_failed_closed": search_failed_closed,
            "consumer_status_preserved": unavailable["skill_receipts"][
                "consumer_status"
            ],
        }
        compose("up", "-d", "qdrant")
        recovered = None
        for _attempt in range(60):
            try:
                recovered = call("/v1/status", {})
                if recovered["qdrant"]["available"]:
                    break
            except ArtifactServiceError:
                pass
            time.sleep(0.5)
        recovered_search = call(
            "/v1/search",
            {"query": "artifact ownership", "limit": 3},
        )
        evidence["recovery"] = {
            "qdrant_available": recovered["qdrant"]["available"],
            "points": recovered["qdrant"]["points"],
            "search_results": len(recovered_search["results"]),
        }

        to_embedded = artifact_runtime.update_active_backend("embedded")
        embedded_search = call(
            "/v1/search",
            {"query": "artifact ownership", "limit": 3},
        )
        embedded_status = call("/v1/status", {})
        to_server = artifact_runtime.update_active_backend("server")
        server_search = call(
            "/v1/search",
            {"query": "artifact ownership", "limit": 3},
        )
        server_status = call("/v1/status", {})
        evidence["rollback_rehearsal"] = {
            "to_embedded": to_embedded,
            "embedded_active": embedded_status["service"]["active_backend"],
            "embedded_results": len(embedded_search["results"]),
            "to_server": to_server,
            "server_active": server_status["service"]["active_backend"],
            "server_results": len(server_search["results"]),
            "canonical_mutation": "disabled",
        }
        evidence["passed"] = all(
            (
                evidence["outage"]["service_status_available"],
                evidence["outage"]["qdrant_available"] is False,
                evidence["outage"]["search_failed_closed"],
                evidence["recovery"]["qdrant_available"],
                evidence["recovery"]["points"] == baseline["qdrant"]["points"],
                evidence["recovery"]["search_results"] == 3,
                evidence["rollback_rehearsal"]["embedded_active"] == "embedded",
                evidence["rollback_rehearsal"]["server_active"] == "server",
            )
        )
    finally:
        artifact_runtime.update_active_backend("server")
        compose("up", "-d", "qdrant")
    security.atomic_write_json(
        root / "resilience-evidence.json",
        evidence,
        replace=True,
    )
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
