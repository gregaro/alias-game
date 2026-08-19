"""Safety tests for canonical pre-show chat responses."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import chat_scorer
from chat_scorer import Scorer, warmup_safe_response


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


class ShowStartTests(unittest.TestCase):
    def test_begin_show_clears_countdown_state_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "state.json")
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({"phase": "starting", "starts_at": 12345,
                           "warmup_messages": [{"safe": True, "text": "🔥"}]}, f)

            scorer = Scorer(None, None, {})
            with patch.object(chat_scorer, "STATE_FILE", state_file):
                scorer.begin_show()
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)

            self.assertEqual(state["phase"], "idle")
            self.assertIsNone(state["question"])
            self.assertIsNone(state["window_ends_at"])
            self.assertNotIn("starts_at", state)
            self.assertNotIn("warmup_messages", state)


if __name__ == "__main__":
    unittest.main()
