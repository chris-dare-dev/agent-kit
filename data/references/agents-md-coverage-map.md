# AGENTS.md rule-coverage map (S5.1 — the safety gate for the thin root contract)

> Milestone `provider-neutral-agent-kit-m4` (E5). This map is the **statement-by-statement coverage
> checklist** the roadmap's E5 risk note demands: *"Compressing ~29 KB of normative text to ≤ 5 KiB
> risks silently dropping a load-bearing rule; requires an explicit statement-by-statement coverage
> checklist mapping every normative rule to its new home before the old content is removed."*
>
> **Nothing was removed from `AGENTS.md` until every source statement below had a classified new home.**

## How this file is consumed (single source of truth)

The fenced ```json block at the bottom is the **machine source**. `data/scripts/generate-root-contract.py`
parses it to render the ≤5 KiB `AGENTS.md` router, and `--check` uses it as the coverage gate:

- `class: "router"` statements carry the **condensed `text`** the generator emits (under the heading named
  by `out_section`) plus a distinctive **`anchor`** phrase that is a substring of that `text`. `--check`
  greps the regenerated `AGENTS.md` for every `router` anchor (no injected HTML-comment markers — the
  proof costs zero bytes); a router rule that fails to render is a hard failure (exit 3). `--check` also
  pins the router id set (`REQUIRED_ROUTER_IDS` in the generator) so a row cannot be silently deleted, and
  asserts anchor uniqueness so a duplicate cannot mask a dropped render.
- `class: "mcp-served"` statements are inventory/detail reachable via an MCP tool (named in `new_home`);
  they are intentionally NOT in the router.
- `class: "redundant"` statements already live in another tier (named in `new_home`) and are dropped.

Every statement of the pre-M4 `AGENTS.md` (read at `data/claude-md/AGENTS.md`, 29,160 B / `workspace/AGENTS.md`
29,123 B, body-identical below the 37 B Obsidian frontmatter) has exactly one row. Router `text` is the
ONLY hand-authored prose in the pipeline — the inventory-via-MCP block is generated live from
`data/facts/catalog.json` (so counts never go stale). The per-agent **family index is `list_agents`-served,
not inlined**: rendering the full ~100 agent names would blow the byte budget without adding a rule, so
the router carries only a live "N agents across M domains — enumerate via `list_agents`" pointer.

## CLAUDE.md-tier sweep (S5.1 AC: "+ any normative content in the CLAUDE.md tiers")

Swept `workspace/CLAUDE.md` (= `data/claude-md/workspace-root.md`), `GitLab/CLAUDE.md`, and
`platform/CLAUDE.md` for a normative **agent-routing / escalation / cross-agent-coordination** rule
living ONLY there and absent from `AGENTS.md`. **Result: NONE** — all three tiers point to MCP tools or
name the workspace-root `CLAUDE.md` as the single home for cross-cutting rules (git conventions, external
write policy, GitOps edit rules); none independently states an agent-dispatch rule the router lacks.
Recorded as the audited row `audit.claude-md-tier-sweep` so the sweep is not re-litigated. Two embedded
`AGENTS.md` cell-rules (`ci-no-envsubst`, `irsa-naming`) are the reverse case — they duplicate
workspace-root `CLAUDE.md` and are classified `redundant` with that file as their home.

## Human summary (counts derived from the json block below)

| Class | Meaning | Count |
|---|---|---|
| `router` | Normative, kept in the ≤5 KiB generated router (condensed) | 18 |
| `mcp-served` | Inventory/detail reachable via an MCP tool — removed from the file | 10 |
| `redundant` | Already stated in another tier — dropped | 3 |
| `audit` | Sweep-result / bookkeeping row (no content) | 1 |

**Byte outcome:** the pre-M4 file's four "keep" sections summed to 15,510 B (3× the 5 KiB target); the
Escalation Matrix alone was 8,745 B / 35 rows. It is **extracted verbatim** to
`data/references/escalation-matrix.md` (MCP-served via `get_reference("escalation-matrix")`); the router
keeps only a terse decision procedure (`escalation.procedure`). That extraction plus condensing the
remaining router prose (each statement keeps its rule, sheds its wordcount) is what makes ≤5120 B
achievable without dropping a rule.

## S5.3 scope note (deliberate, surfaced at the push gate)

`workspace/CLAUDE.md` carries ~18.8 KB of genuinely provider-neutral, safety-critical policy (AWS accounts,
External System Write Policy, GitOps edit rules, Git conventions). Fully migrating that into the router is
a far higher blast-radius change than a Size-M milestone and is **deferred to a follow-up milestone**
(`provider-neutral-agent-kit-m5`). M4's S5.3 adds only the `@AGENTS.md` import so Claude sessions inline
the shared router; nothing safety-critical moves, so "no normative rule lost" holds trivially.

```json
{
  "source_file": "data/claude-md/AGENTS.md",
  "generated_note": "Machine source for generate-root-contract.py. Router 'text' is authored here; the inventory-via-MCP block (counts + family span) is catalog-generated and has no rows.",
  "byte_budget": 5120,
  "escalation_extract": "data/references/escalation-matrix.md",
  "out_section_order": ["Dispatch patterns", "Cross-agent coordination", "Escalation", "Web apps", "Path & subdirectory references"],
  "statements": [
    {"id": "intro", "source_section": "preamble (L6-8)", "class": "router", "out_section": "_header", "anchor": "provider-neutral router", "disposition": "condensed", "text": "The workspace agent registry, generated as a thin provider-neutral router: the full agent/skill/entrypoint inventory is MCP-served (below); only normative dispatch, coordination, escalation, and path rules stay inline."},

    {"id": "inv.platform", "source_section": "Agent Inventory > Platform Agents (L12-26)", "class": "mcp-served", "new_home": "list_agents / get_agent({name}) + catalog kind=agent; domain constants (mesh PQC/NLB/AUTO_PASSTHROUGH, Keycloak realmImport) via get_context_guide", "disposition": "removed"},
    {"id": "inv.confluence", "source_section": "Agent Inventory > Confluence Agents (L28-38)", "class": "mcp-served", "new_home": "Confluence/AGENTS.md (pointer kept in Path & subdirectory references)", "disposition": "removed"},
    {"id": "inv.milestone", "source_section": "Agent Inventory > Milestone Pipeline Agents (L40-53)", "class": "mcp-served", "new_home": "list_agents / get_agent + the /milestone-pipeline command (data/commands/milestone-pipeline.md)", "disposition": "removed"},
    {"id": "inv.gitlab", "source_section": "Agent Inventory > GitLab Workflow Agents (L55-62)", "class": "mcp-served", "new_home": "list_agents / get_agent (the no-MCP-tools capability rule is preserved as router coord.specialist-no-mcp)", "disposition": "removed"},
    {"id": "inv.k8sops", "source_section": "Agent Inventory > Kubernetes Operations Agents (L64-75)", "class": "mcp-served", "new_home": "list_agents / get_agent + catalog kind=agent", "disposition": "removed"},
    {"id": "inv.milestone-memory-note", "source_section": "Agent Inventory > Milestone note (L44)", "class": "mcp-served", "new_home": "get_agent (each milestone agent's frontmatter carries memory: project); low-value inline note", "disposition": "removed"},

    {"id": "dispatch.orchestrator-workers", "source_section": "Parallel Agent Dispatch Patterns > Orchestrator+Workers (L81-86)", "class": "router", "out_section": "Dispatch patterns", "anchor": "Orchestrator + Workers", "disposition": "condensed", "text": "- **Orchestrator + Workers** — 1 orchestrator does deep config analysis (Helm values, overlays, rendered manifests) while 3-6 workers run parallel kubectl diagnostics across clusters/namespaces (ArgoCD sync, Envoy xDS, pod logs, SE/DR state)."},
    {"id": "dispatch.parallel-cluster-triage", "source_section": "Parallel Agent Dispatch Patterns > Parallel Cluster Triage (L88-89)", "class": "router", "out_section": "Dispatch patterns", "anchor": "Parallel Cluster Triage", "disposition": "condensed", "text": "- **Parallel Cluster Triage** — for a multi-cluster issue, dispatch one `cluster-health` agent per cluster simultaneously, not sequentially."},
    {"id": "dispatch.post-deploy-verify", "source_section": "Parallel Agent Dispatch Patterns > Post-Deployment Verification (L91-95)", "class": "router", "out_section": "Dispatch patterns", "anchor": "Post-deployment verification", "disposition": "condensed", "text": "- **Post-deployment verification** — after new ServiceEntries/DestinationRules: confirm ArgoCD Synced+Healthy, confirm the LIVE spec matches the rendered manifest (not just sync status), and `rollout restart` pods predating the SE so they pick up new Envoy clusters."},
    {"id": "dispatch.three-agent-fix", "source_section": "Parallel Agent Dispatch Patterns > Three-Agent Parallel Fix (L97-112)", "class": "router", "out_section": "Dispatch patterns", "anchor": "Non-overlapping parallel fix", "disposition": "condensed", "text": "- **Non-overlapping parallel fix** — when one symptom has independent root causes across repos, dispatch agents with DISJOINT repo ownership: name each agent's owned paths + its must-not-touch set; pre-state what's already diagnosed; grant push up-front; require a concise structured report (commit SHA/URL + change + verification). Split *within* one repo runs sequentially — same-repo commits race the index."},

    {"id": "webapps.list", "source_section": "Custom Web App Inventory (L114-122)", "class": "router", "out_section": "Web apps", "anchor": "bootstrap-only CI trap", "disposition": "condensed", "text": "Custom web apps (detail via `get_app_context({app})`): **admin-web-app** (`source/admin-web-app/`, operations), **mosaic-web-app** (`source/mosaic-web-app/`, operations+tenants), **landing-page-web-app** (`source/landing-page-web-app/`, core-services). All use the container-build model and share the **bootstrap-only CI trap**: new NON-image manifests in `k8s-manifests/base/` won't reach the deploy repo without a CI fix (escalate to `ci-pipelines`)."},

    {"id": "paths.routing-layer", "source_section": "Path Reference (L124-126)", "class": "router", "out_section": "Path & subdirectory references", "anchor": "charts/routing-layer/k8s-manifests", "disposition": "verbatim", "text": "- routing-layer source: `charts/routing-layer/k8s-manifests/`."},

    {"id": "coord.confluence-platform", "source_section": "Cross-Agent Coordination > Platform<->Confluence (L130-137)", "class": "mcp-served", "new_home": "get_agent (each agent's domain) — the confluence<->platform mapping is inventory, not a rule", "disposition": "removed"},
    {"id": "coord.gitlab-platform", "source_section": "Cross-Agent Coordination > Platform<->GitLab Workflow (L139-146)", "class": "mcp-served", "new_home": "get_agent (each agent's domain) — inventory, not a rule", "disposition": "removed"},
    {"id": "coord.specialist-no-mcp", "source_section": "Agent Inventory > GitLab note (L57) + GitLab MCP Tools Reference (L213)", "class": "router", "out_section": "Cross-agent coordination", "anchor": "call `mcp__GitLab__*`", "disposition": "condensed", "text": "- Specialist agents (gitlab-workflow, release-manager, the K8s-ops agents) carry only Read/Glob/Grep/Bash — they CANNOT call `mcp__GitLab__*` or other MCP tools. GitLab/MCP reads and writes happen in the MAIN session or via skills."},
    {"id": "coord.post-edit-commit", "source_section": "Cross-Agent Coordination > Post-edit commit suggestion (L148)", "class": "router", "out_section": "Cross-agent coordination", "anchor": "push routine commits to `main`", "disposition": "condensed", "text": "- After editing a platform repo, push routine commits to `main` after authorization; use no feature branch/MR unless Chris asks. `dev` is retired. CI renders to `deploy:dev`; Kargo promotes `deploy:stage`+`deploy:main`."},
    {"id": "coord.agent-artifact-finalization", "source_section": "Workspace root CLAUDE.md > Agent-artifact finalization (2026-07-17)", "class": "router", "out_section": "Cross-agent coordination", "anchor": "Agent-artifact finalization", "disposition": "provider-neutral rule", "text": "- **Agent-artifact finalization** — use canonical skills; after terminal validation, refresh Obsidian and emit the append-only receipt. Qdrant is derived search; Graphiti stays candidate-only/write-disabled pending explicit approval. Preserve sources and report capture failures; never write sinks."},
    {"id": "coord.workflow-tenant", "source_section": "Cross-Cutting Workflows > New Tenant (L152-155)", "class": "router", "out_section": "Cross-agent coordination", "anchor": "New-tenant provisioning", "disposition": "condensed", "text": "- **New-tenant provisioning** — gitlab-workflow (linked issues per repo) -> tenant-onboarding (changes across repos) -> release-manager (dev->stage->prod)."},
    {"id": "coord.workflow-chart-upgrade", "source_section": "Cross-Cutting Workflows > Chart Upgrade (L157-160)", "class": "router", "out_section": "Cross-agent coordination", "anchor": "Chart-version upgrade", "disposition": "condensed", "text": "- **Chart-version upgrade** — gitlab-workflow (issues) -> helm-apps (charts/values) + ci-pipelines (templates) -> release-manager (promotion + notes)."},
    {"id": "coord.workflow-oidc", "source_section": "Cross-Cutting Workflows > OIDC Integration (L162-167)", "class": "router", "out_section": "Cross-agent coordination", "anchor": "OIDC integration", "disposition": "condensed", "text": "- **OIDC integration** — security (Keycloak client, realm `workspace`) -> helm-apps (app OIDC values) -> service-mesh (cross-cluster SE+DR sender / SE+EF receiver when app + Keycloak differ) -> security (`skip-auth-regex` for callbacks); validate `/auth-chain-debug` + `/cross-cluster-connectivity`."},

    {"id": "escalation.matrix-table", "source_section": "Escalation Matrix (L169-209, 35 rows, 8745 B)", "class": "mcp-served", "new_home": "data/references/escalation-matrix.md (extracted VERBATIM) via get_reference(\"escalation-matrix\")", "disposition": "extracted"},
    {"id": "escalation.procedure", "source_section": "Escalation Matrix intro (L171) + aws-refresh row (L199)", "class": "router", "out_section": "Escalation", "anchor": "get_reference(\"escalation-matrix\")", "disposition": "condensed", "text": "For any operational symptom, consult the full symptom -> first-responder -> escalation-target -> trigger table via `get_reference(\"escalation-matrix\")` and start with THAT symptom's named first responder (usually a read-only skill); for general/unlocalized breakage the first responder is `cluster-health`. Escalate to the specialist agent only when a write or config change is needed. Credential errors (`ExpiredToken`/`Unauthorized`/SSO-expired) -> `/aws-refresh` first, always."},

    {"id": "gitlab.tools-reference", "source_section": "GitLab MCP Tools Reference (L211-224)", "class": "mcp-served", "new_home": "the who-can-call rule is preserved as router coord.specialist-no-mcp; the dynamic-surface + REST-fallback hint survives in the GitLab skills (/pipeline-status, /ci-pipeline-debug, /issue-create, /deploy-check) and data/references/ci-templates-layer-model.md — call mcp__GitLab__discover_tools when a tool seems missing, else REST https://gitlab.example.com/api/v4/...", "disposition": "removed"},

    {"id": "redundant.ci-no-envsubst", "source_section": "Agent Inventory > ci-pipelines cell (L18)", "class": "redundant", "new_home": "workspace-root CLAUDE.md (data/claude-md/workspace-root.md) > 'AWS Accounts' > 'CI variable substitution: NOT supported'", "disposition": "dropped"},
    {"id": "redundant.irsa-naming", "source_section": "Agent Inventory > platform-infra cell (L22)", "class": "redundant", "new_home": "workspace-root CLAUDE.md > 'AWS Accounts' > 'IRSA role naming: platform-{clusterShort}-{service}-role-{env}'", "disposition": "dropped"},
    {"id": "redundant.chart-ownership", "source_section": "Chart Ownership (L226-228)", "class": "redundant", "new_home": "helm-apps agent Chart Ownership Table (data/agents/helm-apps.md) + get_app_context; a 1-line pointer is kept as router paths.chart-ownership", "disposition": "dropped"},

    {"id": "paths.agents", "source_section": "Subdirectory References > agent defs + Confluence (L232-233)", "class": "router", "out_section": "Path & subdirectory references", "anchor": "data/agents/*.md", "disposition": "condensed", "text": "- Agent definitions (canonical): `data/agents/*.md` (symlinked `.claude/agents/`; enumerate `ls .claude/agents/`). Confluence agents: `Confluence/AGENTS.md`."},
    {"id": "paths.skills", "source_section": "Subdirectory References > Skills (L234)", "class": "router", "out_section": "Path & subdirectory references", "anchor": ".claude/skills/", "disposition": "condensed", "text": "- Skills: `.claude/skills/` (enumerate `ls .claude/skills/`), or `list_skills` / `get_skill({name})`."},
    {"id": "paths.chart-ownership", "source_section": "Chart Ownership pointer (L228) + Subdirectory context", "class": "router", "out_section": "Path & subdirectory references", "anchor": "Chart ownership", "disposition": "condensed", "text": "- Chart ownership (which agent owns which of the 48 charts): the `helm-apps` agent's Chart Ownership Table (`data/agents/helm-apps.md`) + `get_app_context({app})`."},
    {"id": "paths.pointer", "source_section": "Subdirectory References > Pointer file (L235)", "class": "router", "out_section": "Path & subdirectory references", "anchor": "pointer back to this registry", "disposition": "condensed", "text": "- `platform/AGENTS.md` is deliberately a pointer back to this registry (not a fifth copy-pair)."},

    {"id": "audit.claude-md-tier-sweep", "source_section": "S5.1 CLAUDE.md-tier sweep", "class": "audit", "new_home": "n/a", "disposition": "sweep-done-empty: no router-only agent-routing rule found in workspace-root/GitLab/platform CLAUDE.md tiers absent from this router"}
  ]
}
```
