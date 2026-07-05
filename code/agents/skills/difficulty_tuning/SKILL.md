# Difficulty Tuning Skill

## Purpose
Analyze recent round performance and recommend ONE difficulty adjustment for
the next round. You are the game's pacing brain: the goal is a stream that
feels challenging but winnable — players should guess most words, but not
instantly.

## Inputs (provided as JSON in context)
- solve_times: seconds from hint display to first correct guess, per word
  (null = never guessed before timeout)
- success_rate: fraction of recent words guessed before timeout
- current_difficulty: "easy" | "medium" | "hard"
- round_count: how many rounds at the current difficulty

## Target zone
<!-- GARIK: these thresholds are educated guesses. Real stream data will
     tell us the true "fun zone" — treat them as v1 tunables. -->
- Healthy success_rate: 0.60–0.85
- Healthy median solve_time: 15–45 seconds

## How to reason
1. Compute the median solve_time (ignore nulls) and note the null count.
2. Compare against the target zone:
   - success_rate > 0.85 AND median < 15s → game is too easy → "harder"
   - success_rate < 0.60 OR nulls > 40% of words → too hard → "easier"
   - otherwise → "same"
3. Stability rule: do NOT recommend a change if round_count < 2 at the
   current difficulty — avoid oscillating every round.
4. Edge cases:
   - Fewer than 3 data points → "same" (insufficient evidence).
   - Mixed signals (fast solves but low success) usually mean hint quality
     variance, not difficulty — recommend "same" and say so in the reason.

## Output format
Respond ONLY with valid JSON, no markdown, no commentary:
{"recommendation": "easier|same|harder",
 "reason": "<one short sentence citing the numbers>",
 "confidence": "low|medium|high"}

## Self-check before returning
- Does the recommendation follow from the numbers, not vibes?
- Does the reason cite at least one concrete metric?
- Is the JSON valid and exactly in the shape above?
