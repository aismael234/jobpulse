"""M8: years of experience, skills and title lines read off a resume without an LLM."""

from datetime import date

import pytest

from jobpulse.core.resume_facts import read_resume, title_lines, years_of_experience

TODAY = date(2026, 11, 1)

RESUME = """Jordan Example
jordan@example.com | 555-0100

Experience
Software Engineer, Acme Corp
Jan 2022 – Present
- Built Python services on AWS with PostgreSQL and Docker.
Backend Developer Intern, Beta Inc
Jun 2021 - Aug 2021
- Wrote REST APIs in FastAPI.

Education
B.S. Computer Science, State University
Sep 2017 – May 2021

Skills
Python, TypeScript, React, Kubernetes
"""


def test_years_merge_ranges_and_leave_education_out() -> None:
    # Jan 2022 through Nov 2026 is 59 months; Jun–Aug 2021 adds 3: 62 months, 5 years.
    assert years_of_experience(RESUME, today=TODAY) == 5


def test_overlapping_ranges_count_once() -> None:
    text = "Engineer, A\n2019 - 2021\nEngineer, B\nMar 2020 – Feb 2021\n"
    assert years_of_experience(text, today=TODAY) == 3


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("Developer 03/2018 – 06/2020", 2),
        ("Engineer, Sept. 2023 to current", 3),
        ("No dates at all", None),
        ("Engineer 2030 - 2031", None),  # in the future
        ("Engineer 2021 - 2019", None),  # backwards
    ],
)
def test_date_formats(text: str, years: int | None) -> None:
    assert years_of_experience(text, today=TODAY) == years


def test_projects_and_certifications_are_not_work() -> None:
    text = "Projects\nSide project 2015 - 2020\nExperience\nEngineer, Acme 2024 - Present\n"
    assert years_of_experience(text, today=TODAY) == 2


def test_title_lines_skip_sentences_and_contact_lines() -> None:
    titles = title_lines(RESUME)
    assert "Software Engineer, Acme Corp" in titles
    assert "Backend Developer Intern, Beta Inc" in titles
    assert not any("example.com" in line for line in titles)
    assert not any(line.endswith(".") for line in titles)


def test_read_resume_suggests_families_and_finds_skills() -> None:
    facts = read_resume(RESUME, today=TODAY)
    assert facts.years == 5
    assert {"software", "backend"} <= set(facts.families)
    assert {"python", "aws", "postgresql", "docker", "fastapi", "typescript", "react"} <= set(
        facts.skills
    )
    assert facts.skills["python"] == 2
