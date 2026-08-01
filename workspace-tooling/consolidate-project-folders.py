#!/usr/bin/env python3
"""
consolidate-project-folders.py — one-shot: collapse the duplicate hierarchy.

Moves each project's HUMAN notes from its original `Notes/<human_dir>/` into the linker's
`Notes/Projects/<Project>/` (so there is ONE folder per project: human notes + the generated
_index.md hub + _sources/ symlinks + the roadmap-tree drawing). Then:
  - fixes every `.canvas` file-node path that pointed at the old location (the ONLY path-based
    reference; wikilinks resolve by basename so they're move-safe, and no explicit-path md-links
    exist — verified),
  - removes the now-empty original folders.

Special cases handled from the manifest:
  - Cost Optimization: human_dir basename ("Cost Optimization & AutoScaling") != project name.
  - Notes/Other: shared — its "Token Optimization*" file routes to a NEW "Token Optimization"
    project folder; the rest (Multicluster) routes to Multicluster.

--dry-run prints the plan without touching disk. Idempotent-ish: re-running after a successful
move finds nothing to move.
"""
import json
import os
import shutil
import sys

from path_contract import default_manifest_path, load_project_manifest


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST = str(default_manifest_path(SCRIPT_DIR))
PROJECT_MAP = load_project_manifest(MANIFEST)
VAULT = PROJECT_MAP["vault_root"]
PROJ_ROOT = os.path.join(VAULT, "Notes", "Projects")


def is_generated(name):
    return name == "_index.md" or name.endswith(".excalidraw.md") or name.startswith("_")


def target_for(human_dir_basename, project, filename):
    """Return the destination PROJECT NAME for a file in an original human folder."""
    if human_dir_basename == "Other":
        if filename.lower().startswith("token optimization"):
            return "Token Optimization"
        return project  # Multicluster
    return project


def main():
    dry = "--dry-run" in sys.argv
    m = PROJECT_MAP

    moves = []          # (src_abs, dst_abs)
    canvas_remap = {}   # old_vault_rel -> new_vault_rel
    src_dirs = set()

    for project, cfg in m["projects"].items():
        for hd in cfg.get("human_dirs", []):
            hd_abs = os.path.join(VAULT, hd)
            if not os.path.isdir(hd_abs):
                continue
            src_dirs.add(hd_abs)
            base = os.path.basename(hd)
            for name in sorted(os.listdir(hd_abs)):
                sp = os.path.join(hd_abs, name)
                if not os.path.isfile(sp) or is_generated(name):
                    continue
                tproj = target_for(base, project, name)
                dst_dir = os.path.join(PROJ_ROOT, tproj)
                dp = os.path.join(dst_dir, name)
                moves.append((sp, dp))
                canvas_remap[f"{hd}/{name}"] = f"Notes/Projects/{tproj}/{name}"

    print(f"{'DRY-RUN' if dry else 'APPLY'} — {len(moves)} files to move, {len(canvas_remap)} canvas remaps\n")
    for sp, dp in moves:
        rel_s = os.path.relpath(sp, VAULT)
        rel_d = os.path.relpath(dp, VAULT)
        collide = os.path.exists(dp)
        print(f"  {'COLLISION!' if collide else 'move'}  {rel_s}  ->  {rel_d}")
        if dry or collide:
            continue
        os.makedirs(os.path.dirname(dp), exist_ok=True)
        shutil.move(sp, dp)

    # Fix canvas node paths
    if not dry:
        import glob
        fixed = 0
        for cv in glob.glob(os.path.join(VAULT, "Notes", "Canvases", "*.canvas")):
            txt = open(cv, encoding="utf-8").read()
            orig = txt
            for old, new in canvas_remap.items():
                txt = txt.replace(f'"file":"{old}"', f'"file":"{new}"')
            if txt != orig:
                open(cv, "w", encoding="utf-8").write(txt)
                fixed += 1
        print(f"\ncanvas files updated: {fixed}")

        # Remove now-empty original dirs
        removed = []
        for d in sorted(src_dirs):
            # skip if it's also a Projects/ dir (shouldn't be) or non-empty
            leftover = [x for x in os.listdir(d) if not x.startswith(".")]
            if not leftover:
                os.rmdir(d)
                removed.append(os.path.relpath(d, VAULT))
            else:
                print(f"  NOT removed (non-empty): {os.path.relpath(d, VAULT)} -> {leftover}")
        print(f"removed empty original folders: {len(removed)}")
        for r in removed:
            print("   ", r)
    else:
        print("\n(dry-run: no canvas edits, no folder removals)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
