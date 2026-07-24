#!/usr/bin/env python3
"""Fail-closed, stdlib schema-bundle integrity check for milestone pipeline v2.

This checks the contract properties CI can enforce without downloading a JSON
Schema implementation: complete bundle membership, local reference resolution,
strict object declarations, and load-bearing field/enum sentinels. When the
optional `jsonschema` package is already available, it also compiles every
document against Draft 2020-12.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
EXPECTED = {
    "milestone-pipeline-v2-definitions.schema.json",
    "milestone-pipeline-state-v2.schema.json",
    "milestone-review-manifest-v2.schema.json",
    "milestone-implementation-evidence-v2.schema.json",
    "milestone-trust-policy-v2.schema.json",
    "milestone-publication-intent-v2.schema.json",
    "milestone-release-manifest-v2.schema.json",
    "milestone-operations-plan-v2.schema.json",
    "milestone-operations-evidence-v2.schema.json",
    "milestone-waivers-v2.schema.json",
}


def _strict_format_checker():
    from jsonschema import FormatChecker  # type: ignore[import-not-found]

    checker = FormatChecker()

    @checker.checks("date-time", raises=(TypeError, ValueError))
    def valid_date_time(value: object) -> bool:
        if not isinstance(value, str) or "T" not in value:
            return False
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed.utcoffset() is not None

    return checker


def _pointer(document: Any, fragment: str) -> Any:
    current = document
    if fragment in {"", "#"}:
        return current
    if not fragment.startswith("#/"):
        raise KeyError(f"unsupported JSON pointer fragment {fragment!r}")
    for raw in fragment[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(token)]
        else:
            current = current[token]
    return current


def _walk(value: Any, path: str = "$"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}/{index}")


def validate_bundle(documents: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    names = set(documents)
    if names != EXPECTED:
        errors.append(f"schema set mismatch: missing={sorted(EXPECTED - names)} extra={sorted(names - EXPECTED)}")
    for name, document in sorted(documents.items()):
        if document.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            errors.append(f"{name}: wrong or missing Draft 2020-12 marker")
        if document.get("$id") != name:
            errors.append(f"{name}: $id must equal filename")
        for path, value in _walk(document):
            if not isinstance(value, dict):
                continue
            if (value.get("type") == "object" and "additionalProperties" not in value
                    and value.get("x-abstract") is not True):
                errors.append(f"{name}:{path}: object schema omits additionalProperties policy")
            ref = value.get("$ref")
            if isinstance(ref, str):
                target_name, sep, fragment = ref.partition("#")
                target_name = target_name or name
                target = documents.get(target_name)
                if target is None:
                    errors.append(f"{name}:{path}: missing local ref target {target_name!r}")
                    continue
                try:
                    _pointer(target, f"#{fragment}" if sep else "")
                except (KeyError, IndexError, ValueError) as exc:
                    errors.append(f"{name}:{path}: unresolved ref {ref!r}: {exc}")

    try:
        review = documents["milestone-review-manifest-v2.schema.json"]
        receipt = review["$defs"]["reviewReceipt"]
        required = set(receipt["required"])
        if not {"agent_task_id", "agent_body_snapshot_path"} <= required:
            errors.append("review schema: runtime task id/body snapshot are not required")
        implementation = documents["milestone-implementation-evidence-v2.schema.json"]
        repositories = implementation["allOf"][1]["properties"]["repositories"]
        if repositories.get("minItems") != 1 or repositories.get("maxItems") != 1:
            errors.append("implementation schema: v2.0 single-source repo cardinality is not exact")
        release = documents["milestone-release-manifest-v2.schema.json"]
        if "remote" not in release["$defs"]["renderedRevision"]["required"]:
            errors.append("release schema: rendered revision remote is not required")
        method = release["$defs"]["publicationVerification"]["properties"]["method"]
        if method.get("const") != "git-ls-remote+exact-commit":
            errors.append("release schema: reproducible remote verification method is not fixed")
        operations = documents["milestone-operations-evidence-v2.schema.json"]
        for definition in ("authorization", "apply", "verification", "probe"):
            if definition not in operations.get("$defs", {}):
                errors.append(f"operations schema: missing strict {definition} definition")
        state = documents["milestone-pipeline-state-v2.schema.json"]
        impl_status = state["properties"]["implementation_status"]["enum"]
        if "not_required" in impl_status:
            errors.append("state schema: implementation status still conflates publication not-required")
    except (KeyError, IndexError, TypeError) as exc:
        errors.append(f"schema sentinel lookup failed: {exc}")
    return errors


def load_bundle() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(SCHEMA_DIR.glob("milestone-*.schema.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"{path}: cannot parse schema: {exc}")
        if not isinstance(value, dict):
            raise SystemExit(f"{path}: schema root must be an object")
        result[path.name] = value
    return result


def optional_meta_validate(
    documents: dict[str, dict[str, Any]], *, required: bool = False
) -> list[str]:
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-not-found]
    except ImportError:
        return ["jsonschema is required for meta-schema validation"] if required else []
    errors: list[str] = []
    for name, document in sorted(documents.items()):
        try:
            Draft202012Validator.check_schema(document)
        except Exception as exc:  # jsonschema exposes several validation subclasses
            errors.append(f"{name}: Draft 2020-12 meta-schema rejection: {exc}")
    return errors


def representative_instances() -> dict[str, dict[str, Any]]:
    sha = "a" * 64
    commit = "b" * 40
    timestamp = "2026-07-12T00:00:00Z"
    producer = {"kind": "deterministic-tool", "name": "fixture", "provider": "local"}
    deterministic_producer = {
        "kind": "deterministic-tool", "name": "milestone-pipeline-artifacts.py",
        "provider": "local", "version": sha,
    }
    evidence = {
        "path": "artifacts/checks/fixture.json", "sha256": sha,
        "media_type": "application/json", "size_bytes": 2,
        "collector": "fixture", "command": "tool check",
    }
    receipt_base = {
        "stage": "assessment", "provider": "codex", "model": None,
        "agent_body_path": "data/agents/milestone-adversary.md",
        "agent_body_snapshot_path": "artifacts/reviews/milestone-adversary-task-a-agent.md",
        "agent_body_sha256": sha, "agent_kit_commit": commit,
        "workspace_root": "/workspace", "agent_task_id": "task-a",
        "prompt_path": "artifacts/reviews/milestone-adversary-task-a-prompt.md",
        "prompt_sha256": sha, "critique_path": "docs/a.md", "critique_sha256": sha,
        "reviewed_base": commit, "reviewed_head": commit,
        "reviewed_remote_url": "ssh://git.example/repo.git",
        "started_at": timestamp, "completed_at": timestamp, "verdict": "SHIP",
        "check_evidence_refs": [], "check_attempt_refs": [],
        "findings_register_sha256": None,
        "assessment_manifest_sha256": None, "operations_plan_sha256": None,
        "release_manifest_sha256": None,
        "delivery_requirements_sha256": None,
        "findings_snapshot_path": None, "operations_plan_snapshot_path": None,
        "release_manifest_snapshot_path": None,
    }
    receipt_b = copy.deepcopy(receipt_base)
    receipt_b.update({
        "role": "milestone-delivery-integrity-adversary", "agent_task_id": "task-b",
        "agent_body_path": "data/agents/milestone-delivery-integrity-adversary.md",
        "agent_body_snapshot_path": "artifacts/reviews/milestone-delivery-integrity-adversary-task-b-agent.md",
        "prompt_path": "artifacts/reviews/milestone-delivery-integrity-adversary-task-b-prompt.md",
        "critique_path": "docs/b.md", "critique_sha256": "c" * 64,
    })
    receipt_a = dict(receipt_base, role="milestone-adversary")
    common = {
        "schema_version": 2, "milestone_id": "m1", "generation": 1,
        "created_at": timestamp, "producer": producer,
    }
    binding = {"sha256": sha, "generation": 1, "phase": "plan-reviewed"}
    instances: dict[str, dict[str, Any]] = {
        "milestone-trust-policy-v2.schema.json": {
            "schema_version": 2,
            "source_remote": "https://git.example/source.git",
            "render_remote_prefixes": ["https://git.example/deploy.git"],
            "artifact_registry_prefixes": ["registry.example/workspace"],
            "artifact_resolver": None,
            "automatic_gitops": {
                "kind": "ci-render-argocd-auto-sync-v1",
                "render": {
                    "remote": "https://git.example/deploy.git", "branch": "deploy:dev",
                    "protected": True, "provenance_path": ".workspace/source-revision.json",
                },
                "ci_render": {
                    "provider": "gitlab", "project": "platform/source",
                    "source_ref": "main", "pipeline_source": "push",
                    "config_sha256": sha, "deploy_job": "deploy:dev",
                    "protected_environment": "dev", "writes_only_render_target": True,
                },
                "targets": [{
                    "id": "dev/example/service-registry-api", "environment": "dev",
                    "account": "123", "cluster": "tenant-example",
                    "resource": "Application/service-registry-api",
                    "argocd_application": "service-registry-api",
                    "argocd_server": "https://argocd.example.invalid",
                    "argocd_context": "dev", "argocd_config_path": "/config/argocd.json",
                    "argocd_config_sha256": sha, "certificate_authority_sha256": sha,
                    "argocd_project": "tenant-example",
                    "source_repo_url": "https://git.example/deploy.git",
                    "source_target_revision": "deploy:dev",
                    "source_path": "tenant-example/service-registry-api",
                    "destination_server": "https://kubernetes.example.invalid",
                    "destination_namespace": "service-registry",
                    "verification_action_sha256": sha,
                    "automated": {"enabled": True, "prune": True, "self_heal": True, "allow_empty": False},
                }],
                "cascade_steps": [
                    {"id": "source-publication", "kind": "source-publication", "depends_on": [], "target_id": None},
                    {"id": "ci-render", "kind": "ci-render", "depends_on": ["source-publication"], "target_id": None},
                    {"id": "render-publication", "kind": "render-publication", "depends_on": ["ci-render"], "target_id": None},
                    {"id": "argocd-auto-sync-dev-example-service-registry-api", "kind": "argocd-auto-sync", "depends_on": ["render-publication"], "target_id": "dev/example/service-registry-api"},
                ],
            },
        },
        "milestone-review-manifest-v2.schema.json": {
            **common,
            "reviewed": {"repo": "repo", "base_commit": commit, "head_commit": commit,
                         "diff_sha256": sha, "remote_url": "ssh://git.example/repo.git"},
            "required_reviewers": ["milestone-adversary", "milestone-delivery-integrity-adversary"],
            "reviews": [receipt_a, receipt_b], "closure_reviews": [], "operations_reviews": [],
        },
        "milestone-implementation-evidence-v2.schema.json": {
            **common,
            "repositories": [{"repo": "repo", "path": "/workspace/repo", "base_commit": commit,
                              "head_commit": commit, "commit_range": f"{commit}..{commit}",
                              "commits": [commit], "branch": "dev",
                              "remote_url": "ssh://git.example/repo.git"}],
            "checks": [{"name": "check", "argv": ["tool", "check"], "command": "tool check",
                        "repo_head": commit, "executable_path": "/usr/bin/tool",
                        "executable_sha256": sha, "exit_code": 0, "started_at": timestamp,
                        "completed_at": timestamp, "evidence": evidence}],
            "critique": {"code_review_manifest_sha256": sha, "findings_register_sha256": sha,
                         "gate_exit_code": 0, "checked_at": timestamp,
                         "open_critical": 0, "open_high": 0},
            "rectification": {"commit": commit, "not_required_reason": None,
                              "closure_review_sha256": sha},
            "generated_artifacts": [],
        },
        "milestone-release-manifest-v2.schema.json": {
            **common, "publication_required": False, "not_required_reason": "local only",
            "delivery_kind": "not-required", "source_revisions": [],
            "published_revisions": [], "rendered_revisions": [], "artifacts": [],
        },
        "milestone-publication-intent-v2.schema.json": {
            **common, "producer": deterministic_producer,
            "intent_id": "m1-publication-aaaaaaaaaaaa",
            "scope": {
                "mode": "publish", "repo": "repo", "remote": "origin",
                "remote_url": "https://git.example/repo.git", "branch": "dev",
                "commit": commit, "expected_remote_head": None,
                "git_executable_path": "/usr/bin/git",
                "git_executable_sha256": sha,
                "execution_environment": {
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "HOME": "/workspace/artifacts/publication/isolated-home",
                    "XDG_CONFIG_HOME": "/workspace/artifacts/publication/isolated-home",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/workspace/repo/.git/objects",
                },
                "ssh_known_hosts_path": None, "ssh_known_hosts_sha256": None,
                "isolated_git_dir": "/workspace/artifacts/publication/push-sandbox.git",
                "alternate_object_directory": "/workspace/repo/.git/objects",
                "push_argv": [
                    "/usr/bin/git", "-c", "core.hooksPath=/dev/null",
                    "--git-dir=/workspace/artifacts/publication/push-sandbox.git", "push",
                    "--force-with-lease=refs/heads/dev:", "--",
                    "https://git.example/repo.git", f"{commit}:refs/heads/dev",
                ],
            },
            "scope_hash": sha,
            "precondition": {
                "observed_commit": None, "observed_at": timestamp,
                "evidence": evidence,
            },
            "authorization": {
                "decision": "approved", "by": "Chris Dare",
                "method": "human-explicit", "at": timestamp,
                "scope_hash": sha,
            },
            "superseded_intents": [],
            "execution_attempts": [],
        },
        "milestone-operations-plan-v2.schema.json": {
            **common, "operations_required": False, "not_required_reason": "no target",
            "plan_hash": sha, "max_evidence_age_seconds": 60, "targets": [],
        },
        "milestone-operations-evidence-v2.schema.json": {
            **common, "producer": deterministic_producer, "plan_hash": sha, "targets": [],
        },
        "milestone-waivers-v2.schema.json": {
            **common, "producer": deterministic_producer, "plan_hash": sha, "waivers": [],
        },
        "milestone-pipeline-state-v2.schema.json": {
            "schema_version": 2, "id": "m1", "created_at": timestamp, "updated_at": timestamp,
            "phase": "plan-reviewed", "phase_history": [{"phase": "plan-reviewed", "at": timestamp}],
            "agent_kit_commit": commit, "kit_upgrade_history": [],
            "check_run_head": commit,
            "check_run_hashes": {"artifacts/checks/fixture.json": sha}, "check_run_history": {},
            "check_run_attempts": [{"path": "artifacts/checks/fixture.json", "sha256": sha}],
            "publication_required": True, "operations_required": True,
            "implementation_status": "published", "operational_status": "pending", "review_status": "closed",
            "review_manifest": "artifacts/review-manifest.json",
            "implementation_evidence": "artifacts/implementation-evidence.json",
            "publication_intent": "artifacts/publication-intent.json",
            "release_manifest": "artifacts/release-manifest.json",
            "operations_plan": "artifacts/operations-plan.json",
            "operations_evidence": "artifacts/operations-evidence.json", "waivers": "artifacts/waivers.json",
            "artifact_bindings": {
                "review_manifest": {**binding, "path": "artifacts/review-manifest.json",
                                    "review_hashes": {"milestone-adversary": sha},
                                    "closure_hash": sha, "closure_receipt_hash": sha,
                                    "closure_attempt_hashes": [sha],
                                    "operations_review_hash": sha,
                                    "operations_review_receipt_hash": sha,
                                    "operations_review_attempt_hashes": [sha],
                                    "immutable_root_hash": sha},
                "implementation_evidence": {**binding, "path": "artifacts/implementation-evidence.json"},
                "publication_intent": {
                    **binding, "path": "artifacts/publication-intent.json",
                    "scope_hash": sha, "authorization_hash": sha,
                    "supersession_hashes": [], "execution_hashes": [],
                },
                "release_manifest": {**binding, "path": "artifacts/release-manifest.json"},
                "operations_plan": {**binding, "path": "artifacts/operations-plan.json", "plan_hash": sha},
            },
        },
    }
    publication = instances["milestone-publication-intent-v2.schema.json"]
    publication_scope_hash = hashlib.sha256(json.dumps(
        publication["scope"], sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    publication["scope_hash"] = publication_scope_hash
    publication["authorization"]["scope_hash"] = publication_scope_hash
    operations_plan = instances["milestone-operations-plan-v2.schema.json"]
    operations_plan["plan_hash"] = hashlib.sha256(json.dumps(
        {key: value for key, value in operations_plan.items() if key != "plan_hash"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return instances


def validate_representative_instances(
    documents: dict[str, dict[str, Any]], *, required: bool = False
) -> list[str]:
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-not-found]
        from referencing import Registry, Resource  # type: ignore[import-not-found]
    except ImportError:
        return ["jsonschema/referencing are required for instance validation"] if required else []
    registry = Registry().with_resources([
        (document["$id"], Resource.from_contents(document))
        for document in documents.values()
    ])
    errors: list[str] = []
    for schema_name, instance in representative_instances().items():
        validator = Draft202012Validator(
            documents[schema_name], registry=registry,
            format_checker=_strict_format_checker(),
        )
        for error in validator.iter_errors(instance):
            location = "/".join(str(part) for part in error.absolute_path) or "$"
            errors.append(f"{schema_name} representative instance at {location}: {error.message}")
    return errors


def validate_instance(
    documents: dict[str, dict[str, Any]], schema_name: str, instance: Any,
    *, required: bool = False,
) -> list[str]:
    if schema_name not in documents:
        return [f"unknown schema {schema_name!r}"]
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-not-found]
        from referencing import Registry, Resource  # type: ignore[import-not-found]
    except ImportError:
        return ["jsonschema/referencing are required for instance validation"] if required else []
    registry = Registry().with_resources([
        (document["$id"], Resource.from_contents(document))
        for document in documents.values()
    ])
    validator = Draft202012Validator(
        documents[schema_name], registry=registry,
        format_checker=_strict_format_checker(),
    )
    errors: list[str] = []
    for error in validator.iter_errors(instance):
        location = "/".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"{schema_name} instance at {location}: {error.message}")
    errors.extend(_semantic_instance_errors(schema_name, instance))
    return errors


def _semantic_instance_errors(schema_name: str, instance: Any) -> list[str]:
    """Enforce declared cross-field/unique-key constraints Draft 2020-12 cannot express."""
    if not isinstance(instance, dict):
        return []
    errors: list[str] = []
    if schema_name == "milestone-trust-policy-v2.schema.json":
        automatic = instance.get("automatic_gitops")
        if isinstance(automatic, dict):
            targets = automatic.get("targets") or []
            target_rows = [row for row in targets if isinstance(row, dict)]
            if automatic.get("kind") == "ci-render-argocd-auto-sync-fanout-v1":
                legs = automatic.get("render_legs") or []
                leg_ids = sorted(
                    str(leg.get("id")) for leg in legs if isinstance(leg, dict)
                )
                triples = sorted((
                    str(row.get("id")),
                    re.sub(r"[^A-Za-z0-9._-]+", "-", str(row.get("id"))).strip("-") or "target",
                    str(row.get("render_leg_id")),
                ) for row in target_rows)
                expected_steps = [
                    {"id": "source-publication", "kind": "source-publication", "depends_on": [], "target_id": None, "render_leg_id": None},
                    {"id": "image-build", "kind": "image-build", "depends_on": ["source-publication"], "target_id": None, "render_leg_id": None},
                    {"id": "chart-bump", "kind": "chart-bump", "depends_on": ["image-build"], "target_id": None, "render_leg_id": None},
                ]
                for leg_id in leg_ids:
                    expected_steps.append({"id": f"ci-render-{leg_id}", "kind": "ci-render", "depends_on": ["chart-bump"], "target_id": None, "render_leg_id": leg_id})
                    expected_steps.append({"id": f"render-publication-{leg_id}", "kind": "render-publication", "depends_on": [f"ci-render-{leg_id}"], "target_id": None, "render_leg_id": leg_id})
                for target_id, slug, leg_id in triples:
                    expected_steps.append({"id": f"argocd-auto-sync-{slug}", "kind": "argocd-auto-sync", "depends_on": [f"render-publication-{leg_id}"], "target_id": target_id, "render_leg_id": leg_id})
            else:
                pairs = sorted((
                    row.get("id"),
                    re.sub(r"[^A-Za-z0-9._-]+", "-", str(row.get("id"))).strip("-")
                    or "target",
                ) for row in target_rows)
                expected_steps = [
                    {"id": "source-publication", "kind": "source-publication", "depends_on": [], "target_id": None},
                    {"id": "ci-render", "kind": "ci-render", "depends_on": ["source-publication"], "target_id": None},
                    {"id": "render-publication", "kind": "render-publication", "depends_on": ["ci-render"], "target_id": None},
                    *[
                        {"id": f"argocd-auto-sync-{slug}", "kind": "argocd-auto-sync", "depends_on": ["render-publication"], "target_id": target_id}
                        for target_id, slug in pairs
                    ],
                ]
            if automatic.get("cascade_steps") != expected_steps:
                errors.append(
                    f"{schema_name} instance: every conditional cascade step must be exact and ordered"
                )
    if schema_name in {
        "milestone-operations-plan-v2.schema.json",
        "milestone-operations-evidence-v2.schema.json",
    }:
        targets = instance.get("targets")
        if isinstance(targets, list):
            ids = [target.get("id") for target in targets if isinstance(target, dict)]
            duplicates = sorted({value for value in ids if ids.count(value) > 1})
            if duplicates:
                errors.append(
                    f"{schema_name} instance: targets violate x-workspace-unique-by id: "
                    f"{duplicates}"
                )
            slugs = [
                re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-")
                or "target" for value in ids
            ]
            if len(slugs) != len(set(slugs)):
                errors.append(f"{schema_name} instance: target ids collide as evidence paths")
    if schema_name == "milestone-operations-plan-v2.schema.json":
        canonical_plan_hash = hashlib.sha256(json.dumps(
            {key: value for key, value in instance.items() if key != "plan_hash"},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        if instance.get("plan_hash") != canonical_plan_hash:
            errors.append(f"{schema_name} instance: plan_hash is not canonical")
        for index, target in enumerate(instance.get("targets") or []):
            if not isinstance(target, dict):
                continue
            label = f"{schema_name} target[{index}]"
            profile = target.get("verification_profile")
            contexts = target.get("execution_contexts")
            environment = target.get("execution_environment")
            desired = target.get("desired")
            if not all(isinstance(value, dict) for value in (
                profile, contexts, environment, desired
            )):
                continue
            profile_kind = profile.get("kind")
            if profile_kind not in {
                "argocd-web-workload-v1", "argocd-istio-internal-http-v1",
                "argocd-istio-eastwest-v1",
            }:
                errors.append(f"{label}: unsupported verification profile kind")
                continue
            argocd = contexts.get("argocd")
            kube = contexts.get("kubernetes")
            if not isinstance(argocd, dict) or not isinstance(kube, dict):
                errors.append(f"{label}: typed workload requires Argo and Kubernetes contexts")
                continue
            expected_contexts = {"argocd", "kubernetes"}
            if profile_kind == "argocd-istio-eastwest-v1":
                expected_contexts |= {"sender_kubernetes", "receiver_kubernetes"}
            if set(contexts) != expected_contexts:
                errors.append(f"{label}: verification profile context set is not exact")
            if profile_kind == "argocd-istio-eastwest-v1":
                sender_context = contexts.get("sender_kubernetes")
                receiver_context = contexts.get("receiver_kubernetes")
                if not isinstance(sender_context, dict) or not isinstance(receiver_context, dict):
                    errors.append(f"{label}: east-west sender/receiver contexts are required")
                elif (receiver_context.get("cluster_server") != kube.get("cluster_server")
                      or receiver_context.get("certificate_authority_sha256")
                      != kube.get("certificate_authority_sha256")
                      or sender_context.get("cluster_server")
                      == receiver_context.get("cluster_server")):
                    errors.append(f"{label}: east-west context topology is not sender-to-receiver exact")
            target_bindings = {
                "MILESTONE_TARGET_ID": target.get("id"),
                "MILESTONE_ENVIRONMENT": target.get("environment"),
                "MILESTONE_ACCOUNT": target.get("account"),
                "MILESTONE_CLUSTER": target.get("cluster"),
                "MILESTONE_RESOURCE": target.get("resource"),
                "KUBECONFIG": kube.get("kubeconfig_path"),
                "ARGOCD_SERVER": argocd.get("server"),
            }
            if any(environment.get(key) != value for key, value in target_bindings.items()):
                errors.append(f"{label}: execution environment is not target/context bound")
            app_name = str(target.get("resource", "")).split("/", 1)[-1]
            if profile.get("argocd_application") != app_name:
                errors.append(f"{label}: Argo application does not equal target resource")
            observation = target.get("observation_command")
            if isinstance(observation, list) and observation:
                expected_observation = [
                    observation[0], "app", "get", app_name, "--server",
                    argocd.get("server"), "--output", "json", "--config",
                    argocd.get("config_path"), "--argocd-context",
                    argocd.get("context"),
                ]
                if Path(str(observation[0])).name != "argocd" or observation != expected_observation:
                    errors.append(f"{label}: observation is not exact typed Argo JSON query")
            apply_method = target.get("apply_method")
            apply = target.get("apply_command")
            if apply_method == "gitops-manual-sync" and isinstance(apply, list) and apply:
                secret_argument = any(
                    re.search(r"(?i)(?:token|password|passwd|secret|auth[-_]?key)", str(part))
                    or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", str(part))
                    for part in apply
                )
                if (Path(str(apply[0])).name != "argocd" or apply[1:4] != ["app", "sync", app_name]
                        or "--revision" not in apply or "--server" not in apply
                        or secret_argument):
                    errors.append(f"{label}: apply is not target-bound revision-pinned Argo sync")
                else:
                    try:
                        revision = apply[apply.index("--revision") + 1]
                        server = apply[apply.index("--server") + 1]
                    except IndexError:
                        revision = server = None
                    if revision != desired.get("render_commit") or server != argocd.get("server"):
                        errors.append(f"{label}: apply revision/server differs from desired context")
                    value_options = {
                        "--server", "--revision", "--timeout", "--config",
                        "--argocd-context",
                    }
                    bool_options = {"--prune", "--grpc-web"}
                    cursor = 4
                    seen_values: set[str] = set()
                    safe_flags = True
                    while cursor < len(apply):
                        part = apply[cursor]
                        if part in bool_options:
                            cursor += 1
                            continue
                        equals_option = next(
                            (option for option in value_options
                             if isinstance(part, str) and part.startswith(option + "=")),
                            None,
                        )
                        if equals_option is not None:
                            safe_flags = safe_flags and equals_option not in seen_values and bool(
                                part.split("=", 1)[1]
                            )
                            seen_values.add(equals_option)
                            cursor += 1
                            continue
                        if part not in value_options or cursor + 1 >= len(apply):
                            safe_flags = False
                            break
                        safe_flags = safe_flags and part not in seen_values
                        seen_values.add(part)
                        cursor += 2
                    if not safe_flags or not {
                        "--server", "--revision", "--config", "--argocd-context"
                    } <= seen_values:
                        errors.append(f"{label}: apply contains unsupported/duplicate Argo flags")
            elif apply_method == "gitops-auto-sync-observe-v1":
                binding = target.get("auto_sync_binding")
                if (target.get("apply_command") is not None
                        or target.get("apply_executable_sha256") is not None
                        or target.get("apply_timeout_seconds") is not None
                        or profile_kind == "argocd-web-workload-v1"
                        or not isinstance(binding, dict)):
                    errors.append(f"{label}: explicit auto-sync observer shape is invalid")
                else:
                    action = {
                        "execution_environment": target.get("execution_environment"),
                        "execution_contexts": target.get("execution_contexts"),
                        "verification_profile": target.get("verification_profile"),
                        "observation_command": target.get("observation_command"),
                        "observation_executable_sha256": target.get("observation_executable_sha256"),
                        "observation_timeout_seconds": target.get("observation_timeout_seconds"),
                        "verification_contract": target.get("verification_contract"),
                    }
                    action_sha = hashlib.sha256(json.dumps(
                        action, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    ).encode("utf-8")).hexdigest()
                    if (binding.get("target_id") != target.get("id")
                            or binding.get("argocd_application_uid")
                            != profile.get("argocd_application_uid")
                            or binding.get("verification_action_sha256") != action_sha):
                        errors.append(f"{label}: auto-sync binding/action hash mismatch")
            else:
                errors.append(f"{label}: unknown or malformed apply method")
            contract = target.get("verification_contract") or []
            by_kind = {
                item.get("kind"): item for item in contract if isinstance(item, dict)
            }
            common_kinds = {
                "argocd-synced", "deployment-observed-generation",
                "service-selects-workload",
            }
            if profile_kind == "argocd-web-workload-v1":
                expected_kinds = common_kinds | {
                    "ingress-routes-service", "behavioral-smoke",
                }
            elif profile_kind == "argocd-istio-internal-http-v1":
                expected_kinds = common_kinds | {
                    "endpointslice-ready-backends", "istio-probe-origin-ready",
                    "internal-behavioral-smoke",
                }
                expected_host = (
                    f"{profile.get('service_name')}.{profile.get('resource_namespace')}"
                    ".svc.cluster.local"
                )
                if (profile.get("service_host") != expected_host
                        or not str(profile.get("behavioral_smoke_url", "")).startswith(
                            f"http://{expected_host}:"
                        )):
                    errors.append(f"{label}: same-cluster profile does not use exact Service FQDN")
            else:
                expected_kinds = common_kinds | {
                    "endpointslice-ready-backends", "istio-probe-origin-ready",
                    "receiver-gateway-proxy-ready",
                    "sender-serviceentry-route-exact", "sender-destinationrule-mtls-exact",
                    "receiver-serviceentry-route-exact", "receiver-destinationrule-mtls-exact",
                    "receiver-envoyfilter-cluster-exact", "sender-istio-xds-synced",
                    "receiver-istio-xds-synced", "sender-istio-cluster-healthy-endpoints",
                    "receiver-istio-cluster-healthy-endpoints", "eastwest-behavioral-smoke",
                }
                global_host = str(profile.get("global_service_host", ""))
                if (not global_host.endswith(".global")
                        or ".svc.cluster-" not in global_host
                        or not str(profile.get("behavioral_smoke_url", "")).startswith(
                            f"http://{global_host}:"
                        )):
                    errors.append(f"{label}: east-west profile does not use exact .global host")
            if desired.get("image_digest") is not None:
                expected_kinds.add("pod-image-digest")
            if set(by_kind) != expected_kinds or len(contract) != len(expected_kinds):
                errors.append(f"{label}: typed probe set must be exact and complete")
                continue
            if profile_kind != "argocd-web-workload-v1":
                # Exact argv is validated by the deterministic runtime. The schema
                # checker still rejects mislabeled tools and profile borrowing.
                expected_tools = {
                    "argocd-synced": "argocd",
                    "deployment-observed-generation": "kubectl",
                    "pod-image-digest": "kubectl",
                    "service-selects-workload": "kubectl",
                    "endpointslice-ready-backends": "kubectl",
                    "istio-probe-origin-ready": "kubectl",
                    "receiver-gateway-proxy-ready": "kubectl",
                    "internal-behavioral-smoke": "kubectl",
                    "sender-serviceentry-route-exact": "kubectl",
                    "sender-destinationrule-mtls-exact": "kubectl",
                    "receiver-serviceentry-route-exact": "kubectl",
                    "receiver-destinationrule-mtls-exact": "kubectl",
                    "receiver-envoyfilter-cluster-exact": "kubectl",
                    "sender-istio-xds-synced": "istioctl",
                    "receiver-istio-xds-synced": "istioctl",
                    "sender-istio-cluster-healthy-endpoints": "istioctl",
                    "receiver-istio-cluster-healthy-endpoints": "istioctl",
                    "eastwest-behavioral-smoke": "kubectl",
                }
                for kind, item in by_kind.items():
                    command = item.get("command")
                    if (not isinstance(command, list) or not command
                            or Path(str(command[0])).name != expected_tools[kind]):
                        errors.append(f"{label}: {kind} uses the wrong typed collector")
                continue
            kube_prefix = [
                kube.get("kubeconfig_path"), kube.get("context")
            ]
            expected_commands = {
                "argocd-synced": lambda exe: [
                    exe, "app", "get", app_name, "--server", argocd.get("server"),
                    "--output", "json", "--config", argocd.get("config_path"),
                    "--argocd-context", argocd.get("context"),
                ],
                "deployment-observed-generation": lambda exe: [
                    exe, "--kubeconfig", kube_prefix[0], "--context", kube_prefix[1],
                    "get", "deployment", profile.get("deployment_name"), "--namespace",
                    profile.get("destination_namespace"), "--output", "json",
                ],
                "pod-image-digest": lambda exe: [
                    exe, "--kubeconfig", kube_prefix[0], "--context", kube_prefix[1],
                    "get", "pods", "--namespace", profile.get("destination_namespace"),
                    "--selector", profile.get("pod_selector"), "--output", "json",
                ],
                "service-selects-workload": lambda exe: [
                    exe, "--kubeconfig", kube_prefix[0], "--context", kube_prefix[1],
                    "get", "service", profile.get("service_name"), "--namespace",
                    profile.get("destination_namespace"), "--output", "json",
                ],
                "ingress-routes-service": lambda exe: [
                    exe, "--kubeconfig", kube_prefix[0], "--context", kube_prefix[1],
                    "get", "ingress", profile.get("ingress_name"), "--namespace",
                    profile.get("destination_namespace"), "--output", "json",
                ],
                "behavioral-smoke": lambda exe: [
                    exe, "--disable", "--silent", "--show-error", "--max-time",
                    str(by_kind["behavioral-smoke"].get("timeout_seconds")), "--output",
                    "/dev/null", "--write-out", "%{http_code}", "--request", "GET",
                    profile.get("behavioral_smoke_url"),
                ],
            }
            expected_tools = {
                "argocd-synced": "argocd", "deployment-observed-generation": "kubectl",
                "pod-image-digest": "kubectl", "service-selects-workload": "kubectl",
                "ingress-routes-service": "kubectl", "behavioral-smoke": "curl",
            }
            for kind, item in by_kind.items():
                command = item.get("command")
                if (not isinstance(command, list) or not command
                        or Path(str(command[0])).name != expected_tools[kind]
                        or command != expected_commands[kind](command[0])):
                    errors.append(f"{label}: {kind} command is not the exact typed collector")
    if schema_name == "milestone-publication-intent-v2.schema.json":
        scope = instance.get("scope")
        if isinstance(scope, dict):
            mode = scope.get("mode")
            if mode == "adopt-preexisting" and (
                scope.get("expected_remote_head") != scope.get("commit")
                or scope.get("push_argv") is not None
                or scope.get("delivery_effect") is not None
            ):
                errors.append(
                    f"{schema_name} instance: adopt-preexisting requires exact remote "
                    "head equality and no push action"
                )
            if mode == "publish":
                expected_push = [
                    scope.get("git_executable_path"), "-c", "core.hooksPath=/dev/null",
                    f"--git-dir={scope.get('isolated_git_dir')}", "push",
                    f"--force-with-lease=refs/heads/{scope.get('branch')}:"
                    f"{scope.get('expected_remote_head') or ''}", "--",
                    scope.get("remote_url"),
                    f"{scope.get('commit')}:refs/heads/{scope.get('branch')}",
                ]
                if (scope.get("expected_remote_head") == scope.get("commit")
                        or scope.get("push_argv") != expected_push):
                    errors.append(
                        f"{schema_name} instance: publish scope must contain the exact "
                        "force-with-lease action and cannot adopt the current head"
                    )
            canonical_scope_hash = hashlib.sha256(json.dumps(
                scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            if instance.get("scope_hash") != canonical_scope_hash:
                errors.append(f"{schema_name} instance: scope_hash is not canonical")
            authorization = instance.get("authorization")
            precondition = instance.get("precondition")
            if isinstance(authorization, dict):
                if authorization.get("scope_hash") != instance.get("scope_hash"):
                    errors.append(f"{schema_name} instance: authorization scope hash mismatch")
                expected_decision = "approved" if mode == "publish" else "acknowledged"
                if authorization.get("decision") != expected_decision:
                    errors.append(f"{schema_name} instance: wrong authorization decision for mode")
            if isinstance(precondition, dict):
                if precondition.get("observed_commit") != scope.get("expected_remote_head"):
                    errors.append(f"{schema_name} instance: precondition/head mismatch")
                if isinstance(authorization, dict):
                    try:
                        observed_at = datetime.fromisoformat(
                            str(precondition.get("observed_at")).replace("Z", "+00:00")
                        )
                        authorized_at = datetime.fromisoformat(
                            str(authorization.get("at")).replace("Z", "+00:00")
                        )
                        if observed_at > authorized_at:
                            errors.append(f"{schema_name} instance: authorization precedes observation")
                    except ValueError:
                        pass
            environment = scope.get("execution_environment")
            if isinstance(environment, dict):
                fixed = {
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0",
                }
                if (
                    any(environment.get(key) != value for key, value in fixed.items())
                    or not isinstance(environment.get("HOME"), str)
                    or environment.get("HOME") != environment.get("XDG_CONFIG_HOME")
                    or not environment["HOME"].startswith("/")
                ):
                    errors.append(f"{schema_name} instance: publication environment is not isolated")
                if environment.get("GIT_ALTERNATE_OBJECT_DIRECTORIES") != scope.get(
                    "alternate_object_directory"
                ):
                    errors.append(f"{schema_name} instance: alternate object path mismatch")
    if schema_name == "milestone-release-manifest-v2.schema.json":
        sources = instance.get("source_revisions")
        published = instance.get("published_revisions")
        rendered = instance.get("rendered_revisions")
        artifacts = instance.get("artifacts")
        source_pairs = {
            (item.get("repo"), item.get("commit")) for item in sources or []
            if isinstance(item, dict)
        }
        intermediate_pairs = {
            (item.get("repo"), item.get("commit"))
            for item in instance.get("intermediate_revisions") or []
            if isinstance(item, dict)
        }
        for item in published or []:
            if isinstance(item, dict) and (
                item.get("commit") != item.get("source_commit")
                or (item.get("repo"), item.get("source_commit")) not in source_pairs
            ):
                errors.append(f"{schema_name} instance: published revision is not exact source")
        rendered_targets: set[str] = set()
        for item in rendered or []:
            if not isinstance(item, dict):
                continue
            rendered_source = (item.get("source_repo"), item.get("source_commit"))
            if rendered_source not in source_pairs and rendered_source not in intermediate_pairs:
                errors.append(f"{schema_name} instance: rendered revision source is absent")
            target_ids = item.get("target_ids") or []
            if len(target_ids) != len(set(target_ids)):
                errors.append(f"{schema_name} instance: rendered target ids are duplicated")
            rendered_targets.update(target_ids)
        for item in artifacts or []:
            if isinstance(item, dict) and not set(item.get("target_ids") or []) <= rendered_targets:
                errors.append(f"{schema_name} instance: artifact target lacks rendered provenance")
    return errors


def self_test(documents: dict[str, dict[str, Any]]) -> int:
    failures = 0

    def expect(name: str, ok: bool) -> None:
        nonlocal failures
        print(f"  {name}: {'ok' if ok else 'FAIL'}")
        failures += 0 if ok else 1

    expect("real schema bundle", not validate_bundle(documents))
    expect("representative instances", not validate_representative_instances(documents, required=True))
    missing_id = copy.deepcopy(documents)
    missing_id["milestone-waivers-v2.schema.json"].pop("$id", None)
    expect("missing id refused", any("$id" in e for e in validate_bundle(missing_id)))
    bad_ref = copy.deepcopy(documents)
    bad_ref["milestone-waivers-v2.schema.json"]["allOf"][0]["$ref"] = "missing.schema.json#/$defs/x"
    expect("dangling ref refused", any("missing local ref" in e for e in validate_bundle(bad_ref)))
    loose = copy.deepcopy(documents)
    loose["milestone-waivers-v2.schema.json"]["loose"] = {"type": "object"}
    expect("unconstrained object refused", any("additionalProperties" in e for e in validate_bundle(loose)))
    malformed = copy.deepcopy(documents)
    malformed["milestone-waivers-v2.schema.json"]["required"] = "not-an-array"
    meta_errors = optional_meta_validate(malformed)
    if meta_errors:
        expect("meta-schema mutation refused", any("meta-schema rejection" in e for e in meta_errors))
    duplicate_semantic = {
        "targets": [{"id": "same"}, {"id": "same"}],
    }
    expect(
        "duplicate target semantic refused",
        bool(_semantic_instance_errors(
            "milestone-operations-plan-v2.schema.json", duplicate_semantic
        )),
    )
    missing_cascade = copy.deepcopy(
        representative_instances()["milestone-trust-policy-v2.schema.json"]
    )
    missing_cascade["automatic_gitops"]["cascade_steps"].pop()
    expect(
        "incomplete automatic cascade refused",
        bool(_semantic_instance_errors(
            "milestone-trust-policy-v2.schema.json", missing_cascade
        )),
    )
    bad_adoption = copy.deepcopy(
        representative_instances()["milestone-publication-intent-v2.schema.json"]
    )
    bad_adoption["scope"]["mode"] = "adopt-preexisting"
    bad_adoption["scope"]["expected_remote_head"] = "c" * 40
    bad_adoption["scope"]["push_argv"] = None
    bad_adoption["scope"]["delivery_effect"] = {
        "kind": "ci-render-argocd-auto-sync-v1"
    }
    bad_adoption["authorization"]["decision"] = "acknowledged"
    expect(
        "adoption cannot claim retroactive delivery effect",
        bool(_semantic_instance_errors(
            "milestone-publication-intent-v2.schema.json", bad_adoption
        )),
    )
    sha_fixture = "a" * 64
    ci_fixture = {
        "provider": "gitlab", "project": "platform/chart", "source_ref": "main",
        "pipeline_source": "push", "config_sha256": sha_fixture, "deploy_job": "deploy",
        "protected_environment": "dev", "writes_only_render_target": True,
    }

    def fanout_leg(leg_id: str, remote: str) -> dict[str, Any]:
        return {
            "id": leg_id, "remote": remote, "branch": "dev", "protected": True,
            "provenance_path": ".workspace/source-revisions/app.json", "ci_render": dict(ci_fixture),
        }

    def fanout_tgt(tid: str, leg_id: str, remote: str, env: str, cluster: str) -> dict[str, Any]:
        return {
            "id": tid, "render_leg_id": leg_id, "environment": env, "account": "123",
            "cluster": cluster, "resource": "Application/app", "argocd_application": "app",
            "argocd_server": "https://argocd.example.invalid", "argocd_context": "dev",
            "argocd_config_path": "/config/argocd.json", "argocd_config_sha256": sha_fixture,
            "certificate_authority_sha256": sha_fixture, "argocd_project": "default",
            "source_repo_url": remote, "source_target_revision": "dev", "source_path": f"apps/{cluster}/app",
            "destination_server": "https://kubernetes.example.invalid", "destination_namespace": "app",
            "verification_action_sha256": sha_fixture,
            "automated": {"enabled": True, "prune": True, "self_heal": True, "allow_empty": False},
        }

    fanout_policy = {
        "schema_version": 2, "source_remote": "https://git.example/source.git",
        "render_remote_prefixes": ["https://git.example/deploy-a.git",
                                   "https://git.example/deploy-b.git", "https://git.example/chart.git"],
        "artifact_registry_prefixes": ["registry.example/app"], "artifact_resolver": None,
        "automatic_gitops": {
            "kind": "ci-render-argocd-auto-sync-fanout-v1",
            "image_build": {"provider": "gitlab", "project": "platform/source",
                            "registry_repo": "registry.example/app", "tag_scheme": "source-short-sha"},
            "chart": {"remote": "https://git.example/chart.git", "branch": "main",
                      "bump_path": "base/kustomization.yaml"},
            "render_legs": [fanout_leg("commercial", "https://git.example/deploy-a.git"),
                            fanout_leg("mono", "https://git.example/deploy-b.git")],
            "targets": [fanout_tgt("dev-commercial", "commercial", "https://git.example/deploy-a.git", "dev", "core"),
                        fanout_tgt("test-mono", "mono", "https://git.example/deploy-b.git", "test", "mono")],
            "cascade_steps": [
                {"id": "source-publication", "kind": "source-publication", "depends_on": [], "target_id": None, "render_leg_id": None},
                {"id": "image-build", "kind": "image-build", "depends_on": ["source-publication"], "target_id": None, "render_leg_id": None},
                {"id": "chart-bump", "kind": "chart-bump", "depends_on": ["image-build"], "target_id": None, "render_leg_id": None},
                {"id": "ci-render-commercial", "kind": "ci-render", "depends_on": ["chart-bump"], "target_id": None, "render_leg_id": "commercial"},
                {"id": "render-publication-commercial", "kind": "render-publication", "depends_on": ["ci-render-commercial"], "target_id": None, "render_leg_id": "commercial"},
                {"id": "ci-render-mono", "kind": "ci-render", "depends_on": ["chart-bump"], "target_id": None, "render_leg_id": "mono"},
                {"id": "render-publication-mono", "kind": "render-publication", "depends_on": ["ci-render-mono"], "target_id": None, "render_leg_id": "mono"},
                {"id": "argocd-auto-sync-dev-commercial", "kind": "argocd-auto-sync", "depends_on": ["render-publication-commercial"], "target_id": "dev-commercial", "render_leg_id": "commercial"},
                {"id": "argocd-auto-sync-test-mono", "kind": "argocd-auto-sync", "depends_on": ["render-publication-mono"], "target_id": "test-mono", "render_leg_id": "mono"},
            ],
        },
    }
    expect("fanout trust policy validates against schema",
           not validate_instance(documents, "milestone-trust-policy-v2.schema.json", fanout_policy, required=True))
    expect("fanout cascade DAG is exact",
           not _semantic_instance_errors("milestone-trust-policy-v2.schema.json", fanout_policy))
    fanout_bad_dag = copy.deepcopy(fanout_policy)
    fanout_bad_dag["automatic_gitops"]["cascade_steps"].pop()
    expect("fanout incomplete cascade refused",
           bool(_semantic_instance_errors("milestone-trust-policy-v2.schema.json", fanout_bad_dag)))

    bad_timestamp = copy.deepcopy(
        representative_instances()["milestone-waivers-v2.schema.json"]
    )
    bad_timestamp["created_at"] = "not-a-timestamp"
    expect(
        "malformed date-time refused",
        any("date-time" in error for error in validate_instance(
            documents, "milestone-waivers-v2.schema.json", bad_timestamp,
            required=True,
        )),
    )
    print(f"milestone-pipeline-schema-check self-test: {'OK' if failures == 0 else f'{failures} failure(s)'}")
    return 0 if failures == 0 else 1


def main(argv: list[str]) -> int:
    documents = load_bundle()
    if argv == ["--self-test"]:
        return self_test(documents)
    if len(argv) == 3 and argv[0] == "--instance":
        schema_name = argv[1]
        try:
            instance = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"FAIL: cannot read instance: {exc}", file=sys.stderr)
            return 2
        errors = validate_instance(documents, schema_name, instance, required=True)
        if errors:
            for error in errors:
                print(f"FAIL: {error}", file=sys.stderr)
            return 2
        print(f"milestone-pipeline-schema-check: {schema_name} instance OK")
        return 0
    require_meta = argv == ["--require-meta"]
    if argv and not require_meta:
        raise SystemExit(
            "usage: milestone-pipeline-schema-check.py "
            "[--self-test|--require-meta|--instance SCHEMA JSON]"
        )
    errors = (
        validate_bundle(documents)
        + optional_meta_validate(documents, required=require_meta)
        + validate_representative_instances(documents, required=require_meta)
    )
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 2
    print(f"milestone-pipeline-schema-check: OK ({len(documents)} schemas)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
