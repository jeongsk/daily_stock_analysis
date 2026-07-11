# -*- coding: utf-8 -*-
"""Phase 3: KRW 순매수 금액을 로케일 단위로 포맷하는 format_net_krw_localized 고정.

시장 수급 레코드는 KRW 원 단위(unit="KRW")이므로 억(1e8)/십억(1e9) 스케일로만
표기한다. 완전 오프라인 — `pytest -m "not network"` 차단 게이트에 포함된다.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.report_language import format_net_krw_localized


class TestFormatNetKrwLocalized:
    def test_ko_uses_eok(self):
        # -3.228e11 원 -> -3,228억 (스펙 §4 예시)
        assert format_net_krw_localized(-322800000000, "ko") == "-3,228억"
        assert format_net_krw_localized(1131400000000, "ko") == "+11,314억"
        assert format_net_krw_localized(51200000000, "ko") == "+512억"

    def test_zh_uses_yi_won(self):
        assert format_net_krw_localized(-322800000000, "zh") == "-3,228亿韩元"
        assert format_net_krw_localized(1131400000000, "zh") == "+11,314亿韩元"

    def test_en_uses_won_billions(self):
        assert format_net_krw_localized(-322800000000, "en") == "₩-322.8B"
        assert format_net_krw_localized(1131400000000, "en") == "₩+1,131.4B"

    def test_invalid_is_na(self):
        assert format_net_krw_localized(None, "ko") == "N/A"
        assert format_net_krw_localized("x", "en") == "N/A"
        assert format_net_krw_localized(float("nan"), "zh") == "N/A"

    def test_none_language_defaults_to_zh(self):
        assert format_net_krw_localized(51200000000, None) == "+512亿韩元"
