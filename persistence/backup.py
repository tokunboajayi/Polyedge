"""
SQLite backup to Backblaze B2 for PolyEdge v5.

Schedule
--------
run_backup() is intended to be called once per day at midnight UTC by the
engine scheduler (cron job or asyncio task).

What it does
------------
1. Create a timestamped gzip-compressed copy of polyedge.db in a local
   staging directory (data_store/backups/).
2. Upload the compressed file to Backblaze B2 via the b2sdk library.
3. Keep the last 7 backups both locally and in the B2 bucket.
   Delete files older than that from both locations.
4. Return a BackupResult with success flag, filename, size, and timing.

Filename format: polyedge_backup_YYYYMMDD_HHMMSS.db.gz

Restore
-------
restore_latest() downloads the most recent backup from B2, decompresses
it to a local path, and returns that path.  The caller is responsible for
replacing the live DB (after shutting down the engine).

Configuration (from .env)
--------------------------
  B2_KEY_ID       — Backblaze account key ID
  B2_APPLICATION_KEY — Backblaze application key
  B2_BUCKET_NAME  — target bucket name

If B2 credentials are absent, backup still runs locally and logs a warning
about the missing upload step.

Dependencies
-----------
  b2sdk  — install with: pip install b2sdk
  The import is deferred so startup does not fail when b2sdk is absent
  (local-only mode).

Usage::

    backup = BackupManager()
    result = await backup.run_backup()
    if result.success:
        print(result.filename, result.size_bytes)

    # Restore (blocking):
    path = await backup.restore_latest(dest_path="data_store/restored.db")
"""

import asyncio
import gzip
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_KEEP_BACKUPS: int = 7
_BACKUP_PREFIX  = "polyedge_backup_"
_BACKUP_SUFFIX  = ".db.gz"


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

import dataclasses

@dataclasses.dataclass(slots=True)
class BackupResult:
    success:       bool
    filename:      str
    local_path:    str        # absolute path to compressed backup
    size_bytes:    int
    uploaded:      bool       # True if B2 upload succeeded
    duration_s:    float
    error:         str        # "" on success


# ---------------------------------------------------------------------------
# BackupManager
# ---------------------------------------------------------------------------

class BackupManager:
    """Daily SQLite backup to local staging and Backblaze B2.

    Usage::

        bm = BackupManager()
        result = await bm.run_backup()
    """

    def __init__(self) -> None:
        from persistence.database import DB_PATH
        self._db_path     = DB_PATH
        self._backup_dir  = DB_PATH.parent / "backups"
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run_backup(self) -> BackupResult:
        """Create a compressed backup and upload to B2.

        Returns BackupResult — always succeeds locally; B2 upload is best-effort.
        """
        t0 = datetime.now(timezone.utc)
        ts = t0.strftime("%Y%m%d_%H%M%S")
        filename = f"{_BACKUP_PREFIX}{ts}{_BACKUP_SUFFIX}"
        dest     = self._backup_dir / filename

        # Run the blocking compress step in a thread pool
        try:
            size = await asyncio.get_event_loop().run_in_executor(
                None, self._compress, dest
            )
        except Exception as exc:
            return BackupResult(
                success=False, filename=filename,
                local_path=str(dest), size_bytes=0,
                uploaded=False,
                duration_s=_elapsed(t0),
                error=f"compress_failed: {exc}",
            )

        logger.info(
            "backup_created  file=%s  size_bytes=%d", filename, size
        )

        # Prune old local backups
        await asyncio.get_event_loop().run_in_executor(
            None, self._prune_local
        )

        # Upload to B2
        uploaded = False
        upload_error = ""
        try:
            uploaded = await asyncio.get_event_loop().run_in_executor(
                None, self._upload_b2, dest, filename
            )
        except Exception as exc:
            upload_error = str(exc)
            logger.warning("backup_upload_failed  file=%s  error=%s", filename, exc)

        elapsed = _elapsed(t0)
        logger.info(
            "backup_complete  file=%s  uploaded=%s  duration=%.1fs",
            filename, uploaded, elapsed,
        )

        return BackupResult(
            success=True,
            filename=filename,
            local_path=str(dest),
            size_bytes=size,
            uploaded=uploaded,
            duration_s=round(elapsed, 2),
            error=upload_error,
        )

    async def restore_latest(self, dest_path: str | None = None) -> str:
        """Download the most recent B2 backup and decompress it.

        Args:
            dest_path: Where to write the restored .db file.
                       Defaults to data_store/restored_<timestamp>.db

        Returns:
            Absolute path to the decompressed database file.

        Raises:
            RuntimeError if no backup is found or download fails.
        """
        ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = Path(dest_path) if dest_path else (
            self._backup_dir.parent / f"restored_{ts}.db"
        )

        # Find most recent backup: try B2 first, fall back to local
        gz_path = await asyncio.get_event_loop().run_in_executor(
            None, self._download_latest_b2, self._backup_dir
        )

        if gz_path is None:
            # Fall back to newest local backup
            local_backups = self._list_local_backups()
            if not local_backups:
                raise RuntimeError("No backups found locally or in B2.")
            gz_path = local_backups[-1]
            logger.info("restore_using_local  file=%s", gz_path.name)

        await asyncio.get_event_loop().run_in_executor(
            None, _decompress, gz_path, out_path
        )

        logger.info(
            "restore_complete  source=%s  dest=%s  size_bytes=%d",
            gz_path.name, out_path, out_path.stat().st_size,
        )
        return str(out_path)

    # ------------------------------------------------------------------
    # Internal — local ops (blocking, run in executor)
    # ------------------------------------------------------------------

    def _compress(self, dest: Path) -> int:
        """Compress the live DB to dest using gzip. Returns compressed size."""
        if not self._db_path.exists():
            raise FileNotFoundError(f"DB not found: {self._db_path}")
        with open(self._db_path, "rb") as src, gzip.open(dest, "wb") as gz:
            shutil.copyfileobj(src, gz)
        return dest.stat().st_size

    def _prune_local(self) -> None:
        """Keep only the newest _KEEP_BACKUPS compressed backups."""
        backups = self._list_local_backups()
        for old in backups[:-_KEEP_BACKUPS]:
            try:
                old.unlink()
                logger.debug("backup_pruned_local  file=%s", old.name)
            except OSError as exc:
                logger.warning("backup_prune_local_failed  file=%s  error=%s",
                               old.name, exc)

    def _list_local_backups(self) -> list[Path]:
        """Return local backups sorted oldest → newest."""
        files = sorted(
            self._backup_dir.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}")
        )
        return files

    # ------------------------------------------------------------------
    # Internal — B2 ops (blocking, run in executor)
    # ------------------------------------------------------------------

    def _b2_api(self):
        """Return an authorised B2 API object, or None if credentials absent."""
        try:
            from b2sdk.v2 import B2Api, InMemoryAccountInfo
        except ImportError:
            logger.warning("b2sdk not installed — skipping B2 upload")
            return None, None

        try:
            from config import settings as S
            key_id  = S.B2_KEY_ID
            app_key = S.B2_APPLICATION_KEY
        except AttributeError:
            logger.warning("B2 credentials not configured — skipping upload")
            return None, None

        if not key_id or not app_key:
            logger.warning("B2 credentials empty — skipping upload")
            return None, None

        info = InMemoryAccountInfo()
        api  = B2Api(info)
        api.authorize_account("production", key_id, app_key)
        return api, S.B2_BUCKET_NAME

    def _upload_b2(self, local_path: Path, remote_name: str) -> bool:
        """Upload local_path to B2 bucket. Returns True on success."""
        api, bucket_name = self._b2_api()
        if api is None:
            return False

        bucket = api.get_bucket_by_name(bucket_name)
        bucket.upload_local_file(
            local_file=str(local_path),
            file_name=remote_name,
        )
        logger.info("backup_uploaded_b2  file=%s  bucket=%s", remote_name, bucket_name)

        # Prune old B2 backups (keep last _KEEP_BACKUPS)
        self._prune_b2(api, bucket_name)
        return True

    def _prune_b2(self, api: Any, bucket_name: str) -> None:
        """Delete old B2 backup files beyond _KEEP_BACKUPS."""
        try:
            bucket = api.get_bucket_by_name(bucket_name)
            files  = list(bucket.ls(
                folder_to_list="",
                recursive=False,
                fetch_count=100,
            ))
            # Filter to our backup files, sort by name (timestamp is sortable)
            backups = sorted(
                [f for f, _ in files
                 if f.file_name.startswith(_BACKUP_PREFIX)
                 and f.file_name.endswith(_BACKUP_SUFFIX)],
                key=lambda f: f.file_name,
            )
            for old in backups[:-_KEEP_BACKUPS]:
                bucket.get_file_info(old.id_)
                api.delete_file_version(old.id_, old.file_name)
                logger.debug("backup_pruned_b2  file=%s", old.file_name)
        except Exception as exc:
            logger.warning("backup_b2_prune_failed  error=%s", exc)

    def _download_latest_b2(self, dest_dir: Path) -> Path | None:
        """Download the most recent B2 backup to dest_dir. Returns Path or None."""
        api, bucket_name = self._b2_api()
        if api is None:
            return None

        try:
            bucket  = api.get_bucket_by_name(bucket_name)
            files   = list(bucket.ls(
                folder_to_list="", recursive=False, fetch_count=100
            ))
            backups = sorted(
                [f for f, _ in files
                 if f.file_name.startswith(_BACKUP_PREFIX)
                 and f.file_name.endswith(_BACKUP_SUFFIX)],
                key=lambda f: f.file_name,
            )
            if not backups:
                return None

            latest   = backups[-1]
            dest_gz  = dest_dir / latest.file_name
            download = bucket.download_file_by_name(latest.file_name)
            download.save_to(str(dest_gz))
            logger.info(
                "backup_downloaded_b2  file=%s  dest=%s",
                latest.file_name, dest_gz,
            )
            return dest_gz
        except Exception as exc:
            logger.warning("backup_b2_download_failed  error=%s", exc)
            return None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _elapsed(t0: datetime) -> float:
    return (datetime.now(timezone.utc) - t0).total_seconds()


def _decompress(src: Path, dest: Path) -> None:
    with gzip.open(src, "rb") as gz, open(dest, "wb") as out:
        shutil.copyfileobj(gz, out)
