"""Safety tests for canonical pre-show chat responses."""

import unittest

from chat_scorer import warmup_safe_response


class WarmupFilterTests(unittest.TestCase):
    def test_location_is_canonicalized(self):
        self.assertEqual(warmup_safe_response("Ես Երևանից եմ"), "📍 Երևան")
        self.assertEqual(warmup_safe_response("Los Angeles-ից"), "📍 Լոս Անջելես")

    def test_readiness_and_team_are_canonicalized(self):
        self.assertEqual(warmup_safe_response("պատրաստ"), "Պատրաստ ենք 🔥")
        self.assertEqual(warmup_safe_response("կանաչ"), "🟢 Կանաչ թիմ")
        self.assertEqual(warmup_safe_response("yellow"), "🟡 Ոսկեգույն թիմ")

    def test_arbitrary_text_never_passes_through(self):
        unsafe = "<script>offensive arbitrary message</script>"
        self.assertIsNone(warmup_safe_response(unsafe))

    def test_only_reviewed_fragment_survives_mixed_message(self):
        original = "arbitrary unsafe words Երևան more unsafe words"
        result = warmup_safe_response(original)
        self.assertEqual(result, "📍 Երևան")
        self.assertNotIn("unsafe", result)

    def test_emoji_is_rebuilt_from_allowlist(self):
        self.assertEqual(warmup_safe_response("anything 🔥 anything"), "🔥")
        self.assertIsNone(warmup_safe_response("anything 😈 anything"))


if __name__ == "__main__":
    unittest.main()
