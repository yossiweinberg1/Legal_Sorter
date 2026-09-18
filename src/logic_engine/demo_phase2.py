"""Phase 2 structured-frame demo."""

from .phase2 import FrameEngine

SAMPLE = (
    "On March 4, 2024, plaintiff Jane Smith filed a case against defendant John Doe. "
    "The Court of Appeals affirmed the holding because the record supported the judgment. "
    "It ordered John Doe to pay $12,500."
)


def main() -> None:
    result = FrameEngine().analyze("demo-document", SAMPLE)
    print("FRAMES")
    for frame in result.frames:
        print(frame.to_dict())
    print("\nHYPOTHESES")
    for hypothesis in result.hypotheses:
        print(f"{hypothesis.status} {hypothesis.slot:<8} {hypothesis.value!r} "
              f"[{hypothesis.source}, confidence={hypothesis.confidence:.2f}, rank={hypothesis.rank}]")


if __name__ == "__main__":
    main()
