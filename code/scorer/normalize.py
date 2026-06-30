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
    """True if the normalized message equals any normalized accepted answer.

    Exact match on the normalized form. We deliberately avoid fuzzy/substring
    matching here so that a chatty message like "is it paris lol" does NOT score
    'paris'. If you want to accept answers embedded in a sentence, switch the
    line below to a token-membership check — but exact match keeps scoring
    predictable and hard to game. List variants explicitly in the questions
    file instead.
    """
    msg = normalize(message)
    if not msg:
        return False
    return any(msg == normalize(a) for a in accepted)


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
    ]
    ok = 0
    for msg, acc, expect in cases:
        got = matches(msg, acc)
        flag = "ok " if got == expect else "FAIL"
        if got == expect:
            ok += 1
        print(f"[{flag}] matches({msg!r}, {acc}) = {got}  (normalized: {normalize(msg)!r})")
    print(f"\n{ok}/{len(cases)} passed")
