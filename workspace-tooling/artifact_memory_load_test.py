#!/usr/bin/env python3
"""Measure resident Artifact Memory Service concurrency with background ingest."""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
from contextlib import closing
import statistics
import subprocess
import time
from datetime import datetime
from typing import Any

import artifact_ingestion as ingestion
import artifact_runtime
import artifact_security as security
from artifact_service_client import post_json


QUERIES = (
    "artifact ownership",
    "provider-neutral artifact memory architecture",
    "loopback Qdrant shadow migration",
    "atomic receipt outbox publication",
    "poison receipt dead letter replay",
    "catalog complete degraded failed generation",
    "current historical reconciliation retry",
    "MCP shared profile artifact exposure",
    "Graphiti pilot approval provenance",
    "stable logical artifact IDs CAS finalization",
    "session packet immutable review events",
    "Service Registry Crossplane Kargo ownership",
    "Kargo sole promoter cutover",
    "airgap zero trust gateway PEP",
    "Keycloak OIDC callback cross cluster",
    "Mosaic telemetry Grafana Loki Tempo",
    "Crossplane IRSA migration",
    "CUE overlay production flip",
    "the dispatcher service coherence gate conflictbench",
    "t0redesign-m1 ValidatingAdmissionPolicy PVC",
    "glpat secrets.env pre-push lint",
    "catalog_current revision_id",
    "HANDOFF 2026 07 17 provider neutral artifact memory",
    "service registry marketplace",
    "roadmap milestone ownership",
)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return round(ordered[index], 3)


def _process(pid: int) -> dict[str, Any]:
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "rss=,%cpu="],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    values = result.stdout.split()
    return {
        "rss_bytes": int(values[0]) * 1024 if values else None,
        "cpu_percent": float(values[1]) if len(values) > 1 else None,
    }


def _small_outbox(runtime: artifact_runtime.ArtifactRuntime) -> str:
    security.require_private_file(runtime.build_manifest)
    build = json.loads(runtime.build_manifest.read_text(encoding="utf-8"))
    digest = str(build["embedding"]["manifest_sha256"])
    target = ingestion._qdrant_target(
        f"url:{runtime.qdrant_url}",
        runtime.qdrant_collection,
        ingestion.DEFAULT_EMBEDDING_MODEL,
        collection_generation=runtime.qdrant_generation,
        embedding_model_digest=digest,
    )
    with closing(sqlite3.connect(
        f"file:{runtime.ingestion_state.resolve()}?mode=ro",
        uri=True,
    )) as connection, connection:
        completed = {
            str(row[0])
            for row in connection.execute(
                "SELECT unit_id FROM sink_units "
                "WHERE sink='qdrant' AND target=? AND status='completed'",
                (target,),
            )
        }
    candidates: list[tuple[int, str]] = []
    for path in runtime.outbox_root.iterdir():
        if not path.is_dir() or not path.name.startswith("skill-event-"):
            continue
        manifest = ingestion.load_outbox_manifest(path)
        unit_ids = {
            str(unit["unit_id"]) for unit in ingestion.iter_outbox_units(path)
        }
        if not unit_ids.issubset(completed):
            candidates.append((int(manifest["counts"]["units"]), path.name))
    if not candidates:
        raise RuntimeError("no skill-event outbox exists for idempotent load test")
    return min(candidates)[1]


def main() -> int:
    runtime = artifact_runtime.load_runtime()

    def invoke(query: str) -> tuple[float, int]:
        started = time.perf_counter()
        result = post_json(
            socket_path=runtime.service_socket_path,
            route="/v1/search",
            payload={"query": query, "limit": 5, "include_history": False},
            timeout=30,
        )
        elapsed = (time.perf_counter() - started) * 1000
        return elapsed, len(result.get("results", []))

    status = post_json(
        socket_path=runtime.service_socket_path,
        route="/v1/status",
        payload={},
    )
    pid = int(status["service"]["pid"])
    before = _process(pid)
    cold_started = time.perf_counter()
    invoke(QUERIES[0])
    cold_ms = (time.perf_counter() - cold_started) * 1000
    levels: dict[str, Any] = {}
    ingestion_result: dict[str, Any] | None = None
    errors: list[str] = []
    for concurrency in (1, 5, 10):
        requests = max(40, concurrency * 10)
        latencies: list[float] = []
        underfilled = 0
        background: concurrent.futures.Future[dict[str, Any]] | None = None
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency + 1
        ) as executor:
            if concurrency == 10:
                background = executor.submit(
                    post_json,
                    socket_path=runtime.service_socket_path,
                    route="/v1/internal/ingest",
                    payload={
                        "outbox_name": _small_outbox(runtime),
                        "batch_size": 4,
                    },
                    timeout=120,
                )
            futures = [
                executor.submit(invoke, QUERIES[index % len(QUERIES)])
                for index in range(requests)
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    elapsed, count = future.result()
                    latencies.append(elapsed)
                    underfilled += int(count < 5)
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}"[:1000])
            if background is not None:
                try:
                    ingestion_result = background.result()
                except Exception as exc:
                    errors.append(f"background:{type(exc).__name__}: {exc}"[:1000])
        levels[str(concurrency)] = {
            "requests": requests,
            "completed": len(latencies),
            "errors": requests - len(latencies),
            "underfilled": underfilled,
            "p50_ms": _percentile(latencies, 0.50) if latencies else None,
            "p95_ms": _percentile(latencies, 0.95) if latencies else None,
            "p99_ms": _percentile(latencies, 0.99) if latencies else None,
            "mean_ms": round(statistics.mean(latencies), 3) if latencies else None,
        }
    after = _process(pid)
    final_status = post_json(
        socket_path=runtime.service_socket_path,
        route="/v1/status",
        payload={},
    )
    passed = (
        not errors
        and cold_ms <= 3000
        and levels["1"]["p95_ms"] <= 750
        and levels["5"]["p95_ms"] <= 750
        and levels["10"]["p95_ms"] <= 1500
        and final_status["qdrant"]["points"] == 37527
        and ingestion_result is not None
        and int(ingestion_result.get("failed", 0)) == 0
    )
    evidence = {
        "schema_version": 1,
        "tested_at": datetime.now().astimezone().isoformat(),
        "passed": passed,
        "cold_ms": round(cold_ms, 3),
        "levels": levels,
        "background_ingestion": ingestion_result,
        "errors": errors,
        "process_before": before,
        "process_after": after,
        "final_points": final_status["qdrant"]["points"],
        "slo_ms": {
            "cold_p95": 3000,
            "warm_p95": 750,
            "ten_readers_plus_ingest_p95": 1500,
        },
    }
    path = (
        runtime.qdrant_admin_key_file.parent / "resident-service-load-evidence.json"
    )
    security.atomic_write_json(path, evidence, replace=True)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
