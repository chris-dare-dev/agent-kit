#!/usr/bin/env python3
"""
project-linker.py — consolidate a project's scattered .md across regions.

A single project (e.g. the dispatcher service) branches across regions: vault-root plans/ + docs/,
the GitLab platform/plans/ + platform/docs/, and a root deliverables dir. Obsidian's
ignore-filter hides those trees from the index, so the scattered files are invisible
in the vault even though they belong to one project.

This script, per the manifest scripts/project-map.json:
  1. SYMLINKS each matching file into  Notes/Projects/<project>/_sources/<region>/
     (filesystem-level consolidation — browseable in Finder/terminal; NOT Obsidian-indexed
     because the real target is ignore-filtered).
  2. GENERATES  Notes/Projects/<project>/_index.md  — a real, indexed hub note that maps
     every file (human + agent + code) with click-through links. THIS is the Obsidian view.
  3. PRESERVES existing aliases by default. Destructive pruning/repointing is
     available only with the explicit --allow-delete maintenance flag.

Matching is by filename signals in `scripts/project-map.json`: `slugs`/`contains` claim a
file and optional `excludes` release known cross-project collisions before those positive
signals are evaluated.  The explicit negative override keeps a broad domain slug (for
example `tenant-pep`) without double-indexing a more-specific Kargo-owned roadmap.

SINGLE OWNER: a filename can carry two project slugs, so more than one project may claim it.
Exactly one owns the projection (resolve_owner: frontmatter `vault_owner` > manifest `owns` >
earliest matching signal). Every other claimant gets a CROSS-REFERENCE NOTE at the same vault
path — never a second alias to the same file, which is what produced the vault validator's
DUPLICATE_MARKDOWN_ALIAS class. Keeping the path lets existing hub, Excalidraw, and Canvas links
resolve unchanged. Design: plans/DESIGN-2026-07-18-vault-projection-single-owner-rule.md.

Modes:
  python3 project-linker.py --reconcile            # all projects, create/preserve + regen hubs
  python3 project-linker.py --project the dispatcher service     # one project, full
  python3 project-linker.py --file <abs-or-rel.md>  # link just this file to its project (hook mode)
  python3 project-linker.py --check-ownership       # read-only gate: no source aliased twice
  add  --dry-run  to print actions without touching disk.
  add  --repair-duplicates  to convert legacy duplicate aliases into cross-reference notes.

Hook mode (--file) is fast + targeted: it finds which project+region the file belongs to,
ensures its one symlink, and regenerates only that project's hub. Used by the PostToolUse hook.
"""
import argparse
import datetime
import json
import os
import posixpath
import re
import shutil
import sys
import urllib.parse

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")  # US-Eastern; %Z renders EST/EDT correctly per date
except Exception:
    EASTERN = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(SCRIPT_DIR, "project-map.json")

# roadmap status-board Excalidraw generator (underscore-named so it's importable)
sys.path.insert(0, SCRIPT_DIR)
from path_contract import load_project_manifest  # noqa: E402
import roadmap_status_excalidraw as rte  # noqa: E402


_PROJECT_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_manifest(m):
    """Fail fast when the stable identity or presentation-vault contract is ambiguous."""
    pv = m.get("presentation_vault", {})
    required_pv = ("name", "root", "projects_root", "source_alias_dir")
    missing_pv = [key for key in required_pv if not pv.get(key)]
    if missing_pv:
        raise ValueError("project-map.json presentation_vault missing: " + ", ".join(missing_pv))

    seen = {}
    for display_name, cfg in m.get("projects", {}).items():
        pid = cfg.get("project_id", "")
        if not _PROJECT_ID.fullmatch(pid):
            raise ValueError(f"project {display_name!r} has invalid project_id {pid!r}")
        if pid in seen:
            raise ValueError(
                f"duplicate project_id {pid!r}: {seen[pid]!r} and {display_name!r}"
            )
        seen[pid] = display_name
    return m


def load_manifest():
    return validate_manifest(load_project_manifest(MANIFEST))


def vroot(m):
    return m["vault_root"]


def project_id(cfg):
    """Stable identity. Discovery slugs are aliases and must never be used as identity."""
    return cfg["project_id"]


def source_alias_rel(m, project, label, path):
    """Vault-relative canonical alias for a scattered source file.

    The path intentionally lives below Notes/Projects rather than pointing back into the source
    workspace. Both a Markdown hub and an Excalidraw element can therefore navigate within the
    presentation vault without embedding a machine-specific absolute path.
    """
    pv = m["presentation_vault"]
    return posixpath.join(
        pv["projects_root"], project, pv["source_alias_dir"], label, os.path.basename(path)
    )


def source_alias_href(m, project, label, path):
    """Canonical source alias relative to the project's generated hub."""
    pv = m["presentation_vault"]
    rel = posixpath.join(pv["source_alias_dir"], label, os.path.basename(path))
    return urllib.parse.quote(rel, safe="/")


def match_signals(filename, cfg):
    """Every discovery signal that claims `filename`, as (index, signal) pairs.

    `index` is where the signal itself starts in the lowercased filename — the ordering input for
    single-owner arbitration (see resolve_owner). The three positive conditions are exactly those
    `matches()` has always applied: slug prefix, `contains` substring, and a hyphen/underscore-
    bounded slug SEGMENT anywhere (catches `HANDOFF-<date>-<slug>-…` whose project id sits
    mid-name). Segment-bounding (not raw substring) avoids spurious matches.
    """
    low = filename.lower()
    if any(x.lower() in low for x in cfg.get("excludes", [])):
        return []
    hits = []
    for s in cfg.get("slugs", []):
        sl = s.lower()
        if low.startswith(sl):
            hits.append((0, sl))
        seg = sl.rstrip("-.")
        if len(seg) >= 4:
            mm = re.search(r"(?:^|[-_])" + re.escape(seg) + r"(?:[-_.]|$)", low)
            if mm:
                # mm.start() may sit on the leading separator; index the signal itself.
                hits.append((low.index(seg, mm.start()), seg))
    for c in cfg.get("contains", []):
        cl = c.lower()
        i = low.find(cl)
        if i >= 0:
            hits.append((i, cl))
    return hits


def matches(filename, cfg):
    """True if any discovery signal claims the filename (excludes veto first)."""
    return bool(match_signals(filename, cfg))


def best_signal(filename, cfg):
    """The claim a project stakes on a filename: earliest signal, longest on a tie."""
    hits = match_signals(filename, cfg)
    if not hits:
        return None
    return min(hits, key=lambda h: (h[0], -len(h[1])))


def frontmatter_value(path, key):
    """Read a scalar `key` from a file's YAML frontmatter without a YAML dependency.

    Deliberately minimal: only the leading `---` fenced block, only `key: value` at column 0.
    Returns None when absent, unreadable, or not a simple scalar.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.readline().rstrip("\n") != "---":
                return None
            for _ in range(80):
                line = fh.readline()
                if not line or line.rstrip("\n") == "---":
                    return None
                name, sep, value = line.partition(":")
                if sep and name == key:
                    return value.strip().strip("'\"") or None
    except OSError:
        return None
    return None


def claimants(m, path):
    """Every project that would project `path`, as an ordered [(project, cfg), …].

    Region files are claimed by FILENAME signal, so more than one project can claim them — that is
    the collision this arbitrates. Deliverable and app-dir files are attributed by LOCATION and so
    are claimed only by the project that owns the directory.
    """
    parent = os.path.abspath(os.path.dirname(path))
    name = os.path.basename(path)
    in_region = any(
        parent == os.path.abspath(os.path.join(vroot(m), region_rel))
        for region_rel in m["regions"].values()
    )
    out = []
    for project, cfg in m["projects"].items():
        if in_region:
            if matches(name, cfg):
                out.append((project, cfg))
            continue
        located = [
            os.path.join(vroot(m), d) for d in cfg.get("deliverable_dirs", [])
        ] + [
            os.path.join(vroot(m), app, sub)
            for app in cfg.get("app_dirs", [])
            for sub in ("plans", "docs")
        ]
        if any(parent == os.path.abspath(d) for d in located):
            out.append((project, cfg))
    return out


def resolve_owner(m, path, candidates=None):
    """The single project that owns `path`'s projection. Every other claimant is secondary.

    Resolution order — first decisive step wins:
      1. `excludes`                      (already applied inside match_signals)
      2. frontmatter `vault_owner: <project_id>`
      3. manifest `owns: [<basename>, …]`
      4. earliest matching signal; ties by longest signal, then manifest order

    Step 4 is deliberately NOT "first slug in the manifest": that would make ownership a function
    of dict insertion order, so reordering project-map.json would silently repoint projections.
    Earliest-signal is order-independent and encodes the handoff naming contract, where the
    leading workstream slug is the primary project.
    """
    if candidates is None:
        candidates = claimants(m, path)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]

    name = os.path.basename(path)

    declared = frontmatter_value(path, "vault_owner")
    if declared:
        for project, cfg in candidates:
            if project_id(cfg) == declared:
                return project

    for project, cfg in candidates:
        if name in cfg.get("owns", []):
            return project

    order = {project: i for i, project in enumerate(m["projects"])}

    def rank(item):
        project, cfg = item
        signal = best_signal(name, cfg)
        if signal is None:  # location-attributed claimant: no filename signal to rank
            return (0, 0, order[project])
        return (signal[0], -len(signal[1]), order[project])

    return min(candidates, key=rank)[0]


def proj_folder(m, project):
    return os.path.join(vroot(m), m["projects_root"], project)


def scan_region_for_project(m, region_rel, cfg):
    """Return abs paths of .md files in a region matching the project (slugs + contains)."""
    region_abs = os.path.join(vroot(m), region_rel)
    out = []
    if not os.path.isdir(region_abs):
        return out
    for name in os.listdir(region_abs):
        if not name.lower().endswith(".md"):
            continue
        if matches(name, cfg):
            out.append(os.path.join(region_abs, name))
    return sorted(out)


def scan_deliverables(m, deliverable_dirs):
    """Return abs paths of all files in each deliverable dir (the dir IS the project)."""
    out = []
    for d in deliverable_dirs:
        d_abs = os.path.join(vroot(m), d)
        if not os.path.isdir(d_abs):
            continue
        for name in sorted(os.listdir(d_abs)):
            p = os.path.join(d_abs, name)
            if os.path.isfile(p):
                out.append(p)
    return out


def scan_app_dirs(m, cfg):
    """Scan <app_dir>/plans + <app_dir>/docs for ALL .md — attributed by LOCATION (the file is in
    the project's source repo), NOT by filename slug (e.g. admin-mcp-server owns the roadmap
    `admin-mcp-hardening-roadmap.md`, whose name doesn't match the slug). Returns {label: [paths]}."""
    out = {}
    for app in cfg.get("app_dirs", []):
        for sub, label in (("plans", "src-plans"), ("docs", "src-docs")):
            d_abs = os.path.join(vroot(m), app, sub)
            if not os.path.isdir(d_abs):
                continue
            for name in sorted(os.listdir(d_abs)):
                p = os.path.join(d_abs, name)
                if os.path.isfile(p) and name.lower().endswith(".md"):
                    out.setdefault(label, []).append(p)
    return out


def ensure_symlink(link_path, target_abs, dry_run, allow_delete=False):
    """Create a relative link without replacing existing paths by default."""
    link_dir = os.path.dirname(link_path)
    rel = os.path.relpath(target_abs, link_dir)
    if os.path.islink(link_path):
        if os.readlink(link_path) == rel:
            return None  # already correct
        if not allow_delete:
            return f"PRESERVE (existing symlink differs): {link_path}"
        if not dry_run:
            os.remove(link_path)
    elif os.path.exists(link_path):
        return f"SKIP (real file in the way): {link_path}"
    if not dry_run:
        os.makedirs(link_dir, exist_ok=True)
        os.symlink(rel, link_path)
    return f"link {os.path.relpath(link_path, vroot(_M))} -> {rel}"


REFERENCE_MARKER = (
    "<!-- workspace:cross-reference — generated by scripts/project-linker.py; do not edit by hand -->"
)


def render_reference_note(m, project, owner_project, label, target_abs):
    """A secondary claimant's projection: a real note naming the owner, not a second alias.

    Lives at the SAME vault path the owning alias would occupy, so every generated hub link,
    Excalidraw board URI, and hand-authored Canvas node keeps resolving. Being a distinct real
    file rather than a symlink is what clears DUPLICATE_MARKDOWN_ALIAS.
    """
    pv = m["presentation_vault"]
    name = os.path.basename(target_abs)
    here = posixpath.join(pv["projects_root"], project, pv["source_alias_dir"], label)
    canonical = source_alias_rel(m, owner_project, label, target_abs)
    href = urllib.parse.quote(posixpath.relpath(canonical, here), safe="/")
    owner_hub = posixpath.join(pv["projects_root"], owner_project, "_index")
    owner_cfg = m["projects"][owner_project]

    lines = [
        "---",
        "type: cross-reference",
        f"owner_project: {project_id(owner_cfg)}",
        f"owner_display_name: {json.dumps(owner_project)}",
        f"canonical_alias: {json.dumps(canonical)}",
        f"source_basename: {json.dumps(name)}",
        "tags:",
        "  - type/cross-reference",
        f"  - project/{project_id(m['projects'][project])}",
        f"  - owner/{project_id(owner_cfg)}",
        "---",
        REFERENCE_MARKER,
        f"# {name} — cross-reference",
        "",
        f"This source is relevant to **{project}** but is **owned by "
        f"[[{owner_hub}|{owner_project}]]**, which holds its canonical projection.",
        "",
        f"- Canonical alias: [{name}]({href})",
        "",
        "> One project owns each source file's projection; every other claimant gets this note",
        "> instead of a second alias. See",
        "> `plans/DESIGN-2026-07-18-vault-projection-single-owner-rule.md`.",
        "",
    ]
    return "\n".join(lines)


def ensure_reference_note(note_path, body, dry_run, allow_convert=False):
    """Materialize a cross-reference note without deleting anything by default.

    A legacy duplicate SYMLINK at this path is preserved (and reported) unless conversion is
    explicitly permitted (--repair-duplicates, or the broader --allow-delete). A foreign real file
    is never overwritten; only a note this script generated is refreshed, which keeps repeat runs
    idempotent.
    """
    if os.path.islink(note_path):
        if not allow_convert:
            return f"PRESERVE (duplicate alias awaiting --allow-delete): {note_path}"
        if not dry_run:
            os.remove(note_path)
    elif os.path.exists(note_path):
        try:
            with open(note_path, encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            return f"SKIP (unreadable file in the way): {note_path}"
        if REFERENCE_MARKER not in current:
            return f"SKIP (real file in the way): {note_path}"
        if current == body:
            return None  # already correct
    if not dry_run:
        os.makedirs(os.path.dirname(note_path), exist_ok=True)
        with open(note_path, "w", encoding="utf-8") as fh:
            fh.write(body)
    return f"reference {os.path.relpath(note_path, vroot(_M))}"


def project_sources_map(m, project, cfg):
    """Compute the desired {region_label: [target_abs,...]} for a project."""
    desired = {}
    for label, region_rel in m["regions"].items():
        files = scan_region_for_project(m, region_rel, cfg)
        if files:
            desired[label] = files
    deliv = scan_deliverables(m, cfg.get("deliverable_dirs", []))
    if deliv:
        desired["deliverables"] = deliv
    for label, paths in scan_app_dirs(m, cfg).items():
        desired.setdefault(label, []).extend(paths)
    return desired


def sync_project(m, project, cfg, dry_run, allow_delete=False, repair_duplicates=False):
    """Project one project's sources.

    `repair_duplicates` is the NARROW maintenance gate: it permits converting a legacy duplicate
    alias into a cross-reference note and nothing else — no stale-alias pruning, no handoff-mirror
    deletion. `allow_delete` remains the broad maintenance flag and implies it.
    """
    allow_convert = allow_delete or repair_duplicates
    actions = []
    pf = proj_folder(m, project)
    sources_root = os.path.join(pf, "_sources")
    desired = project_sources_map(m, project, cfg)

    # Build desired set: _sources/<label>/<basename> -> (target, owning project).
    # A file this project merely CLAIMS — but does not own — keeps the same vault path so hub,
    # board, and canvas links stay resolvable, but holds a cross-reference note instead of a
    # second alias to the canonical file.
    desired_links = {}
    for label, targets in desired.items():
        for t in targets:
            link = os.path.join(sources_root, label, os.path.basename(t))
            desired_links[link] = (t, label, resolve_owner(m, t) or project)

    for link, (target, label, owner) in sorted(desired_links.items()):
        if owner == project:
            a = ensure_symlink(link, target, dry_run, allow_delete=allow_delete)
        else:
            a = ensure_reference_note(
                link,
                render_reference_note(m, project, owner, label, target),
                dry_run,
                allow_convert=allow_convert,
            )
        if a:
            actions.append(a)

    # Prune stale symlinks under _sources/ not in desired set (only symlinks we own)
    if allow_delete and os.path.isdir(sources_root):
        for root, _dirs, files in os.walk(sources_root):
            for name in files:
                p = os.path.join(root, name)
                if os.path.islink(p) and p not in desired_links:
                    actions.append(f"prune {os.path.relpath(p, vroot(m))}")
                    if not dry_run:
                        os.remove(p)
        # remove now-empty region dirs
        if not dry_run:
            for root, dirs, files in os.walk(sources_root, topdown=False):
                if root != sources_root and not os.listdir(root):
                    os.rmdir(root)

    if not dry_run:
        board_path, done, tot = rte.build_for_project(
            m, project, cfg, allow_delete=allow_delete
        )
        if board_path:
            actions.append(f"board {os.path.relpath(board_path, vroot(m))}  ({done}/{tot} milestones done)")
        write_hub(m, project, cfg, desired)
    actions.append(f"hub  {os.path.relpath(os.path.join(pf, '_index.md'), vroot(m))}  ({sum(len(v) for v in desired.values())} sources)")
    return actions


def audit_ownership(m):
    """Simulate the full projection and report ownership violations before anything is written.

    Validates the EMITTED PLAN, not merely the resolver: it collects the alias each project would
    actually symlink and asserts no canonical source receives two of them. That catches a
    reintroduced double-claim even if `resolve_owner` itself stays pure — which is the failure the
    vault validator only sees after the fact, as DUPLICATE_MARKDOWN_ALIAS.

    Returns (duplicates, orphans):
      duplicates — [(target, [vault-relative alias, …])] for sources aliased by 2+ projects
      orphans    — [(target, owner, [claimant, …])] for sources whose resolved owner never
                   projects them, so no project would hold the canonical alias
    """
    aliases = {}
    projected = {}
    for project, cfg in m["projects"].items():
        desired = project_sources_map(m, project, cfg)
        for label, targets in desired.items():
            for t in targets:
                key = os.path.realpath(t)
                projected.setdefault(key, set()).add(project)
                if (resolve_owner(m, t) or project) == project:
                    aliases.setdefault(key, []).append(
                        source_alias_rel(m, project, label, t)
                    )

    duplicates = [
        (target, sorted(set(found)))
        for target, found in sorted(aliases.items())
        if len(set(found)) > 1
    ]
    orphans = []
    for target, projects in sorted(projected.items()):
        if target not in aliases:
            owner = resolve_owner(m, target)
            orphans.append((target, owner, sorted(projects)))
    return duplicates, orphans


def write_hub(m, project, cfg, desired):
    pf = proj_folder(m, project)
    os.makedirs(pf, exist_ok=True)
    index_path = os.path.join(pf, "_index.md")
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    pid = project_id(cfg)
    pv = m["presentation_vault"]
    presentation_path = posixpath.join(pv["projects_root"], project, "_index.md")
    source_alias_root = posixpath.join(pv["projects_root"], project, pv["source_alias_dir"])
    total = sum(len(v) for v in desired.values())

    lines = []
    lines.append("---")
    lines.append("type: project-hub")
    lines.append(f"project_id: {pid}")
    lines.append(f"project: {pid}")
    lines.append(f"display_name: {json.dumps(project)}")
    lines.append(f"presentation_vault: {json.dumps(pv['name'])}")
    lines.append(f"presentation_path: {json.dumps(presentation_path)}")
    lines.append(f"source_alias_root: {json.dumps(source_alias_root)}")
    lines.append(f"tags:\n  - type/project-hub\n  - project/{pid}")
    lines.append("---")
    lines.append(f"# {project} — project hub")
    lines.append("")
    lines.append(f"> **Auto-generated by `scripts/project-linker.py` — do not edit by hand.** Last sync: {ts}.")
    lines.append(f"> Single-project view across all regions ({total} scattered source files). Human notes below are")
    lines.append("> real indexed notes; agent/code files are symlinked under `_sources/` (filesystem) and listed here —")
    lines.append("> the ignore-filter hides them from Obsidian search, so this hub is the map.")
    lines.append("")

    # Human notes (indexed) — wikilinks. Post-consolidation they live IN the project folder
    # alongside the generated hub; list every .md there except the generated artifacts.
    human_files = []
    if os.path.isdir(pf):
        for name in sorted(os.listdir(pf)):
            if name.lower().endswith(".md") and name != "_index.md" and not name.endswith(".excalidraw.md"):
                human_files.append(name[:-3])  # basename without .md for wikilink
    source_labels = {
        os.path.abspath(path): label
        for label, paths in desired.items()
        for path in paths
    }

    def source_href(path):
        label = source_labels.get(os.path.abspath(path))
        if label:
            return source_alias_href(m, project, label, path)
        # Compatibility fallback for a human note already inside the project folder.
        return urllib.parse.quote(os.path.relpath(path, pf), safe="/")

    def md_links(paths):
        out = []
        for t in sorted(paths, key=lambda p: os.path.basename(p).lower()):
            href = source_href(t)
            out.append(f"- [{os.path.basename(t)}]({href})")
        return out

    # Flatten scattered sources (region map) + categorize by TYPE (not region).
    all_paths = [p for label, ts in desired.items() if label != "deliverables" for p in ts]
    def is_rm(p): return "roadmap" in os.path.basename(p).lower()
    def is_dec(p): return "decision" in os.path.basename(p).lower()
    def is_crit(p): return "critique" in os.path.basename(p).lower()
    def is_handoff(p): return "handoff" in os.path.basename(p).lower()
    decisions = [p for p in all_paths if is_dec(p) and not is_rm(p)]
    critiques = [p for p in all_paths if is_crit(p) and not is_rm(p) and not is_dec(p)]
    handoffs = [p for p in all_paths if is_handoff(p) and not is_rm(p) and not is_dec(p) and not is_crit(p)]
    others = [p for p in all_paths if not is_rm(p) and not is_dec(p) and not is_crit(p) and not is_handoff(p)]
    deliverables = desired.get("deliverables", [])

    # === Roadmap status (LIVE — embeds the board + a non-task status list) ===
    rm_paths = rte.discover_roadmaps(m, cfg)  # [paths] deduped by basename
    if rm_paths:
        parsed = [(p, rte.parse_roadmap(p)) for p in rm_paths]
        counted = [(p, rm) for p, rm in parsed if rm["status"] not in {"cancelled", "superseded"}]
        done = sum(sum(1 for it in rm["items"] if it["status"] == "done") for _p, rm in counted)
        tot = sum(len(rm["items"]) for _p, rm in counted)
        pct = int(100 * done / tot) if tot else 0
        lines.append(f"## Roadmap status — {done}/{tot} milestones done ({pct}%) · {len(rm_paths)} roadmaps")
        lines.append(f"![[{project}-roadmaps.excalidraw]]")
        lines.append("")
        for p, rm in parsed:
            d = sum(1 for it in rm["items"] if it["status"] == "done")
            t = len(rm["items"])
            href = source_href(p)
            progress = (f"{rm['status'].upper()} — excluded from totals"
                        if rm["status"] in {"cancelled", "superseded"}
                        else f"{d}/{t} done")
            lines.append(f"- **{rte.short_label(p, cfg)}** — {progress} — [{os.path.basename(p)}]({href})")
            for it in rm["items"]:
                mark = {"done": "✅", "active": "🟡", "pending": "⚪"}[it["status"]]
                lines.append(f"    - {mark} {it['id']}: {it['title']}")
        lines.append("")

    # === Handoffs (newest first — the per-project "latest handoff" surface) ===
    if handoffs:
        lines.append(f"## Handoffs ({len(handoffs)}) — newest first")
        for t in sorted(handoffs, key=_hdate_key, reverse=True):
            href = source_href(t)
            lines.append(f"- [{os.path.basename(t)}]({href})")
        lines.append("")

    # === Decisions (separate part) ===
    if decisions:
        lines.append(f"## Decisions ({len(decisions)})")
        lines += md_links(decisions)
        lines.append("")

    # === Human notes (indexed wikilinks) ===
    if human_files:
        lines.append(f"## Human notes — indexed ({len(human_files)})")
        for h in human_files:
            lines.append(f"- [[{h}]]")
        lines.append("")

    # === Critiques ===
    if critiques:
        lines.append(f"## Critiques ({len(critiques)})")
        lines += md_links(critiques)
        lines.append("")

    # === Other docs (handoffs, analyses, runbooks, …) ===
    if others:
        lines.append(f"## Other docs ({len(others)})")
        lines += md_links(others)
        lines.append("")

    # === Deliverables (root project dir) ===
    if deliverables:
        lines.append(f"## Deliverables ({len(deliverables)})")
        lines += md_links(deliverables)
        lines.append("")

    lines.append("---")
    lines.append("*Regenerate: `python3 scripts/project-linker.py --project \"" + project + "\"` or `--reconcile` for all.*")

    with open(index_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def classify_file(m, file_abs):
    """Given an abs .md path, return (project, region_label) or (None, None)."""
    fname = os.path.basename(file_abs)
    fdir = os.path.dirname(file_abs)
    # which region?
    region_label = None
    for label, region_rel in m["regions"].items():
        if os.path.abspath(os.path.join(vroot(m), region_rel)) == os.path.abspath(fdir):
            region_label = label
            break
    # deliverable dir?
    if region_label is None:
        for project, cfg in m["projects"].items():
            for d in cfg.get("deliverable_dirs", []):
                if os.path.abspath(os.path.join(vroot(m), d)) == os.path.abspath(fdir):
                    return project, "deliverables"
        # source app dir (<app>/plans|docs) — attribute by LOCATION, not slug
        for project, cfg in m["projects"].items():
            for app in cfg.get("app_dirs", []):
                for sub, label in (("plans", "src-plans"), ("docs", "src-docs")):
                    if os.path.abspath(os.path.join(vroot(m), app, sub)) == os.path.abspath(fdir):
                        return project, label
        return None, None
    # which project owns this filename in that region?
    for project, cfg in m["projects"].items():
        if matches(fname, cfg):
            return project, region_label
    return None, None


def _hdate_key(p):
    """Sort key for handoffs: by the YYYY-MM-DD in the filename, then real mtime (chronological
    tiebreak for same-day handoffs), then basename."""
    mt = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(p))
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = 0.0
    return (mt.group(1) if mt else "0000-00-00", mtime, os.path.basename(p).lower())


def latest_handoff(m, project, cfg):
    """Newest handoff for a project across claimed agent handoffs (regions + app_dirs) + the human
    handoff notes now living in the project folder. Returns (path, date_str) or (None, None)."""
    paths = set()
    for region_rel in m["regions"].values():
        d = os.path.join(vroot(m), region_rel)
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.lower().endswith(".md") and "handoff" in name.lower() and matches(name, cfg):
                    paths.add(os.path.join(d, name))
    for _label, ps in scan_app_dirs(m, cfg).items():
        for p in ps:
            if "handoff" in os.path.basename(p).lower():
                paths.add(p)
    pf = proj_folder(m, project)
    if os.path.isdir(pf):
        for name in os.listdir(pf):
            if name.lower().endswith(".md") and "handoff" in name.lower():
                paths.add(os.path.join(pf, name))
    if not paths:
        return None, None
    latest = sorted(paths, key=_hdate_key, reverse=True)[0]
    d = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(latest))
    return latest, (d.group(1) if d else "—")


def _under_notes(m, path):
    """True if path lives under the indexed Notes/ tree (so Obsidian can resolve a link to it)."""
    notes_dir = os.path.abspath(os.path.join(vroot(m), "Notes"))
    return os.path.abspath(path).startswith(notes_dir + os.sep)


def mirror_handoff_into_vault(m, project, src_abs, allow_delete=False):
    """Obsidian excludes GitLab/ + plans/ + docs/ + scripts/ (see .obsidian/app.json
    userIgnoreFilters), so a relative markdown link into those trees never resolves/opens. To make
    the Home 'Workstreams' link clickable, COPY the latest handoff into the INDEXED project folder
    (Notes/Projects/<project>/handoffs/) and return the copy's abs path. A symlink does NOT work here:
    Obsidian indexes by RESOLVED path, so a link whose real target is ignore-filtered stays unindexed
    (the same reason _sources/ symlinks aren't searchable). Prunes older mirrors so only the current
    latest remains. Files already under Notes/ (human handoff notes) are returned unchanged."""
    if _under_notes(m, src_abs):
        return src_abs
    hdir = os.path.join(proj_folder(m, project), "handoffs")
    os.makedirs(hdir, exist_ok=True)
    dst = os.path.join(hdir, os.path.basename(src_abs))
    if not os.path.isfile(dst) or os.path.getmtime(dst) < os.path.getmtime(src_abs):
        shutil.copy2(src_abs, dst)
    if allow_delete:
        for name in os.listdir(hdir):  # explicit maintenance: keep only current latest
            p = os.path.join(hdir, name)
            if os.path.isfile(p) and os.path.abspath(p) != os.path.abspath(dst):
                os.remove(p)
    return dst


def _handoff_stamp(path, date_str, extra_paths=()):
    """Return (sort_key, display) for a handoff. `date_str` (YYYY-MM-DD from the filename) is the
    authoritative date. Append a wall-clock TIME in US-Eastern from the first filesystem timestamp
    whose calendar date matches date_str, searching across `path` plus any `extra_paths` (e.g. the
    vault mirror copy). ALL birth times (creation) are tried before any mtime, because a later
    move/consolidation — or a fresh git checkout of the source repo — bumps/resets mtime and even
    birthtime, while a stable copy (the Notes/ mirror is never re-cloned) keeps the original birth
    time. When NO candidate lands on date_str the timestamps are all checkout/clone artifacts, so
    we show date-only rather than fabricate a time."""
    paths = [path] + [p for p in extra_paths if p and p != path]
    births, mtimes = [], []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        bt = getattr(st, "st_birthtime", 0)
        if bt and bt > 0:
            births.append(bt)
        mtimes.append(st.st_mtime)
    for ts in births + mtimes:
        dt = datetime.datetime.fromtimestamp(ts, EASTERN) if EASTERN else datetime.datetime.fromtimestamp(ts)
        if dt.strftime("%Y-%m-%d") == date_str:
            tz = dt.strftime("%Z") or "ET"
            return (date_str, dt.strftime("%H:%M:%S")), f"{date_str} {dt.strftime('%H:%M:%S')} {tz}"
    return (date_str, ""), date_str


def update_home_workstreams(m, allow_delete=False):
    """Fill the WORKSTREAMS marker block in Notes/Home.md with each project's newest handoff,
    most-recently-active project first. Links the project to its hub + the latest handoff (mirrored
    into the indexed project folder when the source lives in an Obsidian-excluded tree)."""
    home = os.path.join(vroot(m), "Notes", "Home.md")
    notes_dir = os.path.join(vroot(m), "Notes")
    if not os.path.isfile(home):
        return
    rows = []
    for project, cfg in m["projects"].items():
        latest, date = latest_handoff(m, project, cfg)
        if not latest:
            continue
        link_target = mirror_handoff_into_vault(
            m, project, latest, allow_delete=allow_delete
        )
        sort_key, stamp = _handoff_stamp(latest, date, extra_paths=[link_target])
        rel = urllib.parse.quote(os.path.relpath(link_target, notes_dir), safe="/")
        rows.append((sort_key, f"- **{project}** ([[Notes/Projects/{project}/_index|hub]]) — "
                               f"latest: [{os.path.basename(link_target)}]({rel}) · {stamp}"))
    rows.sort(reverse=True)
    block = ("<!-- WORKSTREAMS:START — auto-generated by scripts/project-linker.py "
             "(newest handoff per project); do not edit between the markers -->\n"
             + "\n".join(r for _d, r in rows)
             + "\n<!-- WORKSTREAMS:END -->")
    text = open(home, encoding="utf-8").read()
    if "<!-- WORKSTREAMS:START" in text:
        text = re.sub(r"<!-- WORKSTREAMS:START.*?<!-- WORKSTREAMS:END -->", block, text, flags=re.S)
        with open(home, "w", encoding="utf-8") as f:
            f.write(text)


def main():
    global _M
    ap = argparse.ArgumentParser()
    ap.add_argument("--reconcile", action="store_true", help="full sync of all projects")
    ap.add_argument("--project", help="sync a single project by name")
    ap.add_argument("--file", help="link just this one .md to its project (hook mode)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--check-ownership",
        action="store_true",
        help="read-only gate: fail if any source would be aliased by two projects",
    )
    ap.add_argument(
        "--allow-delete",
        action="store_true",
        help="explicit maintenance mode: permit stale-alias pruning, wrong-link replacement, and old handoff-mirror deletion",
    )
    ap.add_argument(
        "--repair-duplicates",
        action="store_true",
        help="narrow maintenance mode: convert legacy duplicate aliases into cross-reference "
             "notes, without any other pruning or deletion",
    )
    args = ap.parse_args()

    m = load_manifest()
    _M = m

    if args.check_ownership:
        duplicates, orphans = audit_ownership(m)
        for target, found in duplicates:
            print(f"DUPLICATE-CLAIM {os.path.relpath(target, vroot(m))}", file=sys.stderr)
            for alias in found:
                print(f"    {alias}", file=sys.stderr)
        for target, owner, projects in orphans:
            print(
                f"ORPHAN {os.path.relpath(target, vroot(m))} — resolved owner {owner!r} "
                f"does not project it (claimants: {', '.join(projects)})",
                file=sys.stderr,
            )
        if duplicates or orphans:
            print(
                f"ownership check FAILED: {len(duplicates)} duplicate-claimed, "
                f"{len(orphans)} orphaned",
                file=sys.stderr,
            )
            return 1
        print("ownership check OK: every source has exactly one owning project alias")
        return 0

    if args.file:
        file_abs = os.path.abspath(args.file)
        if not file_abs.lower().endswith(".md") or not os.path.isfile(file_abs):
            return 0  # silently ignore non-md / missing (hook fires on every write)
        project, _label = classify_file(m, file_abs)
        if not project:
            return 0  # not a tracked project file
        actions = sync_project(
            m, project, m["projects"][project], args.dry_run,
            allow_delete=args.allow_delete, repair_duplicates=args.repair_duplicates,
        )
        if "handoff" in os.path.basename(file_abs).lower():
            update_home_workstreams(
                m, allow_delete=args.allow_delete
            )  # a new handoff → refresh Home's latest-handoff list
        for a in actions:
            print(a)
        return 0

    if args.project:
        if args.project not in m["projects"]:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 1
        for a in sync_project(
            m, args.project, m["projects"][args.project], args.dry_run,
            allow_delete=args.allow_delete, repair_duplicates=args.repair_duplicates,
        ):
            print(a)
        return 0

    if args.reconcile:
        for project, cfg in m["projects"].items():
            acts = sync_project(
                m, project, cfg, args.dry_run,
                allow_delete=args.allow_delete, repair_duplicates=args.repair_duplicates,
            )
            print(f"== {project} ==")
            for a in acts:
                print("  " + a)
        if not args.dry_run:
            update_home_workstreams(m, allow_delete=args.allow_delete)
            print("== Home.md workstreams updated ==")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
