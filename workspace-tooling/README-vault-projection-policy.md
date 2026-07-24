# AgentDocs projection policy

`artifact-policy.json` changes the AgentDocs mirror from a broad workspace crawl to a
default-deny presentation policy. The canonical files do not move: this policy controls only
whether a **new Obsidian alias** may be created.

The initial allowlist contains only `docs/_curated/**/*.md`. That directory is intentionally empty
until a document is promoted deliberately. Repository plans, handoffs, runtime evidence, agent
memory, failed runs, worktrees, environments, and vendored content remain discoverable by the
artifact catalog but are not newly mirrored into Obsidian.

## Safety semantics

Normal scheduled `build-agent-vault.sh` runs:

1. create missing allowlisted aliases;
2. leave every existing symlink byte-for-byte unchanged;
3. leave regular files and directories untouched;
4. perform no unlink, replacement, stale pruning, or empty-directory deletion.

This means the first phase stops amplification without cleaning historical aliases. A later prune
must consume a reviewed audit manifest and is intentionally not implemented here.

## Audit

```bash
python3 scripts/vault_projection_policy.py \
  --workspace "$HOME/Work/workspace" \
  --vault "$HOME/Work/workspace/Vault" \
  audit

scripts/build-agent-vault.sh --audit-policy
```

The JSON report lists allowed planned aliases, missing allowed aliases, existing allowed aliases,
live policy-excluded aliases that may be considered by a future cleanup, wrong-target aliases,
broken aliases, outside-workspace aliases, and destination collisions. Audit mode never modifies
the workspace or vault. With `--output`, the report path must be new and outside both trees;
existing reports are never replaced.

Add a static document deliberately by extending an `allow_rules` entry in
`artifact-policy.json`. Exclusion rules take precedence over allow rules.
