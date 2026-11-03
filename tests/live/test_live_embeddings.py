"""The real embedding model, read from data/models (run `jobpulse embeddings download`)."""

import os
from pathlib import Path

import pytest

from jobpulse.config import AppConfig
from jobpulse.core.similarity import dot
from jobpulse.embeddings.base import EmbedderUnavailable
from jobpulse.embeddings.fastembed import FastEmbedEmbedder
from jobpulse.runtime import build_embedder

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("JOBPULSE_LIVE"), reason="set JOBPULSE_LIVE=1"),
]
CACHE = Path(__file__).resolve().parents[2] / "data" / "models"


def test_related_texts_are_closer_than_unrelated_ones() -> None:
    try:
        embedder = FastEmbedEmbedder(cache_dir=CACHE)
    except EmbedderUnavailable as exc:
        pytest.skip(str(exc))
    backend, api, bakery = embedder.embed(
        [
            "Built REST APIs in Python and PostgreSQL for a payments backend.",
            "Backend engineer: design HTTP services with Python and SQL databases.",
            "Bakery seeks a pastry chef for early morning shifts.",
        ]
    )
    assert embedder.dims == 384
    assert len(backend) == 384
    assert dot(backend, backend) == pytest.approx(1.0, abs=1e-4)
    assert dot(backend, api) > dot(backend, bakery) + 0.1


def test_the_runtime_embedder_loads_the_cached_model_on_first_use() -> None:
    config = AppConfig.model_validate(
        {"contact_email": "dev@example.com", "embeddings": {"cache_dir": str(CACHE)}}
    )
    embedder = build_embedder(config)
    assert embedder is not None
    try:
        [vector] = embedder.embed(["Backend engineer: Python services on Postgres."])
    except EmbedderUnavailable as exc:
        pytest.skip(str(exc))
    assert embedder.dims == len(vector) == 384
    assert embedder.model == FastEmbedEmbedder(cache_dir=CACHE).model
