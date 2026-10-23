"""Integration setup: a real SQLite database, the real adapters over respx, a notifier that
records what it was asked to send, and a job-board adapter whose results each test scripts."""

import copy
import html
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
import time_machine
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import SIMPLIFY_NEW_GRAD_URL, AppConfig, SearchEntry
from jobpulse.core.dedupe import canonicalize_url
from jobpulse.core.locations import normalize_locations
from jobpulse.core.models import FetchResult, Posting, Target
from jobpulse.core.timeparse import utcnow
from jobpulse.db.session import create_engine_for, make_session_factory
from jobpulse.db.tables import (
    Base,
    Company,
    HealthIncident,
    PostingRow,
    Run,
    Search,
    SourceState,
)
from jobpulse.discovery import Discoverer
from jobpulse.httpclient import HttpClient
from jobpulse.notify.base import Notification, NotifierError
from jobpulse.poller import CycleReport, Poller
from jobpulse.runtime import build_adapters
from jobpulse.sources.ats_detect import AtsDetector
from jobpulse.sources.jobspy_boards import JobSpyAdapter
from tests.factories import NOW, FakeMonotonic, FakeSleep

GH_LIST = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
GH_DETAIL = re.compile(
    r"https://boards-api\.greenhouse\.io/v1/boards/acme/jobs/(\d+)\?pay_transparency=true"
)
LEVER_LIST = "https://api.lever.co/v0/postings/beta?mode=json"
DEFAULT_DESCRIPTION = "Build Python services. 3+ years of experience."

BASE_CONFIG: dict[str, Any] = {
    "contact_email": "dev@example.com",
    "notify": {"channels": []},
    "filters": {
        "title": {
            "include": [
                {"pattern": r"\bbackend\b", "weight": 1.0},
                {"pattern": r"\bplatform\b", "weight": 0.8},
            ],
            "exclude": [r"\bmanager\b"],
        },
        "level": {"exclude": [r"\bstaff\b"]},
        "description": {"max_years_experience": 6, "stack_keywords": {r"\bpython\b": 1.0}},
    },
    "scoring": {"instant_threshold": 0.5},
    "watchlist": [
        {"name": "Acme", "ats": "greenhouse", "token": "acme", "priority": 5},
        {"name": "Beta", "ats": "lever", "token": "beta", "priority": 3},
    ],
}


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def make_config(**overrides: Any) -> AppConfig:
    return AppConfig.model_validate(_merge(BASE_CONFIG, overrides))


def gh_job(
    job_id: int,
    title: str = "Backend Engineer",
    *,
    published: datetime,
    location: str = "San Francisco, CA; Remote - US",
    updated: datetime | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    return {
        "absolute_url": url or f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
        "data_compliance": [],
        "internal_job_id": job_id + 900_000,
        "location": {"name": location},
        "metadata": None,
        "id": job_id,
        "updated_at": (updated or published).isoformat(),
        "requisition_id": f"R-{job_id}",
        "title": title,
        "company_name": "Acme",
        "first_published": published.isoformat(),
        "language": "en",
        "application_deadline": None,
    }


def lever_posting(
    posting_id: str,
    title: str = "Backend Engineer",
    *,
    created: datetime,
    token: str = "beta",
    location: str = "San Francisco, CA",
) -> dict[str, Any]:
    return {
        "id": posting_id,
        "text": title,
        "categories": {"location": location, "allLocations": [location]},
        "createdAt": int(created.timestamp() * 1000),
        "descriptionPlain": DEFAULT_DESCRIPTION,
        "lists": [],
        "additionalPlain": "",
        "workplaceType": "hybrid",
        "hostedUrl": f"https://jobs.lever.co/{token}/{posting_id}",
        "applyUrl": f"https://jobs.lever.co/{token}/{posting_id}/apply",
    }


def simplify_listing(
    listing_id: str,
    title: str = "Backend Engineer, New Grad",
    *,
    company: str = "Delta",
    url: str | None = None,
    posted: datetime,
    active: bool = True,
    visible: bool = True,
    locations: Sequence[str] = ("San Francisco, CA",),
    category: str = "Software",
) -> dict[str, Any]:
    """One record of the SimplifyJobs listings.json feed."""
    return {
        "source": "Simplify",
        "category": category,
        "company_name": company,
        "id": listing_id,
        "title": title,
        "active": active,
        "date_updated": int(posted.timestamp()),
        "date_posted": int(posted.timestamp()),
        "url": url or f"https://jobs.example.com/{company.lower()}/{listing_id}",
        "locations": list(locations),
        "company_url": f"https://simplify.jobs/c/{company}",
        "is_visible": visible,
        "sponsorship": "Other",
        "degrees": ["Bachelor's"],
    }


def board_posting(
    external_id: str,
    *,
    source: str = "linkedin",
    title: str = "Backend Engineer",
    company: str = "Acme",
    location: str = "San Francisco, CA",
    direct_url: str | None = None,
) -> Posting:
    """A job-board posting the way the JobSpy adapter will build one."""
    url = f"https://www.linkedin.com/jobs/view/{external_id}"
    locations, is_remote = normalize_locations([location])
    return Posting(
        source=source,  # type: ignore[arg-type]
        external_id=external_id,
        company_name=company,
        company_token=None,
        title=title,
        locations=locations,
        is_remote=is_remote,
        workplace_type=None,
        url=url,
        apply_url=direct_url,
        canonical_url=canonicalize_url(direct_url or url),
        posted_at=None,
        posted_at_precision="none",
    )


class RecordingNotifier:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Notification] = []
        self.failures_left = 0

    async def send(self, message: Notification) -> None:
        if self.failures_left:
            self.failures_left -= 1
            raise NotifierError("channel down")
        self.sent.append(message)


class FakeBoardAdapter:
    """A job-board source: each fetch returns the next scripted result (never complete)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.results: list[list[Posting] | FetchResult] = []

    async def fetch(self, target: Target) -> FetchResult:
        result = self.results.pop(0)
        if isinstance(result, FetchResult):
            return result
        return FetchResult.success(result, fetched_at=utcnow(), complete_listing=False)

    async def enrich(self, posting: Posting) -> Posting:
        return posting


@dataclass
class GreenhouseMock:
    """The Acme Greenhouse board: set the list per poll; details come from ``descriptions``."""

    route: respx.Route
    descriptions: dict[int, str] = field(default_factory=dict)

    def respond(self, *jobs: dict[str, Any]) -> None:
        self.route.side_effect = None
        self.route.return_value = httpx.Response(
            200, json={"jobs": list(jobs), "meta": {"total": len(jobs)}}
        )

    def fail_with(self, outcome: httpx.Response | Exception) -> None:
        if isinstance(outcome, Exception):
            self.route.side_effect = outcome
        else:
            self.route.side_effect = None
            self.route.return_value = outcome


@dataclass
class SimplifyMock:
    """The SimplifyJobs feed: set the listings (and ETag) per poll, or a 304, or a failure."""

    route: respx.Route

    def respond(self, *listings: dict[str, Any], etag: str | None = None) -> None:
        self.route.side_effect = None
        self.route.return_value = httpx.Response(
            200, json=list(listings), headers={"ETag": etag} if etag else {}
        )

    def not_modified(self) -> None:
        self.route.side_effect = None
        self.route.return_value = httpx.Response(304)

    def fail_with(self, outcome: httpx.Response | Exception) -> None:
        if isinstance(outcome, Exception):
            self.route.side_effect = outcome
        else:
            self.route.side_effect = None
            self.route.return_value = outcome

    def sent_etag(self, call: int = -1) -> str | None:
        return self.route.calls[call].request.headers.get("If-None-Match")


class ScriptedScrape:
    """``scrape_jobs`` stand-in for the real JobSpy adapter: each site answers from its own
    queue of frames (or exceptions), and every call's arguments are kept."""

    def __init__(self) -> None:
        self.frames: dict[str, list[Any]] = {"linkedin": [], "indeed": []}
        self.calls: list[dict[str, Any]] = []

    def queue(self, site: str, *outcomes: Any) -> None:
        self.frames[site].extend(outcomes)

    def __call__(self, **arguments: Any) -> Any:
        self.calls.append(arguments)
        outcome = self.frames[arguments["site_name"][0]].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def sites(self) -> list[str]:
        return [call["site_name"][0] for call in self.calls]


@dataclass
class Env:
    sessions: sessionmaker[Session]
    http: HttpClient
    config: AppConfig
    notifier: Any
    adapters: dict[str, Any]
    discoverer: Discoverer | None = None

    def poller(self) -> Poller:
        return Poller(
            config=self.config,
            sessions=self.sessions,
            adapters=self.adapters,
            notifier=self.notifier,
            discoverer=self.discoverer,
        )

    async def poll(self, **kwargs: Any) -> CycleReport:
        return await self.poller().run_cycle(**kwargs)

    @property
    def board(self) -> FakeBoardAdapter:
        board = self.adapters["linkedin"]
        assert isinstance(board, FakeBoardAdapter)
        return board

    def postings(self) -> list[PostingRow]:
        with self.sessions() as session:
            return list(session.scalars(select(PostingRow).order_by(PostingRow.id)))

    def posting(self, source: str, external_id: str) -> PostingRow:
        with self.sessions() as session:
            return session.scalars(
                select(PostingRow).where(
                    PostingRow.source == source, PostingRow.external_id == external_id
                )
            ).one()

    def company(self, token: str) -> Company:
        with self.sessions() as session:
            return session.scalars(select(Company).where(Company.token == token)).one()

    def runs(self) -> list[Run]:
        with self.sessions() as session:
            return list(session.scalars(select(Run).order_by(Run.id)))

    def add_search(self, name: str = "backend-sf", site: str = "linkedin", **fields: Any) -> None:
        """Configure a search; the next poll syncs it into the database."""
        entry = SearchEntry.model_validate(
            {"name": name, "site": site, "search_term": "backend engineer", **fields}
        )
        self.reconfigure(searches=[*self.config.searches, entry])

    def reconfigure(self, **sections: Any) -> None:
        self.config = self.config.model_copy(update=sections)

    def search(self, name: str = "backend-sf") -> Search:
        with self.sessions() as session:
            return session.scalars(select(Search).where(Search.name == name)).one()

    def feed_state(self) -> SourceState:
        with self.sessions() as session:
            return session.scalars(select(SourceState)).one()

    def incidents(self) -> list[HealthIncident]:
        with self.sessions() as session:
            return list(session.scalars(select(HealthIncident).order_by(HealthIncident.id)))

    def companies(self) -> list[Company]:
        with self.sessions() as session:
            return list(session.scalars(select(Company).order_by(Company.id)))

    def add_runs(self, source: str, target: str, samples: Sequence[tuple[datetime, int]]) -> None:
        """Successful past runs, as (started_at, fetched_count)."""
        with self.sessions() as session:
            session.add_all(
                Run(
                    source=source,
                    target=target,
                    started_at=started,
                    finished_at=started + timedelta(seconds=5),
                    ok=True,
                    fetched_count=count,
                )
                for started, count in samples
            )
            session.commit()

    def update_posting(self, source: str, external_id: str, **values: Any) -> None:
        with self.sessions() as session:
            session.execute(
                update(PostingRow)
                .where(PostingRow.source == source, PostingRow.external_id == external_id)
                .values(**values)
            )
            session.commit()


@pytest.fixture
def database(tmp_path: Any) -> sessionmaker[Session]:
    engine = create_engine_for(f"sqlite:///{tmp_path / 'jobpulse.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def env(
    database: sessionmaker[Session], http: HttpClient, time_machine: time_machine.TimeMachineFixture
) -> Env:
    time_machine.move_to(NOW, tick=False)
    config = make_config()
    adapters: dict[str, Any] = dict(build_adapters(http, config))
    adapters["linkedin"] = FakeBoardAdapter("linkedin")
    discoverer = Discoverer(AtsDetector(http), max_validations=10, max_page_fetches=5)
    return Env(database, http, config, RecordingNotifier(), adapters, discoverer)


@pytest.fixture
def feed(respx_mock: respx.MockRouter) -> SimplifyMock:
    return SimplifyMock(respx_mock.get(SIMPLIFY_NEW_GRAD_URL))


@pytest.fixture
def boards(env: Env) -> ScriptedScrape:
    """The real JobSpy adapter for both sites, over a scripted ``scrape_jobs``."""
    scrape = ScriptedScrape()
    adapter = JobSpyAdapter(
        scrape=scrape, sleep=FakeSleep(), rng=random.Random(5), monotonic=FakeMonotonic()
    )
    env.adapters["linkedin"] = adapter
    env.adapters["indeed"] = adapter
    return scrape


@pytest.fixture
def gh(respx_mock: respx.MockRouter) -> GreenhouseMock:
    mock = GreenhouseMock(respx_mock.get(GH_LIST))

    def detail(request: httpx.Request) -> httpx.Response:
        match = GH_DETAIL.match(str(request.url))
        assert match is not None
        job_id = int(match.group(1))
        description = mock.descriptions.get(job_id, DEFAULT_DESCRIPTION)
        return httpx.Response(
            200,
            json={
                "id": job_id,
                "content": html.escape(f"<p>{description}</p>"),
                "departments": [{"id": 1, "name": "Engineering"}],
                "offices": [],
                "pay_input_ranges": [],
            },
        )

    respx_mock.get(url__regex=GH_DETAIL.pattern).mock(side_effect=detail)
    return mock
