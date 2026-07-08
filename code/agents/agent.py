"""An Agent = a config block + its skill file + a provider-agnostic model."""
import json
import os
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

SKILLS_DIR = Path(__file__).parent / "skills"


def build_model(cfg: dict):
    """Provider-agnostic factory. Swap providers by editing config only."""
    common = dict(
        model=cfg["model"],
        max_tokens=cfg.get("max_tokens", 400),
    )
    # The newest Anthropic models (Opus 4.8, Sonnet 5) reject sampling
    # params outright — send temperature only when the config asks for it.
    if "temperature" in cfg:
        common["temperature"] = cfg["temperature"]
    provider = cfg["provider"]
    if provider == "anthropic":
        return ChatAnthropic(**common)
    if provider == "openai":
        return ChatOpenAI(**common)
    if provider == "openrouter":
        # One key, every vendor: OpenRouter speaks the OpenAI wire protocol,
        # so model slugs like "google/gemini-..." work through ChatOpenAI.
        return ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
            **common,
        )
    raise ValueError(f"Unknown provider: {provider!r}")


def load_skill(skill_name: str) -> str:
    """Read a SKILL.md off disk on demand (conditional context)."""
    path = SKILLS_DIR / skill_name / "SKILL.md"
    if not path.exists():
        raise FileNotFoundError(f"Skill file not found: {path}")
    return path.read_text(encoding="utf-8")


class Agent:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        self.output = cfg.get("output", "text")
        self.model = build_model(cfg)
        # Optional "fallback" config block: a second model tried when the
        # primary fails (provider outage, truncated/unparseable JSON, ...).
        fallback_cfg = cfg.get("fallback")
        self.fallback_model = build_model(fallback_cfg) if fallback_cfg else None
        self.system_prompt = load_skill(cfg["skill"])

    def run(self, user_input: str, context: dict | None = None):
        human = user_input
        if context:
            human += "\n\nContext:\n" + json.dumps(context, ensure_ascii=False)

        messages = [("system", self.system_prompt), ("human", human)]
        try:
            return self._invoke(self.model, messages)
        except Exception as e:
            if self.fallback_model is None:
                raise
            print(f"[{self.name}] primary model failed ({type(e).__name__}: "
                  f"{e}), retrying on fallback")
            return self._invoke(self.fallback_model, messages)

    def _invoke(self, model, messages):
        raw = model.invoke(messages).content.strip()
        return self._parse_json(raw) if self.output == "json" else raw

    @staticmethod
    def _parse_json(raw: str):
        # Models sometimes reason out loud around the JSON despite the
        # skills saying not to; recover the object instead of failing.
        if "```" in raw:
            for block in raw.split("```")[1::2]:  # inside-fence chunks
                block = block.strip().removeprefix("json").strip()
                if block.startswith(("{", "[")):
                    return json.loads(block)
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            return json.loads(raw[start : end + 1])
        return json.loads(raw)  # nothing JSON-like: raise the real error
