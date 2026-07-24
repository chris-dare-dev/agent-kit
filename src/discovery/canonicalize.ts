/**
 * Name canonicalization for name-addressed lookups.
 *
 * Reference and context-guide display names are derived from filenames
 * (hyphenated on disk) but stored in space form (`git-topology.md` ->
 * "git topology"). Every CLAUDE.md tier, however, documents and calls the
 * HYPHENATED form — `get_reference("git-topology")`,
 * `get_context_guide("service-mesh")` — so an exact-match lookup misses.
 *
 * Collapsing hyphen, whitespace, and case to a single canonical key
 * (lower-cased, hyphen-joined) makes both `"git-topology"` and
 * `"git topology"` resolve to the same entry. Applied at lookup time only,
 * this is strictly additive: whichever form an existing caller already uses
 * keeps working, so no name-addressed tool is ever renamed.
 */
export function canonicalizeName(name: string): string {
  return name.trim().toLowerCase().replace(/[\s-]+/g, "-");
}
