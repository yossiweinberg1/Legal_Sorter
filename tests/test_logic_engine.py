from src.logic_engine import WordSenseEngine


def test_analyzes_every_word_and_preserves_positions():
    document = "The plaintiff filed $12.50 on 3/4/2024."
    results = WordSenseEngine().analyze(document)
    assert any(result.text == "plaintiff" and result.choice_source == "legal_dictionary" for result in results)
    assert any(result.text == "$12.50" and result.choice_source == "rules" for result in results)
    assert any(result.text == "3/4/2024" and result.choice_source == "rules" for result in results)
    words = {result.text.casefold() for result in results if result.kind == "word"}
    assert words >= {"the", "plaintiff", "filed", "on"}
    for result in results:
        assert document[result.start:result.end] == result.text


def test_groups_do_not_remove_component_words():
    results = WordSenseEngine().analyze("Mr. Smith visited 10 Main Street.")
    assert any(result.kind == "group" and result.text == "Mr. Smith" for result in results)
    assert any(result.kind == "word" and result.text == "Smith" for result in results)
