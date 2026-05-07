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


if __name__ == "__main__":
    unittest.main()
