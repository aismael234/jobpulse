"""The setup wizard's logic (M8), shared by the dashboard and the tests: answers turned into
profile sections, the resume's prefill, and the board seed applied once onboarding is done.

The answers live in the profile (``Profile.answers``). Each change rebuilds every section
``core.profile_builder.build_rules`` writes, except those listed in ``answers.custom``:
sections you edited in Advanced stay as you left them. A profile without answers (imported
or written by hand) is all custom: the wizard never touches it.
"""

import logging
import secrets
import string
from dataclasses import replace
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from jobpulse.config import (
    AppConfig,
    Profile,
    SetupAnswers,
    company_blocklist,
    filter_rules,
    parse_profile,
    profile_data,
)
from jobpulse.core.profile_builder import SECTIONS, build_rules
from jobpulse.core.resume_facts import ResumeFacts
from jobpulse.core.roles import level_for_years
from jobpulse.db import repo
from jobpulse.db.session import session_scope
from jobpulse.seed import packaged_seed, plan_seed, seed_keywords, write_seed
from jobpulse.sources.ats_detect import AtsDetector

log = logging.getLogger(__name__)

TOPIC_CHARS = 32
MAX_PREFILLED_MAIN_SKILLS = 5
MAX_PREFILLED_SKILLS = 20


def _get(data: dict[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(data: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    node = data
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value


def apply_answers(profile: Profile, answers: SetupAnswers) -> Profile:
    """``profile`` with ``answers`` stored and every section built from them, except the
    custom ones. Raises ``ProfileError`` when the result doesn't validate."""
    data = profile_data(profile)
    for path, value in build_rules(answers.to_core()).items():
        if path not in answers.custom:
            _set(data, path, value)
    data["answers"] = answers.model_dump(mode="json")
    return parse_profile(data)


def note_custom(profile: Profile) -> Profile:
    """After an Advanced edit: the sections that no longer match what the answers build
    become custom, so changing the answers later keeps them."""
    answers = profile.answers
    if answers is None:
        return profile
    data = profile_data(profile)
    canonical = profile_data(apply_answers(Profile(), answers.model_copy(update={"custom": []})))
    custom = [
        path
        for path in SECTIONS
        if path in answers.custom or _get(data, path) != _get(canonical, path)
    ]
    if custom == answers.custom:
        return profile
    return profile.model_copy(update={"answers": answers.model_copy(update={"custom": custom})})


def prefill(answers: SetupAnswers, facts: ResumeFacts) -> SetupAnswers:
    """Answers the resume suggests, for the questions not answered yet."""
    update: dict[str, Any] = {}
    if not answers.roles and facts.families:
        update["roles"] = list(facts.families)
    if answers.max_years is None and facts.years is not None:
        update["level"] = level_for_years(facts.years)
    if not answers.skills and facts.skills:
        ranked = [key for key, _ in facts.skills.most_common(MAX_PREFILLED_SKILLS)]
        update["skills"] = {
            key: "main" if index < MAX_PREFILLED_MAIN_SKILLS else "also"
            for index, key in enumerate(ranked)
        }
    return answers.model_copy(update=update) if update else answers


def generate_topic() -> str:
    """A fresh ntfy topic: 32 random letters and digits, too long to guess."""
    alphabet = string.ascii_letters + string.digits
    return "jobpulse-" + "".join(secrets.choice(alphabet) for _ in range(TOPIC_CHARS))


async def apply_seed(
    sessions: sessionmaker[Session], config: AppConfig, detector: AtsDetector, *, now: datetime
) -> list[str]:
    """Add the packaged seed's boards (first poll silent), with Workday keywords from the
    chosen roles; the labels added. Run once, when onboarding finishes."""
    seed = packaged_seed()
    with sessions() as session:
        known = repo.known_companies(session)
        blocklist = repo.blocklist(
            session, company_blocklist(config), agencies=config.hide_agencies
        )
    plan = await plan_seed(
        seed,
        known=known,
        blocklist=blocklist,
        config=config,
        rules=replace(filter_rules(config), blocklist=blocklist),
        detector=detector,
        keywords=seed_keywords(config),
    )
    with session_scope(sessions) as session:
        added, _ = write_seed(session, plan, now=now)
    log.info("seed: added %d boards after onboarding", len(added))
    return added
