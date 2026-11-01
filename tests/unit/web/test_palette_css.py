from pathlib import Path

from jobpulse.core.palette import SWATCHES

CSS = (Path(__file__).resolve().parents[3] / "src/jobpulse/web/static/app.css").read_text()


def test_every_slot_has_its_colors_in_the_stylesheet() -> None:
    for slot, swatch in enumerate(SWATCHES):
        assert f".bullet.slot-{slot} {{ color: {swatch.pastel}; }}" in CSS
        assert f".bullet.slot-{slot}.cb {{ color: {swatch.color_blind}; }}" in CSS
