from dataclasses import dataclass, field
from hashlib import sha1
from typing import Literal

Verification = Literal["passed", "failed", "unverified"]


@dataclass(frozen=True, slots=True)
class Finding:
    entity: str
    file: str
    line: int
    col: int
    score: float
    snippet: str
    matched: str
    pattern_name: str
    verification: Verification = "unverified"
    commit: str | None = None
    author: str | None = None
    date: str | None = None

    def fingerprint(self) -> str:
        """Stable hash used for baseline / .plenoignore."""
        h = sha1()
        h.update(self.entity.encode())
        h.update(b"\0")
        h.update(self.file.encode())
        h.update(b"\0")
        h.update(self.matched.encode())
        return h.hexdigest()[:16]


@dataclass(slots=True)
class ScanStats:
    files_scanned: int = 0
    files_skipped_binary: int = 0
    files_skipped_size: int = 0
    bytes_scanned: int = 0
    duration_ms: int = 0
    findings: list[Finding] = field(default_factory=list)
    commits_scanned: int = 0
