# -*- coding: utf-8 -*-
"""공유 귀인 캡처 헬퍼 계약: all-or-nothing, 재정규화 없음, fail-open."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.data_processing import extract_signal_attribution_for_metadata


def _dashboard(attr):
    return {"sentiment_score": 70, "signal_attribution": attr}


class TestExtractSignalAttributionForMetadata:
    def test_valid_attribution_copied_verbatim(self):
        attr = {
            "technical_indicators": 40, "news_sentiment": 25,
            "fundamentals": 20, "market_conditions": 15,
            "strongest_bullish_signal": "MA golden cross",
            "strongest_bearish_signal": None,
        }
        out = extract_signal_attribution_for_metadata(_dashboard(attr))
        assert out == attr          # 값 복사 — 재정규화/보정 없음
        assert out is not attr      # 원본 dict 비공유(사본)

    def test_all_zero_is_preserved(self):
        # all-zero("유효 신호 없음")는 저장 대상 — dominant 파생 단계에서 None 처리
        attr = {k: 0 for k in (
            "technical_indicators", "news_sentiment", "fundamentals", "market_conditions")}
        out = extract_signal_attribution_for_metadata(_dashboard(attr))
        assert out["technical_indicators"] == 0
        assert out["strongest_bullish_signal"] is None

    def test_missing_or_invalid_weight_drops_whole_key(self):
        # all-or-nothing: 하나라도 숫자가 아니면 None
        for bad in ({"technical_indicators": None}, {"news_sentiment": "N/A"},
                    {"fundamentals": "abc"}, {}):
            attr = {"technical_indicators": 40, "news_sentiment": 25,
                    "fundamentals": 20, "market_conditions": 15, **bad}
            if not bad:
                attr.pop("market_conditions")
            assert extract_signal_attribution_for_metadata(_dashboard(attr)) is None

    def test_non_dict_inputs_fail_open(self):
        assert extract_signal_attribution_for_metadata(None) is None
        assert extract_signal_attribution_for_metadata({}) is None
        assert extract_signal_attribution_for_metadata({"signal_attribution": "text"}) is None
        assert extract_signal_attribution_for_metadata(_dashboard([1, 2])) is None

    def test_numeric_strings_accepted_as_stored_numbers(self):
        # 정규화기가 놓친 문자열 숫자("40")는 float로 변환해 저장(집계 파생 안전성)
        attr = {"technical_indicators": "40", "news_sentiment": 25.5,
                "fundamentals": 20, "market_conditions": 14.5}
        out = extract_signal_attribution_for_metadata(_dashboard(attr))
        assert out["technical_indicators"] == 40.0
