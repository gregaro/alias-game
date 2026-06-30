import json
import sys

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

load_dotenv()

HINT_SYSTEM_PROMPT = """You are the hint-generation agent for a live word-guessing game based on Alias.

The target WORD will be given in Armenian, and ALL of your hints MUST be written in Armenian.

Given the target WORD, produce hints that help players guess it WITHOUT ever using:
- the word itself
- any part of the word, a plural, or an obvious word-family member
- a direct translation of the word into any language

Rules:
- Produce exactly 3 hints, ordered from hardest (vaguest) to easiest (most specific).
- Hint 1 should be oblique. Hint 3 should make most players confident.
- Keep each hint to one short sentence.
- All hints must be in natural, fluent Armenian.
- Never reveal the answer.

Respond ONLY with valid JSON, no markdown and no commentary, in exactly this shape:
{"word": "<the word>", "hints": ["<hint1>", "<hint2>", "<hint3>"]}"""


def generate_hints(word: str) -> dict:
    model = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.8, max_tokens=400)
    response = model.invoke([
        ("system", HINT_SYSTEM_PROMPT),
        ("human", f"WORD: {word}"),
    ])
    raw = response.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


if __name__ == "__main__":
    word = sys.argv[1] if len(sys.argv) > 1 else "կատու"
    try:
        result = generate_hints(word)
    except json.JSONDecodeError:
        print("Model did not return valid JSON. Try again or lower temperature.")
        sys.exit(1)
    print(f"\nWord: {result['word']}\n")
    for i, hint in enumerate(result["hints"], 1):
        print(f"  Hint {i}: {hint}")
    print()
