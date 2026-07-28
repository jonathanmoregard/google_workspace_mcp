"""Tests for the docs_preview code generator (codegen/generate.py).

Covers: golden-file output on a small fixture discovery subset, byte-level
idempotency, committed-artifact freshness, and integration-point sync
(tool_tiers.yaml, SERVICE_MODULES, scope maps).
"""

import json
from pathlib import Path

import yaml

from codegen.generate import generate, write_files

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"
GENERATED_DIR = REPO_ROOT / "gdocs_preview" / "generated"


def _generate_from_fixtures() -> dict[str, str]:
    return generate(
        docs_path=FIXTURES / "docs-v1.json",
        drive_path=FIXTURES / "drive-v3.json",
        overlay_path=FIXTURES / "overlay.json",
        config_path=FIXTURES / "generator_config.json",
    )


class TestGoldenOutput:
    def test_fixture_output_matches_golden_files(self):
        files = _generate_from_fixtures()
        golden_names = sorted(p.name for p in GOLDEN.iterdir())
        assert sorted(files) == golden_names
        for name, content in files.items():
            expected = (GOLDEN / name).read_text(encoding="utf-8")
            assert content == expected, f"golden mismatch for {name}"

    def test_fixture_preview_member_is_marked_and_separated(self):
        files = _generate_from_fixtures()
        assert "docs_api_accept_suggestion" in files["docs_preview.py"]
        assert "[DEVELOPER PREVIEW]" in files["docs_preview.py"]
        assert "docs_api_accept_suggestion" not in files["docs_batch_update.py"]
        assert "[GA]" in files["docs_batch_update.py"]

    def test_fixture_write_mode_respects_config(self):
        files = _generate_from_fixtures()
        # insertText is suggest-compatible -> gets write_mode
        assert "write_mode" in files["docs_batch_update.py"]
        # acceptSuggestion is listed as unsupported -> no write_mode
        assert "write_mode" not in files["docs_preview.py"]


class TestIdempotency:
    def test_real_inputs_generate_identical_bytes(self):
        first = generate()
        second = generate()
        assert first == second

    def test_write_files_round_trip_is_stable(self, tmp_path):
        files = _generate_from_fixtures()
        out = tmp_path / "gen"
        written_first = write_files(files, out)
        assert sorted(written_first) == sorted(files)
        written_second = write_files(_generate_from_fixtures(), out)
        assert written_second == []
        for name, content in files.items():
            assert (out / name).read_text(encoding="utf-8") == content


class TestCommittedArtifacts:
    def test_committed_generated_files_are_up_to_date(self):
        """Regenerating from committed inputs must reproduce committed outputs."""
        files = generate()
        committed = sorted(
            p.name for p in GENERATED_DIR.iterdir() if p.name != "__pycache__"
        )
        assert sorted(files) == committed
        for name, content in files.items():
            on_disk = (GENERATED_DIR / name).read_text(encoding="utf-8")
            assert on_disk == content, (
                f"{name} is stale; run `python codegen/generate.py`"
            )

    def test_manifest_counts(self):
        manifest = json.loads((GENERATED_DIR / "manifest.json").read_text())
        tools = manifest["tools"]
        assert len(tools) == 61
        assert sum(1 for t in tools if t["preview"]) == 8
        assert sum(1 for t in tools if not t["preview"]) == 53
        names = [t["name"] for t in tools]
        assert names == sorted(names)
        assert len(set(names)) == len(names)


class TestIntegrationPoints:
    def test_tool_tiers_yaml_matches_manifest(self):
        manifest = json.loads((GENERATED_DIR / "manifest.json").read_text())
        expected = [t["name"] for t in manifest["tools"]]
        tiers = yaml.safe_load(
            (REPO_ROOT / "core" / "tool_tiers.yaml").read_text(encoding="utf-8")
        )
        assert "docs_preview" in tiers
        assert tiers["docs_preview"]["core"] == []
        assert tiers["docs_preview"]["extended"] == [
            "docs_review_list_suggestions",
            "docs_review_capabilities",
            "docs_review_read_document",
        ]
        assert sorted(tiers["docs_preview"]["complete"]) == expected

    def test_service_modules_entry(self):
        from main import SERVICE_MODULES, VALID_SERVICES

        assert SERVICE_MODULES["docs_preview"] == "gdocs_preview"
        assert "docs_preview" in VALID_SERVICES

    def test_scope_maps_entries(self):
        from auth.scopes import (
            DOCS_READONLY_SCOPE,
            DOCS_WRITE_SCOPE,
            DRIVE_READONLY_SCOPE,
            DRIVE_SCOPE,
            TOOL_READONLY_SCOPES_MAP,
            TOOL_SCOPES_MAP,
        )

        full = TOOL_SCOPES_MAP["docs_preview"]
        assert DOCS_READONLY_SCOPE in full
        assert DOCS_WRITE_SCOPE in full
        assert DRIVE_SCOPE in full
        readonly = TOOL_READONLY_SCOPES_MAP["docs_preview"]
        assert readonly == [DOCS_READONLY_SCOPE, DRIVE_READONLY_SCOPE]

    def test_permissions_mode_entry(self):
        from auth.permissions import SERVICE_PERMISSION_LEVELS

        levels = dict(SERVICE_PERMISSION_LEVELS["docs_preview"])
        assert set(levels) == {"readonly", "full"}
