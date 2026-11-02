"""M4: what the LLM is asked, and the hash that names the question for the cache."""

from typing import Any

from jobpulse.llm.prompt import (
    MAX_DESCRIPTION_CHARS,
    PROMPT_VERSION,
    build_request,
    posting_text,
    resume_block,
    system_prompt,
)
from tests.factories import make_posting

RESUME = "Jane Doe. Backend engineer: Python, Go, Postgres. 4 years."


def test_the_posting_as_text() -> None:
    posting = make_posting(
        company_name="Acme",
        title="Backend Engineer",
        locations=("San Francisco, CA", "Remote (US)"),
        workplace_type="hybrid",
        department="Payments",
        description_text="Build Python services.\n3+ years of experience.",
    )
    text = posting_text(posting)
    assert text.startswith("<posting>")
    assert text.endswith("</posting>")
    for expected in (
        "Company: Acme",
        "Title: Backend Engineer",
        "Locations: San Francisco, CA; Remote (US)",
        "Workplace: hybrid",
        "Department: Payments",
        "Build Python services.\n3+ years of experience.",
    ):
        assert expected in text


def test_a_posting_without_a_description_says_so() -> None:
    text = posting_text(make_posting(description_text=None, locations=()))
    assert "(no description)" in text
    assert "Locations: not given" in text


def test_a_long_description_is_cut_with_a_note() -> None:
    text = posting_text(make_posting(description_text="x" * (MAX_DESCRIPTION_CHARS + 500)))
    assert text.count("x") == MAX_DESCRIPTION_CHARS
    assert "cut" in text


def test_the_resume_is_wrapped() -> None:
    block = resume_block(RESUME)
    assert block.startswith("The job seeker's resume:")
    assert f"<resume>\n{RESUME}\n</resume>" in block


def test_the_request() -> None:
    posting = make_posting(description_text="Build Python services.")
    request = build_request(posting, resume=RESUME, model="claude-opus-5-5", effort="low")
    assert request.system == system_prompt("backend software engineer")
    assert request.system.startswith("You help one job seeker, a backend software engineer,")
    assert request.resume == RESUME
    assert request.posting == posting_text(posting)
    assert len(request.input_hash) == 64
    int(request.input_hash, 16)


def test_the_hash_names_exactly_this_question() -> None:
    posting = make_posting(description_text="Build Python services.")

    def hash_of(**changes: Any) -> str:
        question = {"resume": RESUME, "model": "claude-opus-5-5", "effort": "low", **changes}
        return build_request(question.pop("posting", posting), **question).input_hash

    same = hash_of()
    assert hash_of() == same
    assert hash_of(resume=RESUME + " Now 5 years.") != same
    assert hash_of(model="claude-sonnet-5-5") != same
    assert hash_of(effort="high") != same
    assert hash_of(role="data engineer") != same
    assert hash_of(posting=make_posting(description_text="Build Go services.")) != same
    assert PROMPT_VERSION  # bumping it changes every hash, so old answers are asked again
