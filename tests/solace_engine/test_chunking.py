"""Behavioral tests for the checkpoint engine's content-defined chunker."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from solace_engine.chunking import iter_file_chunks, split_bytes


def sample_data(size: int) -> bytes:
    """Return repeatable, varied bytes without storing a large fixture."""
    blocks = (hashlib.sha256(index.to_bytes(4, "big")).digest()
              for index in range((size + 31) // 32))
    return b"".join(blocks)[:size]


class ChunkingTests(unittest.TestCase):
    def test_empty_input_yields_no_chunks(self):
        self.assertEqual(split_bytes(b""), [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty"
            path.write_bytes(b"")
            self.assertEqual(list(iter_file_chunks(path)), [])

    def test_small_input_is_one_unchanged_chunk(self):
        data = b"short content"
        self.assertEqual(split_bytes(data), [data])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "small"
            path.write_bytes(data)
            self.assertEqual(list(iter_file_chunks(path)), [data])

    def test_input_up_to_one_mib_is_one_whole_chunk(self):
        for size in (1048575, 1048576):
            with self.subTest(size=size):
                data = sample_data(size)
                memory_chunks = split_bytes(data)
                self.assertEqual(len(memory_chunks), 1)
                self.assertEqual(memory_chunks[0], data)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "whole"
                    path.write_bytes(data)
                    file_chunks = list(iter_file_chunks(path))
                    self.assertEqual(len(file_chunks), 1)
                    self.assertEqual(file_chunks[0], data)

    def test_input_immediately_above_one_mib_is_chunked(self):
        data = sample_data(1048577)
        chunks = split_bytes(data)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(b"".join(chunks), data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chunked"
            path.write_bytes(data)
            self.assertEqual(list(iter_file_chunks(path)), chunks)

    def test_chunks_reconstruct_input_and_obey_size_bounds(self):
        data = sample_data(1048577)
        chunks = split_bytes(data, min_size=128, average_size=256, max_size=512)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(b"".join(chunks), data)
        self.assertTrue(all(128 <= len(chunk) <= 512 for chunk in chunks[:-1]))
        self.assertTrue(0 < len(chunks[-1]) <= 512)

    def test_fixed_content_has_repeatable_boundaries_across_entry_points(self):
        data = sample_data(1048577)
        parameters = {"min_size": 256, "average_size": 512, "max_size": 1024}
        expected = split_bytes(data, **parameters)
        self.assertGreater(len(expected), 1)
        self.assertEqual(split_bytes(data, **parameters), expected)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large"
            path.write_bytes(data)
            self.assertEqual(list(iter_file_chunks(path, **parameters)), expected)

    def test_maximum_forces_cuts_in_repetitive_content(self):
        data = b"x" * 1048577
        chunks = split_bytes(data, min_size=64, average_size=128, max_size=256)
        self.assertEqual(b"".join(chunks), data)
        self.assertTrue(all(64 <= len(chunk) <= 256 for chunk in chunks[:-1]))
        self.assertTrue(all(len(chunk) <= 256 for chunk in chunks))

    def test_invalid_size_order_is_rejected(self):
        for sizes in ((0, 128, 256), (128, 64, 256), (64, 512, 256)):
            with self.subTest(sizes=sizes), self.assertRaises(ValueError):
                split_bytes(b"data", *sizes)


if __name__ == "__main__":
    unittest.main()
