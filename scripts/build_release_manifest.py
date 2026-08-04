"""Build or verify the deterministic ChronosHFT release manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from governance.release_manifest import (  # noqa: E402
    ReleaseManifestError,
    verify_release_manifest,
    write_release_manifest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="write release-manifest.json")
    build.add_argument(
        "--output",
        type=Path,
        default=Path("release-manifest.json"),
    )

    verify = subparsers.add_parser("verify", help="verify manifest and files")
    verify.add_argument(
        "--manifest",
        type=Path,
        default=Path("release-manifest.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.project_root.resolve()
    try:
        if args.command == "build":
            output = args.output
            if not output.is_absolute():
                output = root / output
            manifest = write_release_manifest(root, output)
        else:
            manifest_path = args.manifest
            if not manifest_path.is_absolute():
                manifest_path = root / manifest_path
            manifest = verify_release_manifest(root, manifest_path)
    except ReleaseManifestError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "release_digest": manifest["release_digest"],
                "file_count": len(manifest["files"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
