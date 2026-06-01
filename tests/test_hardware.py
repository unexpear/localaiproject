"""Tests for hardware detection — specifically the structured VRAM
parsing added for hardware-aware model placement.

The live ``get_vram_info`` shells out to ``nvidia-smi``, which is
environment-dependent, so we test the pure parser
``_parse_nvidia_smi_memory`` deterministically and only smoke-check
that ``get_vram_info`` / ``summary`` don't raise.
"""
import unittest

from app import hardware


_MIB = 1024 * 1024


class ParseNvidiaSmiMemoryTests(unittest.TestCase):
    def test_single_gpu_plain_integer(self):
        # --format=csv,noheader,nounits prints bare MiB integers.
        self.assertEqual(
            hardware._parse_nvidia_smi_memory("12288"),
            12288 * _MIB,
        )

    def test_multi_gpu_sums(self):
        self.assertEqual(
            hardware._parse_nvidia_smi_memory("12288\n8192"),
            (12288 + 8192) * _MIB,
        )

    def test_tolerates_mib_suffix(self):
        # If --nounits wasn't honoured the value may carry a unit.
        self.assertEqual(
            hardware._parse_nvidia_smi_memory("12288 MiB"),
            12288 * _MIB,
        )

    def test_tolerates_trailing_whitespace_and_blank_lines(self):
        self.assertEqual(
            hardware._parse_nvidia_smi_memory("  12288  \n\n"),
            12288 * _MIB,
        )

    def test_mixed_valid_and_garbage_keeps_valid(self):
        # One parseable line is enough; garbage lines are skipped.
        self.assertEqual(
            hardware._parse_nvidia_smi_memory("12288\ngarbage"),
            12288 * _MIB,
        )

    def test_empty_returns_none(self):
        self.assertIsNone(hardware._parse_nvidia_smi_memory(""))
        self.assertIsNone(hardware._parse_nvidia_smi_memory(None))

    def test_all_garbage_returns_none(self):
        self.assertIsNone(hardware._parse_nvidia_smi_memory("nope\nstill nope"))

    def test_float_value_is_floored_to_int_mib(self):
        # nvidia occasionally prints a decimal; int(float(...)) handles it.
        self.assertEqual(
            hardware._parse_nvidia_smi_memory("12288.0"),
            12288 * _MIB,
        )


class HardwareSummarySmokeTests(unittest.TestCase):
    def test_get_vram_info_never_raises(self):
        # On a machine without nvidia-smi this returns None; with one,
        # an int. Either way it must not raise.
        result = hardware.get_vram_info()
        self.assertTrue(result is None or isinstance(result, int))

    def test_summary_includes_vram_key(self):
        s = hardware.summary()
        self.assertIn("vram", s)
        # gpu stays a string-or-None (backward compat for renderers).
        self.assertIn("gpu", s)


if __name__ == "__main__":
    unittest.main()
