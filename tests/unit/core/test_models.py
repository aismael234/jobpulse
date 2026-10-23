import pytest

from jobpulse.core.models import FetchResult
from tests.factories import NOW, make_posting


def test_unchanged_is_a_success_without_postings() -> None:
    result = FetchResult.unchanged(fetched_at=NOW, etag='W/"abc"')
    assert result.ok
    assert result.not_modified
    assert result.postings == ()
    assert result.etag == 'W/"abc"'


def test_skip_is_a_failure_that_was_never_attempted() -> None:
    result = FetchResult.skip("http_429", "backing off", fetched_at=NOW)
    assert not result.ok
    assert result.skipped
    assert result.error_kind == "http_429"


def test_success_carries_closed_ids_and_an_etag() -> None:
    result = FetchResult.success(
        [make_posting()],
        fetched_at=NOW,
        complete_listing=False,
        closed_external_ids=["x1", "x2"],
        etag='"v2"',
    )
    assert result.closed_external_ids == ("x1", "x2")
    assert result.etag == '"v2"'


@pytest.mark.parametrize(
    "fields",
    [
        {"ok": True, "skipped": True},
        {"ok": False, "error_kind": "other", "closed_external_ids": ("x",)},
        {"ok": False, "error_kind": "other", "not_modified": True},
        {"ok": True, "not_modified": True, "postings": (make_posting(),)},
    ],
    ids=["skipped-success", "failure-with-closures", "failed-304", "304-with-postings"],
)
def test_contradictory_results_are_rejected(fields: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="FetchResult"):
        FetchResult(fetched_at=NOW, **fields)  # type: ignore[arg-type]
