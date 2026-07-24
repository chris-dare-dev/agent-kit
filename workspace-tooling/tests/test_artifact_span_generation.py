from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import re
import sys
import unittest
from dataclasses import replace
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_ingestion as ingestion  # noqa: E402
import artifact_span_generation as spans  # noqa: E402


class WordpieceCounter:
    """Small deterministic stand-in for the pinned tokenizer.

    It deliberately reports the same character offsets as its count uses, so
    these tests exercise span construction rather than a model download.
    """

    _token = re.compile(r"\S+")

    def __call__(self, value: str) -> int:
        # The production ceiling includes the tokenizer's two special tokens.
        return len(self.offsets(value)) + 2

    def offsets(self, value: str) -> list[tuple[int, int]]:
        return [match.span() for match in self._token.finditer(value)]


def artifact(relative_path: str, text: str) -> ingestion.CatalogArtifact:
    raw = text.encode("utf-8")
    return ingestion.CatalogArtifact(
        artifact_id="artifact-1",
        revision_id="revision-1",
        relative_path=relative_path,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        mtime_ns=1,
        artifact_type="plan",
        authority_class="working",
        lifecycle_hints=("active",),
        source_scope="workspace",
        repository=None,
        project="test-project",
    )


def generate(
    text: str,
    *,
    relative_path: str = "plans/example.md",
    generation: str = "generation-a",
    profile_digest: str = "profile-a",
    model_manifest_digest: str = "model-a",
) -> list[spans.ExactSpan]:
    return spans.exact_spans(
        artifact=artifact(relative_path, text),
        text=text,
        counter=WordpieceCounter(),
        profile_digest=profile_digest,
        model_manifest_digest=model_manifest_digest,
        generation=generation,
    )


class ExactSpanGenerationTests(unittest.TestCase):
    def test_utf8_and_crlf_coordinates_are_exact(self) -> None:
        text = "# Café 🚀\r\n\r\nnaïve α\r\n"
        generated = generate(text)

        self.assertEqual(len(generated), 1)
        result = generated[0]
        raw = text.encode("utf-8")
        self.assertEqual(result.content, text)
        self.assertEqual((result.char_start, result.char_end), (0, len(text)))
        self.assertEqual((result.byte_start, result.byte_end), (0, len(raw)))
        self.assertEqual((result.line_start, result.line_end), (1, 3))
        self.assertEqual(
            raw[result.byte_start : result.byte_end].decode("utf-8"),
            result.content,
        )
        self.assertEqual(
            result.span_sha256,
            hashlib.sha256(raw).hexdigest(),
        )

    def test_duplicate_paragraphs_have_unambiguous_distinct_intervals(self) -> None:
        text = "# First\nsame paragraph\n# Second\nsame paragraph\n"
        generated = generate(text)

        self.assertEqual([item.heading for item in generated], ["First", "Second"])
        self.assertEqual(len({item.span_id for item in generated}), 2)
        self.assertEqual(len({item.byte_start for item in generated}), 2)
        for item in generated:
            self.assertEqual(
                text.encode("utf-8")[item.byte_start : item.byte_end].decode("utf-8"),
                item.content,
            )
            self.assertIn("same paragraph", item.content)

    def test_markdown_headings_split_sections_but_fenced_heading_does_not(self) -> None:
        text = (
            "# Top\n"
            "outside\n"
            "```markdown\n"
            "# Not a heading\n"
            "```\n"
            "## Real heading\n"
            "inside\n"
        )
        generated = generate(text)

        self.assertEqual(
            [item.heading for item in generated],
            ["Top", "Real heading"],
        )
        self.assertIn("# Not a heading", generated[0].content)
        self.assertNotIn("## Real heading", generated[0].content)
        self.assertTrue(generated[1].content.startswith("## Real heading\n"))
        self.assertEqual(generated[0].char_end, generated[1].char_start)

    def test_oversized_single_line_advances_and_overlaps_within_bounds(self) -> None:
        text = " ".join(f"token-{index:04d}" for index in range(700))
        generated = generate(text, relative_path="logs/oversized.txt")

        self.assertGreater(len(generated), 1)
        self.assertEqual(generated[0].char_start, 0)
        self.assertEqual(generated[-1].char_end, len(text))
        self.assertEqual(len({item.point_id for item in generated}), len(generated))
        self.assertTrue(
            all(
                item.content_tokens <= spans.TARGET_CONTENT_TOKENS
                and item.embedding_tokens <= spans.MAX_EMBEDDING_TOKENS
                for item in generated
            )
        )
        self.assertTrue(
            all(
                current.char_start < prior.char_end
                for prior, current in zip(generated, generated[1:])
            )
        )
        self.assertTrue(
            all(
                text[item.char_start : item.char_end] == item.content
                for item in generated
            )
        )

    def test_span_and_point_ids_are_deterministic_and_generation_scoped(self) -> None:
        text = "first line\nsecond line\n"
        first = generate(text, relative_path="notes/example.txt")
        repeated = generate(text, relative_path="notes/example.txt")
        next_generation = generate(
            text,
            relative_path="notes/example.txt",
            generation="generation-b",
        )
        next_model = generate(
            text,
            relative_path="notes/example.txt",
            model_manifest_digest="model-b",
        )
        next_profile = generate(
            text,
            relative_path="notes/example.txt",
            profile_digest="profile-b",
        )

        self.assertEqual(first, repeated)
        self.assertEqual(
            [item.span_id for item in first],
            [item.span_id for item in next_generation],
        )
        self.assertNotEqual(
            [item.point_id for item in first],
            [item.point_id for item in next_generation],
        )
        self.assertEqual(
            [item.span_id for item in first],
            [item.span_id for item in next_model],
        )
        self.assertNotEqual(
            [item.point_id for item in first],
            [item.point_id for item in next_model],
        )
        self.assertNotEqual(
            [item.span_id for item in first],
            [item.span_id for item in next_profile],
        )

    def test_revision_change_changes_span_identity(self) -> None:
        text = "unchanged bytes\n"
        original = artifact("notes/example.txt", text)
        changed_revision = replace(original, revision_id="revision-2")

        first = spans.exact_spans(
            artifact=original,
            text=text,
            counter=WordpieceCounter(),
            profile_digest="profile-a",
            model_manifest_digest="model-a",
            generation="generation-a",
        )
        second = spans.exact_spans(
            artifact=changed_revision,
            text=text,
            counter=WordpieceCounter(),
            profile_digest="profile-a",
            model_manifest_digest="model-a",
            generation="generation-a",
        )

        self.assertNotEqual(first[0].span_id, second[0].span_id)
        self.assertNotEqual(first[0].point_id, second[0].point_id)


class StructuredInputValidationTests(unittest.TestCase):
    def test_json_valid_invalid_depth_and_node_bounds(self) -> None:
        spans._validate_structured(Path("artifact.json"), '{"a":[1,2,3]}')

        with self.assertRaisesRegex(spans.SpanGenerationError, "invalid JSON"):
            spans._validate_structured(Path("artifact.json"), '{"a":')

        deeply_nested = "0"
        for _ in range(65):
            deeply_nested = f"[{deeply_nested}]"
        with self.assertRaisesRegex(
            spans.SpanGenerationError,
            "exceeds parser bounds",
        ):
            spans._validate_structured(Path("artifact.json"), deeply_nested)

        too_many_nodes = "[" + ",".join("0" for _ in range(10_001)) + "]"
        with self.assertRaisesRegex(
            spans.SpanGenerationError,
            "exceeds parser bounds",
        ):
            spans._validate_structured(Path("artifact.json"), too_many_nodes)

    @unittest.skipUnless(
        importlib.util.find_spec("yaml") is not None,
        "PyYAML is not installed",
    )
    def test_yaml_valid_invalid_and_depth_bounds(self) -> None:
        spans._validate_structured(
            Path("artifact.yaml"),
            "service:\n  name: memory\n  ports:\n    - 6333\n",
        )

        with self.assertRaisesRegex(spans.SpanGenerationError, "invalid YAML"):
            spans._validate_structured(Path("artifact.yml"), "service: [broken")

        deeply_nested = ""
        for depth in range(66):
            deeply_nested += "  " * depth + f"level-{depth}:\n"
        deeply_nested += "  " * 66 + "value\n"
        with self.assertRaisesRegex(
            spans.SpanGenerationError,
            "exceeds parser bounds",
        ):
            spans._validate_structured(Path("artifact.yaml"), deeply_nested)


class ManifestPublicationTests(unittest.TestCase):
    def test_publication_is_durable_and_never_replaces_a_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            root.chmod(0o700)
            destination = root / "generation.sqlite3"
            first = root / ".first"
            first.write_bytes(b"first")
            first.chmod(0o600)

            spans._publish_private_manifest(first, destination, root)

            self.assertEqual(destination.read_bytes(), b"first")
            self.assertFalse(first.exists())
            second = root / ".second"
            second.write_bytes(b"second")
            second.chmod(0o600)
            with self.assertRaisesRegex(
                spans.SpanGenerationError,
                "already exists",
            ):
                spans._publish_private_manifest(second, destination, root)
            self.assertEqual(destination.read_bytes(), b"first")
            self.assertEqual(second.read_bytes(), b"second")


if __name__ == "__main__":
    unittest.main()
