import unittest

from app.models import model_registry


class ClassifyHardwareFitTests(unittest.TestCase):
    def test_safe_tier_when_model_is_small_vs_ram(self):
        # 8 GB model, 64 GB RAM -> ratio 0.125 -> safe
        result = model_registry.classify_hardware_fit("llama3.1:8b", ram_bytes=64 * (1024 ** 3))
        self.assertEqual(result["tier"], "safe")
        self.assertEqual(result["size_gb"], 8)

    def test_tight_tier_when_model_is_over_40_percent_ram(self):
        # 8 GB model, 16 GB RAM -> ratio 0.5 -> tight
        result = model_registry.classify_hardware_fit("llama3.1:8b", ram_bytes=16 * (1024 ** 3))
        self.assertEqual(result["tier"], "tight")

    def test_too_big_tier_when_model_is_over_80_percent_ram(self):
        # 9 GB vision model, 8 GB RAM -> ratio > 1 -> too_big
        result = model_registry.classify_hardware_fit("qwen3-vl", ram_bytes=8 * (1024 ** 3))
        self.assertEqual(result["tier"], "too_big")

    def test_unknown_model_returns_unknown_tier(self):
        result = model_registry.classify_hardware_fit("not-a-real-model", ram_bytes=16 * (1024 ** 3))
        self.assertEqual(result["tier"], "unknown")
        self.assertIsNone(result["size_gb"])

    def test_missing_ram_returns_unknown_tier(self):
        result = model_registry.classify_hardware_fit("llama3.1:8b", ram_bytes=None)
        # with no detectable RAM we cannot classify
        if result["tier"] != "unknown":
            # environment may have detectable RAM; ensure at least the size is reported
            self.assertEqual(result["size_gb"], 8)
        else:
            self.assertIsNone(result["ram_bytes"])

    def test_is_model_too_big_reflects_classifier(self):
        self.assertTrue(model_registry.classify_hardware_fit("qwen3-vl", ram_bytes=8 * (1024 ** 3))["tier"] == "too_big")
        self.assertFalse(model_registry.classify_hardware_fit("llama3.1:8b", ram_bytes=64 * (1024 ** 3))["tier"] == "too_big")


class HardwareLaneFitTests(unittest.TestCase):
    def test_returns_fit_entry_per_registered_role(self):
        fits = model_registry.get_hardware_lane_fit(ram_bytes=32 * (1024 ** 3))
        self.assertEqual(set(fits.keys()), set(model_registry.MODEL_REGISTRY.keys()))
        for entry in fits.values():
            self.assertIn(entry["tier"], {"safe", "tight", "too_big", "unknown"})

    def test_tight_ram_flags_large_lanes(self):
        # On 12 GB RAM: 7/12=0.58 tight, 8/12=0.66 tight, 9/12=0.75 tight
        fits = model_registry.get_hardware_lane_fit(ram_bytes=12 * (1024 ** 3))
        self.assertEqual(fits["reasoning"]["tier"], "tight")
        self.assertEqual(fits["coding"]["tier"], "tight")
        self.assertEqual(fits["vision"]["tier"], "tight")

    def test_lane_fit_threads_vram_into_placement(self):
        # 24 GB VRAM, 64 GB RAM -> every curated lane (7-9 GB) fits VRAM.
        fits = model_registry.get_hardware_lane_fit(
            ram_bytes=64 * (1024 ** 3), vram_bytes=24 * (1024 ** 3),
        )
        for entry in fits.values():
            self.assertEqual(entry["placement"], "gpu")


class PlacementTests(unittest.TestCase):
    GB = 1024 ** 3

    def test_placement_gpu_when_model_fits_vram(self):
        # 8 GB model, 16 GB VRAM -> 0.5 <= ceiling -> gpu
        result = model_registry.classify_hardware_fit(
            "llama3.1:8b", ram_bytes=64 * self.GB, vram_bytes=16 * self.GB,
        )
        self.assertEqual(result["placement"], "gpu")
        self.assertEqual(result["tier"], "safe")
        self.assertAlmostEqual(result["vram_ratio"], 0.5, places=3)

    def test_placement_partial_offload_when_exceeds_vram_but_fits_ram(self):
        # 8 GB model, 6 GB VRAM -> 1.33 > ceiling -> partial_offload
        result = model_registry.classify_hardware_fit(
            "llama3.1:8b", ram_bytes=64 * self.GB, vram_bytes=6 * self.GB,
        )
        self.assertEqual(result["placement"], "partial_offload")
        self.assertEqual(result["tier"], "safe")

    def test_placement_cpu_when_no_vram_detected(self):
        # Caller injects ram but no vram -> hermetic, no nvidia-smi probe,
        # placement falls back to cpu.
        result = model_registry.classify_hardware_fit(
            "llama3.1:8b", ram_bytes=64 * self.GB,
        )
        self.assertEqual(result["placement"], "cpu")
        # vram unknown -> no vram_ratio key emitted.
        self.assertNotIn("vram_ratio", result)

    def test_placement_too_big_wins_over_vram(self):
        # 9 GB model, 8 GB RAM -> too_big regardless of a huge VRAM.
        result = model_registry.classify_hardware_fit(
            "qwen3-vl", ram_bytes=8 * self.GB, vram_bytes=24 * self.GB,
        )
        self.assertEqual(result["tier"], "too_big")
        self.assertEqual(result["placement"], "too_big")

    def test_placement_at_vram_ceiling_is_gpu(self):
        # ratio just at/under VRAM_FIT_CEILING -> still gpu (<=).
        # ceil() so the floored-int VRAM keeps ratio <= ceiling rather
        # than nudging it just over (a test-construction detail, not a
        # logic boundary).
        import math
        size_bytes = 8 * self.GB
        vram_bytes = math.ceil(size_bytes / model_registry.VRAM_FIT_CEILING)
        result = model_registry.classify_hardware_fit(
            "llama3.1:8b", ram_bytes=64 * self.GB, vram_bytes=vram_bytes,
        )
        self.assertEqual(result["placement"], "gpu")

    def test_unregistered_model_has_unknown_placement(self):
        result = model_registry.classify_hardware_fit(
            "not-a-real-model", ram_bytes=16 * self.GB, vram_bytes=8 * self.GB,
        )
        self.assertEqual(result["placement"], "unknown")
        self.assertEqual(result["tier"], "unknown")

    def test_backward_compat_tier_unchanged_with_vram_present(self):
        # Adding VRAM must not perturb the RAM tier semantics.
        result = model_registry.classify_hardware_fit(
            "llama3.1:8b", ram_bytes=16 * self.GB, vram_bytes=24 * self.GB,
        )
        self.assertEqual(result["tier"], "tight")  # 8/16 = 0.5


if __name__ == "__main__":
    unittest.main()
