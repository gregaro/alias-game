"""Loads config, spawns every configured agent, runs them by name.

This is the manual-orchestration (Pattern 1) layer: YOU decide which agent
runs when. When you later want automated feedback loops between agents,
this is where a LangGraph graph would slot in.
"""
from pathlib import Path

import yaml

import agent as agent_mod
import db
from agent import Agent

CONFIG_PATH = Path(__file__).parent / "config.yaml"


class Orchestrator:
    def __init__(self, config_path: Path = CONFIG_PATH):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        # Must happen before any model is built — it decides their callbacks.
        agent_mod.set_observability(self.config.get("observability", "none"))

        self.agents: dict[str, Agent] = {
            name: Agent(name, cfg)
            for name, cfg in self.config["agents"].items()
        }
        db.init_db()

    def list_agents(self) -> list[str]:
        return list(self.agents)

    def spawn(self, name: str) -> Agent:
        if name not in self.agents:
            raise KeyError(f"No agent named {name!r} in config")
        return self.agents[name]

    def run(self, name: str, user_input: str, context=None,
            persist: bool = True, trace=None, **tags):
        """Run one agent once. `trace` and `tags` are passed straight through
        to the trace log, so a call can be attributed to the word and round
        it belongs to."""
        agent = self.spawn(name)
        result = agent.run(user_input, context, trace=trace, **tags)
        if persist:
            db.save_state(name, "last_output", result)
        return result

    def settings(self, section: str) -> dict:
        """A non-agent config block (e.g. `refinement`), or {} if absent."""
        return self.config.get(section) or {}
