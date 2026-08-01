#!/usr/bin/env python3
"""
roadmap_status_excalidraw.py — per-project LIVE roadmap status board (Excalidraw).

For each project, renders `Notes/Projects/<project>/<project>-roadmaps.excalidraw.md` as a
fixed-width portfolio board. Roadmaps are grouped into Focus / Underway / Queued / Complete /
Archive lanes, then laid out as two-column panels. Milestones, spikes, and review handoffs have
distinct visual types; completion state is a separate accent so type and status never compete.

STATUS CONVENTION (canonical = checkbox; lenient legacy fallback). A milestone/spike is:
  done     if its line has `- [x]` / `[X]`  (or legacy ✅ / SHIPPED / DONE / COMPLETE / LIVE)
  active   if its line has `- [/]` / `[~]` / `[-]`  (or legacy 🚧 / WIP / IN PROGRESS)
  pending  if `- [ ]`  (or no marker)
Agents mark a milestone done by ticking its checkbox. See data/references (roadmap-status-convention).

Live: the board's signature includes the parsed status, so it regenerates only when a checkbox
flips (or the roadmap set changes) — the PostToolUse hook fires it instantly on agent roadmap
edits; a 15-min launchd timer backstops. Deterministic (hash-derived ids/seeds) → no idle churn.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from path_contract import default_manifest_path, load_project_manifest  # noqa: E402

MANIFEST = str(default_manifest_path(SCRIPT_DIR))

BOARD_SCHEMA_VERSION = 14  # round-trip-stable rail inset + link-carried scene fingerprint
MAX_CLOSED_REVIEWS = 4

# Visual type is encoded by the card surface. Status is encoded independently by the left rail,
# glyph, and progress treatment. This avoids the old ambiguity where green could mean either
# "milestone" or "done" depending on context.
C_MILESTONE = ("#eff6ff", "#60a5fa")
C_SPIKE = ("#f5f3ff", "#8b5cf6")
C_AUDIT = ("#fff1f2", "#fb7185")
C_EPIC = ("#f8fafc", "#cbd5e1")
C_PANEL = ("#ffffff", "#cbd5e1")
C_HEADER = ("#0f172a", "#0f172a")

STATUS_STROKE = {"done": "#16a34a", "active": "#d97706", "pending": "#64748b"}
STATUS_LABEL = {"done": "✓ DONE", "active": "● ACTIVE", "pending": "○ QUEUED"}
REVIEW_VISUAL = {
    "requested": (("#fff1f2", "#fb7185"), "↗ REQUESTED", "#9f1239"),
    "in-review": (("#fff7ed", "#f97316"), "◐ IN REVIEW", "#9a3412"),
    "verdict-received": (("#eff6ff", "#3b82f6"), "◆ VERDICT READY", "#1d4ed8"),
    "closed": (("#f0fdf4", "#22c55e"), "✓ CLOSED", "#166534"),
    "drift": (("#fef2f2", "#dc2626"), "! CONTRACT DRIFT", "#991b1b"),
    "unresolved": (("#fff7ed", "#ea580c"), "! UNRESOLVED", "#9a3412"),
}
LANES = (
    ("focus", "FOCUS", "Open reviews or active work", ("#fff1f2", "#fb7185"), "#9f1239"),
    ("underway", "UNDERWAY", "Progress exists; more work remains", ("#fffbeb", "#f59e0b"), "#92400e"),
    ("queued", "QUEUED", "Planned work not yet started", ("#eff6ff", "#60a5fa"), "#1d4ed8"),
    ("complete", "COMPLETE", "All tracked work is done", ("#f0fdf4", "#22c55e"), "#166534"),
    ("archive", "ARCHIVE", "Cancelled or superseded history", ("#f1f5f9", "#94a3b8"), "#475569"),
)


def _validate_manifest(m):
    """Validate the fields that make generated element links stable and portable."""
    pv = m.get("presentation_vault", {})
    required_pv = ("name", "root", "projects_root", "source_alias_dir")
    missing_pv = [key for key in required_pv if not pv.get(key)]
    if missing_pv:
        raise ValueError("project-map.json presentation_vault missing: " + ", ".join(missing_pv))
    seen = set()
    for display_name, cfg in m.get("projects", {}).items():
        pid = cfg.get("project_id", "")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", pid):
            raise ValueError(f"project {display_name!r} has invalid project_id {pid!r}")
        if pid in seen:
            raise ValueError(f"duplicate project_id {pid!r}")
        seen.add(pid)
    return m


def load_manifest():
    return _validate_manifest(load_project_manifest(MANIFEST))


def _h(key, mod=2**31):
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % mod


def status_of(line):
    """done | active | pending from a milestone/spike line (checkbox first, then legacy markers)."""
    if re.search(r"\[[xX]\]", line):
        return "done"
    if re.search(r"\[[/~\-]\]", line):
        return "active"
    if re.search(r"\[ \]", line):
        return "pending"
    if "✅" in line or re.search(r"\b(SHIPPED|DONE|COMPLETE|LIVE)\b", line):
        return "done"
    if "🚧" in line or re.search(r"\b(WIP|IN[\s-]?PROGRESS)\b", line, re.I):
        return "active"
    return "pending"


_RANK = {"pending": 0, "active": 1, "done": 2}


def _strip_md(t):
    """Clean a title fragment: drop checkboxes, emoji, backticks, markdown — KEEP em-dash text."""
    t = re.sub(r"\[[ xX/~\-]\]", "", t)
    for e in ("✅", "🚧", "🔍", "☑", "◐", "☐"):
        t = t.replace(e, "")
    # Backticks are presentation syntax; their contents are often the most important noun in a
    # milestone title (role, route, artifact, or milestone id). Preserve the text itself.
    t = t.replace("`", "")
    t = re.sub(r"[*_#]", "", t)
    return t.strip(" :—-·").strip()


def _display_title(title, limit=40):
    """Bound text only at the fixed-width Excalidraw rendering edge.

    Parsed roadmap data is also consumed by portfolio projections and hubs, where truncating the
    semantic title destroys useful context. Keep the model lossless and shorten only the drawing.
    """
    if len(title) <= limit:
        return title
    return title[: max(1, limit - 1)].rstrip() + "…"


def _semantic_title(source_subject):
    """Remove planning metadata from a roadmap subject without fixed-width truncation.

    Some roadmap headings carry the full milestone brief after a concise title. That prose is
    useful as provenance but unusable as a Base/card label. Keep it separately as
    ``source_subject`` and return the semantic leading title here.
    """
    title = source_subject.strip()
    title = re.sub(r"^←\s*E\d+\s*:\s*", "", title, flags=re.I)
    title = re.sub(r"^\(CLOSED[^)]*\)\s*:\s*", "", title, flags=re.I)
    split_patterns = (
        r"\s+—\s+(?:milestone|spike)\s+ID\b",
        r"\s+—\s+(?:Must|Should|Could|Won't)\b",
        r"\s+Milestone\s+ID\b",
        r"\s+Spike\s+ID\b",
        r"\s+Verdict\b",
        r"\s+RESOLVED\s+\d{4}-\d{2}-\d{2}\b",
        r"\s+Outcome\s*:",
        r"\s+Output\s*:",
        r"\s+Decision\s+doc\s*→",
        r"\s+Blocks\b",
        r"\s+RICE\s*(?:\(|=|:)",
    )
    for pattern in split_patterns:
        title = re.split(pattern, title, maxsplit=1, flags=re.I)[0]
    return title.strip(" :—-·.").strip() or source_subject.strip() or "untitled"


def _norm_id(raw):
    return "M" + re.sub(r"\D", "", raw) if raw[0].lower() == "m" else raw.upper()


def line_status(ln):
    """If ln is a status-DECLARATION line (its subject IS a status), return done/active/pending;
    else None. Catches `- [x]`, `- **✅ DONE …**`, `#### ✅ M1`, `> **✅ SHIPPED**`, but NOT a
    story line like `- **RS-0** … ✅ DONE` where the marker sits mid-line (so per-story ticks
    don't leak up)."""
    s = re.sub(r"^\s*>+\s*", "", ln)       # drop leading blockquote(s) so `> ✅ SHIPPED` is read
    s = re.sub(r"^\s*[-*]\s*", "", s)
    s = re.sub(r"^#+\s*", "", s)
    mcb = re.match(r"\[([ xX/~\-])\]", s)
    if mcb:
        c = mcb.group(1)
        return "done" if c in "xX" else ("active" if c in "/~-" else "pending")
    s = re.sub(r"^\*+\s*", "", s)
    if re.match(r"(✅|☑)|(DONE|SHIPPED|COMPLETE|LIVE)\b", s):
        return "done"
    if re.match(r"(🚧|◐|🔍)|(WIP|IN[\s-]?PROGRESS)\b", s, re.I):
        return "active"
    if s.startswith("☐"):
        return "pending"
    return None


def backtick_id(ln):
    """Find a milestone/spike id inside a backtick slug: `…-m3` / `…-m3-foo` / `…-spike-2`.
    The number must end the segment or be followed by '-' (so an instance type 'm6i' is skipped)."""
    for mt in re.finditer(r"`([a-z0-9][a-z0-9-]*)`", ln):
        slug = mt.group(1)
        s = re.search(r"-spike-(\d+)(?:-|$)", slug)
        if s:
            return "SP" + s.group(1)
        # Allow ONE optional trailing letter so sub-milestone ids parse as DISTINCT
        # milestones: `…-m1b`, `…-m2c`, `…-m5a` → M1b/M2c/M5a (a platform-wide
        # convention — crossplane-irsa-migration m5a/b/c, dispatcher-prove-and-distribute
        # m1b, dna-rem m1b/m2b/m2c). A no-suffix id is byte-identical to before
        # (`…-m1` → M1). The single-letter + `(?:-|$)` anchor still skips multi-char
        # instance types (`m6id`) and non-slug forms (`m6i.large` — the '.' breaks the
        # backtick slug capture), preserving the original m6i guard.
        m2 = re.search(r"-m(\d+[a-z]?)(?:-|$)", slug)
        if m2:
            return "M" + m2.group(1)
    return None


def canonical_ref(ln):
    """Preserve a canonical milestone/spike slug when the subject line provides one."""
    for match in re.finditer(r"`([a-z0-9][a-z0-9-]*)`", ln):
        if backtick_id(match.group(0)):
            return match.group(1)
    return None


def subject(ln):
    """Return (norm_id, title) if ln is a milestone/spike subject line; else None."""
    if re.match(r"^#{2,5}\s+Smoke checkpoint\b", ln, re.I):  # cross-ref gate heading, never a milestone
        return None
    if re.match(r"^#{2,5}\s+(Now|Next|Later)\b", ln):  # lane headings (may name milestones in prose) — not subjects
        return None
    m = re.match(r"^#{2,5}\s+Milestone\s+([Mm]?\d+)\b(.*)$", ln)        # ## Milestone m7 — title
    if m:
        return _norm_id("M" + re.sub(r"\D", "", m.group(1))), _strip_md(m.group(2))
    # heading carrying a backtick milestone/spike slug anywhere
    # (### ✅ EP-1: … — `slug-m1` — SHIPPED   /   ## Milestone — `slug-m1`)
    if re.match(r"^#{2,5}\s", ln):
        bid = backtick_id(ln)
        if bid:
            return bid, _strip_md(ln)
    # bold bullet whose FIRST token is the backtick slug (a DEFINITION, not a mid-line
    # cross-reference to another roadmap's milestone): - **`slug-spike-2`** — ✅ DONE
    mb = re.match(r"^\s*[-*]?\s*\*\*\s*(`[a-z0-9][a-z0-9-]*`)", ln)
    if mb:
        bid = backtick_id(mb.group(1))
        if bid:
            return bid, _strip_md(ln)
    m = re.match(r"^#{2,5}\s*(.*\bM(\d+)(?=[:\s]).*)$", ln)             # #### [✅] M1: title — meta
    if m:
        return "M" + m.group(2), _strip_md(re.sub(r"—.*$", "", m.group(1)).split(f"M{m.group(2)}", 1)[-1])
    if re.match(r"^#{2,5}\s", ln):                                      # #### [provisional] srm-m3 — title
        mlow = re.search(r"\b[a-z][a-z0-9]*-m(\d+)(?=[\s:)\]—-]|$)", ln)  # lowercase slug heading
        if mlow:
            return "M" + mlow.group(1), _strip_md(re.sub(r"^#{2,5}\s*(?:\[[^\]]*\]\s*)?", "", ln))
    # **`slug-m1-…` — title**  — the milestone number must be followed by '-' or the closing
    # backtick (so an instance type like 'm6i' inside the slug is NOT mistaken for milestone m6).
    m = re.match(r"^\s*[-*]?\s*\*\*`[a-z0-9][a-z0-9-]*?-m(\d+)(?=[-`])[a-z0-9-]*`\s*[—-]?\s*(.*?)\*\*", ln)
    if m:
        return "M" + m.group(1), _strip_md(m.group(2))
    m = re.match(r"^\s*[-*]\s*(?:\[[ xX/~\-]\]\s*)?\*\*(M\d+|SP\d+)(?=[:\s*])(.*)$", ln)  # - [x] **M1:** title
    if m:
        return m.group(1), _strip_md(m.group(2))
    m = re.match(r"^\*\*(SP\d+)(?=[:\s])(.*)$", ln)                     # **SP1 status:** ...
    if m:
        return m.group(1), _strip_md(m.group(2))
    return None


# Trailing roadmap sections that are NOT milestone content — a milestone's status scan must stop here
# so a following generic checklist (the universal Definition of Done) isn't read as its status.
_SECTION_BREAK = re.compile(
    r"^#{1,6}\s+(Definition of Done|Cross[- ]?references?|Revision[- ]history|Change[- ]?log|"
    r"Appendix|Glossary|References|Related\b|Acceptance criteria|Notes\b|"
    r"Review checkpoints?\b)", re.I)  # /handoff review audit tasks — never milestone status


def _heading_has_marker(ln):
    """True if a subject line itself carries an explicit status token (checkbox or legacy
    word) — mirrors the non-default branches of status_of(). A bare `#### M1: …` returns False."""
    return bool(re.search(r"\[[ xX/~\-]\]", ln) or "✅" in ln or "🚧" in ln
                or re.search(r"\b(SHIPPED|DONE|COMPLETE|LIVE)\b", ln)
                or re.search(r"(WIP|IN[\s-]?PROGRESS)", ln, re.I))


def resolve_status(lines, i, end):
    """The status the board assigns to the milestone whose subject is lines[i] (section [i+1,end)),
    plus whether that status was EXPLICITLY declared. Returns (status, explicit).

    explicit is False only when the milestone has NO status marker anywhere in its section — the
    drift case where it silently defaults to `pending`. This is what the linter flags. Kept in
    lock-step with parse_roadmap's own resolution so the guard can never disagree with the board."""
    st = status_of(lines[i])
    explicit = _heading_has_marker(lines[i])
    for j in range(i + 1, end):
        # Stop at a trailing section-break heading. Its generic `- [ ]` checklist (esp. the universal
        # "Definition of Done") is NOT this milestone's status — without this the LAST milestone before
        # such a block silently inherits the block's first checkbox. Matched by TITLE so it never fires
        # on a milestone's own sub-headings (### Objective / ### Stories / ### LIVE-GROUNDED REALITY …).
        if _SECTION_BREAK.match(lines[j]):
            break
        ds = line_status(lines[j])
        if ds is not None:
            explicit = True
            if _RANK[ds] > _RANK[st]:
                st = ds
            break  # FIRST status-decl only — avoids per-story tick leakage
    return st, explicit


def parse_checkpoints(lines):
    """Review-checkpoint audit tasks (`/handoff review` → handoff-contract.md §6). These are
    deliberately NOT milestones (see _SECTION_BREAK), but the board renders them as a distinct
    audit-flag class so a session's end + audit points are visible. The line pattern
    (`- [ ] (optional) session audit …`) is unique enough to scan for anywhere in the doc.
    Each: {date, covers, handoff, reviewer, done}."""
    out, in_section, section_level = [], False, None
    in_html_comment = False
    for ln in lines:
        # Roadmap templates document the checkpoint syntax inside HTML comments. Strip those
        # comments before looking for headings or checkboxes so placeholders such as
        # ``plans/<file>`` can never become live review records or generated Obsidian links.
        visible = ln
        while visible:
            if in_html_comment:
                comment_end = visible.find("-->")
                if comment_end < 0:
                    visible = ""
                    break
                visible = visible[comment_end + 3:]
                in_html_comment = False
            comment_start = visible.find("<!--")
            if comment_start < 0:
                break
            comment_end = visible.find("-->", comment_start + 4)
            if comment_end < 0:
                visible = visible[:comment_start]
                in_html_comment = True
                break
            visible = visible[:comment_start] + visible[comment_end + 3:]
        ln = visible
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", ln)
        if heading:
            level, title = len(heading.group(1)), heading.group(2)
            if re.fullmatch(r"Review checkpoints?", title, re.I):
                in_section, section_level = True, level
                continue
            if in_section and level <= section_level:
                in_section = False
        if not in_section:
            continue
        m = re.match(r"^\s*-\s*\[([ xX])\]\s*\(optional\)\s*session audit\b(.*)$", ln, re.I)
        if not m:
            continue
        done, rest = m.group(1) in "xX", m.group(2)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", rest)
        cov = re.search(r"covers\s+(.*?)(?:·|·|handoff:|reviewer:|$)", rest, re.I)
        rev = re.search(r"reviewer:\s*([^\s·]+)", rest, re.I)
        hnd = re.search(r"handoff:\s*`?([^`·]+?)`?(?:\s*·|$)", rest, re.I)
        out.append({
            "date": dm.group(1) if dm else "",
            "covers": _strip_md(cov.group(1)).strip(" ·") if cov else "",
            "reviewer": rev.group(1).strip() if rev else "",
            "handoff": hnd.group(1).strip() if hnd else "",
            "done": done,
        })
    return out


def _frontmatter(path):
    """Read the small scalar/list subset used by the handoff contract without a YAML dependency."""
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out, list_key = {}, None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        item = re.match(r"^\s+-\s+(.+?)\s*$", line)
        if item and list_key:
            out.setdefault(list_key, []).append(item.group(1).strip(' "\''))
            continue
        field = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
        if not field:
            continue
        key, value = field.group(1), field.group(2).strip()
        if value:
            out[key] = value.strip(' "\'')
            list_key = None
        else:
            out[key] = []
            list_key = key
    return out


def enrich_checkpoints(m, checkpoints, roadmap_path, items=None):
    """Join parser-safe roadmap checkpoints to their review-handoff frontmatter.

    The roadmap checkbox remains the compact completion marker, while the review handoff owns the
    richer requested -> in-review -> verdict-received -> closed lifecycle. Drift between the two
    artifacts is surfaced as ``sync-needed`` instead of being silently flattened to open/closed.
    """
    enriched = []
    for checkpoint in checkpoints:
        ck = dict(checkpoint)
        ck.update({"checkpoint_date": ck.get("date", ""), "checkpoint_reviewer": ck.get("reviewer", ""),
                   "checkpoint_covers": ck.get("covers", "")})
        rel = ck.get("handoff", "").strip().strip("`").lstrip("/")
        root = os.path.abspath(m["vault_root"])
        handoff_path = os.path.abspath(os.path.join(root, rel)) if rel else ""
        inside_root = bool(handoff_path) and os.path.commonpath((root, handoff_path)) == root
        meta = _frontmatter(handoff_path) if handoff_path else {}
        state = str(meta.get("review_status") or "").lower()
        canonical_reviewer = str(meta.get("reviewer_target") or "")
        covered = meta.get("milestones_covered") if isinstance(meta.get("milestones_covered"), list) else []
        roadmap_covered = [part.strip() for part in ck.get("covers", "").split(",") if part.strip()]
        binding_errors = []
        if not rel:
            binding_errors.append("missing-handoff-path")
        elif not inside_root:
            binding_errors.append("handoff-outside-workspace")
        elif not meta:
            binding_errors.append("missing-or-unparseable-handoff")
        required = {
            "authorship": "agent-generated", "type": "handoff", "handoff_kind": "review",
            "status": "complete",
        }
        for key, expected in required.items():
            if meta and meta.get(key) != expected:
                binding_errors.append(f"invalid-{key}")
        for key in ("project", "date", "companion", "roadmap", "reviewer_target", "review_status"):
            if meta and not meta.get(key):
                binding_errors.append(f"missing-{key}")
        if meta and state not in {"requested", "in-review", "verdict-received", "closed"}:
            binding_errors.append("invalid-review-status")
        if meta and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(meta.get("date", ""))):
            binding_errors.append("invalid-date")
        if meta and not covered:
            binding_errors.append("missing-milestones-covered")
        tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
        if meta and state and f"review/{state}" not in tags:
            binding_errors.append("review-tag-status-mismatch")
        if meta.get("roadmap"):
            declared = os.path.abspath(os.path.join(root, str(meta["roadmap"]).lstrip("/")))
            if declared != os.path.abspath(roadmap_path):
                binding_errors.append("roadmap-backlink-mismatch")
        mismatches = []
        if state in {"requested", "in-review", "verdict-received", "closed"} and bool(ck["done"]) != (state == "closed"):
            mismatches.append("status")
        if meta.get("date") and ck.get("date") and str(meta["date"]) != ck["date"]:
            mismatches.append("date")
        if canonical_reviewer and ck.get("reviewer") and canonical_reviewer != ck["reviewer"]:
            mismatches.append("reviewer")
        if covered and set(covered) != set(roadmap_covered):
            mismatches.append("scope")
        display_status = "unresolved" if binding_errors else ("drift" if mismatches else state)
        refs = {}
        for item in items or []:
            for ref in (item.get("canonical_id"), item.get("id")):
                if ref:
                    refs[str(ref).lower()] = item["id"]
        resolved = [value for value in (refs.get(ref.lower()) for ref in (covered or roadmap_covered)) if value]
        additional_scope = [ref for ref in (covered or roadmap_covered) if ref.lower() not in refs]
        ck.update({
            "date": str(meta.get("date") or ck.get("date") or ""),
            "covers": ", ".join(covered or roadmap_covered),
            "reviewer": canonical_reviewer or ck.get("reviewer") or "",
            "review_status": state or "unresolved",
            "display_status": display_status,
            "reviewer_target": canonical_reviewer,
            "milestones_covered": covered or roadmap_covered,
            "resolved_item_ids": resolved,
            "additional_scope": additional_scope,
            "handoff_exists": bool(meta),
            "binding_errors": binding_errors,
            "mismatches": mismatches,
        })
        enriched.append(ck)
    return enriched


def parse_roadmap(path):
    """Return {title, status, epics:[{id,title}], items:[{id,title,status}]} for one roadmap.

    Detects milestones across the formats in use: `#### M1: …`, `## Milestone m7 — …`,
    `**\\`slug-m1-…\\` — …**` (id mid-slug), `- [x] **M1:** …`, `- **SP1:** …`. Status for each
    milestone = the FIRST status-declaration line in its SECTION (heading → next milestone), so a
    `✅ DONE` bullet a few lines under the heading is attributed to it. Per-story ticks don't leak."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    proj_status = "active"
    epics = []

    # collect epics + subject lines (with index)
    subs, current_epic, current_lane = [], None, None
    for i, ln in enumerate(lines):
        sm = re.match(r"^status:\s*([A-Za-z]+)", ln)
        if sm:
            proj_status = sm.group(1).lower()
        lane = re.match(r"^#{2,5}\s+(Now|Next|Later)\b", ln, re.I)
        if lane:
            current_lane = lane.group(1).lower()
        # Epic ids: numeric (E1, E12) or letter-suffixed (E-A, E-B) — the `E-[A-Z0-9]+` form
        # requires the hyphen, so acronym headings like `### ECR …` / `### EKS …` never match.
        em = re.match(r"^###\s+(E\d+|E-[A-Z0-9]+)\b[:\s]*(.*)", ln)
        if em:
            current_epic = em.group(1)
            epics.append({"id": current_epic, "title": _strip_md(em.group(2))[:120]})
            continue
        sub = subject(ln)
        if sub:
            subs.append((i, sub[0], sub[1], canonical_ref(ln), current_epic, current_lane))

    # status per milestone = best of its heading + the FIRST status-decl in its section
    items, order = {}, []
    for k, (i, idv, title, canonical_id, epic_id, lane) in enumerate(subs):
        end = subs[k + 1][0] if k + 1 < len(subs) else len(lines)
        st, _explicit = resolve_status(lines, i, end)
        clean = re.sub(r"^[(\[]?(?:M\d+|SP\d+)[)\]]?\b\s*[:\-—]?\s*", "", title).strip()
        semantic = _semantic_title(clean)
        if idv not in items:
            items[idv] = {"title": (semantic or idv), "source_subject": (clean or idv), "status": st,
                          "kind": "spike" if idv.startswith("SP") else "milestone",
                          "canonical_id": canonical_id, "epic_id": epic_id, "lane": lane,
                          "source_order": len(order)}
            order.append(idv)
        else:
            if _RANK[st] > _RANK[items[idv]["status"]]:
                items[idv]["status"] = st
            for key, value in (("canonical_id", canonical_id), ("epic_id", epic_id), ("lane", lane)):
                if not items[idv].get(key) and value:
                    items[idv][key] = value
    return {"title": os.path.basename(path)[:-3], "status": proj_status, "epics": epics,
            "items": [{"id": i, **items[i]} for i in order],
            "checkpoints": parse_checkpoints(lines)}


# ---- lint guard: catch milestones authored with NO status line (silent-pending drift) ----
def lint_roadmap(path):
    """Return the milestones/spikes in one roadmap that have NO status marker anywhere in their
    section — the ones the board silently renders as `pending` because nobody declared a status.
    Each: {id, line, title}. An id that appears on several subject lines (e.g. a `#### M1:` heading
    plus a `- **M1:**` run-with pointer) is a violation only if EVERY occurrence is unmarked."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    subs = [(i, s[0], s[1]) for i, ln in enumerate(lines) if (s := subject(ln))]
    per_id = {}
    for k, (i, idv, title) in enumerate(subs):
        end = subs[k + 1][0] if k + 1 < len(subs) else len(lines)
        _st, explicit = resolve_status(lines, i, end)
        rec = per_id.setdefault(idv, {"explicit": False, "line": i + 1, "title": title[:60]})
        if explicit:
            rec["explicit"] = True
    return [{"id": idv, "line": r["line"], "title": r["title"]}
            for idv, r in per_id.items() if not r["explicit"]]


def lint_all(m):
    """Lint every discovered roadmap across all projects. Print a report; return the violation count
    (0 = clean). Used by CI / the refresh backstop so status-line drift can't silently return."""
    seen, total = set(), 0
    for _proj, cfg in m["projects"].items():
        for path in discover_roadmaps(m, cfg):
            if path in seen:
                continue
            seen.add(path)
            v = lint_roadmap(path)
            if v:
                total += len(v)
                print(f"\n{os.path.relpath(path, m['vault_root'])}:")
                for it in v:
                    print(f"  line {it['line']:>4}  {it['id']:<5} NO status line (defaults to pending): {it['title']}")
    if total:
        print(f"\n✗ {total} milestone(s) lack a status line. Add `- [ ]` / `- [/]` / `- [x]` under "
              "each heading (convention: pending / in-progress / done) so the board reflects reality.")
    else:
        print("✓ roadmap-status lint clean — every detected milestone/spike has an explicit status line.")
    return total


# ---- roadmap discovery (mirrors project-linker matching: slugs OR contains) ----
def _matches(name, cfg):
    low = name.lower()
    if any(x.lower() in low for x in cfg.get("excludes", [])):
        return False
    return (any(low.startswith(s.lower()) for s in cfg.get("slugs", []))
            or any(c.lower() in low for c in cfg.get("contains", [])))


def discover_roadmaps(m, cfg):
    found = {}
    for region_rel in m["regions"].values():
        region_abs = os.path.join(m["vault_root"], region_rel)
        if not os.path.isdir(region_abs):
            continue
        for name in os.listdir(region_abs):
            low = name.lower()
            if (low.endswith(".md") and "roadmap" in low and "handoff" not in low
                    and _matches(name, cfg)
                    and not re.search(r"revision|changelist", low)):  # changelists/diffs are not roadmaps
                if name not in found or "/platform/" in found[name]:
                    found[name] = os.path.join(region_abs, name)
    # source-repo roadmaps (<app>/plans) — attributed by LOCATION, fill gaps not already found
    for app in cfg.get("app_dirs", []):
        d = os.path.join(m["vault_root"], app, "plans")
        if os.path.isdir(d):
            for name in os.listdir(d):
                low = name.lower()
                if (low.endswith(".md") and "roadmap" in low and "handoff" not in low
                        and name not in found
                        and not re.search(r"revision|changelist", low)):
                    found[name] = os.path.join(d, name)
    return [found[k] for k in sorted(found)]


def discover_review_handoffs(m, cfg):
    """Find project-attributable review handoffs so missing checkpoints fail visibly."""
    found = {}
    search_dirs = [os.path.join(m["vault_root"], rel) for rel in m.get("regions", {}).values()]
    search_dirs.extend(os.path.join(m["vault_root"], app, "plans") for app in cfg.get("app_dirs", []))
    for directory in search_dirs:
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            low = name.lower()
            claim_name = re.sub(r"^handoff-\d{4}-\d{2}-\d{2}-", "", name, flags=re.I)
            if low.endswith("session-review.md") and _matches(claim_name, cfg):
                found.setdefault(name, os.path.join(directory, name))
    return [found[name] for name in sorted(found)]


def orphan_review_records(m, cfg, roadmaps):
    """Return review handoffs that are attributable to the project but lack a roadmap checkpoint."""
    bound = {
        os.path.normpath(ck.get("handoff", "").strip().strip("`").lstrip("/"))
        for _path, rm in roadmaps for ck in rm.get("checkpoints", []) if ck.get("handoff")
    }
    out = []
    for handoff_path in discover_review_handoffs(m, cfg):
        rel = os.path.normpath(os.path.relpath(handoff_path, m["vault_root"]))
        if rel in bound:
            continue
        meta = _frontmatter(handoff_path)
        covered = meta.get("milestones_covered") if isinstance(meta.get("milestones_covered"), list) else []
        declared_roadmap = str(meta.get("roadmap") or "")
        roadmap_path = os.path.join(m["vault_root"], declared_roadmap) if declared_roadmap else handoff_path
        seed = {
            "date": str(meta.get("date") or ""), "covers": ", ".join(covered),
            "reviewer": str(meta.get("reviewer_target") or ""), "handoff": rel,
            "done": meta.get("review_status") == "closed",
        }
        record = enrich_checkpoints(m, [seed], roadmap_path, items=[])[0]
        record["binding_errors"] = list(record.get("binding_errors", [])) + ["missing-roadmap-checkpoint"]
        record["display_status"] = "unresolved"
        record["orphan"] = True
        record["roadmap_label"] = os.path.basename(declared_roadmap)[:-3] if declared_roadmap else "unbound review"
        out.append(record)
    return out


def short_label(path, cfg):
    original = os.path.basename(path)[:-3]
    base = original
    for s in sorted(cfg.get("slugs", []), key=len, reverse=True):
        if base.lower().startswith(s.lower()):
            base = base[len(s):]
            break
    base = base.strip("-")
    if base.endswith("-roadmap"):
        base = base[:-8]
    if base and base != "roadmap":
        return base
    return original[:-8] if original.endswith("-roadmap") else original


# ---- excalidraw element emitters ----
def _token(key, length=10):
    return hashlib.sha256(key.encode()).hexdigest()[:length]


def _rect(nid, x, y, w, h, colors, link=None, stroke=1, opacity=100):
    bg, st = colors
    return {"id": nid, "type": "rectangle", "x": x, "y": y, "width": w, "height": h, "angle": 0,
            "strokeColor": st, "backgroundColor": bg, "fillStyle": "solid", "strokeWidth": stroke,
            "strokeStyle": "solid", "roughness": 0, "opacity": opacity, "groupIds": [],
            "roundness": None, "seed": _h(nid + "s"), "version": 1,
            "versionNonce": _h(nid + "v"), "isDeleted": False,
            "boundElements": [], "updated": 1, "link": link, "locked": False}


def _box(nid, x, y, w, h, colors, text_id, link, stroke=1):
    box = _rect(nid, x, y, w, h, colors, link=link, stroke=stroke)
    box["boundElements"] = [{"type": "text", "id": text_id}]
    return box


def _text(tid, box, label, size=14, color="#0f172a", align="center", padding=10):
    return {"id": tid, "type": "text", "x": box["x"] + padding, "y": box["y"] + padding,
            "width": max(1, box["width"] - 2 * padding),
            "height": max(1, box["height"] - 2 * padding), "angle": 0, "strokeColor": color,
            "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": 1,
            "strokeStyle": "solid", "roughness": 0, "opacity": 100, "groupIds": [], "roundness": None,
            "seed": _h(tid + "s"), "version": 1, "versionNonce": _h(tid + "v"), "isDeleted": False,
            "boundElements": None, "updated": 1, "link": None, "locked": False, "text": label,
            "fontSize": size, "fontFamily": 2, "textAlign": align, "verticalAlign": "middle",
            "containerId": box["id"], "originalText": label, "lineHeight": 1.25, "baseline": size - 2}


def _labeled_box(els, nid, x, y, w, h, colors, label, *, link=None, size=14,
                 color="#0f172a", align="center", stroke=1, padding=10):
    tid = nid + "-text"
    box = _box(nid, x, y, w, h, colors, tid, link, stroke=stroke)
    els.extend((box, _text(tid, box, label, size, color=color, align=align, padding=padding)))
    return box


def _railed_card(els, nid, x, y, w, h, colors, label, rail_color, *, link=None,
                 size=13, color="#0f172a", stroke=1):
    """Visible square card + flush rail + structurally inset text container.

    Excalidraw normalizes bound-text padding when it compresses/saves a drawing. Offsetting the
    transparent text container itself keeps a durable 24px gutter even after that normalization.
    """
    outer = _rect(nid, x, y, w, h, colors, stroke=stroke)
    rail = _rect(nid + "-rail", x, y, 8, h, (rail_color, rail_color), stroke=0)
    content_id, text_id = nid + "-content", nid + "-content-text"
    content = _box(content_id, x + 24, y, w - 24, h, ("transparent", "transparent"),
                   text_id, link, stroke=0)
    text_element = _text(text_id, content, label, size, color=color, align="left", padding=8)
    els.extend((outer, rail, content, text_element))
    return outer


def _source_label(m, cfg, path):
    """Return the canonical _sources region label for an attributed source path."""
    parent = os.path.abspath(os.path.dirname(path))
    for label, region_rel in m.get("regions", {}).items():
        if parent == os.path.abspath(os.path.join(m["vault_root"], region_rel)):
            return label
    for app in cfg.get("app_dirs", []):
        for sub, label in (("plans", "src-plans"), ("docs", "src-docs")):
            if parent == os.path.abspath(os.path.join(m["vault_root"], app, sub)):
                return label
    for deliverable_dir in cfg.get("deliverable_dirs", []):
        if parent == os.path.abspath(os.path.join(m["vault_root"], deliverable_dir)):
            return "deliverables"
    return None


def presentation_source_path(m, project, cfg, path):
    """Return a vault-relative canonical source alias, never a source-workspace path."""
    label = _source_label(m, cfg, path)
    if not label:
        raise ValueError(f"roadmap source has no presentation alias region: {path}")
    pv = m["presentation_vault"]
    parts = (pv["projects_root"], project, pv["source_alias_dir"], label, os.path.basename(path))
    return "/".join(str(part).strip("/") for part in parts if str(part).strip("/"))


def link_to(m, project, cfg, path):
    """Portable Obsidian URI: vault identity + vault-relative canonical alias."""
    pv = m["presentation_vault"]
    query = urllib.parse.urlencode(
        {"vault": pv["name"], "file": presentation_source_path(m, project, cfg, path)},
        quote_via=urllib.parse.quote,
    )
    return "obsidian://open?" + query


def project_hub_link(m, project, scene_sig=None):
    pv = m["presentation_vault"]
    target = "/".join((pv["projects_root"].strip("/"), project, "_index.md"))
    params = {"vault": pv["name"], "file": target}
    if scene_sig:
        params["scene_sig"] = scene_sig
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return "obsidian://open?" + query


def handoff_link_to(m, project, checkpoint, cfg=None):
    """Portable link to the review handoff itself, not merely its owning roadmap."""
    rel = checkpoint.get("handoff", "").strip().strip("`").lstrip("/")
    if not rel:
        return None
    source_label = None
    parent = os.path.abspath(os.path.dirname(os.path.join(m["vault_root"], rel)))
    for label, region_rel in m.get("regions", {}).items():
        if parent == os.path.abspath(os.path.join(m["vault_root"], region_rel)):
            source_label = label
            break
    if not source_label:
        for app in (cfg or {}).get("app_dirs", []):
            if parent == os.path.abspath(os.path.join(m["vault_root"], app, "plans")):
                source_label = "src-plans"
                break
    if not source_label:
        return None
    pv = m["presentation_vault"]
    alias = "/".join((pv["projects_root"].strip("/"), project,
                      pv["source_alias_dir"].strip("/"), source_label, os.path.basename(rel)))
    query = urllib.parse.urlencode({"vault": pv["name"], "file": alias}, quote_via=urllib.parse.quote)
    return "obsidian://open?" + query


def roadmap_lane(rm):
    """Map one roadmap into an action-oriented portfolio lane."""
    if rm.get("status") in {"cancelled", "superseded"}:
        return "archive"
    if any(ck.get("display_status") in {"drift", "unresolved"} or
           ck.get("review_status", "closed" if ck.get("done") else "requested") != "closed"
           for ck in rm.get("checkpoints", [])):
        return "focus"
    statuses = [item["status"] for item in rm.get("items", [])]
    if "active" in statuses:
        return "focus"
    if statuses and all(status == "done" for status in statuses):
        return "complete"
    if "done" in statuses:
        return "underway"
    return "queued"


def build_board(m, project, cfg, roadmaps, orphan_reviews=None):
    """Render a fixed-width portfolio pulse; completed history stays summarized, not duplicated."""
    CANVAS_W, MARGIN, GAP = 1440, 48, 24
    CONTENT_W, REVIEW_W = CANVAS_W - 2 * MARGIN, 660
    if len(roadmaps) > 12:
        roadmap_columns, pulse_limit = 3, 1
    elif len(roadmaps) > 6:
        roadmap_columns, pulse_limit = 3, 2
    else:
        roadmap_columns, pulse_limit = 2, 4
    PANEL_PAD, ITEM_H, ITEM_GAP = 24, 82, 12
    element_ns, els = cfg["project_id"], []
    orphan_reviews = orphan_reviews or []
    sig = _board_sig(m, project, cfg, roadmaps, orphan_reviews)

    counted = [rm for _path, rm in roadmaps if rm["status"] not in {"cancelled", "superseded"}]
    total_done = sum(sum(item["status"] == "done" for item in rm["items"]) for rm in counted)
    total_items = sum(len(rm["items"]) for rm in counted)
    active_items = sum(sum(item["status"] == "active" for item in rm["items"]) for rm in counted)
    pct = int(100 * total_done / total_items) if total_items else 0

    reviews = []
    for path, rm in roadmaps:
        for checkpoint in rm.get("checkpoints", []):
            reviews.append({**checkpoint, "roadmap_path": path,
                            "roadmap_label": short_label(path, cfg), "orphan": False})
    reviews.extend(orphan_reviews)
    open_reviews = sum(record.get("review_status") != "closed" or
                       record.get("display_status") in {"drift", "unresolved"} for record in reviews)

    # A transparent origin anchor makes fit-to-content preserve the top breathing room; without an
    # actual element at y=0 the plugin trims the whitespace and floats its toolbar over the header.
    els.append(_rect(f"{element_ns}-viewport-anchor", MARGIN, 0, 1, 1,
                     ("#f8fafc", "#f8fafc"), stroke=0, opacity=0))
    # Header: stable top padding prevents the Excalidraw toolbar obscuring the project identity.
    y = 72
    header_id = f"{element_ns}-scene-{sig}-header"
    els.append(_rect(header_id, MARGIN, y, CONTENT_W, 132, C_HEADER,
                     link=project_hub_link(m, project, sig), stroke=0))
    _labeled_box(els, header_id + "-title", MARGIN + 24, y + 24, 620, 84, C_HEADER,
                 f"{project}\nROADMAP PULSE · LIVE SOURCES", size=24,
                 color="#f8fafc", align="left", stroke=0, padding=8)
    chip_specs = (
        (f"{pct}%\nCOMPLETE", ("#1e293b", "#334155")),
        (f"{active_items}\nACTIVE", ("#422006", "#d97706")),
        (f"{open_reviews}\nOPEN REVIEWS", ("#4c0519", "#e11d48")),
        (f"{len(roadmaps)}\nROADMAPS", ("#172554", "#3b82f6")),
    )
    chip_w, chip_gap = 148, 12
    chip_x = MARGIN + CONTENT_W - 24 - (len(chip_specs) * chip_w + (len(chip_specs) - 1) * chip_gap)
    for i, (label, colors) in enumerate(chip_specs):
        _labeled_box(els, f"{header_id}-chip-{i}", chip_x + i * (chip_w + chip_gap), y + 34,
                     chip_w, 64, colors, label, size=13, color="#f8fafc", stroke=1)

    # Compact legend: visual type and delivery state are intentionally orthogonal.
    y += 152
    legend = (
        ("M  MILESTONE", C_MILESTONE, "#1d4ed8"), ("SP  SPIKE", C_SPIKE, "#6d28d9"),
        ("↗  REVIEW", C_AUDIT, "#9f1239"),
        ("✓  DONE", ("#f0fdf4", "#22c55e"), "#166534"),
        ("●  ACTIVE", ("#fffbeb", "#f59e0b"), "#92400e"),
        ("○  QUEUED", ("#f1f5f9", "#94a3b8"), "#475569"),
    )
    legend_gap, legend_w = 12, (CONTENT_W - 5 * 12) // 6
    for i, (label, colors, color) in enumerate(legend):
        _labeled_box(els, f"{element_ns}-legend-{i}", MARGIN + i * (legend_w + legend_gap), y,
                     legend_w, 42, colors, label, size=12, color=color)
    y += 70

    # The review rail uses the handoff frontmatter lifecycle and links to the handoff itself.
    if reviews:
        reviews.sort(key=lambda r: (r.get("display_status") == "closed",
                                    r.get("date", ""), r.get("handoff", "")), reverse=False)
        pending_reviews = [r for r in reviews if r.get("display_status") != "closed"]
        closed_reviews = sorted((r for r in reviews if r.get("display_status") == "closed"),
                                key=lambda r: r.get("date", ""), reverse=True)
        shown_reviews = pending_reviews + closed_reviews[:MAX_CLOSED_REVIEWS]
        hidden_closed = max(0, len(closed_reviews) - MAX_CLOSED_REVIEWS)
        review_header = (f"REVIEW HANDOFFS · {open_reviews} OPEN · {len(closed_reviews)} CLOSED\n"
                         "Frontmatter is authoritative; drift and unresolved bindings fail visibly")
        if hidden_closed:
            review_header += f" · {hidden_closed} older closed hidden"
        _labeled_box(els, f"{element_ns}-reviews-header", MARGIN, y, CONTENT_W, 58, C_AUDIT,
                     review_header, size=14, color="#9f1239", align="left", padding=16)
        y += 76
        review_h = 108
        for i, record in enumerate(shown_reviews):
            col, row = i % 2, i // 2
            x0, y0 = MARGIN + col * (REVIEW_W + GAP), y + row * (review_h + 16)
            status = record.get("display_status", "unresolved")
            colors, state_label, color = REVIEW_VISUAL.get(status, REVIEW_VISUAL["unresolved"])
            rid = f"{element_ns}-review-{_token(record.get('handoff', '') + '|' + str(record.get('roadmap_label', '')))}"
            problems = record.get("binding_errors", []) + record.get("mismatches", [])
            scope = record.get("milestones_covered", [])
            scope_text = ", ".join(scope) if scope else "scope unresolved"
            resolved = len(record.get("resolved_item_ids", []))
            additional = len(record.get("additional_scope", []))
            detail = ("Needs repair · " + ", ".join(problems)) if problems else (
                f"{record.get('roadmap_label', 'roadmap')} · {resolved} linked · {additional} additional")
            label = (f"{state_label} · {record.get('date') or 'date unresolved'} · "
                     f"{record.get('reviewer_target') or record.get('reviewer') or 'reviewer unresolved'}\n"
                     f"{detail}\nCovers · {_display_title(scope_text, 92)}")
            review_link = handoff_link_to(m, project, record, cfg)
            _railed_card(els, rid, x0, y0, REVIEW_W, review_h, colors, label, colors[1],
                         link=review_link, size=13, color=color, stroke=2)
        y += ((len(shown_reviews) + 1) // 2) * (review_h + 16) + 12

    groups = {key: [] for key, *_rest in LANES}
    for path, rm in roadmaps:
        groups[roadmap_lane(rm)].append((path, rm))

    def pulse_items(rm):
        active = [item for item in rm["items"] if item["status"] == "active"]
        pending = [item for item in rm["items"] if item["status"] == "pending"]
        if active or pending:
            return (active + pending)[:pulse_limit]
        return [item for item in rm["items"] if item["status"] == "done"][-min(3, pulse_limit):]

    def panel_height(rm, compact=False):
        if compact:
            return 158
        count = len(pulse_items(rm))
        return 266 if not count else 254 + count * (ITEM_H + ITEM_GAP)

    def panel_sort(entry):
        path, rm = entry
        reviews_open = sum(ck.get("display_status") != "closed" for ck in rm.get("checkpoints", []))
        active = sum(item["status"] == "active" for item in rm["items"])
        return (-reviews_open, -active, short_label(path, cfg).lower())

    for lane_key, lane_title, lane_desc, lane_colors, lane_text in LANES:
        entries = sorted(groups[lane_key], key=panel_sort)
        if not entries:
            continue
        _labeled_box(els, f"{element_ns}-lane-{lane_key}", MARGIN, y, CONTENT_W, 58, lane_colors,
                     f"{lane_title} · {len(entries)} ROADMAPS\n{lane_desc}", size=14,
                     color=lane_text, align="left", padding=16)
        y += 76
        compact_lane = len(roadmaps) > 6 and lane_key in {"complete", "archive"}
        lane_columns = 4 if compact_lane else roadmap_columns
        lane_panel_w = (CONTENT_W - (lane_columns - 1) * GAP) // lane_columns
        for row_start in range(0, len(entries), lane_columns):
            row_entries = entries[row_start:row_start + lane_columns]
            row_h = max(panel_height(rm, compact_lane) for _path, rm in row_entries)
            for col, (path, rm) in enumerate(row_entries):
                x0, y0 = MARGIN + col * (lane_panel_w + GAP), y
                ph, panel_link = panel_height(rm, compact_lane), link_to(m, project, cfg, path)
                panel_w = lane_panel_w
                roadmap_id = f"{element_ns}-roadmap-{_token(presentation_source_path(m, project, cfg, path))}"
                els.append(_rect(roadmap_id, x0, y0, panel_w, ph, C_PANEL, link=panel_link))
                done = sum(item["status"] == "done" for item in rm["items"])
                active = sum(item["status"] == "active" for item in rm["items"])
                pending = sum(item["status"] == "pending" for item in rm["items"])
                total = len(rm["items"])
                rpct = int(100 * done / total) if total else 0
                review_count = sum(ck.get("display_status") != "closed" for ck in rm.get("checkpoints", []))
                if compact_lane:
                    _labeled_box(els, roadmap_id + "-title", x0 + 16, y0 + 14, panel_w - 32, 56,
                                 C_PANEL, f"{_display_title(short_label(path, cfg), 36)}\n"
                                 f"{rm['status'].upper()} · {len(rm['epics'])} EPICS", link=panel_link,
                                 size=12, align="left", stroke=0, padding=4)
                    bar_x, bar_y, bar_w = x0 + 16, y0 + 78, panel_w - 32
                    els.append(_rect(roadmap_id + "-bar-bg", bar_x, bar_y, bar_w, 8,
                                     ("#e2e8f0", "#e2e8f0"), stroke=0))
                    if total and done:
                        els.append(_rect(roadmap_id + "-bar-fill", bar_x, bar_y, bar_w * done / total, 8,
                                         ("#22c55e", "#22c55e"), stroke=0))
                    _labeled_box(els, roadmap_id + "-summary", x0 + 16, y0 + 98, panel_w - 32, 44,
                                 lane_colors, f"{done}/{total} · {rpct}%\nHISTORY COLLAPSED",
                                 link=panel_link, size=10, color=lane_text, align="left", padding=10)
                    continue
                _labeled_box(els, roadmap_id + "-title", x0 + PANEL_PAD, y0 + 18,
                             panel_w - 2 * PANEL_PAD, 56,
                             C_PANEL, f"{_display_title(short_label(path, cfg), 52)}\n"
                             f"{rm['status'].upper()} · {len(rm['epics'])} EPICS", link=panel_link,
                             size=15, align="left", stroke=0, padding=4)
                summary = f"{done}/{total} · {rpct}%\n{active} ACTIVE · {pending} QUEUED"
                if review_count:
                    summary = f"{done}/{total} · {rpct}%\n{review_count} REVIEW OPEN"
                bar_x, bar_y, bar_w = x0 + PANEL_PAD, y0 + 84, panel_w - 2 * PANEL_PAD
                els.append(_rect(roadmap_id + "-bar-bg", bar_x, bar_y, bar_w, 8,
                                 ("#e2e8f0", "#e2e8f0"), stroke=0))
                if total and done:
                    els.append(_rect(roadmap_id + "-bar-fill", bar_x, bar_y, bar_w * done / total, 8,
                                     ("#22c55e", "#22c55e"), stroke=0))
                _labeled_box(els, roadmap_id + "-summary", x0 + PANEL_PAD, y0 + 104,
                             panel_w - 2 * PANEL_PAD, 40, lane_colors, summary,
                             link=panel_link, size=11, color=lane_text, align="left", padding=12)
                epic_ids = [epic["id"] for epic in rm["epics"]]
                epic_label = "EPICS · " + ("  ".join(epic_ids[:10]) if epic_ids else "none declared")
                if len(epic_ids) > 10:
                    epic_label += f"  +{len(epic_ids) - 10}"
                _labeled_box(els, roadmap_id + "-epics", x0 + PANEL_PAD, y0 + 156,
                             panel_w - 2 * PANEL_PAD, 32, C_EPIC, epic_label, link=panel_link,
                             size=12, color="#475569", align="left", padding=12)
                items_y = y0 + 204
                shown = pulse_items(rm)
                for index, item in enumerate(shown):
                    iy = items_y + index * (ITEM_H + ITEM_GAP)
                    kind = item.get("kind") or ("spike" if item["id"].startswith("SP") else "milestone")
                    colors = C_SPIKE if kind == "spike" else C_MILESTONE
                    kind_label = "SPIKE" if kind == "spike" else "MILESTONE"
                    item_id = f"{roadmap_id}-item-{_token(item.get('canonical_id') or item['id'])}"
                    title = _display_title(item["title"], 60 if roadmap_columns == 3 else 96)
                    label = f"{item['id']} · {title}\n{kind_label}   {STATUS_LABEL[item['status']]}"
                    status_color = STATUS_STROKE[item["status"]]
                    _railed_card(els, item_id, x0 + PANEL_PAD, iy,
                                 panel_w - 2 * PANEL_PAD, ITEM_H, colors, label, status_color,
                                 link=panel_link, size=12 if roadmap_columns == 3 else 13)
                hidden = max(0, total - len(shown))
                footer_y = items_y + len(shown) * (ITEM_H + ITEM_GAP) - (ITEM_GAP if shown else 0) + 12
                footer = (f"SHOWING {len(shown)} OF {total} · {done} DONE IN HISTORY"
                          + (f" · {hidden} COLLAPSED" if hidden else ""))
                _labeled_box(els, roadmap_id + "-footer", x0 + PANEL_PAD, footer_y,
                             panel_w - 2 * PANEL_PAD, 32, C_EPIC, footer, link=panel_link,
                             size=10 if roadmap_columns == 3 else 11,
                             color="#64748b", align="left", padding=10)
            y += row_h + 24
        y += 12

    scene = {"type": "excalidraw", "version": 2, "source": "workspace-roadmap-status",
             "elements": els, "appState": {"gridSize": None, "viewBackgroundColor": "#f8fafc",
                                                "scrollX": 0, "scrollY": 0, "zoom": {"value": 0.5}},
             "files": {}}
    text = (f"---\nexcalidraw-plugin: parsed\nroadmap-board-sig: {sig}\n"
            "roadmap-board-layout: portfolio-pulse-v2\n"
            f"project_id: {cfg['project_id']}\npresentation_vault: {json.dumps(m['presentation_vault']['name'])}\n"
            "tags: [excalidraw, type/roadmap-status]\n---\n\n"
            "> Auto-generated LIVE portfolio pulse — regenerate, don't hand-edit. Blue=milestone · "
            "violet=spike · rose=review. Status is a separate accent. Open review cards link directly "
            "to their handoffs; completed history is summarized in each roadmap panel.\n\n"
            "# Excalidraw Data\n\n## Text Elements\n\n## Drawing\n```json\n"
            + json.dumps(scene, indent=0, ensure_ascii=False) + "\n```\n%%\n")
    return text, total_done, total_items, sig


def _board_sig(m, project, cfg, roadmaps, orphan_reviews=None):
    """Hash every rendered semantic plus the link/layout contract.

    The previous status-only signature left drawings stale after title, epic, or link changes.
    A schema version makes intentional renderer changes invalidate old generated boards once.
    """
    payload = {
        "schema": BOARD_SCHEMA_VERSION,
        "project": project,
        "project_id": cfg["project_id"],
        "presentation_vault": m["presentation_vault"]["name"],
        "roadmaps": [
            {
                "source": presentation_source_path(m, project, cfg, path),
                "label": short_label(path, cfg),
                "status": rm["status"],
                "title": rm["title"],
                "epics": rm["epics"],
                "items": [
                    {key: item.get(key) for key in ("id", "title", "status", "kind", "canonical_id",
                                                    "epic_id", "lane", "source_order")}
                    for item in rm["items"]
                ],
                "checkpoints": rm.get("checkpoints", []),
            }
            for path, rm in roadmaps
        ],
        "orphan_reviews": orphan_reviews or [],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()[:12]


def existing_sig(out_path):
    if not os.path.exists(out_path):
        return None
    with open(out_path, encoding="utf-8", errors="replace") as f:
        head = f.read(2000)
    m = re.search(r"roadmap-board-sig:\s*([0-9a-f]+)", head)
    return m.group(1) if m else None


def scene_fingerprint_present(out_path, cfg, sig):
    if not os.path.exists(out_path):
        return False
    with open(out_path, encoding="utf-8", errors="replace") as f:
        return f"scene_sig={sig}" in f.read()


def build_for_project(m, project, cfg, force=False, allow_delete=False):
    proj_folder = os.path.join(m["vault_root"], m["projects_root"], project)
    out_path = os.path.join(proj_folder, f"{project}-roadmaps.excalidraw.md")
    paths = discover_roadmaps(m, cfg)
    if not paths:
        if os.path.exists(out_path):
            if allow_delete:
                os.remove(out_path)
            else:
                return out_path, 0, 0
        return None, 0, 0
    roadmaps = []
    for path in paths:
        roadmap = parse_roadmap(path)
        roadmap["checkpoints"] = enrich_checkpoints(m, roadmap.get("checkpoints", []), path,
                                                     roadmap.get("items", []))
        roadmaps.append((path, roadmap))
    orphan_reviews = orphan_review_records(m, cfg, roadmaps)
    sig = _board_sig(m, project, cfg, roadmaps, orphan_reviews)
    if not force and existing_sig(out_path) == sig and scene_fingerprint_present(out_path, cfg, sig):
        counted = [rm for _p, rm in roadmaps if rm["status"] not in {"cancelled", "superseded"}]
        done = sum(sum(1 for it in rm["items"] if it["status"] == "done") for rm in counted)
        tot = sum(len(rm["items"]) for rm in counted)
        return out_path, done, tot
    text, done, tot, _sig = build_board(m, project, cfg, roadmaps, orphan_reviews)
    os.makedirs(proj_folder, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".roadmap-board-", suffix=".tmp", dir=proj_folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, out_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return out_path, done, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reconcile", action="store_true")
    ap.add_argument("--project")
    ap.add_argument("--force", action="store_true",
                    help="rewrite target boards even when their semantic signature matches")
    ap.add_argument(
        "--allow-delete",
        action="store_true",
        help="explicit maintenance mode: remove a generated board when its source roadmaps vanish",
    )
    ap.add_argument("--parse", help="debug: print parsed status of a roadmap file")
    ap.add_argument("--lint", help="lint ONE roadmap file: list milestones with no status line (exit 1 if any)")
    ap.add_argument("--lint-all", action="store_true",
                    help="lint EVERY discovered roadmap; exit 1 if any milestone lacks a status line")
    args = ap.parse_args()
    if args.parse:
        print(json.dumps(parse_roadmap(args.parse), indent=2))
        return 0
    if args.lint:
        v = lint_roadmap(args.lint)
        for it in v:
            print(f"line {it['line']}: {it['id']} — no status line (defaults to pending): {it['title']}")
        if not v:
            print("✓ clean — every detected milestone/spike has a status line.")
        return 1 if v else 0
    m = load_manifest()
    if args.lint_all:
        return 1 if lint_all(m) else 0
    targets = [args.project] if args.project else (list(m["projects"]) if args.reconcile else [])
    if not targets:
        ap.print_help()
        return 0
    for proj in targets:
        if proj not in m["projects"]:
            print(f"unknown project: {proj}", file=sys.stderr)
            continue
        path, done, tot = build_for_project(
            m, proj, m["projects"][proj],
            force=args.force,
            allow_delete=args.allow_delete,
        )
        rel = os.path.relpath(path, m["vault_root"]) if path else "(no roadmaps)"
        print(f"{proj}: {done}/{tot} milestones done -> {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
