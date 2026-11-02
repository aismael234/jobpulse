"""fastembed (ONNX) embeddings. The model is read from a local cache and never downloaded at
runtime: ``download_model`` (``jobpulse embeddings download``) is the only path that
downloads."""

import math
from array import array
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path
from typing import Any

from jobpulse.core.similarity import Vector
from jobpulse.embeddings.base import EmbedderUnavailable

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_CACHE_DIR = Path("data/models")
_BATCH_SIZE = 32


def model_id(model: str = DEFAULT_MODEL) -> str:
    """``model`` with the fastembed version, without loading anything. Raises
    ``EmbedderUnavailable`` when fastembed isn't installed."""
    try:
        version = metadata.version("fastembed")
    except metadata.PackageNotFoundError:
        raise EmbedderUnavailable("the fastembed package isn't installed") from None
    return f"{model}@fastembed-{version}"


def _normalized(values: Sequence[float]) -> Vector:
    norm = math.sqrt(math.fsum(float(v) * float(v) for v in values)) or 1.0
    return array("f", (float(v) / norm for v in values))


class FastEmbedEmbedder:
    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbedderUnavailable(f"the fastembed package isn't installed ({exc})") from None
        try:
            self._model: Any = TextEmbedding(
                model_name=model, cache_dir=str(cache_dir), local_files_only=True
            )
        except Exception as exc:  # fastembed raises plain ValueError/OSError variants
            raise EmbedderUnavailable(
                f"{model} isn't in {cache_dir}; run `jobpulse embeddings download` "
                f"({type(exc).__name__})"
            ) from None
        self.model = model_id(model)
        self.dims = int(self._model.embedding_size)

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        if not texts:
            return []
        return [_normalized(v) for v in self._model.embed(list(texts), batch_size=_BATCH_SIZE)]


def download_model(model: str = DEFAULT_MODEL, cache_dir: Path = DEFAULT_CACHE_DIR) -> str:
    """Download ``model`` into ``cache_dir`` (Hugging Face), then load it offline to prove the
    cache works. Returns the model id with its version."""
    from fastembed import TextEmbedding

    cache_dir.mkdir(parents=True, exist_ok=True)
    TextEmbedding(model_name=model, cache_dir=str(cache_dir))
    return FastEmbedEmbedder(model, cache_dir).model
