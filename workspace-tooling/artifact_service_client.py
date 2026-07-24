#!/usr/bin/env python3
"""Bounded Unix-domain-socket client for the resident Artifact Memory Service."""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any

import artifact_runtime


MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Wire-protocol contract. These MUST track artifact_memory_service; the service
# test suite asserts equality so a drifting copy cannot ship silently. They are
# duplicated rather than imported because importing the service module would
# pull the resident retrieval/ingestion stack into every thin client.
PROTOCOL_VERSION = 1
PROTOCOL_HEADER = "X-Artifact-Memory-Protocol"
UPGRADE_REQUIRED_STATUS = 426


class ArtifactServiceError(RuntimeError):
    """The local service rejected or could not complete a request."""


class ArtifactServiceProtocolError(ArtifactServiceError):
    """The running service no longer serves this client's wire protocol."""


class _UnixHTTPConnection(http.client.HTTPConnection):
    """A stdlib HTTP connection whose transport is one Unix-domain socket."""

    def __init__(self, socket_path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self.socket_path))
        except BaseException:
            connection.close()
            raise
        self.sock = connection


def post_json(
    *,
    socket_path: Path,
    route: str,
    payload: dict[str, Any],
    timeout: float = 30.0,
) -> dict[str, Any]:
    try:
        socket_path = artifact_runtime.require_private_socket(
            socket_path,
            "Artifact Memory Service socket",
        )
    except artifact_runtime.RuntimeConfigError as exc:
        raise ArtifactServiceError(f"service socket is unavailable: {exc}") from exc
    if not route.startswith("/v1/") or "?" in route or "#" in route:
        raise ArtifactServiceError("service route is not allowed")
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 1024 * 1024:
        raise ArtifactServiceError("service request exceeds 1 MiB")
    connection = _UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request(
            "POST",
            route,
            body=encoded,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "workspace-artifact-service-client/1",
                "Connection": "close",
                PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        # Fence both directions before trusting the body: the service refuses
        # us with 426, and a service that still serves us but announces a
        # different protocol is equally unsafe to parse. A missing header is an
        # older service and stays acceptable until the rollout tightens.
        served = response.getheader(PROTOCOL_HEADER)
        if (
            response.status == UPGRADE_REQUIRED_STATUS
            or (served is not None and served.strip() != str(PROTOCOL_VERSION))
        ):
            raise ArtifactServiceProtocolError(
                f"artifact memory service was upgraded (service protocol "
                f"{served or 'unknown'}, client speaks {PROTOCOL_VERSION}); "
                "restart this process to load the current client"
            )
        if response.status != 200:
            detail = raw[:4096].decode("utf-8", errors="replace")
            raise ArtifactServiceError(
                f"service rejected request with HTTP {response.status}: {detail[:1000]}"
            )
    except ArtifactServiceError:
        raise
    except (http.client.HTTPException, TimeoutError, OSError) as exc:
        raise ArtifactServiceError(f"service unavailable: {exc}") from exc
    finally:
        connection.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ArtifactServiceError("service response exceeds 2 MiB")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactServiceError("service returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ArtifactServiceError("service response must be a JSON object")
    return result
