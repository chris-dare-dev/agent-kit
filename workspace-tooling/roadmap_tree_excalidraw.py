#!/usr/bin/env python3
"""
roadmap_tree_excalidraw.py — generate a per-project roadmap-tree Excalidraw drawing.

Each project's `*roadmap*.md` files form a tree: one PARENT roadmap (the program/north-star)
fanning out to the child roadmaps. This emits a native Obsidian-Excalidraw-plugin drawing
`Notes/Projects/<project>/<project>-roadmap-tree.excalidraw.md` — boxes (one per roadmap, each
linking to the source file) + bound parent→child arrows.

Approach (per the research brief): direct stdlib emission of the `.excalidraw.md` wrapper with an
uncompressed `## Drawing` json block. ALL ids/seeds/versionNonce are hash-derived from stable keys
(no time/random) so an unchanged roadmap set yields a BYTE-IDENTICAL file (idempotent, like _index.md).
REGENERATE-DON'T-HAND-EDIT (the vault has plugin compress:true; a human save rewrites + re-randomizes).

Parent selection: manifest `parent_roadmap` (basename match) → else keyword heuristic
(prove-and-distribute / north-star / master / program / overview / "<slug>-roadmap") → else shortest label.

Usage:
  python3 scripts/roadmap_tree_excalidraw.py --reconcile      # all projects
  python3 scripts/roadmap_tree_excalidraw.py --project the dispatcher service
  underscore filename so project-linker can import build_for_project().
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys
import urllib.parse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from path_contract import default_manifest_path, load_project_manifest  # noqa: E402

MANIFEST = str(default_manifest_path(SCRIPT_DIR))

PARENT_KEYWORDS = ["prove-and-distribute", "north-star", "master", "program", "overview"]


def load_manifest():
    return load_project_manifest(MANIFEST)


def _h(key, mod=2**31):
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % mod


def discover_roadmaps(m, project, cfg):
    """Return [(label, abs_path)] of the project's roadmap files, deduped by basename."""
    found = {}
    for region_rel in m["regions"].values():
        region_abs = os.path.join(m["vault_root"], region_rel)
        if not os.path.isdir(region_abs):
            continue
        for name in os.listdir(region_abs):
            low = name.lower()
            hit = (any(low.startswith(s.lower()) for s in cfg.get("slugs", []))
                   or any(c.lower() in low for c in cfg.get("contains", [])))
            if low.endswith(".md") and "roadmap" in low and hit:
                # prefer vault-root plans/ over platform-* when basename repeats
                if name not in found or "/platform/" in found[name]:
                    found[name] = os.path.join(region_abs, name)
    return sorted((label_of(n, cfg), p) for n, p in found.items())


def label_of(filename, cfg):
    base = filename[:-3] if filename.lower().endswith(".md") else filename
    for s in sorted(cfg["slugs"], key=len, reverse=True):
        if base.lower().startswith(s.lower()):
            base = base[len(s):]
            break
    base = base.strip("-")
    if base.endswith("-roadmap"):
        base = base[:-len("-roadmap")]
    return base or "roadmap"


def pick_parent(roadmaps, cfg):
    """roadmaps = [(label, path)]. Return (parent_idx)."""
    pref = cfg.get("parent_roadmap")
    if pref:
        pref_low = pref.lower()
        for i, (_lbl, path) in enumerate(roadmaps):
            if os.path.basename(path).lower().startswith(pref_low) or pref_low in os.path.basename(path).lower():
                return i
    for kw in PARENT_KEYWORDS:
        for i, (lbl, _p) in enumerate(roadmaps):
            if kw in lbl.lower():
                return i
    # else shortest label
    return min(range(len(roadmaps)), key=lambda i: len(roadmaps[i][0])) if roadmaps else 0


# --- element emitters (per research brief) ---
def _box(nid, x, y, w, h, text_id, link, is_parent):
    return {"id": nid, "type": "rectangle", "x": x, "y": y, "width": w, "height": h, "angle": 0,
            "strokeColor": "#1971c2" if is_parent else "#1864ab",
            "backgroundColor": "#ffd8a8" if is_parent else "#a5d8ff",
            "fillStyle": "solid", "strokeWidth": 2 if is_parent else 1, "strokeStyle": "solid",
            "roughness": 0, "opacity": 100, "groupIds": [], "roundness": {"type": 3},
            "seed": _h(nid + "s"), "version": 1, "versionNonce": _h(nid + "v"), "isDeleted": False,
            "boundElements": [{"type": "text", "id": text_id}], "updated": 1, "link": link, "locked": False}


def _text(tid, box, label):
    return {"id": tid, "type": "text", "x": box["x"] + 8, "y": box["y"] + 8, "width": box["width"] - 16,
            "height": 24, "angle": 0, "strokeColor": "#1e1e1e", "backgroundColor": "transparent",
            "fillStyle": "solid", "strokeWidth": 1, "strokeStyle": "solid", "roughness": 0, "opacity": 100,
            "groupIds": [], "roundness": None, "seed": _h(tid + "s"), "version": 1,
            "versionNonce": _h(tid + "v"), "isDeleted": False, "boundElements": None, "updated": 1,
            "link": None, "locked": False, "text": label, "fontSize": 16, "fontFamily": 1,
            "textAlign": "center", "verticalAlign": "middle", "containerId": box["id"],
            "originalText": label, "lineHeight": 1.25, "baseline": 14}


def _arrow(aid, p, c):
    x0, y0 = p["x"] + p["width"] / 2, p["y"] + p["height"]
    x1, y1 = c["x"] + c["width"] / 2, c["y"]
    return {"id": aid, "type": "arrow", "x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0, "angle": 0,
            "strokeColor": "#2f9e44", "backgroundColor": "transparent", "fillStyle": "solid",
            "strokeWidth": 1, "strokeStyle": "solid", "roughness": 0, "opacity": 100, "groupIds": [],
            "roundness": None, "seed": _h(aid + "s"), "version": 1, "versionNonce": _h(aid + "v"),
            "isDeleted": False, "boundElements": None, "updated": 1, "link": None, "locked": False,
            "points": [[0, 0], [x1 - x0, y1 - y0]], "lastCommittedPoint": [x1 - x0, y1 - y0],
            "startBinding": {"elementId": p["id"], "focus": 0, "gap": 4},
            "endBinding": {"elementId": c["id"], "focus": 0, "gap": 4},
            "startArrowhead": None, "endArrowhead": "arrow"}


def tree_sig(parent, children):
    """Stable signature of the roadmap SET — regenerate only when this changes."""
    key = parent[0] + "||" + "|".join(sorted(lbl for lbl, _ in children))
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def existing_sig(out_path):
    if not os.path.exists(out_path):
        return None
    with open(out_path, encoding="utf-8", errors="replace") as f:
        head = f.read(2000)
    m = re.search(r"roadmap-tree-sig:\s*([0-9a-f]+)", head)
    return m.group(1) if m else None


def build_scene(project, parent, children, proj_folder, sig):
    """parent/children = [(label, abs_path)]. Returns the .excalidraw.md text."""
    COL, ROWY, W, H = 300, 170, 250, 80
    els = []

    def link_for(path):
        # Obsidian deep-link by ABSOLUTE filesystem path. A relative md-path or a [[wikilink]]
        # resolves through Obsidian's LINK INDEX, which EXCLUDES the ignore-filtered plans/ +
        # repos/ trees the roadmaps live in → broken element links. `obsidian://open?path=`
        # opens by filesystem path, bypassing the index, so it works for ignore-filtered files.
        return "obsidian://open?path=" + urllib.parse.quote(os.path.abspath(path), safe="")

    n = max(len(children), 1)
    px = (n - 1) * COL / 2
    pid, ptid = f"{project}-rect-root", f"{project}-txt-root"
    pbox = _box(pid, px, 0, W, H, ptid, link_for(parent[1]), True)
    els += [pbox, _text(ptid, pbox, parent[0])]
    for i, (lbl, path) in enumerate(children):
        cid, ctid, aid = f"{project}-rect-{i}", f"{project}-txt-{i}", f"{project}-arr-{i}"
        cbox = _box(cid, i * COL, ROWY, W, H, ctid, link_for(path), False)
        els += [cbox, _text(ctid, cbox, lbl)]
        els.append(_arrow(aid, pbox, cbox))
        pbox["boundElements"].append({"type": "arrow", "id": aid})
        cbox["boundElements"].append({"type": "arrow", "id": aid})

    scene = {"type": "excalidraw", "version": 2, "source": "workspace-roadmap-tree",
             "elements": els, "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"}, "files": {}}
    # Empty ## Text Elements — the label text lives ONLY in the Drawing JSON `text` field.
    # Emitting text in BOTH the Text Elements section AND the Drawing makes the plugin sync
    # them and double-render (duplicate cards). The `roadmap-tree-sig` lets the generator
    # skip-if-unchanged so it never fights the plugin's own (compressed) re-saves.
    return (f"---\nexcalidraw-plugin: parsed\nroadmap-tree-sig: {sig}\ntags: [excalidraw, type/roadmap-tree]\n---\n\n"
            "> Auto-generated by `scripts/roadmap_tree_excalidraw.py` — regenerated only when the roadmap set changes.\n\n"
            "# Excalidraw Data\n\n## Text Elements\n\n## Drawing\n```json\n"
            + json.dumps(scene, indent=0) + "\n```\n%%\n")


def build_for_project(m, project, cfg):
    """Generate the tree drawing. Returns (out_path, n_roadmaps) or (None, 0) if <2 roadmaps."""
    roadmaps = discover_roadmaps(m, project, cfg)
    proj_folder = os.path.join(m["vault_root"], m["projects_root"], project)
    out_path = os.path.join(proj_folder, f"{project}-roadmap-tree.excalidraw.md")
    if len(roadmaps) < 2:
        # nothing to tree; remove a stale drawing if present
        if os.path.exists(out_path):
            os.remove(out_path)
        return None, len(roadmaps)
    pidx = pick_parent(roadmaps, cfg)
    parent = roadmaps[pidx]
    children = [r for i, r in enumerate(roadmaps) if i != pidx]
    sig = tree_sig(parent, children)
    # Skip-if-unchanged: if the on-disk file already reflects this roadmap set, leave it
    # alone — the plugin may have compressed/edited it, and overwriting would fight its saves
    # (the cause of flaky links + duplicate cards). Only (re)write when the roadmap set changes.
    if existing_sig(out_path) == sig:
        return out_path, len(roadmaps)
    os.makedirs(proj_folder, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_scene(project, parent, children, proj_folder, sig))
    return out_path, len(roadmaps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reconcile", action="store_true")
    ap.add_argument("--project")
    args = ap.parse_args()
    m = load_manifest()
    targets = [args.project] if args.project else (list(m["projects"]) if args.reconcile else [])
    if not targets:
        ap.print_help()
        return 0
    for proj in targets:
        if proj not in m["projects"]:
            print(f"unknown project: {proj}", file=sys.stderr)
            continue
        path, n = build_for_project(m, proj, m["projects"][proj])
        rel = os.path.relpath(path, m["vault_root"]) if path else "(skipped — <2 roadmaps)"
        print(f"{proj}: {n} roadmaps -> {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
