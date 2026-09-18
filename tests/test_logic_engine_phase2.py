from src.logic_engine import FrameEngine


def test_phase2_uses_fixed_slots_and_phase1_results():
    result = FrameEngine().analyze("doc-1", "The plaintiff filed a case on 3/4/2024.")
    assert result.sense_results
    assert result.frames
    assert set(result.frames[0].best_values) == {"who", "what", "where", "when", "how", "why", "outcome"}
    assert any(h.slot == "who" and h.value == "plaintiff" for h in result.hypotheses)


def test_pattern_triggers_and_rejected_answers_are_preserved():
    result = FrameEngine().analyze("doc-2", "Mr. Smith filed a case. The court affirmed it.")
    assert any(frame.trigger_text == "Mr. Smith" for frame in result.frames)
    assert any(h.status == "≠" for h in result.hypotheses)
    assert len(result.hypotheses) > 1


def test_depth_is_limited_to_two():
    result = FrameEngine().analyze("doc-3", "The plaintiff filed and the defendant appealed and the court affirmed.")
    by_id = {frame.frame_id: frame for frame in result.frames}
    for frame in result.frames:
        depth = 1
        parent = frame.parent_frame_id
        while parent:
            depth += 1
            parent = by_id[parent].parent_frame_id
        assert depth <= 2
