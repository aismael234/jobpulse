from jobpulse.core.palette import SLOTS, SWATCHES, phone_bullet


def test_eight_distinct_slots() -> None:
    assert SLOTS == 8
    for attribute in ("name", "pastel", "color_blind", "emoji", "shape"):
        assert len({getattr(swatch, attribute) for swatch in SWATCHES}) == SLOTS, attribute


def test_phone_bullets() -> None:
    assert phone_bullet(0, color_blind=False) == "🟢"
    assert phone_bullet(1, color_blind=False) == "🟣"
    # color-blind mode adds the slot's shape, so color is never the only cue
    assert phone_bullet(0, color_blind=True) == "🟢●"
    assert phone_bullet(2, color_blind=True) == "🔵▲"
