/**
 * One content digest for a file, a symlink, or a whole directory tree.
 *
 * This module exists because init and uninstall each had their own `sha256()`,
 * agreeing only by a comment that said "same hashing rule init used". Both
 * hashed a directory's sorted CHILD NAMES:
 *
 *     createHash("sha256").update(readdirSync(path).sort().join("\n"))
 *
 * That is blind to every edit that matters. Change the text of
 * `.claude/commands/roadmap.md`, add a file two levels down, flip a symlink to
 * a new target, chmod something — the name list is identical, so the digest is
 * identical, so uninstall concludes the tree is untouched and `rmSync`s it
 * recursively. The user's edits go with it. Confirmed reproducible, and now
 * pinned by tests/uninstall.test.mjs.
 *
 * What a digest has to cover to license a recursive delete:
 *
 *   relative path   so a rename is a change, not a coincidence of counts
 *   entry type      so a file replaced by a directory is a change
 *   content         the actual bug: names alone say nothing about bytes
 *   symlink target  a retargeted link has identical bytes at the link itself
 *   mode            a chmod is a change the owner cares about
 *
 * Symlinks are hashed by their TARGET PATH, never by following them. A planted
 * `.claude/skills -> data/skills` link is "unchanged" as long as it still points
 * where init put it; whether data/ has since moved on is a different question,
 * and following the link would also mean re-hashing the entire bundled tree on
 * every comparison.
 *
 * Errs toward "changed": anything unreadable digests to a value that will not
 * match, so the caller skips rather than deletes.
 */
import { createHash } from "node:crypto";
import { lstatSync, readFileSync, readdirSync, readlinkSync } from "node:fs";
import { join } from "node:path";

/** Stable, unambiguous record for one entry. NUL-separated: no path can forge it. */
function entryRecord(relPath, stat, payload) {
  const type = stat.isSymbolicLink() ? "symlink" : stat.isDirectory() ? "dir" : "file";
  const mode = (stat.mode & 0o7777).toString(8);
  return `${relPath}\0${type}\0${mode}\0${payload}\0`;
}

function walk(absPath, relPath, hash) {
  const stat = lstatSync(absPath);

  if (stat.isSymbolicLink()) {
    // Normalise separators so a link recorded on Windows and compared on the
    // same host does not appear to change with path style alone.
    hash.update(entryRecord(relPath, stat, readlinkSync(absPath).split("\\").join("/")));
    return;
  }
  if (stat.isDirectory()) {
    hash.update(entryRecord(relPath, stat, ""));
    const names = readdirSync(absPath).sort();
    for (const name of names) {
      walk(join(absPath, name), relPath ? `${relPath}/${name}` : name, hash);
    }
    return;
  }
  hash.update(entryRecord(relPath, stat, createHash("sha256").update(readFileSync(absPath)).digest("hex")));
}

/**
 * Digest of `path`, or undefined if it cannot be read at all.
 *
 * undefined is deliberately NOT a hash: a caller comparing it against a
 * recorded value gets a non-match, which routes to "skip", not "delete".
 */
export function treeDigest(path) {
  try {
    const hash = createHash("sha256");
    walk(path, "", hash);
    return hash.digest("hex");
  } catch {
    return undefined;
  }
}
