"""Files shipped inside the package (M7): the board seed every install starts from
(``seed/boards.json``) and the staffing agencies ``hide_agencies`` keeps out
(``seed/agencies.json``)."""

import json
from functools import cache
from importlib.resources import files

SEED_FILE = "seed/boards.json"
AGENCIES_FILE = "seed/agencies.json"


def read_text(name: str) -> str:
    return files(__name__).joinpath(name).read_text(encoding="utf-8")


@cache
def agency_names() -> tuple[str, ...]:
    """The staffing agencies, by company name as their postings give it."""
    data = json.loads(read_text(AGENCIES_FILE))
    return tuple(data["agencies"])
