"""Local file originals under data/files."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path


class LocalFileStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, data: bytes, *, suffix: str = "") -> tuple[str, str, Path]:
        file_id = uuid.uuid4().hex
        digest = hashlib.sha256(data).hexdigest()
        name = f"{file_id}{suffix}"
        path = self.root / name
        path.write_bytes(data)
        return file_id, digest, path

    def put_bytes_deterministic(
        self,
        key: str,
        data: bytes,
        *,
        suffix: str = ".json",
        overwrite: bool = False,
    ) -> tuple[str, str, Path]:
        """Write at a path derived from key.

        Default is write-once. Pass ``overwrite=True`` only when the DB row
        owner is attaching/repairing the original that must match the row.
        """
        file_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / f"{file_id}{suffix}"
        if overwrite or not path.exists():
            path.write_bytes(data)
        return file_id, digest, path

    def exists(self) -> bool:
        return self.root.is_dir()
