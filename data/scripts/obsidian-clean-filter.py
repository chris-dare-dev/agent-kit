#!/usr/bin/env python3
"""
obsidian-clean-filter.py — git clean filter for Markdown files.

Strips volatile Obsidian-managed YAML frontmatter properties so they never
end up in git commits (Obsidian auto-updates them, causing noisy diffs).

Volatile keys stripped:  modified  created  cssclass  cssclasses  banner  cover

Register once per clone:
    python3 data/scripts/obsidian-setup.sh
  or manually:
    git config filter.obsidian-clean.clean  "python3 $(pwd)/data/scripts/obsidian-clean-filter.py"
    git config filter.obsidian-clean.smudge "cat"

Usage (by git, not directly):
    cat file.md | python3 obsidian-clean-filter.py
"""
import sys

# Keys Obsidian manages autonomously — we never want these in git.
VOLATILE = {'modified', 'created', 'cssclass', 'cssclasses', 'banner', 'cover'}


def process(content: str) -> str:
    # Normalize line endings
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    if not content.startswith('---\n'):
        return content

    # Find the CLOSING --- (not the opening one at position 0)
    close_pos = content.find('\n---\n', 4)
    if close_pos == -1:
        return content  # Unclosed frontmatter — pass through

    fm_text = content[4:close_pos]        # text between the two --- markers
    body    = content[close_pos + 5:]     # text after closing ---\n

    # Strip volatile keys with a simple regex scan (no YAML parse needed)
    changed = False
    new_lines = []
    skip_indent = False   # True while consuming an indented/continuation block

    for line in fm_text.splitlines(keepends=True):
        # Check if this is a top-level key line (no leading whitespace)
        m = None if line[:1] in (' ', '\t') else __import__('re').match(
            r'^([a-zA-Z_][a-zA-Z0-9_-]*)\s*:', line
        )

        if m:
            skip_indent = False
            if m.group(1) in VOLATILE:
                skip_indent = True   # skip this key and any following indented lines
                changed = True
                continue

        if skip_indent and line[:1] in (' ', '\t'):
            continue   # continuation line for a volatile key (e.g. list items)
        else:
            skip_indent = False

        new_lines.append(line)

    if not changed:
        return content

    new_fm = ''.join(new_lines).rstrip('\n')

    if not new_fm.strip():
        # All fields were volatile — drop the frontmatter block entirely
        return body

    return f'---\n{new_fm}\n---\n{body}'


if __name__ == '__main__':
    sys.stdout.write(process(sys.stdin.read()))
