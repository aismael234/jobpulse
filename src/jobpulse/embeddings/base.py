"""What the poller and the analysis need from an embedding model."""

from collections.abc import Callable, Sequence
from typing import Protocol

from jobpulse.core.similarity import Vector


class EmbedderUnavailable(Exception):
    """The model can't be used: the package is missing, or the model isn't downloaded."""


class Embedder(Protocol):
    model: str  # the model id and version; vectors from another model are never mixed

    @property
    def dims(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        """One L2-normalized vector per text, in order. Synchronous and CPU-bound: callers in
        an event loop run it in a thread."""
        ...


class LazyEmbedder:
    """An embedder that loads its model on first use, so an install without a resume never
    pays for it (about 200 MB of memory). A failed load raises ``EmbedderUnavailable`` and is
    tried again next time: the model may have been downloaded in between."""

    def __init__(self, model: str, load: Callable[[], Embedder]) -> None:
        self.model = model
        self._load = load
        self._loaded: Embedder | None = None

    def _embedder(self) -> Embedder:
        if self._loaded is None:
            loaded = self._load()
            if loaded.model != self.model:
                raise EmbedderUnavailable(f"loaded {loaded.model}, expected {self.model}")
            self._loaded = loaded
        return self._loaded

    @property
    def dims(self) -> int:
        return self._embedder().dims

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        return self._embedder().embed(texts) if texts else []
