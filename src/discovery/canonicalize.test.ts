import { test } from "node:test";
import assert from "node:assert/strict";
import { canonicalizeName } from "./canonicalize.js";

test("canonicalizeName collapses hyphen, whitespace, and case to one key", () => {
  assert.equal(canonicalizeName("git-topology"), "git-topology");
  assert.equal(canonicalizeName("git topology"), "git-topology");
  assert.equal(canonicalizeName("Git Topology"), "git-topology");
  assert.equal(canonicalizeName("  service-mesh  "), "service-mesh");
  assert.equal(canonicalizeName("service mesh"), "service-mesh");
  // mixed / repeated separators collapse to a single hyphen
  assert.equal(canonicalizeName("a - b"), "a-b");
  assert.equal(canonicalizeName("a  b"), "a-b");
});
