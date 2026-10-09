"""Behavioral tests for durable, content-addressed project objects."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solace_engine.errors import CorruptObject
from solace_engine.object_store import ObjectStore


class ObjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ObjectStore(self.root / "store")

    def object_path(self, digest):
        return self.root / "store" / "objects" / digest[:2] / digest

    def test_repeat_write_reuses_verified_object_and_reads_exact_bytes(self):
        data = b"\x00binary\xff\x00content"
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(self.store.put_bytes(data), digest)
        stored = self.object_path(digest)
        first_inode = stored.stat().st_ino
        self.assertEqual(self.store.read_object(digest), data)
        self.assertEqual(self.store.put_bytes(data), digest)
        self.assertEqual(stored.stat().st_ino, first_inode)
        self.assertTrue(self.store.verify_object(digest))

    def test_malformed_digest_is_rejected(self):
        for invalid in ("../outside", "g" * 64, "A" * 64, "0" * 63, ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.read_object(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.verify_object(invalid)

    def test_missing_object_fails_verification(self):
        digest = hashlib.sha256(b"never stored").hexdigest()
        self.assertFalse(self.store.verify_object(digest))
        with self.assertRaises(CorruptObject):
            self.store.read_object(digest)

    def test_tampering_is_detected_before_read_or_reuse(self):
        digest = self.store.put_bytes(b"original")
        self.object_path(digest).write_bytes(b"tampered")
        self.assertFalse(self.store.verify_object(digest))
        with self.assertRaises(CorruptObject):
            self.store.read_object(digest)
        with self.assertRaises(CorruptObject):
            self.store.put_bytes(b"original")
        self.assertEqual(self.object_path(digest).read_bytes(), b"tampered")

    def test_interrupted_publish_leaves_no_object_or_temporary_file(self):
        data = b"unfinished write"
        digest = hashlib.sha256(data).hexdigest()
        with patch("solace_engine.object_store.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.store.put_bytes(data)
        self.assertFalse(self.object_path(digest).exists())
        self.assertEqual(list((self.root / "store" / "objects").rglob("*.tmp")), [])

    def test_interrupted_file_sync_cleans_temporary_file_before_publish(self):
        data = b"write completed but fsync failed"
        digest = hashlib.sha256(data).hexdigest()
        with patch("solace_engine.object_store.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.put_bytes(data)
        self.assertFalse(self.object_path(digest).exists())
        self.assertEqual(list((self.root / "store" / "objects").rglob("*.tmp")), [])

    def test_file_at_whole_object_boundary_and_empty_file(self):
        for data in (b"", b"a" * 1048576):
            with self.subTest(size=len(data)):
                path = self.root / "source"
                path.write_bytes(data)
                digest, size, chunks = self.store.put_file(path)
                self.assertEqual(digest, hashlib.sha256(data).hexdigest())
                self.assertEqual(size, len(data))
                self.assertEqual(chunks, (digest,))
                self.assertEqual(self.store.read_object(digest), data)

    def test_large_file_records_ordered_chunks_and_whole_file_digest(self):
        data = b"a" * 1048576 + b"b"
        path = self.root / "large"
        path.write_bytes(data)
        digest, size, chunks = self.store.put_file(path)
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(size, len(data))
        self.assertGreater(len(chunks), 1)
        self.assertEqual(b"".join(self.store.read_object(chunk) for chunk in chunks), data)


if __name__ == "__main__":
    unittest.main()
