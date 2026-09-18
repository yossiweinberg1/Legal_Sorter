"""Run with: python -m src.logic_engine.demo"""

from .sense_engine import WordSenseEngine

SAMPLE = (
    "On March 4, 2024, plaintiff Jane Smith filed a case against the defendant "
    "at 10 Main Street. The court affirmed the holding and awarded $12,500."
)


def main() -> None:
    results = WordSenseEngine().analyze(SAMPLE)
    for item in results:
        meanings = "; ".join(m.definition for m in item.possible_meanings) or "(no dictionary meaning found)"
        chosen = item.chosen_meaning.definition if item.chosen_meaning else "(none)"
        print(f"{item.start:>3}-{item.end:<3} {item.kind:<5} {item.text!r}")
        print(f"      possible: {meanings}")
        print(f"      chosen:   {chosen} [{item.choice_source}, confidence={item.confidence:.2f}]")


if __name__ == "__main__":
    main()
