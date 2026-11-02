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


def test_jobpulse_models_dir_moves_the_default_model_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image bakes the model outside the data volume and points this variable at it."""
    from jobpulse.config import EmbeddingsConfig, InfraConfig

    assert EmbeddingsConfig().cache_dir == Path("data/models")
    monkeypatch.setenv("JOBPULSE_MODELS_DIR", str(tmp_path / "baked"))
    assert InfraConfig().embeddings.cache_dir == tmp_path / "baked"
    # config.yaml still wins.
    explicit = InfraConfig.model_validate({"embeddings": {"cache_dir": "elsewhere"}})
    assert explicit.embeddings.cache_dir == Path("elsewhere")


def test_embeddings_download_defaults_to_jobpulse_models_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []

    def fake_download(model: str, cache_dir: Path) -> str:
        calls.append(cache_dir)
        return model

    monkeypatch.setattr(cli, "download_model", fake_download)
    monkeypatch.setenv("JOBPULSE_MODELS_DIR", str(tmp_path / "baked"))
    result = runner.invoke(cli.app, ["embeddings", "download"])
    assert result.exit_code == 0, result.output
    assert calls == [tmp_path / "baked"]


def test_onnxruntime_and_hugging_face_telemetry_are_off_before_fastembed_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """onnxruntime 1.31 uploads usage events to Microsoft unless ORT_DISABLE_TELEMETRY is set."""
    import os

    monkeypatch.delenv("ORT_DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_TELEMETRY", raising=False)
    with pytest.raises(EmbedderUnavailable):
        FastEmbedEmbedder(cache_dir=tmp_path / "models")
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_an_explicit_telemetry_setting_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")
    with pytest.raises(EmbedderUnavailable):
        FastEmbedEmbedder(cache_dir=tmp_path / "models")
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "0"
