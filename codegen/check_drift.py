#!/usr/bin/env python3
"""Discovery-drift checker for the generated docs_preview tool surface.

Two checks:
  1. Staleness (always, offline-safe): regenerating from the *committed*
     discovery/overlay inputs must reproduce the committed artifacts in
     gdocs_preview/generated/ byte-for-byte.
  2. Drift (network): refetch the public discovery documents, regenerate using
     the fresh discovery plus the committed overlay/config, and diff against
     the committed artifacts. Network absence is tolerated with a clear SKIP
     (exit 0) so scheduled runs do not fail spuriously.

Revision-only discovery bumps produce no generated-code diff (generated files
embed no revision), so they are intentionally not reported as drift and the
committed discovery JSONs are left untouched.

Exit codes:
  0  no drift (or drift fixed in place via --update, or network SKIP)
  1  drift detected (without --update)
  2  committed artifacts are stale relative to committed inputs

Usage:
  python codegen/check_drift.py             # check only
  python codegen/check_drift.py --update    # on drift: write fresh discovery
                                            # JSONs + regenerate artifacts
  python codegen/check_drift.py --offline   # staleness check only
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codegen.generate import (  # noqa: E402
    DEFAULT_OUT_DIR,
    DISCOVERY_DIR,
    generate,
    write_files,
)

DISCOVERY_URLS = {
    "docs-v1.json": "https://docs.googleapis.com/$discovery/rest?version=v1",
    "drive-v3.json": "https://www.googleapis.com/discovery/v1/apis/drive/v3/rest",
}

FETCH_TIMEOUT_SECONDS = 30


def read_committed_artifacts() -> dict[str, str]:
    files = {}
    for path in sorted(DEFAULT_OUT_DIR.iterdir()):
        if path.name == "__pycache__":
            continue
        files[path.name] = path.read_text(encoding="utf-8")
    return files


def diff_summary(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    lines = []
    for name in sorted(set(expected) | set(actual)):
        if name not in expected:
            lines.append(f"  extra committed file: {name}")
        elif name not in actual:
            lines.append(f"  missing committed file: {name}")
        elif expected[name] != actual[name]:
            lines.append(f"  changed: {name}")
    return lines


def fetch_discovery(tmp_dir: Path) -> dict[str, Path] | None:
    """Fetch public discovery docs. Returns None (skip) on network failure."""
    fetched = {}
    for filename, url in DISCOVERY_URLS.items():
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "gdocs-review-mcp drift check"}
            )
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
            # Validate it parses and looks like a discovery document. NOTE:
            # fetched content is untrusted data; it is only parsed as JSON and
            # fed to the deterministic generator, never executed.
            parsed = json.loads(raw)
            if parsed.get("discoveryVersion") is None and parsed.get("kind") is None:
                print(f"SKIP: {url} did not return a discovery document.")
                return None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            print(f"SKIP: could not fetch {url}: {exc}")
            return None
        except json.JSONDecodeError as exc:
            print(f"SKIP: invalid JSON from {url}: {exc}")
            return None
        target = tmp_dir / filename
        target.write_bytes(raw)
        fetched[filename] = target
    return fetched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip the network refetch; only verify committed inputs -> artifacts.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="On drift, write fresh discovery JSONs and regenerate in place.",
    )
    args = parser.parse_args()

    committed = read_committed_artifacts()

    # Check 1: committed inputs must reproduce committed artifacts.
    from_committed = generate()
    stale = diff_summary(committed, from_committed)
    if stale:
        print("STALE: committed artifacts do not match committed inputs:")
        print("\n".join(stale))
        print("Run: python codegen/generate.py")
        return 2
    print("OK: committed artifacts match committed inputs.")

    if args.offline:
        print("Offline mode: skipping discovery refetch.")
        return 0

    # Check 2: fresh discovery + committed overlay must match committed artifacts.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="discovery-drift-") as tmp:
        tmp_dir = Path(tmp)
        fetched = fetch_discovery(tmp_dir)
        if fetched is None:
            print("SKIP: network unavailable; drift check not performed.")
            return 0

        fresh = generate(
            docs_path=fetched["docs-v1.json"],
            drive_path=fetched["drive-v3.json"],
        )
        drift = diff_summary(committed, fresh)
        if not drift:
            print("OK: no drift between fresh discovery and committed artifacts.")
            return 0

        print("DRIFT: fresh discovery changes the generated tool surface:")
        print("\n".join(drift))
        if not args.update:
            print("Re-run with --update to refresh discovery JSONs and artifacts.")
            return 1

        for filename, tmp_path in fetched.items():
            (DISCOVERY_DIR / filename).write_bytes(tmp_path.read_bytes())
        written = write_files(generate(), DEFAULT_OUT_DIR)
        print(f"UPDATED: discovery JSONs refreshed; regenerated {written}.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
