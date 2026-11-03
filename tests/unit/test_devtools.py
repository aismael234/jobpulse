import json
from datetime import date
from pathlib import Path

import pytest

from jobpulse.devtools.fixtures import FixtureScraper


def write_scenario(tmp_path: Path) -> Path:
    (tmp_path / "jobspy").mkdir()
    (tmp_path / "jobspy" / "linkedin.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "id": "li-4101234567",
                        "site": "linkedin",
                        "job_url": "https://www.linkedin.com/jobs/view/4101234567",
                        "title": "Backend Engineer",
                        "company": "Acme",
                        "location": "San Francisco, CA, USA",
                        "date_posted": "2026-10-23",
                        "is_remote": False,
                    }
                ],
                "errors": {},
                "warnings": {"linkedin": ["is_remote isn't supported, ignoring it"]},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "routes.yaml").write_text(
        "routes: []\n"
        "jobspy:\n"
        "  - {site: linkedin, search_term: backend engineer, file: jobspy/linkedin.json}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_answers_a_search_with_its_recorded_rows(tmp_path: Path) -> None:
    scrape = FixtureScraper(write_scenario(tmp_path))
    frame = scrape(site_name=["linkedin"], search_term="backend engineer", hours_old=3, verbose=0)
    [row] = frame.to_dict("records")
    assert row["job_url"] == "https://www.linkedin.com/jobs/view/4101234567"
    assert row["date_posted"] == date(2026, 10, 23)
    assert "job_url_direct" in row  # every JobSpy column is there
    assert frame.attrs == {
        "errors": {},
        "warnings": {"linkedin": ["is_remote isn't supported, ignoring it"]},
    }


def test_an_unrecorded_search_raises(tmp_path: Path) -> None:
    scrape = FixtureScraper(write_scenario(tmp_path))
    with pytest.raises(LookupError, match="no JobSpy fixture for indeed"):
        scrape(site_name=["indeed"], search_term="backend engineer")
