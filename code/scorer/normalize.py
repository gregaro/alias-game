"""
normalize.py — turn a raw chat message into a canonical form for matching.

The YouTube Data API returns the original text the viewer typed (the
creator-device auto-translation does NOT touch the API data), so the matcher
sees real Armenian — and has to handle it properly:

  - NFC Unicode normalization (combining forms / lookalikes collapse to one form)
  - case folding (Armenian is bicameral: Ա/ա, Բ/բ ...)
  - stripping punctuation, including Armenian marks that sit *inside* a word,
    e.g. the question mark ՞ (U+055E) over a vowel, the full stop ։ (U+0589),
    the emphasis ՛ and exclamation ՜ marks, and the Armenian hyphen ֊ (U+058A)
  - whitespace collapse

For spelling variants and Latin transliteration, the per-question
accepted-answers list is the main tool: list each acceptable form. This module
just makes the comparison fair and consistent on both sides.
"""

import unicodedata

# Typo tolerance: a normalized answer of at least this many characters may be
# matched at up to this edit distance. Tuned against a real rehearsal's chat
# log (2026-07-14, ~70 wrong-scoring messages reviewed by hand): distance 1
# recovered 6 genuine one-character typos with zero false positives and zero
# collisions with any other word's answers across three episodes. Distance 2
# started pulling in genuinely ambiguous guesses (a typo OR a different,
# wrong word), so we don't go there. Below the length floor, one edit is a
# large fraction of the string — "գազ" at distance 1 matches half the
# alphabet's worth of unrelated short guesses, so short answers stay exact.
FUZZY_MIN_LENGTH = 5
FUZZY_MAX_DISTANCE = 1


def edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein distance, capped: returns cap+1 once exceeded rather than
    finishing the full DP table. Answers here are short chat messages, so
    there's no need for anything fancier than the textbook algorithm."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def normalize(text: str) -> str:
    """Return a canonical, comparable form of a chat answer."""
    if not text:
        return ""

    # 1. Canonical Unicode form so visually-identical strings compare equal.
    text = unicodedata.normalize("NFC", text)

    # 2. Case fold (stronger than lower(); covers Armenian and more).
    text = text.casefold()

    # 3. Handle punctuation. Armenian places some marks *inside* a word (over a
    #    vowel) — the question mark ՞, emphasis ՛, exclamation ՜, apostrophe ՚,
    #    and the Armenian hyphen ֊. Those must be DELETED so the word stays whole
    #    ("Յո՞ւպիտեր" -> "յուպիտեր"). All other punctuation/symbols separate
    #    tokens, so they become a space.
    WORD_INTERNAL = {
        "\u0559", "\u055A", "\u055B", "\u055C", "\u055D",
        "\u055E", "\u055F", "\u058A",
    }
    out = []
    for ch in text:
        if ch in WORD_INTERNAL:
            continue                      # delete, no space
        cat = unicodedata.category(ch)
        if cat[0] in ("P", "S", "Z", "C"):
            out.append(" ")              # other punctuation/separators -> space
        else:
            out.append(ch)
    text = "".join(out)

    # 4. Collapse runs of whitespace and trim.
    text = " ".join(text.split())
    return text


def matches(message: str, accepted: list[str]) -> bool:
    """True if the normalized message equals any normalized accepted answer —
    or is a one-character typo of a long enough one (see FUZZY_MIN_LENGTH /
    FUZZY_MAX_DISTANCE above).

    We deliberately avoid SUBSTRING/sentence matching here so that a chatty
    message like "is it paris lol" does NOT score 'paris' — that part is
    still exact. The fuzzy part only forgives a slipped keystroke on the
    SAME word; a different (if related) word — a synonym, a wrong guess —
    sits far enough away in edit distance that it stays unmatched. Those
    still need to be listed explicitly in the questions file, same as ever.
    """
    msg = normalize(message)
    if not msg:
        return False
    for a in accepted:
        na = normalize(a)
        if msg == na:
            return True
        if len(na) >= FUZZY_MIN_LENGTH and edit_distance(msg, na) <= FUZZY_MAX_DISTANCE:
            return True
    return False


if __name__ == "__main__":
    # Self-test: Armenian + English cases. Run `python normalize.py`.
    cases = [
        # (message, accepted_list, should_match)
        ("Յուպիտեր", ["Յուպիտեր"], True),
        ("յուպիտեր", ["Յուպիտեր"], True),              # case
        ("  Յուպիտեր։ ", ["Յուպիտեր"], True),          # trailing full stop ։ + spaces
        ("Յո՞ւպիտեր", ["Յուպիտեր"], True),             # question mark inside word
        ("ՅՈՒՊԻՏԵՐ", ["Յուպիտեր"], True),              # all caps
        ("Jupiter", ["Յուպիտեր", "Jupiter"], True),    # latin transliteration variant
        ("jupiter!!!", ["Jupiter"], True),             # punctuation
        ("Մարս", ["Յուպիտեր", "Jupiter"], False),      # wrong answer
        ("is it jupiter", ["Jupiter"], False),         # sentence, exact-match guards it
        ("", ["Jupiter"], False),                       # empty
        # Fuzzy: one-character typo on a long (>=5 char) answer, real case
        # from a 2026-07-14 rehearsal — recovered live, no false positive.
        ("Վարդավար", ["Վարդավառ"], True),
        ("Փորիզ", ["Փարիզ"], True),
        # Fuzzy must NOT bridge to a different word, even a related one —
        # only a typo of the SAME string. Distance from "Յուպիտեր" is > 1.
        ("Մարրս", ["Յուպիտեր"], False),
        # Short answers stay exact even at distance 1 — a 3-letter word one
        # edit away covers too much of the alphabet to mean anything.
        ("գաս", ["գազ"], False),
    ]
    ok = 0
    for msg, acc, expect in cases:
        got = matches(msg, acc)
        flag = "ok " if got == expect else "FAIL"
        if got == expect:
            ok += 1
        print(f"[{flag}] matches({msg!r}, {acc}) = {got}  (normalized: {normalize(msg)!r})")
    print(f"\n{ok}/{len(cases)} passed")
