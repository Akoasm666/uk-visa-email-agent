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

    @staticmethod
    def file_id_for(digest: str, *, suffix: str) -> str:
        """Path-unique id: content digest + extension (not digest alone)."""
        return f"{digest}{suffix}"

    def content_path(self, digest: str, *, suffix: str = "") -> Path:
        return self.root / self.file_id_for(digest, suffix=suffix)

    def path_for_file_id(self, file_id: str) -> Path:
        return self.root / file_id

    def put_bytes_content_addressed(
        self, data: bytes, *, suffix: str = ""
    ) -> tuple[str, str, Path, bool]:
        """Store by content hash + suffix so path identity is exact.

        Returns ``(file_id, digest, path, created_new)``.
        ``file_id`` equals the on-disk filename (digest + suffix).
        """
        digest = hashlib.sha256(data).hexdigest()
        file_id = self.file_id_for(digest, suffix=suffix)
        path = self.path_for_file_id(file_id)
        created_new = False
        if not path.exists():
            tmp = self.root / f".tmp-{uuid.uuid4().hex}{suffix}"
            tmp.write_bytes(data)
            tmp.replace(path)
            created_new = True
        return file_id, digest, path, created_new

    def delete_file(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

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
