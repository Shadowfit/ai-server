import unittest

from app.core.dtw_calculator import (
    classify_sync_haptic_cue,
    classify_sync_visual_cue,
)


class SyncFeedbackTests(unittest.TestCase):
    def test_high_sync_is_green_and_no_haptic(self) -> None:
        visual = classify_sync_visual_cue(82)
        haptic = classify_sync_haptic_cue(82)

        self.assertEqual(visual.zone, "green")
        self.assertFalse(visual.flashing)
        self.assertEqual(haptic.pattern, "off")
        self.assertFalse(haptic.enabled)

    def test_mid_sync_is_orange_with_light_haptic(self) -> None:
        visual = classify_sync_visual_cue(55)
        haptic = classify_sync_haptic_cue(55)

        self.assertEqual(visual.zone, "orange")
        self.assertFalse(visual.flashing)
        self.assertEqual(haptic.pattern, "light_repeat")
        self.assertTrue(haptic.enabled)

    def test_low_sync_is_red_flash_with_warning_haptic(self) -> None:
        visual = classify_sync_visual_cue(32)
        haptic = classify_sync_haptic_cue(32)

        self.assertEqual(visual.zone, "red")
        self.assertTrue(visual.flashing)
        self.assertEqual(visual.animation, "twinkle_flash")
        self.assertEqual(haptic.pattern, "warning_repeat")
        self.assertTrue(haptic.enabled)


if __name__ == "__main__":
    unittest.main()
