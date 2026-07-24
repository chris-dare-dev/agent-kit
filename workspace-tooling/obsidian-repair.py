#!/usr/bin/env python3
"""
obsidian-repair.py — Fix frontmatter keys that were concatenated without newlines
by the obsidian-stamp.py bug (split_fm stripped trailing \\n, causing appended keys
to run together: 'memory: projecttype: agent').

Repairs ONLY the frontmatter block; never touches the body.

Usage:
    python3 obsidian-repair.py [--dry-run] [--verbose] PATH [PATH ...]
"""
import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# These are the only keys obsidian-stamp.py appends (safe to insert newline before)
STAMP_KEYS = ['type', 'project', 'status', 'date', 'tags']

EXCLUDED_DIRS = {'.venv', 'node_modules', '.git', '__pycache__', '.obsidian'}


def split_fm(content: str) -> Tuple[Optional[str], bool]:
    if not content.startswith('---\n'):
        return None, False
    close = content.find('\n---\n', 4)
    if close == -1:
        return None, False
    return content[4:close], True


def reconstruct(content: str, new_fm: str) -> str:
    close = content.find('\n---\n', 4)
    body = content[close + 5:]
    return '---\n' + new_fm + '\n---\n' + body


def remove_stray_bare_tags(fm_text: str) -> str:
    """
    Remove bare '- tag' lines at column 0 that duplicate entries already in
    the indented tags block.  These appear when the original file used
    non-indented list items and the stamp script (buggy version) inserted new
    indented items BEFORE them instead of alongside.
    """
    m = re.search(r'^tags\s*:\s*\n((?:[ \t]+-[^\n]*\n)*)', fm_text, re.MULTILINE)
    if not m:
        return fm_text

    block_tags = {
        re.sub(r'^[\s\-]+', '', ln).strip()
        for ln in m.group(1).splitlines()
        if ln.strip()
    }

    def maybe_remove(match: re.Match) -> str:
        tag_val = match.group(1).strip()
        return '' if tag_val in block_tags else match.group(0)

    return re.sub(r'^- ([^\n]+)\n', maybe_remove, fm_text, flags=re.MULTILINE)


def undo_bad_splits(fm_text: str) -> str:
    """Undo line-splits that the repair script itself introduced inside key names.

    The previous lookbehind ``(?<=[^\n])`` matched ``_`` in ``node_type:`` and
    spaces inside indented blocks, causing it to insert spurious newlines. This
    undoes those splits before the main repair runs.
    """
    fixed = fm_text
    # Rejoin split key names: node_\ntype: → node_type:
    fixed = re.sub(r'(_)\n(type\s*:)', r'\1\2', fixed)
    # Rejoin orphaned-whitespace lines: line of only spaces/tabs then key
    for key in STAMP_KEYS:
        fixed = re.sub(rf'^([ \t]+)\n({re.escape(key)}\s*:)', r'\1\2', fixed, flags=re.MULTILINE)
    return fixed


def repair_fm(fm_text: str) -> Tuple[str, bool]:
    """Fix stamp script artifacts in frontmatter text."""
    fixed = fm_text

    # 0. Undo bad splits the prior repair run may have introduced
    fixed = undo_bad_splits(fixed)

    # 1. Insert missing newlines before stamp-added keys that got concatenated.
    # Positive lookbehind: there MUST be a preceding character that is not
    # ``_`` or whitespace (so we never match at start-of-string, never split
    # compound key names like ``node_type:``, and never affect indented lines).
    for key in STAMP_KEYS:
        fixed = re.sub(rf'(?<=[^\s_])({re.escape(key)}\s*:)', r'\n\1', fixed)

    # 2. Remove stray bare '- tag' items that duplicate the indented block
    fixed = remove_stray_bare_tags(fixed)

    return fixed, fixed != fm_text


def repair_file(filepath: Path, dry_run: bool, verbose: bool) -> bool:
    try:
        content = filepath.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        print(f'WARNING: {filepath}: {e}', file=sys.stderr)
        return False

    fm_text, has_fm = split_fm(content)
    if not has_fm:
        return False

    fixed_fm, changed = repair_fm(fm_text)
    if not changed:
        return False

    if verbose:
        print(f'  REPAIR {filepath}')

    if not dry_run:
        filepath.write_text(reconstruct(content, fixed_fm), encoding='utf-8')
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description='Repair concatenated frontmatter keys.')
    parser.add_argument('paths', nargs='+')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    repaired = 0
    for p_str in args.paths:
        p = Path(p_str)
        files = [p] if p.is_file() else list(p.rglob('*.md'))
        for f in files:
            if f.suffix == '.md' and not any(ex in f.parts for ex in EXCLUDED_DIRS):
                if repair_file(f, args.dry_run, args.verbose):
                    repaired += 1

    mode = '[DRY RUN] ' if args.dry_run else ''
    print(f'{mode}Repaired {repaired} files.')


if __name__ == '__main__':
    main()
