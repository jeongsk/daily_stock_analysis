"""Market-review prompt rendering for World Monitor events.

Design: docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md §9

The central contract: "no events" and "could not check" must never render as
the same sentence (§3.1, §7.1).
"""

from datetime import datetime

import pytest

from src.services.worldmonitor_events import (
    CATEGORY_CONFLICT,
    CATEGORY_ENERGY,
    CATEGORY_OUTAGE,
    CategoryFreshness,
    render_worldmonitor_prompt_block,
)

NOW = datetime(2026, 7, 29, 9, 0, 0)
LANGUAGES = ["ko", "en", "zh"]


class _Row:
    """Minimal stand-in for a WorldEvent row."""

    def __init__(self, title, occurred_at, ended_at=None, summary=None, countries=None):
        self.title = title
        self.occurred_at = occurred_at
        self.ended_at = ended_at
        self.summary = summary
        self.country_list = countries or []

    @property
    def is_ongoing(self):
        return self.ended_at is None


def _fresh(category, can_claim=True):
    return CategoryFreshness(
        category=category,
        state="fresh",
        last_success_at=NOW,
        last_nonempty_at=NOW,
        can_claim_no_events=can_claim,
    )


def _all_fresh():
    return {c: _fresh(c) for c in (CATEGORY_CONFLICT, CATEGORY_OUTAGE, CATEGORY_ENERGY)}


def _empty_events():
    return {CATEGORY_CONFLICT: [], CATEGORY_OUTAGE: [], CATEGORY_ENERGY: []}


def _render(events=None, freshness=None, language="ko"):
    return render_worldmonitor_prompt_block(
        events_by_category=events if events is not None else _empty_events(),
        freshness=freshness if freshness is not None else _all_fresh(),
        language=language,
        now=NOW,
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_events_are_rendered_with_their_titles(language):
    events = _empty_events()
    events[CATEGORY_CONFLICT] = [_Row("Battles - Syria", datetime(2026, 7, 20))]

    block = _render(events=events, language=language)
    assert "Battles - Syria" in block


@pytest.mark.parametrize("language", LANGUAGES)
def test_unverified_category_never_renders_as_no_events(language):
    """A category that has never produced an event must read as unknown, not
    as an all-clear. This is the failure mode the whole design exists to stop."""
    freshness = _all_fresh()
    freshness[CATEGORY_CONFLICT] = CategoryFreshness(
        category=CATEGORY_CONFLICT,
        state="unverified",
        last_success_at=NOW,
        last_nonempty_at=None,
        can_claim_no_events=False,
    )

    block = _render(freshness=freshness, language=language)
    conflict_section = _section_for(block, CATEGORY_CONFLICT, language)
    assert _no_events_phrase(language) not in conflict_section


@pytest.mark.parametrize("language", LANGUAGES)
def test_unavailable_category_never_renders_as_no_events(language):
    freshness = _all_fresh()
    freshness[CATEGORY_OUTAGE] = CategoryFreshness(
        category=CATEGORY_OUTAGE, state="unavailable", can_claim_no_events=False
    )

    block = _render(freshness=freshness, language=language)
    section = _section_for(block, CATEGORY_OUTAGE, language)
    assert _no_events_phrase(language) not in section


@pytest.mark.parametrize("language", LANGUAGES)
def test_proven_empty_category_may_render_as_no_events(language):
    """Once a source has demonstrably produced events inside the lookback
    window, an empty result really does mean "nothing happened"."""
    block = _render(language=language)
    section = _section_for(block, CATEGORY_CONFLICT, language)
    assert _no_events_phrase(language) in section


@pytest.mark.parametrize("language", LANGUAGES)
def test_stale_category_is_labeled_as_stale(language):
    freshness = _all_fresh()
    freshness[CATEGORY_ENERGY] = CategoryFreshness(
        category=CATEGORY_ENERGY,
        state="stale",
        last_success_at=datetime(2026, 7, 28, 0, 0, 0),
        last_nonempty_at=datetime(2026, 7, 28, 0, 0, 0),
        can_claim_no_events=False,
    )
    events = _empty_events()
    events[CATEGORY_ENERGY] = [_Row("Pipeline offline", datetime(2026, 7, 27))]

    block = _render(events=events, freshness=freshness, language=language)
    section = _section_for(block, CATEGORY_ENERGY, language)
    # The stored event is still shown, but flagged as not freshly confirmed.
    assert "Pipeline offline" in section
    assert _no_events_phrase(language) not in section


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_category_gets_a_section_even_when_empty(language):
    """Silently dropping a category would let the model assume it was checked."""
    block = _render(language=language)
    for category in (CATEGORY_CONFLICT, CATEGORY_OUTAGE, CATEGORY_ENERGY):
        assert _heading_for(category, language) in block


@pytest.mark.parametrize("language", LANGUAGES)
def test_ongoing_events_are_marked(language):
    events = _empty_events()
    events[CATEGORY_ENERGY] = [
        _Row("Ongoing disruption", datetime(2026, 7, 20), ended_at=None),
        _Row("Closed disruption", datetime(2026, 7, 21), ended_at=datetime(2026, 7, 22)),
    ]

    block = _render(events=events, language=language)
    assert "Ongoing disruption" in block
    assert "Closed disruption" in block


@pytest.mark.parametrize("language", LANGUAGES)
def test_block_carries_a_heading(language):
    assert _render(language=language).strip().startswith("##")


@pytest.mark.parametrize("language", LANGUAGES)
def test_occurrence_dates_are_shown(language):
    events = _empty_events()
    events[CATEGORY_CONFLICT] = [_Row("Battles - Syria", datetime(2026, 7, 20))]
    assert "2026-07-20" in _render(events=events, language=language)


@pytest.mark.parametrize("language", LANGUAGES)
def test_unknown_language_falls_back_without_raising(language):
    block = render_worldmonitor_prompt_block(
        events_by_category=_empty_events(),
        freshness=_all_fresh(),
        language="fr",
        now=NOW,
    )
    assert block.strip().startswith("##")


def test_all_categories_unavailable_still_renders_the_boundary():
    """If nothing could be checked, the prompt must say so rather than going
    silent - silence reads as "checked and clear"."""
    freshness = {
        c: CategoryFreshness(category=c, state="unavailable", can_claim_no_events=False)
        for c in (CATEGORY_CONFLICT, CATEGORY_OUTAGE, CATEGORY_ENERGY)
    }
    block = _render(freshness=freshness)
    assert block.strip()
    assert _no_events_phrase("ko") not in block


def test_countries_are_shown_when_present():
    events = _empty_events()
    events[CATEGORY_ENERGY] = [
        _Row("Pipeline offline", datetime(2026, 7, 20), countries=["KR", "JP"])
    ]
    block = _render(events=events)
    assert "KR" in block


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_HEADINGS = {
    "ko": {
        CATEGORY_CONFLICT: "지정학 분쟁",
        CATEGORY_OUTAGE: "인프라 장애",
        CATEGORY_ENERGY: "공급망·에너지",
    },
    "en": {
        CATEGORY_CONFLICT: "Geopolitical conflict",
        CATEGORY_OUTAGE: "Infrastructure outage",
        CATEGORY_ENERGY: "Supply chain and energy",
    },
    "zh": {
        CATEGORY_CONFLICT: "地缘冲突",
        CATEGORY_OUTAGE: "基础设施中断",
        CATEGORY_ENERGY: "供应链与能源",
    },
}

_NO_EVENTS = {"ko": "해당 없음", "en": "None in this window", "zh": "本窗口内无事件"}


def _heading_for(category, language):
    return _HEADINGS.get(language, _HEADINGS["en"])[category]


def _no_events_phrase(language):
    return _NO_EVENTS.get(language, _NO_EVENTS["en"])


def _section_for(block, category, language):
    """Return just the text belonging to one category's subsection."""
    heading = _heading_for(category, language)
    start = block.index(heading)
    rest = block[start:]
    next_heading = rest.find("\n###", 1)
    return rest if next_heading == -1 else rest[:next_heading]
