#!/usr/bin/env python3
"""Repair malformed mixed-indent `tags:` lists in YAML frontmatter (the bug that makes the
whole block un-parseable, so Obsidian can't read project:/type: and .base filters match nothing).
Normalizes every tag list item to 2-space indent + dedupes. Touches NOTHING else (no authorship).
Usage: fix-frontmatter-tags.py <file.md> ..."""
import sys
def fix(path):
    lines=open(path,encoding="utf-8",errors="replace").read().split("\n")
    if not lines or lines[0].strip()!="---": return False
    try: end=lines.index("---",1)
    except ValueError: return False
    fm=lines[1:end]; body=lines[end+1:]; out=[]; i=0; changed=False
    while i<len(fm):
        ln=fm[i]
        if ln.strip()=="tags:":
            out.append("tags:")
            i+=1; items=[]; mixed=False
            while i<len(fm) and fm[i].lstrip().startswith("- "):
                if not fm[i].startswith("  - "): mixed=True
                items.append(fm[i].lstrip()[2:].strip()); i+=1
            seen=set()
            for it in items:
                if it and it not in seen: seen.add(it); out.append(f"  - {it}")
            if mixed: changed=True
            continue
        out.append(ln); i+=1
    if changed:
        open(path,"w",encoding="utf-8").write("\n".join(["---"]+out+["---"]+body))
    return changed
n=sum(1 for p in sys.argv[1:] if fix(p))
print(f"fixed {n}/{len(sys.argv)-1} files")
