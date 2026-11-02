from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobpulse import cli
from jobpulse.core.similarity import dot
from jobpulse.embeddings.base import Embedder, EmbedderUnavailable
from jobpulse.embeddings.fastembed import FastEmbedEmbedder
from tests.factories import FakeEmbedder

runner = CliRunner()


def test_fastembed_without_a_downloaded_model_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(EmbedderUnavailable, match="jobpulse embeddings download"):
        FastEmbedEmbedder(cache_dir=tmp_path / "models")


def test_fake_embedder_is_an_embedder_with_normalized_vectors() -> None:
    embedder: Embedder = FakeEmbedder()
    python, go, python_again = embedder.embed(
        ["Python services", "Go compilers", "python SERVICES and more"]
    )
    assert dot(python, python) == pytest.approx(1.0, abs=1e-6)
    assert dot(python, python_again) > dot(python, go)


def test_embeddings_download_reports_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Path]] = []

    def fake_download(model: str, cache_dir: Path) -> str:
        calls.append((model, cache_dir))
        return f"{model}@fastembed-test"

    monkeypatch.setattr(cli, "download_model", fake_download)
    target = tmp_path / "models"
    result = runner.invoke(cli.app, ["embeddings", "download", "--cache-dir", str(target)])
    assert result.exit_code == 0, result.output
    assert calls == [("BAAI/bge-small-en-v1.5", target)]
    assert "BAAI/bge-small-en-v1.5@fastembed-test" in result.output
    assert str(target) in result.output


def test_embeddings_download_failure_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(model: str, cache_dir: Path) -> str:
        raise OSError("connection refused")

    monkeypatch.setattr(cli, "download_model", failing)
    result = runner.invoke(cli.app, ["embeddings", "download", "--cache-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert "download failed" in result.output
