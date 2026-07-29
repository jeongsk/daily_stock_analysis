"""Normalization of upstream World Monitor payloads into DSA events.

Design: docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md
§3 (field mapping), §8/§8.1 (scope), §9 (severity_rank)
"""

from datetime import datetime, timezone

from src.services.worldmonitor_events import (
    CATEGORY_CONFLICT,
    CATEGORY_ENERGY,
    CATEGORY_OUTAGE,
    normalize_acled_event,
    normalize_energy_disruption,
    normalize_internet_outage,
)


# ---------------------------------------------------------------------------
# ACLED conflict
# ---------------------------------------------------------------------------


def _acled(**updates):
    values = {
        "id": "acled-SYR123",
        "eventType": "Battles",
        "country": "Syria",
        "occurredAt": 1753000000000,
        "fatalities": 7,
        "actors": ["Actor A", "Actor B"],
        "source": "Local media",
        "admin1": "Aleppo",
        "location": {"latitude": 36.2, "longitude": 37.1},
    }
    values.update(updates)
    return values


def test_acled_maps_occurrence_time_from_epoch_millis():
    event = normalize_acled_event(_acled())
    assert event["category"] == CATEGORY_CONFLICT
    assert event["external_id"] == "acled-SYR123"
    assert event["occurred_at"] == datetime.fromtimestamp(1753000000000 / 1000)


def test_acled_has_no_end_time_so_events_stay_ongoing():
    """The upstream ACLED projection carries no end field at all."""
    assert normalize_acled_event(_acled())["ended_at"] is None


def test_acled_severity_rank_is_the_fatality_count():
    assert normalize_acled_event(_acled(fatalities=7))["severity_rank"] == 7
    assert normalize_acled_event(_acled(fatalities=0))["severity_rank"] == 0


def test_acled_composes_a_title_because_upstream_has_none():
    title = normalize_acled_event(_acled())["title"]
    assert "Battles" in title
    assert "Syria" in title


def test_acled_without_a_usable_occurrence_time_is_rejected():
    """occurredAt becomes NaN when upstream event_date is unparseable; storing
    such a row would break the no-future-information guarantee."""
    assert normalize_acled_event(_acled(occurredAt=0)) is None
    assert normalize_acled_event(_acled(occurredAt=None)) is None


def test_acled_without_an_id_is_rejected():
    assert normalize_acled_event(_acled(id="")) is None


# ---------------------------------------------------------------------------
# Internet outage
# ---------------------------------------------------------------------------


def _outage(**updates):
    values = {
        "id": "outage-1",
        "title": "Nationwide connectivity drop",
        "link": "https://example.test/outage-1",
        "description": "Major disruption",
        "detectedAt": 1753100000000,
        "country": "KR",
        "region": "Asia",
        "severity": "OUTAGE_SEVERITY_MAJOR",
        "categories": ["fixed-line"],
        "cause": "cable cut",
        "outageType": "national",
        "endedAt": 0,
    }
    values.update(updates)
    return values


def test_outage_zero_end_time_means_ongoing():
    """endedAt is a proto number field, so 'not ended' arrives as 0 rather
    than null - treating 0 as an epoch would date the event to 1970."""
    assert normalize_internet_outage(_outage(endedAt=0))["ended_at"] is None


def test_outage_nonzero_end_time_is_preserved():
    event = normalize_internet_outage(_outage(endedAt=1753200000000))
    assert event["ended_at"] == datetime.fromtimestamp(1753200000000 / 1000)


def test_outage_severity_enum_maps_to_the_documented_ranks():
    ranks = {
        "OUTAGE_SEVERITY_TOTAL": 3,
        "OUTAGE_SEVERITY_MAJOR": 2,
        "OUTAGE_SEVERITY_PARTIAL": 1,
        "OUTAGE_SEVERITY_UNSPECIFIED": 0,
    }
    for name, expected in ranks.items():
        assert normalize_internet_outage(_outage(severity=name))["severity_rank"] == expected


def test_outage_unknown_severity_degrades_to_zero_rather_than_failing():
    assert normalize_internet_outage(_outage(severity="SOMETHING_NEW"))["severity_rank"] == 0


def test_outage_maps_country_to_a_dsa_market():
    event = normalize_internet_outage(_outage(country="KR"))
    assert event["markets"] == ["kr"]
    assert event["scope"] == "market"


def test_outage_falls_back_to_region_when_country_is_missing():
    event = normalize_internet_outage(_outage(country="", region="Asia"))
    assert event["countries"] == ["Asia"]
    assert event["scope"] == "global"


def test_outage_without_country_or_region_is_unmapped():
    event = normalize_internet_outage(_outage(country="", region=""))
    assert event["scope"] == "unmapped"
    assert event["markets"] == []


# ---------------------------------------------------------------------------
# Energy disruption
# ---------------------------------------------------------------------------


def _energy(**updates):
    values = {
        "id": "energy-1",
        "assetId": "pipeline-9",
        "assetType": "pipeline",
        "eventType": "outage",
        "startAt": "2026-07-20T00:00:00Z",
        "endAt": "",
        "capacityOfflineBcmYr": 0,
        "capacityOfflineMbd": 1.5,
        "causeChain": ["sabotage"],
        "shortDescription": "Pipeline offline",
        "sources": [{"authority": "IEA", "title": "t", "url": "https://example.test/e", "date": "2026-07-20", "sourceType": "report"}],
        "classifierVersion": "v1",
        "classifierConfidence": 0.8,
        "lastEvidenceUpdate": "2026-07-21",
        "countries": ["KR", "JP"],
    }
    values.update(updates)
    return values


def _utc_as_local(*args):
    """Express a UTC instant as the naive local time the normalizers produce."""
    return (
        datetime(*args, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    )


def test_energy_parses_iso_timestamps():
    event = normalize_energy_disruption(_energy())
    assert event["category"] == CATEGORY_ENERGY
    assert event["occurred_at"] == _utc_as_local(2026, 7, 20, 0, 0, 0)


def test_energy_empty_end_string_means_ongoing():
    assert normalize_energy_disruption(_energy(endAt=""))["ended_at"] is None


def test_energy_end_time_is_parsed_when_present():
    event = normalize_energy_disruption(_energy(endAt="2026-07-25T00:00:00Z"))
    assert event["ended_at"] == _utc_as_local(2026, 7, 25, 0, 0, 0)


def test_epoch_and_iso_sources_agree_on_the_same_instant():
    """Regression: occurred_at mixes rows from both parsers and is compared
    against a local datetime.now(). If the ISO path kept UTC wall-clock values,
    cross-category ordering would skew by the UTC offset (9h on KST) and the
    date shown in the prompt could be off by a day."""
    instant = datetime(2026, 7, 20, 0, 0, 0, tzinfo=timezone.utc)
    from_epoch = normalize_acled_event(
        _acled(occurredAt=int(instant.timestamp() * 1000))
    )["occurred_at"]
    from_iso = normalize_energy_disruption(
        _energy(startAt="2026-07-20T00:00:00Z")
    )["occurred_at"]
    assert from_epoch == from_iso


def test_naive_iso_input_is_taken_as_local_time():
    event = normalize_energy_disruption(_energy(startAt="2026-07-20T00:00:00"))
    assert event["occurred_at"] == datetime(2026, 7, 20, 0, 0, 0)


def test_energy_severity_uses_mbd_scaled_by_ten():
    assert normalize_energy_disruption(_energy(capacityOfflineMbd=1.5))["severity_rank"] == 15


def test_energy_severity_converts_bcm_per_year_when_mbd_is_absent():
    event = normalize_energy_disruption(
        _energy(capacityOfflineMbd=0, capacityOfflineBcmYr=10)
    )
    assert event["severity_rank"] == round(10 * 0.172 * 10)


def test_energy_severity_is_zero_when_both_capacities_are_missing():
    """The upstream projector coerces missing capacity to 0, so ties at rank 0
    are expected and the recency tiebreak decides ordering."""
    event = normalize_energy_disruption(
        _energy(capacityOfflineMbd=0, capacityOfflineBcmYr=0)
    )
    assert event["severity_rank"] == 0


def test_energy_maps_countries_to_markets():
    event = normalize_energy_disruption(_energy(countries=["KR", "JP"]))
    assert sorted(event["markets"]) == ["jp", "kr"]
    assert event["scope"] == "market"


def test_energy_with_countries_outside_dsa_markets_is_global():
    event = normalize_energy_disruption(_energy(countries=["RU", "UA"]))
    assert event["markets"] == []
    assert event["scope"] == "global"


def test_energy_with_empty_countries_is_unmapped_not_global():
    """Upstream documents that pre-denorm rows carry an empty countries array.
    Folding those into 'global' would silently promote an event whose origin is
    unknown into 'affects every market' (design §8.1)."""
    event = normalize_energy_disruption(_energy(countries=[]))
    assert event["scope"] == "unmapped"
    assert event["markets"] == []


def test_energy_takes_the_first_source_url():
    assert normalize_energy_disruption(_energy())["url"] == "https://example.test/e"


def test_energy_without_sources_has_no_url():
    assert normalize_energy_disruption(_energy(sources=[]))["url"] is None


def test_energy_without_a_start_time_is_rejected():
    assert normalize_energy_disruption(_energy(startAt="")) is None
    assert normalize_energy_disruption(_energy(startAt="not-a-date")) is None


def test_all_normalizers_record_the_upstream_endpoint_for_audit():
    assert normalize_acled_event(_acled())["source_endpoint"].endswith("list-acled-events")
    assert normalize_internet_outage(_outage())["source_endpoint"].endswith("list-internet-outages")
    assert normalize_energy_disruption(_energy())["source_endpoint"].endswith("list-energy-disruptions")


def test_categories_are_distinct_per_source():
    assert normalize_internet_outage(_outage())["category"] == CATEGORY_OUTAGE
    assert normalize_acled_event(_acled())["category"] == CATEGORY_CONFLICT
    assert normalize_energy_disruption(_energy())["category"] == CATEGORY_ENERGY


def test_raw_payload_is_preserved_for_future_renormalization():
    event = normalize_energy_disruption(_energy())
    assert event["raw_payload"]["assetId"] == "pipeline-9"


def test_malformed_payloads_return_none_instead_of_raising():
    """A schema drift upstream must skip the record, not break the sync."""
    assert normalize_acled_event(None) is None
    assert normalize_internet_outage("not-a-dict") is None
    assert normalize_energy_disruption([]) is None
