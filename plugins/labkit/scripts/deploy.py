#!/usr/bin/env python3
"""Deploy this working copy into the plugin Claude Code actually runs.

Editing the source directory changes nothing at runtime: Claude Code executes a
versioned snapshot under ~/.claude/plugins/cache/local-skills/labkit/<version>/, and
`claude plugin update` reports "already at the latest version" unless the version
string moves. That combination silently produces a fixed-source-but-broken-behaviour
state, which is why this script exists instead of a note in the README.

    deploy.py [--dry-run] [--version X.Y.Z] [--part major|minor|patch] [--skip-test]

Steps: run the self-test, copy the tree to the local marketplace, bump the version,
refresh the marketplace, update the installed plugin. Restart Claude Code afterwards.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from lab_fs import trash

ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE = Path.home() / ".claude" / "local-marketplace" / "labkit"
CACHE = Path.home() / ".claude" / "plugins" / "cache" / "local-skills" / "labkit"
SKIP = {".git", "__pycache__", "reviews", "review.md", "graphify-out", ".labkit-local", ".labkit-run.lock"}

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass


def bump(version, part):
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return "{0}.0.0".format(major + 1)
    if part == "minor":
        return "{0}.{1}.0".format(major, minor + 1)
    return "{0}.{1}.{2}".format(major, minor, patch + 1)


def claude(*args):
    """Every `claude` subcommand needs stdin redirected on this machine or it hangs."""
    proc = subprocess.run(["claude"] + list(args), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    print("   " + (out[-1] if out else "(no output)"))
    return proc.returncode


def trash_marketplace():
    """Recycle only the named marketplace tree, without following a junction."""
    path = MARKETPLACE.absolute()
    source = ROOT.resolve()
    if source.is_relative_to(path) or path.is_relative_to(source):
        raise ValueError("refusing to recycle a marketplace path overlapping the source")
    trash(path, within=path.parent)


def main():
    ap = argparse.ArgumentParser(prog="deploy.py", description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    ap.add_argument("--version", help="set this exact version instead of bumping")
    ap.add_argument("--part", choices=("major", "minor", "patch"), default="patch")
    ap.add_argument("--skip-test", action="store_true",
                    help="deploy without running the self-test first (not recommended)")
    args = ap.parse_args()

    manifests = [(ROOT / name / "plugin.json") for name in (".claude-plugin", ".codex-plugin")]
    documents = [(manifest, json.loads(manifest.read_text(encoding="utf-8")))
                 for manifest in manifests]
    old_version = documents[0][1]["version"]
    new_version = args.version or bump(old_version, args.part)

    print("source     : {0}".format(ROOT))
    print("version    : {0} -> {1} (Claude and Codex manifests)".format(old_version, new_version))
    print("marketplace: {0}".format(MARKETPLACE))
    print("cache      : {0}".format(CACHE / new_version))
    if args.dry_run:
        print("\ndry run, nothing changed.")
        return 0

    if not args.skip_test:
        print("\n1. self-test")
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "lab_selftest.py")],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        tail = (proc.stdout or "").strip().splitlines()[-1:] or ["(no output)"]
        print("   " + tail[0])
        if proc.returncode != 0:
            print("\nself-test failed; nothing was deployed. Run it directly to see which checks.")
            return 1

    print("\n2. write version")
    for manifest, data in documents:
        data["version"] = new_version
        manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("3. copy to the local marketplace")
    if MARKETPLACE.exists():
        trash_marketplace()
    shutil.copytree(ROOT, MARKETPLACE,
                    ignore=shutil.ignore_patterns(*SKIP))

    print("4. refresh the marketplace")
    rc = claude("plugin", "marketplace", "update", "local-skills")
    if rc:
        print("\nmarketplace refresh failed; installed plugin update was not attempted.")
        return rc
    print("5. update the installed plugin")
    rc = claude("plugin", "update", "labkit@local-skills")
    if rc:
        print("\ninstalled plugin update failed; deployment is not confirmed.")
        return rc

    deployed = CACHE / new_version
    print("\ndeployed: {0}  exists={1}".format(deployed, deployed.exists()))
    print("restart Claude Code for the new version to load.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
