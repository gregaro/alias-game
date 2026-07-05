from dotenv import load_dotenv

load_dotenv()  # load API keys before building any model

from orchestrator import Orchestrator


def main():
    orch = Orchestrator()
    print("Configured agents:", orch.list_agents())

    word = "կատու"
    result = orch.run("hint_generator", f"WORD: {word}")

    print(f"\nWord: {result['word']}\n")
    for i, hint in enumerate(result["hints"], 1):
        print(f"  Hint {i}: {hint}")
    print()


if __name__ == "__main__":
    main()
