---
type: roadmap
status: active
tags:
  - type/roadmap
  - status/active
---
# Phase 2 — DECOMPOSE

**Goal:** convert the goal statement into 2–6 epics, each ≤ 6 weeks of work, with epic-level acceptance criteria, INVEST-validated, and tagged enabler-vs-value.

## Step 1 — Pick the decomposition technique

Pick by problem shape, not by habit. The orchestrator's default for the platform work is **vertical slicing + enabler stories**.

| Problem shape | Best technique | Output shape |
|---|---|---|
| User-facing feature with a clear journey | **User Story Mapping** (Patton) | Backbone of activities → walking-skeleton release slices |
| Domain you don't yet understand (new tenant model, new auth flow) | **Event Storming** (Brandolini) | Domain events → bounded contexts → first epics |
| Behavior-change goal with unclear scope ("make X faster", "improve adoption") | **Impact Mapping** (Adzic) | Goal → Actors → Impacts → Deliverables |
| Platform / infra / migration | **Vertical slicing + enabler stories** | Thin end-to-end slices; enabler stories for foundation work |
| Single-tenant rollout that may go multi-tenant | **Vertical slicing per tenant** | One end-to-end slice for tenant-A, then replicate |
| Cross-cluster federation (Thanos, Kiali, Istio EW) | **Vertical slicing per cluster pair** | Sender→receiver slice for one pair; then replicate |
| Compliance / security gate | **Impact Mapping** | Goal → Auditor view → Concrete controls |

**Default for workspace:** vertical slicing. One end-to-end slice for the smallest meaningful scope (one tenant, one cluster pair, one app), then replicate. Foundation work (IRSA scaffolding, gateway config, ServiceEntry templates) is tagged as **enabler** epics — explicitly horizontal, but acknowledged.

## Step 2 — Produce 2–6 epics

Per epic:
- **Title** — action verb, no conventional commit prefix (per workspace CLAUDE.md). Examples: "Add Kiali multi-cluster secret for tenant-acme", "Migrate Keycloak realm-import to v26 schema", "Establish IRSA scaffolding for cost-visibility-l3".
- **Type** — `enabler` (horizontal foundation, no end-user value alone) or `value` (vertical end-to-end slice).
- **Estimated size** — XS (1–2 days) / S (3–5 days) / M (1–2 weeks) / L (3–4 weeks) / XL (5–6 weeks). XL is the cap; if larger, split.
- **Specialist agent** — name the specialist that should execute this if obvious: `helm-apps`, `gitops`, `service-mesh`, `tenant-onboarding`, `security`, `observability-ops`, `release-manager`, `node-ops`, `helm-migration`, `ci-pipelines`, `cluster-health`, `argocd-ops`, `platform-infra`, `platform-cluster-debug`. Empty if none match — defaults to `general-purpose`.
- **Epic-level AC** — 3–5 bullets, observable outcomes. NOT story-level Given/When/Then; high-level "minimum to call this epic done".
- **Dependencies** — list any other epic in this roadmap that must complete first. If many, the decomposition is wrong — re-cut.
- **Risk notes** — 1–2 lines on the highest-risk aspect; the Phase 3 critic in `/milestone-pipeline` will use this as a starting point.

## Step 3 — INVEST check at the epic level

For each epic, confirm:

| Letter | Test |
|---|---|
| **I**ndependent | Can this epic ship without N other epics in the roadmap? If not → dependency note (or re-cut). |
| **N**egotiable | Is the AC outcome-shaped, leaving HOW open? Or does it lock implementation? |
| **V**aluable | Does this epic produce visible value to the user / system / team? Enabler epics defer value but should still produce a measurable foundation outcome ("cluster-acme has IRSA OIDC trust verified"). |
| **E**stimable | Can the team t-shirt-size this with confidence? If "no idea, need a spike" → split off a spike epic first. |
| **S**mall (relative) | ≤ 6 weeks. If larger → split. |
| **T**estable | Is there an observable check that "this epic is done"? If purely subjective → re-write AC. |

Epics that fail Independent get a `Depends on:` note. Epics that fail Estimable get an associated spike epic in the Phase 3 spike lane.

## Step 4 — Enabler vs Value tagging

Every epic must be tagged. The tag drives Phase 3 prioritization (Now lane usually leads with one enabler + one value pair).

| Tag | Definition | Examples |
|---|---|---|
| `enabler` | Horizontal foundation that no end-user uses directly but unblocks ≥ 2 future epics. | IRSA OIDC trust setup; ServiceEntry scaffolding for a new tenant; Pulumi stack creation; new chart base structure |
| `value` | Vertical slice that produces an observable user/system outcome on its own. | Kiali multi-cluster sees tenant-acme; Grafana dashboard shows per-namespace cost; Keycloak realm imports cleanly on v26 |

Anti-pattern: marking everything as enabler ("all of this is foundation"). If > 60% of epics are enabler, the decomposition is too horizontal — re-cut for vertical slices.

## Step 5 — Specialist-agent map (workspace-specific)

Match each epic to the closest specialist agent. Phase 2 of `/milestone-pipeline` will dispatch to the named specialist.

| Epic shape | Specialist agent |
|---|---|
| New Helm chart, chart upgrade (minor/patch), Helm values changes | `helm-apps` |
| Major Helm upgrade w/ breaking CRD or schema changes | `helm-migration` |
| ApplicationSet / sync-wave / GitOps repo wiring | `gitops` |
| ArgoCD app stuck / unhealthy / Missing | `argocd-ops` |
| Istio mesh, cert-manager, AWS PCA, mTLS, cross-cluster | `service-mesh` |
| Kyverno, Falco, OAuth2-Proxy, Keycloak, policy enforcement | `security` |
| Observability (Prometheus, Grafana, Loki, Promtail) | `observability` |
| Cross-cluster Kiali / Thanos / federated metrics | `observability-ops` |
| Tenant onboarding (cross-cutting setup) | `tenant-onboarding` |
| GitLab CI pipeline / templates | `ci-pipelines` |
| EKS node ops / capacity / IaC drift | `node-ops` |
| Pulumi infrastructure (VPC, IAM, EKS) | `platform-infra` |
| Live-cluster debug (RBAC, webhooks, certs, DNS) | `platform-cluster-debug` |
| Cross-cluster operational health snapshot | `cluster-health` |
| Issue/MR management, cross-project coordination | `gitlab-workflow` |
| Env promotion, release coordination | `release-manager` |
| Custom code/source app changes | `general-purpose` (no specialist match) |

If multiple match, pick the most specific one. If none match, leave empty (defaults to `general-purpose` at execution time).

## Phase 2 output template (writes to the `## Epics` section of the roadmap doc)

```markdown
## Epics

**Decomposition technique:** Vertical slicing + enabler stories
**Rationale:** Platform infra; per-tenant rollout; foundation epics first then value slices.

### E1: <Title — action verb>

- **Type:** enabler | value
- **Size:** XS | S | M | L | XL
- **Specialist:** <agent name or general-purpose>
- **Depends on:** E0 (or none)
- **Acceptance criteria (epic-level):**
  - <observable outcome>
  - <observable outcome>
  - <observable outcome>
- **Risks:** <1–2 lines>

### E2: <Title>
…
```

## Auto-advance vs gate (this phase)

| Condition | Action |
|---|---|
| One decomposition cut is obviously best (problem shape clearly maps to one technique); epics are ≤ 6 weeks each; all INVEST checks pass; enabler/value mix is balanced (40-60% enabler max) | Auto-advance to Phase 3 |
| ≥ 2 cuts are credible AND have materially different downstream consequences | GATE — present both cuts side-by-side with consequences, ask user to pick |
| Any epic > 6 weeks even after splitting attempts | GATE — surface, ask whether to split into multiple epics or accept (with explicit rationale) |
| > 60% of epics are enabler-tagged | GATE — re-cut suggestion; ask user whether to accept or re-cut |
| User explicitly asked for a checkpoint | GATE |

When gating, present the cuts + their consequences clearly; accept user direction; integrate; proceed.

## Hard rules

- **2–6 epics, not 1, not 7+.** 1 epic = milestone, not roadmap. 7+ = decomposition failure.
- **Cap epic size at 6 weeks (XL).** Beyond that, split.
- **Title style: action verbs, no conventional commit prefixes.** Per workspace CLAUDE.md.
- **Every epic has a specialist suggestion** (or explicit `general-purpose` if none fits).
- **Epic-level AC is outcome-shaped, not Given/When/Then.** Story-level AC is Given/When/Then; that's a Phase 3 activity.
- **Don't decompose stories yet.** Stories happen in Phase 3 for Now-lane epics only.
- **Don't skip the dependency note.** "Depends on: none" is a valid value but must be explicit.
