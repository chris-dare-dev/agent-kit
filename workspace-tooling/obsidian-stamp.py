#!/usr/bin/env python3
"""
obsidian-stamp.py — Add Obsidian-compatible YAML frontmatter to Markdown files.

Uses TEXT manipulation (not YAML roundtrip) so existing frontmatter formatting is
never disturbed. Idempotent: re-running adds only what's missing.

Usage:
    python3 obsidian-stamp.py [--dry-run] [--verbose] PATH [PATH ...]
"""
import argparse
import re
import sys
from datetime import date as Date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EXCLUDED_DIRS = {
    '.venv', 'node_modules', '.git', '__pycache__', 'dist', 'build',
    '.obsidian', 'confluence-revisions',
}

# Checked in order; first match wins. Longer/more-specific patterns come first.
PROJECT_PATTERNS: List[Tuple[str, str]] = [
    ('airgap-zerotrust', 'airgap-zerotrust'),
    ('airgap',           'airgap'),
    ('dispatcher',        'dispatcher'),
    ('zero-trust',       'zerotrust'),
    ('zerotrust',        'zerotrust'),
    ('crossplane',       'crossplane'),
    ('kargo',            'kargo'),
    ('service-registry', 'service-registry'),
    ('cost-optimization','cost-optimization'),
    ('milestone-pipeline','milestone-pipeline'),
    ('self-improving',   'self-improving-tooling'),
    ('admin-web-app',    'admin-web-app'),
    ('ruflo',            'ruflo'),
]

# Default status per type (None = don't add status key)
DEFAULT_STATUS = {
    'handoff':          'complete',
    'decision':         'complete',
    'roadmap':          'active',
    'plan':             'active',
    'agent':            'active',
    'command':          'active',
    'reference':        'active',
    'memory':           'active',
    'agent-memory':     'active',
    'skill':            None,   # computed: 'deprecated' or 'active'
    'daily-note':       None,   # computed from date
    'confluence-mirror': None,
    'moc':              None,
    'note':             None,
}


# ---------------------------------------------------------------------------
# Type detection
# ---------------------------------------------------------------------------

def detect_type(filepath: Path) -> str:
    name = filepath.name.lower()
    path_str = str(filepath).replace('\\', '/')

    if re.match(r'\d{4}-\d{2}-\d{2}\.md$', name):
        return 'daily-note'
    if 'handoff' in name:
        return 'handoff'
    if 'roadmap' in name:
        return 'roadmap'
    if re.search(r'-sp\d+', name) or '-decision' in name:
        return 'decision'
    if '/agents/' in path_str:
        return 'agent'
    if '/commands/' in path_str:
        return 'command'
    if '/skills/' in path_str:
        return 'skill'
    if '/references/' in path_str:
        return 'reference'
    if '/memory/' in path_str and re.match(r'(project|feedback|reference|user)_', name):
        return 'memory'
    if 'agent-memory' in path_str:
        return 'agent-memory'
    if '/confluence/' in path_str.lower():
        return 'confluence-mirror'
    if name in ('claude.md', 'agents.md', 'home.md', 'readme.md', 'index.md'):
        return 'moc'
    if '/plans/' in path_str:
        return 'plan'
    return 'note'


def detect_project(filepath: Path) -> Optional[str]:
    path_lower = str(filepath).lower().replace('\\', '/')
    for pattern, canonical in PROJECT_PATTERNS:
        if pattern in path_lower:
            return canonical
    return None


def compute_status(file_type: str, filepath: Path, existing_fm: str) -> Optional[str]:
    """Return status value to add, or None if not applicable/already set."""
    # Never overwrite existing status key
    if re.search(r'^status\s*:', existing_fm, re.MULTILINE):
        return None

    if file_type == 'daily-note':
        m = re.match(r'(\d{4}-\d{2}-\d{2})', filepath.stem)
        if m:
            note_date = datetime.strptime(m.group(1), '%Y-%m-%d').date()
            return 'archived' if note_date < Date.today() else 'active'
        return None

    if file_type == 'skill':
        try:
            content = filepath.read_text(encoding='utf-8', errors='replace')
            if 'DEPRECATED' in content or 'deprecated' in filepath.name.lower():
                return 'deprecated'
        except OSError:
            pass
        return 'active'

    return DEFAULT_STATUS.get(file_type)


# ---------------------------------------------------------------------------
# Text-level frontmatter helpers (no YAML roundtrip)
# ---------------------------------------------------------------------------

def fm_has_key(fm_text: str, key: str) -> bool:
    return bool(re.search(rf'^{re.escape(key)}\s*:', fm_text, re.MULTILINE))


def fm_get_scalar(fm_text: str, key: str) -> Optional[str]:
    m = re.search(rf'^{re.escape(key)}\s*:\s*(.+)$', fm_text, re.MULTILINE)
    return m.group(1).strip() if m else None


def fm_get_tags(fm_text: str) -> List[str]:
    """Extract existing tags (block indented, block flush, or flow style)."""
    # Block style: tags:\n  - foo  OR  tags:\n- foo  (Obsidian sometimes writes without indent)
    m = re.search(r'^tags\s*:\s*\n((?:[ \t]*-[^\n]*\n?)*)', fm_text, re.MULTILINE)
    if m:
        return [re.sub(r'^[\s\-]+', '', ln).strip()
                for ln in m.group(1).splitlines() if ln.strip()]
    # Flow style: tags: [foo, bar]
    m = re.search(r'^tags\s*:\s*\[([^\]]*)\]', fm_text, re.MULTILINE)
    if m:
        return [t.strip().strip('\'"') for t in m.group(1).split(',') if t.strip()]
    return []


def split_fm(content: str) -> Tuple[Optional[str], bool]:
    """
    Returns (fm_text, has_fm).
    fm_text: text between the two --- markers (not including them).
    Returns (None, False) if no valid frontmatter.
    """
    if not content.startswith('---\n'):
        return None, False
    close = content.find('\n---\n', 4)
    if close == -1:
        # Handle EOF close: \n---\n$ or \n---$
        if re.search(r'\n---\s*$', content):
            close = content.rfind('\n---')
        else:
            return None, False
    return content[4:close], True


def reconstruct(content: str, new_fm: str) -> str:
    """Replace the frontmatter block in content with new_fm."""
    close = content.find('\n---\n', 4)
    if close == -1:
        # EOF close
        close = content.rfind('\n---')
        return '---\n' + new_fm + '\n---\n' + content[close + 4:].lstrip('\n')
    body = content[close + 5:]   # text after \n---\n
    return '---\n' + new_fm + '\n---\n' + body


# ---------------------------------------------------------------------------
# Core stamp logic
# ---------------------------------------------------------------------------

def compute_additions(
    fm_text: str,
    file_type: str,
    project: Optional[str],
    status: Optional[str],
    existing_tags: List[str],
) -> List[Tuple[str, object]]:
    """
    Returns list of (key, value) pairs that need to be added.
    key 'tags_append' → value is a list of tag strings to append to existing block.
    key 'tags_new'    → value is a list of tag strings (no tags key exists yet).
    """
    ops: List[Tuple[str, object]] = []
    existing_tag_set = set(existing_tags)
    has_status_tag = any(t.startswith('status/') for t in existing_tags)

    # type:
    if not fm_has_key(fm_text, 'type'):
        ops.append(('type', file_type))

    # project:
    if project and not fm_has_key(fm_text, 'project'):
        ops.append(('project', project))

    # date: (daily notes only)
    if file_type == 'daily-note' and not fm_has_key(fm_text, 'date'):
        m = re.match(r'(\d{4}-\d{2}-\d{2})', Path(fm_text).stem if False else '')
        # Passed in as status derivation above; re-compute from filepath via status call chain.
        # We'll inject it from the caller.

    # status:
    if status and not fm_has_key(fm_text, 'status'):
        ops.append(('status', status))

    # tags:
    desired_tags = [f'type/{file_type}']
    if project:
        desired_tags.append(f'project/{project}')
    if status and not has_status_tag:
        desired_tags.append(f'status/{status}')
    new_tags = [t for t in desired_tags if t not in existing_tag_set]

    if new_tags:
        if fm_has_key(fm_text, 'tags'):
            ops.append(('tags_append', new_tags))
        else:
            ops.append(('tags_new', new_tags))

    return ops


def apply_ops(fm_text: str, ops: List[Tuple[str, object]], date_val: Optional[str] = None) -> str:
    """Apply computed additions to the frontmatter text."""
    result = fm_text
    # split_fm strips the \n that precedes \n---\n, so fm_text may not end with \n.
    # Ensure a trailing newline so appended keys never run together with existing content.
    if result and not result.endswith('\n'):
        result += '\n'

    for key, value in ops:
        if key == 'type':
            result += f'type: {value}\n'
        elif key == 'project':
            result += f'project: {value}\n'
        elif key == 'status':
            result += f'status: {value}\n'
        elif key == 'date':
            result += f'date: {value}\n'
        elif key == 'tags_new':
            tags: List[str] = value  # type: ignore
            result += 'tags:\n' + ''.join(f'  - {t}\n' for t in tags)
        elif key == 'tags_append':
            tags = value  # type: ignore
            # Insert after the last line of the existing tags block (indented OR flush)
            m = re.search(r'^tags\s*:\s*\n((?:[ \t]*-[^\n]*\n?)*)', result, re.MULTILINE)
            if m:
                # Detect existing item indentation and match it
                item_m = re.search(r'^([ \t]*)-', m.group(1), re.MULTILINE)
                indent = item_m.group(1) if item_m else '  '
                insert_at = m.start() + len(m.group(0))
                if not result[insert_at - 1:insert_at].endswith('\n'):
                    addition = '\n' + ''.join(f'{indent}- {t}\n' for t in tags)
                else:
                    addition = ''.join(f'{indent}- {t}\n' for t in tags)
                result = result[:insert_at] + addition + result[insert_at:]
            else:
                # Flow style or couldn't find block — append as new block entries
                result += ''.join(f'  - {t}\n' for t in tags)
            # After mid-block insertion, result may no longer end with \n
            if result and not result.endswith('\n'):
                result += '\n'

    if date_val:
        if not re.search(r'^date\s*:', result, re.MULTILINE):
            result += f'date: {date_val}\n'

    return result


def stamp_file(filepath: Path, dry_run: bool, verbose: bool) -> bool:
    try:
        content = filepath.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        print(f'WARNING: cannot read {filepath}: {e}', file=sys.stderr)
        return False

    file_type = detect_type(filepath)
    project   = detect_project(filepath)

    fm_text, has_fm = split_fm(content)

    if has_fm:
        # Use existing type if already set (don't impose ours)
        existing_type = fm_get_scalar(fm_text, 'type') or file_type
        status = compute_status(existing_type, filepath, fm_text)
        existing_tags = fm_get_tags(fm_text)

        ops = compute_additions(fm_text, file_type, project, status, existing_tags)

        # Inject date for daily-notes
        date_val: Optional[str] = None
        if existing_type == 'daily-note' and not fm_has_key(fm_text, 'date'):
            m2 = re.match(r'(\d{4}-\d{2}-\d{2})', filepath.stem)
            if m2:
                date_val = m2.group(1)

        if not ops and not date_val:
            return False  # Nothing to do

        if verbose:
            keys = [k if k not in ('tags_append', 'tags_new') else 'tags' for k, _ in ops]
            if date_val:
                keys.append('date')
            print(f'  MERGE {keys} → {filepath}')

        if not dry_run:
            new_fm = apply_ops(fm_text, ops, date_val)
            filepath.write_text(reconstruct(content, new_fm), encoding='utf-8')
        return True

    else:
        # No frontmatter at all — build from scratch
        status = compute_status(file_type, filepath, '')
        tags = [f'type/{file_type}']
        if project:
            tags.append(f'project/{project}')
        if status:
            tags.append(f'status/{status}')

        lines = [f'type: {file_type}\n']
        if project:
            lines.append(f'project: {project}\n')

        m2 = re.match(r'(\d{4}-\d{2}-\d{2})', filepath.stem)
        if file_type == 'daily-note' and m2:
            lines.append(f'date: {m2.group(1)}\n')

        if status:
            lines.append(f'status: {status}\n')

        lines.append('tags:\n')
        lines.extend(f'  - {t}\n' for t in tags)

        new_content = '---\n' + ''.join(lines) + '---\n' + content

        if verbose:
            print(f'  ADD frontmatter → {filepath} (type={file_type})')
        if not dry_run:
            filepath.write_text(new_content, encoding='utf-8')
        return True


# ---------------------------------------------------------------------------
# Directory walker
# ---------------------------------------------------------------------------

def should_skip(filepath: Path) -> bool:
    for part in filepath.parts:
        if part in EXCLUDED_DIRS:
            return True
    return False


def process(paths: List[str], dry_run: bool, verbose: bool) -> Tuple[int, int]:
    processed = modified = 0
    for p_str in paths:
        p = Path(p_str)
        if p.is_file():
            if p.suffix == '.md' and not should_skip(p):
                processed += 1
                if stamp_file(p, dry_run, verbose):
                    modified += 1
        elif p.is_dir():
            for md in sorted(p.rglob('*.md')):
                if not should_skip(md):
                    processed += 1
                    if stamp_file(md, dry_run, verbose):
                        modified += 1
        else:
            print(f'WARNING: {p_str} not found', file=sys.stderr)
    return processed, modified


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Stamp Obsidian-compatible frontmatter onto Markdown files.'
    )
    parser.add_argument('paths', nargs='+', help='Files or directories to process')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without writing files')
    parser.add_argument('--verbose', action='store_true',
                        help='Log type detection and changes per file')
    args = parser.parse_args()

    processed, modified = process(args.paths, args.dry_run, args.verbose)
    mode = '[DRY RUN] ' if args.dry_run else ''
    print(f'{mode}Processed {processed} files, modified {modified}.')


if __name__ == '__main__':
    main()
