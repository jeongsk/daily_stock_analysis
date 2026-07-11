# -*- coding: utf-8 -*-
"""Phase 2: 리포트/알림에 KR 수급 결정적 요약 라인이 zh/en/ko로 렌더되는지 고정.

TW _append_institutional_flow 렌더 선례 미러. 오프라인.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.notification import NotificationService

_REC = {
    "code": "005930", "market": "kospi", "unit": "shares",
    "days": [
        {"date": "2026-07-10", "foreign_net": 625985, "institution_net": 2313745, "individual_net": -2851466},
        {"date": "2026-07-09", "foreign_net": 845552, "institution_net": 1107761, "individual_net": -1739937},
        {"date": "2026-07-08", "foreign_net": -3015093, "institution_net": 971031, "individual_net": 2031705},
        {"date": "2026-07-07", "foreign_net": -6145090, "institution_net": -1852807, "individual_net": 7870568},
        {"date": "2026-07-06", "foreign_net": -2018562, "institution_net": 14823, "individual_net": 1917050},
    ],
    "summary": {"foreign_net_5d": -9707208, "institution_net_5d": 2554553},
    "source": "NAVER",
}


class TestFormatNetSharesLocalized:
    def test_ko_uses_manju(self):
        assert NotificationService._format_net_shares_localized(625985, "ko") == "+62.60만주"
        assert NotificationService._format_net_shares_localized(-9707208, "ko") == "-970.72만주"

    def test_zh_uses_wan(self):
        assert NotificationService._format_net_shares_localized(625985, "zh") == "+62.60万股"

    def test_en_uses_millions(self):
        assert NotificationService._format_net_shares_localized(625985, "en") == "+0.63M shares"

    def test_invalid_is_na(self):
        assert NotificationService._format_net_shares_localized(None, "ko") == "N/A"
        assert NotificationService._format_net_shares_localized("x", "en") == "N/A"


def _blocks(record):
    """_append_kr_investor_flows가 소비하는 blocks dict 최소 형태."""
    return {"investor_flows": record}


class TestAppendKrInvestorFlows:
    def _render(self, record, language):
        from src.report_language import get_report_labels

        notifier = NotificationService.__new__(NotificationService)  # __init__ 부작용 회피
        lines = []
        notifier._append_kr_investor_flows(
            lines, _blocks(record), get_report_labels(language), language
        )
        return "\n".join(lines)

    def test_ko_line(self):
        text = self._render(dict(_REC), "ko")
        assert "외국인 -970.72만주" in text
        assert "기관 +255.46만주" in text
        assert "07-10" in text
        assert "NAVER" in text
        assert "개인" not in text  # 개인은 요약 라인에서 제외

    def test_en_line(self):
        text = self._render(dict(_REC), "en")
        assert "shares" in text and "NAVER" in text

    def test_zh_line(self):
        text = self._render(dict(_REC), "zh")
        assert "万股" in text and "NAVER" in text

    def test_no_record_omits_line(self):
        assert self._render(None, "ko") == ""
        assert self._render({"days": []}, "ko") == ""

    def test_all_na_omits_line(self):
        bad = {"summary": {"foreign_net_5d": None, "institution_net_5d": None},
               "days": [{"date": "2026-07-10"}], "source": "NAVER"}
        assert self._render(bad, "ko") == ""
