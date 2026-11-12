"""Answer-bank matching and rendering tests."""

from __future__ import annotations

from jobhunt_engine import answers

BANK = [
    {
        "question": "What is your notice period?",
        "patterns": ["notice period", "when can you start", "joining time"],
        "answer": "{{ notice_period }}",
    },
    {
        "question": "Are you willing to relocate?",
        "patterns": ["willing to relocate", "relocation"],
        "answer": "{{ willing_relocate }}",
    },
]

CANDIDATE = {
    "notice_period": "30 days",
    "willing_relocate": "Yes, within Delhi NCR",
}


def test_similarity_scores_exact_and_contained_questions():
    assert answers.similarity("notice period", "notice period") == 1.0
    assert answers.similarity("What is your notice period?", "notice period") == 0.93
    assert answers.similarity("When can you start?", "when can you start") == 1.0
    assert answers.similarity("What is your notice period?", "relocation") < 0.62


def test_match_entry_finds_fuzzy_question():
    entry, score = answers.match_entry(
        "What's your current notice period?", BANK, threshold=0.62
    )
    assert entry is BANK[0]
    assert score >= 0.62


def test_answer_for_renders_answer_bank_values():
    found = answers.answer_for(
        "What is your notice period?", [], BANK, CANDIDATE, threshold=0.62
    )
    assert found is not None
    answer, source, score = found
    assert answer == "30 days"
    assert source == "answer_bank"
    assert score >= 0.62


def test_prepared_answers_take_precedence():
    prepared = [
        {
            "question": "What is your notice period?",
            "answer": "Immediate",
            "source": "user",
        }
    ]
    found = answers.answer_for("What is your notice period?", prepared, BANK, CANDIDATE)
    assert found is not None
    assert found[0] == "Immediate"
    assert found[1] == "prepared"


def test_placeholder_prepared_answer_falls_back_to_bank():
    prepared = [
        {
            "question": "What is your notice period?",
            "answer": "TODO: fill this in",
            "source": "llm",
        }
    ]
    found = answers.answer_for("What is your notice period?", prepared, BANK, CANDIDATE)
    assert found is not None
    assert found[0] == "30 days"
    assert found[1] == "answer_bank"


def test_is_placeholder():
    assert answers.is_placeholder("")
    assert answers.is_placeholder("TODO")
    assert answers.is_placeholder("[insert notice period]")
    assert not answers.is_placeholder("30 days")


def test_no_match_returns_none():
    assert (
        answers.answer_for("What is your favourite colour?", [], BANK, CANDIDATE)
        is None
    )
