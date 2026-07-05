"""Loads config, spawns every configured agent, runs them by name.

This is the manual-orchestration (Pattern 1) layer: YOU decide which agent
runs when. When you later want automated feedback loops between agents,
this is where a LangGraph graph would slot in.
"""
from pathlib import Path

import yaml

import db
from agent import Agent

CONFIG_PATH = Path(__file__).parent / "config.yaml"


class Orchestrator:
    def __init__(self, config_path: Path = CONFIG_PATH):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

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

    def run(self, name: str, user_input: str, context=None, persist: bool = True):
        agent = self.spawn(name)
        result = agent.run(user_input, context)
        if persist:
            db.save_state(name, "last_output", result)
        return result
