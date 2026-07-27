# plans/

Machine-readable delivery plan for agent-kit, in the `roadmap/1` schema.

One directory per GitHub milestone. Each holds:

- `roadmap.yaml` — the milestone's epics, stories and spikes, with acceptance criteria,
  dependencies, MoSCoW priority, lane and size.
- `issue-map.json` — the item-id to GitHub-issue-number mapping produced when the milestone
  was materialized. This is what makes re-runs idempotent.

Item ids are **write-once** and are carried in the GitHub issue title, so an issue can always
be traced back to its roadmap entry and to the audit finding it closes.

| Milestone | Slug | Epics | Stories | Board |
|---|---|---|---|---|
| M1 Starts Everywhere | `starts-everywhere` | 5 | 34 | [#7](https://github.com/users/chris-dare-dev/projects/7) |
| M2 Gates Green | `gates-green` | 4 | 29 | [#7](https://github.com/users/chris-dare-dev/projects/7) |
| M3 CI Proves It | `ci-proves-it` | 4 | 31 | [#7](https://github.com/users/chris-dare-dev/projects/7) |
| M4 Named and Installable | `named-and-installable` | 4 | 25 | [#7](https://github.com/users/chris-dare-dev/projects/7) |
| M5 Native Everywhere | `native-everywhere` | 3 | 21 | [#7](https://github.com/users/chris-dare-dev/projects/7) |
| M6 Author and Measure | `author-and-measure` | 5 | 31 | [#7](https://github.com/users/chris-dare-dev/projects/7) |
| M7 Providers as Data | `providers-as-data` | 3 | 22 | [#7](https://github.com/users/chris-dare-dev/projects/7) |
| M8 Memory You Can Trust | `memory-you-can-trust` | 2 | 18 | [#7](https://github.com/users/chris-dare-dev/projects/7) |
| M9 Memory That Grows and Tiers | `memory-grows-and-tiers` | 2 | 23 | [#7](https://github.com/users/chris-dare-dev/projects/7) |

## Provenance

These plans were generated from the audit in [`docs/audit-2026-07.md`](../docs/audit-2026-07.md):
185 verified internal findings plus 53 external prior-art recommendations, deduplicated and
sequenced. Every story tags the finding ids it closes.

## Re-materializing

Both tools are dry-run by default; `--apply` is the gated write. Both are idempotent — a
re-run after a partial failure creates only what is missing.

```bash
# create/refresh milestones, epic + story issues, and native sub-issue links
roadmap-to-github.py --repo chris-dare-dev/agent-kit \
    --roadmap plans/<slug>/roadmap.yaml --apply

# populate the Lane / Priority / Size project fields
roadmap-project-fields.py --owner chris-dare-dev --project 7 \
    --roadmap plans/<slug>/roadmap.yaml --apply
```

Status is deliberately **not** written from these files: it is live workflow state, owned by
the board, not a roadmap attribute.
