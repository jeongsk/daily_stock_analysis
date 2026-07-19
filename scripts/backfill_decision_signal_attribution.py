#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill ``signal_attribution`` metadata for pre-capture DecisionSignal rows.

Deferred Task 3b of the signal-attribution-outcomes design
(``docs/superpowers/specs/2026-07-17-signal-attribution-outcomes-design.md``,
decision D6 / ``docs/superpowers/plans/2026-07-17-signal-attribution-outcomes.md``
Task 3b): decision signals captured before the attribution-capture change
(PR #15/#16) never had ``metadata_json.signal_attribution`` written, so their
post-hoc ``dominant_attribution`` outcome axis stays ``None`` and falls into
the ``unknown`` bucket in ``GET /outcomes/stats`` breakdowns.

This script recovers ``signal_attribution`` for those pre-capture signals
from the linked ``analysis_history.raw_result`` report
(``dashboard.signal_attribution``), reusing the exact same all-or-nothing
extraction helper the two capture producers already share
(``extract_signal_attribution_for_metadata`` in
``src/utils/data_processing.py``) — no parallel derivation logic, no
re-normalization, no estimation. Signals whose source report is missing,
unparseable, or lacks a valid/complete attribution are left untouched:
``signal_attribution`` stays absent (NULL semantics preserved), matching the
design's fail-open contract.

Only the signal-side metadata is touched. Existing
``decision_signal_outcomes.dominant_attribution`` rows are intentionally NOT
recomputed here — per D6/D7 of the design, an outcome's snapshot axis is
frozen at evaluation time and only refreshes when that outcome is
force-re-evaluated (``POST /outcomes/run`` with ``force=true``).

Usage:
    uv run python scripts/backfill_decision_signal_attribution.py             # dry-run (default, no writes)
    uv run python scripts/backfill_decision_signal_attribution.py --apply     # write changes
    uv run python scripts/backfill_decision_signal_attribution.py --apply --limit 500
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.storage import AnalysisHistory, DatabaseManager, DecisionSignalRecord  # noqa: E402
from src.utils.data_processing import extract_signal_attribution_for_metadata  # noqa: E402
from src.utils.data_processing import parse_json_field  # noqa: E402


@dataclass
class BackfillStats:
    """Classification counters for a single scan pass."""

    scanned: int = 0
    already_attributed: int = 0
    invalid_metadata: int = 0
    no_source_report_id: int = 0
    source_report_missing: int = 0
    source_raw_result_invalid: int = 0
    source_attribution_unavailable: int = 0
    backfillable: int = 0
    updated: int = 0

    def summary_lines(self, *, apply: bool) -> List[str]:
        mode = "APPLY" if apply else "DRY-RUN"
        lines = [f"[backfill_decision_signal_attribution] mode={mode}"]
        lines.append(f"  scanned:                        {self.scanned}")
        lines.append(f"  already attributed (skipped):   {self.already_attributed}")
        lines.append(f"  invalid/missing metadata:       {self.invalid_metadata}")
        lines.append(f"  no source_report_id:            {self.no_source_report_id}")
        lines.append(f"  source report missing:          {self.source_report_missing}")
        lines.append(f"  source raw_result invalid:      {self.source_raw_result_invalid}")
        lines.append(f"  source attribution unavailable: {self.source_attribution_unavailable}")
        lines.append(f"  backfillable:                   {self.backfillable}")
        lines.append(f"  updated:                        {self.updated}")
        if not apply and self.backfillable:
            lines.append(
                f"[backfill_decision_signal_attribution] dry-run: {self.backfillable} row(s) "
                "would be updated. Re-run with --apply to write."
            )
        return lines


_Candidate = Tuple[DecisionSignalRecord, Dict[str, Any], Dict[str, Any]]


def _parse_json_object(value: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a JSON text column into a dict; None if missing/malformed/non-dict."""
    parsed = parse_json_field(value)
    return parsed if isinstance(parsed, dict) else None


def find_backfill_candidates(
    session: Session,
    *,
    limit: Optional[int] = None,
) -> Tuple[List[_Candidate], BackfillStats]:
    """Scan ``decision_signals`` and classify each row for the attribution backfill.

    Returns ``(candidates, stats)`` where ``candidates`` is a list of
    ``(signal_row, current_metadata, signal_attribution)`` ready to persist.
    Never mutates the session — classification is read-only.
    """

    stats = BackfillStats()
    candidates: List[_Candidate] = []

    query = select(DecisionSignalRecord).order_by(DecisionSignalRecord.id)
    if limit is not None:
        query = query.limit(limit)

    for signal in session.execute(query).scalars().all():
        stats.scanned += 1

        metadata = _parse_json_object(signal.metadata_json)
        if metadata is None:
            stats.invalid_metadata += 1
            continue
        if "signal_attribution" in metadata:
            stats.already_attributed += 1
            continue

        source_report_id = signal.source_report_id
        if not source_report_id:
            stats.no_source_report_id += 1
            continue

        history = session.get(AnalysisHistory, source_report_id)
        if history is None:
            stats.source_report_missing += 1
            continue

        raw_result = _parse_json_object(history.raw_result)
        if raw_result is None:
            stats.source_raw_result_invalid += 1
            continue

        attribution = extract_signal_attribution_for_metadata(raw_result.get("dashboard"))
        if attribution is None:
            stats.source_attribution_unavailable += 1
            continue

        stats.backfillable += 1
        candidates.append((signal, metadata, attribution))

    return candidates, stats


def apply_backfill(session: Session, candidates: List[_Candidate]) -> int:
    """Write ``signal_attribution`` into metadata_json for every candidate; commit once.

    Uses a targeted raw ``UPDATE`` (not ORM attribute assignment) so this
    additive-only backfill does not touch ``updated_at`` through the
    column's ``onupdate`` hook — consistent with the sibling
    ``_backfill_decision_signal_profile_from_metadata`` migration in
    ``src/storage.py``, which uses the same raw-SQL style for the same
    reason. The ``metadata_json = :old_metadata_json`` guard skips a row if
    it changed since the scan instead of silently clobbering it.
    """

    updated = 0
    for signal, metadata, attribution in candidates:
        new_metadata = dict(metadata)
        new_metadata["signal_attribution"] = attribution
        new_metadata_json = json.dumps(new_metadata, ensure_ascii=False, sort_keys=True, default=str)
        result = session.execute(
            text(
                "UPDATE decision_signals SET metadata_json = :new_metadata_json "
                "WHERE id = :signal_id AND metadata_json = :old_metadata_json"
            ),
            {
                "new_metadata_json": new_metadata_json,
                "signal_id": signal.id,
                "old_metadata_json": signal.metadata_json,
            },
        )
        if result.rowcount == 1:
            updated += 1
    if updated:
        session.commit()
    return updated


def run(
    *,
    db_url: Optional[str] = None,
    apply: bool,
    limit: Optional[int] = None,
) -> BackfillStats:
    db = DatabaseManager(db_url=db_url) if db_url else DatabaseManager.get_instance()
    with db.get_session() as session:
        candidates, stats = find_backfill_candidates(session, limit=limit)
        if apply:
            stats.updated = apply_backfill(session, candidates)
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill decision_signals.metadata_json.signal_attribution from linked "
            "analysis_history reports (dry-run by default)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to the database. Without this flag the script only reports what it would do.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only scan the first N decision_signals rows (ordered by id). Default: scan all rows.",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Override the database URL. Default: the configured application database.",
    )
    args = parser.parse_args(argv)

    stats = run(db_url=args.db_url, apply=args.apply, limit=args.limit)
    for line in stats.summary_lines(apply=args.apply):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
