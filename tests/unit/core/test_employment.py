from typing import Any

import pytest

from jobpulse.core.employment import employment_type, posting_employment_type
from tests.factories import make_posting


def gh_metadata(*pairs: tuple[str, Any]) -> dict[str, Any]:
    return {
        "metadata": [
            {"id": i, "name": name, "value": value} for i, (name, value) in enumerate(pairs)
        ]
    }


@pytest.mark.parametrize(
    ("source", "raw", "expected"),
    [
        ("ashby", {"employmentType": "Contract"}, "contract"),
        ("ashby", {"employmentType": "Temporary"}, "contract"),
        ("ashby", {"employmentType": "Intern"}, "internship"),
        ("ashby", {"employmentType": "PartTime"}, "part_time"),
        ("ashby", {"employmentType": "FullTime"}, "full_time"),
        ("lever", {"categories": {"commitment": "Temporary/Contract"}}, "contract"),
        ("lever", {"categories": {"commitment": "Third Party Contract"}}, "contract"),
        ("lever", {"categories": {"commitment": "Contingent Worker"}}, "contract"),
        ("lever", {"categories": {"commitment": "Software Engineering (Full-Time)"}}, "full_time"),
        ("lever", {"categories": {"commitment": "Internship"}}, "internship"),
        ("lever", {"categories": {"commitment": "Part-time Employee"}}, "part_time"),
        ("indeed", {"job_type": "contract"}, "contract"),
        ("indeed", {"job_type": "fulltime, contract"}, "contract"),
        ("indeed", {"job_type": "temporary, fulltime"}, "contract"),
        ("indeed", {"job_type": "parttime, fulltime"}, "full_time"),
        ("indeed", {"job_type": "internship"}, "internship"),
        ("workday", {"timeType": "Part time"}, "part_time"),
        ("workday", {"timeType": "Full time"}, "full_time"),
        ("greenhouse", gh_metadata(("Employment Type", "Full-time")), "full_time"),
        ("greenhouse", gh_metadata(("Employment Type", "Fixed-term contract")), "contract"),
        (
            "greenhouse",
            gh_metadata(("Time Type", "Full time"), ("Regular or Temporary", "Temporary")),
            "contract",
        ),
        ("greenhouse", gh_metadata(("Worker Type", "Contingent Worker")), "contract"),
        ("greenhouse", gh_metadata(("Time Type", "INTR - Intern")), "internship"),
        ("greenhouse", gh_metadata(("Employment Type", ["Contract"])), "contract"),
        # metadata that isn't about employment says nothing
        ("greenhouse", gh_metadata(("Priority", "High"), ("Job Type", "Standard")), "full_time"),
        ("greenhouse", {"metadata": None}, "full_time"),
    ],
)
def test_structured_fields(source: str, raw: dict[str, Any], expected: str) -> None:
    assert employment_type(source, raw, "Backend Engineer", None) == expected


def test_a_structured_field_beats_phrases() -> None:
    assert (
        employment_type(
            "ashby", {"employmentType": "FullTime"}, "Backend Engineer", "Not a contract role."
        )
        == "full_time"
    )


def test_a_structured_field_without_a_type_falls_back_to_phrases() -> None:
    raw = {"categories": {"commitment": "International EOR"}}
    assert employment_type("lever", raw, "Backend Engineer (Contract)", None) == "contract"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Contract", "contract"),
        ("Temporary", "contract"),
        ("Full-time", "full_time"),
        ("Internship", "internship"),
        ("Part-time", "part_time"),
        ("Volunteer", "full_time"),
    ],
)
def test_linkedin_criteria_line(value: str, expected: str) -> None:
    description = (
        "Build services.\nSeniority level: Mid-Senior level\n"
        f"Employment type: {value}\nIndustries: Software Development"
    )
    assert employment_type("linkedin", {}, "Backend Engineer", description) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Backend Engineer (Contract)",
        "Backend Engineer - Contract",
        "Contract Backend Engineer",
        "Backend Engineer, Contractor",
        "Backend Engineer (Contract-to-Hire)",
        "Temporary Backend Engineer",
        "Backend Engineer [Contract]",
    ],
)
def test_contract_titles(title: str) -> None:
    assert employment_type("simplify", {}, title, None) == "contract"


@pytest.mark.parametrize(
    "description",
    [
        "This is a 6-month contract with a possible extension.",
        "12 month contract, hybrid in Austin.",
        "Six-month contract to start.",
        "This is a contract-to-hire position.",
        "C2C candidates welcome.",
        "Corp-to-corp only.",
        "W2 contract, no third parties.",
        "Open to 1099 contractors.",
        "We are hiring on a contract basis.",
        "This is a contract role supporting our platform team.",
        "A temporary position covering a leave.",
        "Independent contractor agreement required.",
    ],
)
def test_contract_phrases(description: str) -> None:
    assert employment_type("simplify", {}, "Backend Engineer", description) == "contract"


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Smart Contract Engineer", "Write Solidity smart contracts."),
        ("Backend Engineer", "Experience with smart contracts is a plus."),
        ("Backend Engineer", "We support government contracts across the DoD."),
        ("Backend Engineer", "Leidos is a defense contractor."),
        ("Backend Engineer", "Build our contract management platform."),
        ("Backend Engineer", "You will manage vendor contracts and temporary housing."),
        ("Backend Engineer", "Our office is at 1099 Main St."),
        ("Backend Engineer", "Requisition 1099719."),
        ("Contracts Platform Engineer", None),
        ("Contract Administrator", None),
        ("Contract Analyst", None),
    ],
)
def test_phrases_that_are_not_contract_work(title: str, description: str | None) -> None:
    assert employment_type("simplify", {}, title, description) == "full_time"


def test_internship_and_part_time_titles() -> None:
    assert employment_type("simplify", {}, "Software Engineer Intern", None) == "internship"
    assert employment_type("simplify", {}, "Software Engineering Internship", None) == "internship"
    assert employment_type("simplify", {}, "Part-Time Backend Developer", None) == "part_time"
    assert employment_type("simplify", {}, "Internal Tools Engineer", None) == "full_time"


def test_no_signal_is_full_time() -> None:
    assert employment_type("greenhouse", {}, "Backend Engineer", None) == "full_time"


def test_from_a_posting() -> None:
    posting = make_posting(
        source="ashby", raw={"employmentType": "Contract"}, description_text="Build things."
    )
    assert posting_employment_type(posting) == "contract"
