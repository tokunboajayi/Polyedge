"""Tests for persistence/backup.py"""

import gzip
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from persistence.backup import (
    BackupManager,
    BackupResult,
    _decompress,
    _elapsed,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_db(tmp_dir: Path) -> Path:
    """Create a small fake SQLite DB file."""
    db = tmp_dir / "polyedge.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    return db


class _PatchedBackupManager(BackupManager):
    """Subclass that redirects paths to a temp directory."""
    def __init__(self, tmp_dir: Path):
        self._db_path    = tmp_dir / "polyedge.db"
        self._backup_dir = tmp_dir / "backups"
        self._backup_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# _decompress helper
# ---------------------------------------------------------------------------

class TestDecompress(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip(self):
        original = self.tmp / "orig.bin"
        original.write_bytes(b"hello backup world")
        compressed = self.tmp / "orig.gz"
        with gzip.open(compressed, "wb") as gz:
            gz.write(original.read_bytes())

        dest = self.tmp / "restored.bin"
        _decompress(compressed, dest)
        self.assertEqual(dest.read_bytes(), b"hello backup world")


# ---------------------------------------------------------------------------
# BackupManager._compress
# ---------------------------------------------------------------------------

class TestBackupManagerCompress(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _make_temp_db(self.tmp)
        self.bm = _PatchedBackupManager(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compress_creates_file(self):
        dest = self.tmp / "backups" / "test.db.gz"
        size = self.bm._compress(dest)
        self.assertTrue(dest.exists())
        self.assertGreater(size, 0)

    def test_compress_file_is_valid_gzip(self):
        dest = self.tmp / "backups" / "test.db.gz"
        self.bm._compress(dest)
        with gzip.open(dest, "rb") as gz:
            content = gz.read()
        self.assertGreater(len(content), 0)

    def test_compress_raises_when_db_missing(self):
        bm = _PatchedBackupManager(self.tmp)
        bm._db_path = self.tmp / "nonexistent.db"
        with self.assertRaises(FileNotFoundError):
            bm._compress(self.tmp / "out.db.gz")


# ---------------------------------------------------------------------------
# BackupManager._prune_local
# ---------------------------------------------------------------------------

class TestPruneLocal(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _make_temp_db(self.tmp)
        self.bm = _PatchedBackupManager(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backups(self, n: int) -> list[Path]:
        files = []
        for i in range(n):
            p = self.bm._backup_dir / f"polyedge_backup_202601{i:02d}_000000.db.gz"
            p.write_bytes(b"fake")
            files.append(p)
        return files

    def test_keeps_7_newest(self):
        self._make_backups(10)
        self.bm._prune_local()
        remaining = self.bm._list_local_backups()
        self.assertEqual(len(remaining), 7)

    def test_keeps_all_when_fewer_than_7(self):
        self._make_backups(4)
        self.bm._prune_local()
        remaining = self.bm._list_local_backups()
        self.assertEqual(len(remaining), 4)

    def test_newest_retained(self):
        files = self._make_backups(10)
        self.bm._prune_local()
        remaining = self.bm._list_local_backups()
        # Should keep the last 7 (highest timestamp names)
        for kept in remaining:
            self.assertIn(kept, files[-7:])


# ---------------------------------------------------------------------------
# BackupManager.run_backup (B2 mocked out)
# ---------------------------------------------------------------------------

class TestRunBackup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _make_temp_db(self.tmp)
        self.bm = _PatchedBackupManager(self.tmp)
        # Stub B2 so no real network call
        self.bm._upload_b2 = MagicMock(return_value=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_returns_backup_result(self):
        result = await self.bm.run_backup()
        self.assertIsInstance(result, BackupResult)

    async def test_success_true(self):
        result = await self.bm.run_backup()
        self.assertTrue(result.success)

    async def test_filename_format(self):
        result = await self.bm.run_backup()
        self.assertTrue(result.filename.startswith("polyedge_backup_"))
        self.assertTrue(result.filename.endswith(".db.gz"))

    async def test_local_file_exists(self):
        result = await self.bm.run_backup()
        self.assertTrue(Path(result.local_path).exists())

    async def test_size_bytes_positive(self):
        result = await self.bm.run_backup()
        self.assertGreater(result.size_bytes, 0)

    async def test_uploaded_true_when_b2_ok(self):
        result = await self.bm.run_backup()
        self.assertTrue(result.uploaded)

    async def test_uploaded_false_when_b2_fails(self):
        self.bm._upload_b2 = MagicMock(side_effect=Exception("network error"))
        result = await self.bm.run_backup()
        self.assertTrue(result.success)   # local backup still succeeded
        self.assertFalse(result.uploaded)
        self.assertIn("network error", result.error)

    async def test_error_empty_on_success(self):
        self.bm._upload_b2 = MagicMock(return_value=True)
        result = await self.bm.run_backup()
        self.assertEqual(result.error, "")

    async def test_failure_when_db_missing(self):
        self.bm._db_path = self.tmp / "missing.db"
        result = await self.bm.run_backup()
        self.assertFalse(result.success)
        self.assertIn("compress_failed", result.error)

    async def test_multiple_backups_prunes_old(self):
        # Create 8 existing backups
        for i in range(8):
            p = self.bm._backup_dir / f"polyedge_backup_202601{i:02d}_000000.db.gz"
            p.write_bytes(b"old")
        await self.bm.run_backup()
        remaining = self.bm._list_local_backups()
        self.assertLessEqual(len(remaining), 7)


# ---------------------------------------------------------------------------
# BackupManager.restore_latest (local fallback)
# ---------------------------------------------------------------------------

class TestRestoreLatest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _make_temp_db(self.tmp)
        self.bm = _PatchedBackupManager(self.tmp)
        # Stub B2 download to return None → use local fallback
        self.bm._download_latest_b2 = MagicMock(return_value=None)
        self.bm._upload_b2 = MagicMock(return_value=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_restore_from_local_backup(self):
        # First create a local backup
        await self.bm.run_backup()
        dest = str(self.tmp / "restored.db")
        path = await self.bm.restore_latest(dest_path=dest)
        self.assertTrue(Path(path).exists())

    async def test_restored_file_is_valid(self):
        await self.bm.run_backup()
        dest = str(self.tmp / "restored2.db")
        path = await self.bm.restore_latest(dest_path=dest)
        content = Path(path).read_bytes()
        # Should start with SQLite magic bytes
        self.assertTrue(content.startswith(b"SQLite format 3"))

    async def test_raises_when_no_backup(self):
        with self.assertRaises(RuntimeError):
            await self.bm.restore_latest(dest_path=str(self.tmp / "out.db"))


if __name__ == "__main__":
    unittest.main()
