"""Item 9 (discovery): board candidates from feed URLs, caps, ordering and the miss cache."""

from datetime import datetime, timedelta

from jobpulse.core.atsurls import AtsRef
from jobpulse.discovery import Discoverer, Sighting
from jobpulse.sources.ats_detect import Validation
from tests.factories import NOW

LEVER_DELTA = "https://jobs.lever.co/delta/98959f08-1d1b-4b75-919e-833f825394b0/apply"
ASHBY_EPSILON = "https://jobs.ashbyhq.com/Epsilon/13f15726-9a2d-4f02-a371-2d4d599ff556/application"
GH_ACME = "https://job-boards.greenhouse.io/acme/jobs/4012345007"
ZETA_GH_JID = "https://careers.zeta.example/careers/?gh_jid=5550001001#job-opening"
WORKDAY = "https://omega.wd5.myworkdayjobs.com/External/job/Seattle-WA/SWE_R1"


def seen(url: str, company: str = "Delta", *, relevant: bool = False) -> Sighting:
    return Sighting(url=url, company_name=company, source="simplify", relevant=relevant)


class FakeDetector:
    """Answers validations and page scans from dictionaries and records every call."""

    def __init__(
        self,
        *,
        boards: dict[str, Validation] | None = None,
        pages: dict[str, list[AtsRef] | None] | None = None,
    ) -> None:
        self.boards = boards or {}
        self.pages = pages or {}
        self.validated: list[AtsRef] = []
        self.fetched: list[str] = []

    async def validate(self, ref: AtsRef) -> Validation:
        self.validated.append(ref)
        return self.boards.get(
            ref.token, Validation(ref, ok=False, error="HTTP 404", error_kind="http_4xx")
        )

    async def refs_in_page(self, url: str) -> list[AtsRef] | None:
        self.fetched.append(url)
        return self.pages.get(url)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def discoverer(
    detector: FakeDetector, *, validations: int = 10, pages: int = 5, clock: Clock | None = None
) -> Discoverer:
    return Discoverer(
        detector,  # type: ignore[arg-type]
        max_validations=validations,
        max_page_fetches=pages,
        clock=clock or Clock(),
    )


def valid(ref: AtsRef, name: str | None = None) -> Validation:
    return Validation(ref, ok=True, name=name, job_count=3)


# --- planning ---------------------------------------------------------------------------


def test_one_candidate_per_board_and_known_boards_are_skipped() -> None:
    plan = discoverer(FakeDetector()).plan(
        [
            seen(LEVER_DELTA),
            seen("https://jobs.lever.co/delta/11111111-2222-4333-8444-555555555555"),
            seen(GH_ACME, "Acme"),
            seen(ASHBY_EPSILON, "Epsilon"),
            seen(WORKDAY, "Omega"),
        ],
        known={("greenhouse", "acme"), ("ashby", "epsilon")},
        greenhouse_hosts=(),
    )
    [candidate] = plan.candidates
    assert candidate.ref == AtsRef("lever", "delta", "98959f08-1d1b-4b75-919e-833f825394b0")
    assert candidate.company_name == "Delta"
    assert candidate.sightings == 2
    assert plan.pages == ()
    assert plan.deferred == 0


def test_relevant_boards_come_first_then_the_most_referenced() -> None:
    sightings = [
        seen("https://jobs.lever.co/aaa"),
        seen("https://jobs.lever.co/bbb"),
        seen("https://jobs.lever.co/bbb"),
        seen("https://jobs.lever.co/ccc", relevant=True),
    ]
    plan = discoverer(FakeDetector(), validations=2).plan(sightings, known=(), greenhouse_hosts=())
    assert [c.ref.token for c in plan.candidates] == ["ccc", "bbb"]
    assert plan.deferred == 1


def test_gh_jid_links_on_unmapped_hosts_become_one_page_fetch_per_host() -> None:
    plan = discoverer(FakeDetector()).plan(
        [
            seen(ZETA_GH_JID, "Zeta"),
            seen("https://careers.zeta.example/careers/?gh_jid=5550001002", "Zeta", relevant=True),
            seen("https://stripe.com/jobs/search?gh_jid=8172487", "Stripe"),
            seen("http://192.168.1.4/careers?gh_jid=1", "LAN"),
            seen(WORKDAY, "Omega"),
        ],
        known=(),
        greenhouse_hosts={"stripe.com"},
    )
    assert plan.candidates == ()
    [page] = plan.pages
    assert page.url == "https://careers.zeta.example/careers/?gh_jid=5550001002"  # the relevant one


def test_page_fetches_are_capped() -> None:
    sightings = [seen(f"https://careers{i}.example/jobs?gh_jid={i}", f"C{i}") for i in range(4)]
    plan = discoverer(FakeDetector(), pages=1).plan(sightings, known=(), greenhouse_hosts=())
    assert len(plan.pages) == 1
    assert plan.deferred == 3


def test_disabled_caps_plan_nothing() -> None:
    plan = discoverer(FakeDetector(), validations=0, pages=0).plan(
        [seen(LEVER_DELTA), seen(ZETA_GH_JID, "Zeta")], known=(), greenhouse_hosts=()
    )
    assert plan.candidates == ()
    assert plan.pages == ()
    assert plan.deferred == 2


# --- running ----------------------------------------------------------------------------


async def test_valid_boards_are_returned_with_their_validation() -> None:
    ref = AtsRef("lever", "delta")
    detector = FakeDetector(boards={"delta": valid(ref)})
    outcome = await discoverer(detector).run([seen(LEVER_DELTA)], known=(), greenhouse_hosts=())
    assert detector.validated == [AtsRef("lever", "delta", "98959f08-1d1b-4b75-919e-833f825394b0")]
    [found] = outcome.valid
    assert found.validation.ref == ref
    assert found.candidate.company_name == "Delta"
    assert outcome.invalid == []


async def test_a_page_reveals_the_board_and_it_is_validated_in_the_same_cycle() -> None:
    zeta = AtsRef("greenhouse", "zeta")
    detector = FakeDetector(
        boards={"zeta": valid(zeta, "Zeta")},
        pages={ZETA_GH_JID: [AtsRef("lever", "elsewhere"), zeta]},
    )
    outcome = await discoverer(detector).run(
        [seen(ZETA_GH_JID, "Zeta")], known=(), greenhouse_hosts=()
    )
    assert detector.fetched == [ZETA_GH_JID]
    assert detector.validated == [zeta]  # the Greenhouse board, since the link has a gh_jid
    [found] = outcome.valid
    assert found.candidate.company_name == "Zeta"
    assert found.candidate.url == ZETA_GH_JID


async def test_a_page_that_names_a_known_board_is_not_validated() -> None:
    detector = FakeDetector(pages={ZETA_GH_JID: [AtsRef("greenhouse", "zeta")]})
    outcome = await discoverer(detector).run(
        [seen(ZETA_GH_JID, "Zeta")], known={("greenhouse", "zeta")}, greenhouse_hosts=()
    )
    assert detector.validated == []
    assert outcome.valid == []


async def test_page_candidates_share_the_validation_cap() -> None:
    detector = FakeDetector(
        boards={"delta": valid(AtsRef("lever", "delta"))},
        pages={ZETA_GH_JID: [AtsRef("greenhouse", "zeta")]},
    )
    outcome = await discoverer(detector, validations=1).run(
        [seen(LEVER_DELTA), seen(ZETA_GH_JID, "Zeta")], known=(), greenhouse_hosts=()
    )
    assert [ref.token for ref in detector.validated] == ["delta"]
    assert detector.fetched == []  # no room left to validate what the page would reveal
    assert outcome.deferred == 1


async def test_misses_are_remembered_until_they_expire() -> None:
    clock = Clock()
    gone = Validation(AtsRef("lever", "delta"), ok=False, error="HTTP 404", error_kind="http_4xx")
    flaky = Validation(AtsRef("lever", "beta"), ok=False, error="HTTP 503", error_kind="http_5xx")
    detector = FakeDetector(boards={"delta": gone, "beta": flaky}, pages={ZETA_GH_JID: []})
    finder = discoverer(detector, clock=clock)
    sightings = [seen(LEVER_DELTA), seen("https://jobs.lever.co/beta"), seen(ZETA_GH_JID, "Zeta")]

    first = await finder.run(sightings, known=(), greenhouse_hosts=())
    assert {found.candidate.ref.token for found in first.invalid} == {"delta", "beta"}
    assert len(detector.validated) == 2
    assert len(detector.fetched) == 1

    await finder.run(sightings, known=(), greenhouse_hosts=())
    assert len(detector.validated) == 2  # both cached
    assert len(detector.fetched) == 1  # the page without a board, too

    clock.now += timedelta(hours=3)  # past the transient TTL, inside the permanent one
    await finder.run(sightings, known=(), greenhouse_hosts=())
    assert [ref.token for ref in detector.validated[2:]] == ["beta"]
    assert len(detector.fetched) == 1

    clock.now += timedelta(days=8)
    await finder.run(sightings, known=(), greenhouse_hosts=())
    assert sorted(ref.token for ref in detector.validated[3:]) == ["beta", "delta"]
    assert len(detector.fetched) == 2


async def test_an_unreadable_page_is_retried_after_the_transient_ttl() -> None:
    clock = Clock()
    detector = FakeDetector(pages={ZETA_GH_JID: None})
    finder = discoverer(detector, clock=clock)
    sightings = [seen(ZETA_GH_JID, "Zeta")]
    await finder.run(sightings, known=(), greenhouse_hosts=())
    await finder.run(sightings, known=(), greenhouse_hosts=())
    assert len(detector.fetched) == 1
    clock.now += timedelta(hours=3)
    await finder.run(sightings, known=(), greenhouse_hosts=())
    assert len(detector.fetched) == 2
