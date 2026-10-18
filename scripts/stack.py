#!/usr/bin/env python3
"""Portable Compose wrapper; installs nothing and preserves volume data on down."""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def compose():
    docker = shutil.which("docker")
    if docker and subprocess.run([docker, "compose", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return [docker, "compose"]
    standalone = shutil.which("docker-compose")
    if standalone:
        return [standalone]
    raise SystemExit("Docker and Compose are required. Install Docker Desktop or Docker + Colima, start the runtime, then retry.")


if __name__ == "__main__":
    args = sys.argv[1:]
    profile = []
    for flag, name in (("--replay", "replay"), ("--ai", "ai")):
        if flag in args:
            args.remove(flag)
            profile.extend(["--profile", name])
    if not args:
        args = ["up", "--build", "-d"]
    raise SystemExit(subprocess.call(compose() + ["-f", str(ROOT / "compose.yaml")] + profile + args, cwd=ROOT))
