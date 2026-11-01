"""The eight colors of saved filters: a pastel for the dashboard, the nearest colored-circle
emoji for the phone (ntfy shows only text and emoji), and for color-blind mode an
Okabe–Ito-based color plus a shape per slot, so color is never the only cue."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Swatch:
    name: str
    pastel: str
    color_blind: str  # Okabe–Ito-based
    emoji: str
    shape: str


SWATCHES: tuple[Swatch, ...] = (
    Swatch("mint", "#b9ead3", "#009e73", "🟢", "●"),
    Swatch("lavender", "#d9c8f2", "#cc79a7", "🟣", "■"),
    Swatch("sky", "#bfdcf7", "#0072b2", "🔵", "▲"),
    Swatch("peach", "#ffd3b5", "#e69f00", "🟠", "◆"),
    Swatch("butter", "#f8eaa0", "#f0e442", "🟡", "★"),
    Swatch("rose", "#f6c3cd", "#d55e00", "🔴", "✚"),
    Swatch("sand", "#e3d2bf", "#a6761d", "🟤", "▼"),
    Swatch("silver", "#d6dae0", "#999999", "⚪", "⬢"),
)
SLOTS = len(SWATCHES)


def phone_bullet(slot: int, *, color_blind: bool) -> str:
    """The slot's emoji, followed by its shape in color-blind mode."""
    swatch = SWATCHES[slot]
    return swatch.emoji + swatch.shape if color_blind else swatch.emoji
