"""GitHub helpers: shallow clone via git, repo enumeration via `gh` CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def shallow_clone(slug_or_url: str, *, depth: int = 1, full: bool = False) -> Iterator[Path]:
    """Clone `<owner>/<repo>` or a full URL into a temp dir; yield path."""
    if "://" not in slug_or_url and "@" not in slug_or_url:
        url = f"https://github.com/{slug_or_url}.git"
    else:
        url = slug_or_url

    tmp = tempfile.mkdtemp(prefix="pleno-scan-")
    try:
        cmd = ["git", "clone", "--quiet"]
        if not full:
            cmd += [f"--depth={depth}"]
        cmd += [url, tmp]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        yield Path(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def list_org_repos(org: str, *, include_archived: bool = False) -> list[str]:
    """Return `owner/repo` slugs for an org. Requires `gh` CLI authenticated."""
    cmd = [
        "gh",
        "repo",
        "list",
        org,
        "--limit",
        "1000",
        "--json",
        "nameWithOwner,isArchived",
    ]
    out = subprocess.check_output(cmd, text=True)
    items = json.loads(out)
    return [
        i["nameWithOwner"]
        for i in items
        if include_archived or not i.get("isArchived")
    ]
