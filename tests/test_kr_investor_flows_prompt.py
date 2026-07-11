# -*- coding: utf-8 -*-
"""Phase 2: analyzer LLM 프롬프트에 KR 수급 값이 주입되는지 고정(오프라인).

TW 三大法人 프롬프트 주입(analyzer.py ~3897) 미러. context.fundamental_context
["investor_flows"] 레코드가 있으면 프롬프트에 수급 표가 들어가고, 없으면 생략된다.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.analyzer import _kr_investor_flows_prompt_section  # 신규 순수 헬퍼(Step 3에서 추가)

_REC = {
    "code": "005930", "market": "kospi", "unit": "shares",
    "days": [
        {"date": "2026-07-10", "foreign_net": 625985, "institution_net": 2313745, "individual_net": -2851466},
        {"date": "2026-07-09", "foreign_net": 845552, "institution_net": 1107761, "individual_net": -1739937},
    ],
    "summary": {"foreign_net_5d": 1471537, "institution_net_5d": 3421506},
    "source": "NAVER",
}


class TestKrFlowsPromptSection:
    def test_section_present_when_record_valid(self):
        text = _kr_investor_flows_prompt_section({"investor_flows": _REC})
        assert text  # 비어있지 않음
        assert "1471537" in text or "3421506" in text  # 5일 누적 값 노출
        assert "2026-07-10" in text  # 최신 확정일
        assert "NAVER" in text

    def test_section_empty_when_no_record(self):
        assert _kr_investor_flows_prompt_section({}) == ""
        assert _kr_investor_flows_prompt_section({"investor_flows": None}) == ""
        assert _kr_investor_flows_prompt_section(None) == ""

    def test_section_empty_when_days_missing(self):
        bad = {"investor_flows": {"summary": {}, "days": []}}
        assert _kr_investor_flows_prompt_section(bad) == ""
