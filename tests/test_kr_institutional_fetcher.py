# -*- coding: utf-8 -*-
"""KrInstitutionalFetcher 오프라인 테스트.

픽스처는 전부 2026-07-10 실제 응답 캡처다(삼성전자 005930 / KOSPI).
네트워크 접근 없음 — `pytest -m "not network"` 게이트에 포함된다.
"""

from data_provider.kr_institutional_fetcher import (
    _clamp_days,
    _date_from_daum,
    _date_from_dot_yy,
    _date_from_yyyymmdd,
    _to_int,
)


class TestPureHelpers:
    def test_to_int_parses_signed_comma_strings(self):
        # 네이버 integration 값 형식: 부호 접두 + 콤마
        assert _to_int("+625,985") == 625985
        assert _to_int("-2,851,466") == -2851466
        assert _to_int("11,314") == 11314
        assert _to_int("0") == 0
        # 다음 JSON은 네이티브 숫자
        assert _to_int(635576) == 635576
        assert _to_int(-6170033) == -6170033
        assert _to_int(285000.0) == 285000

    def test_to_int_rejects_blanks_and_garbage(self):
        assert _to_int(None) is None
        assert _to_int("") is None
        assert _to_int("-") is None
        assert _to_int("--") is None
        assert _to_int("—") is None
        assert _to_int("46.58%") is None
        assert _to_int("날짜") is None

    def test_clamp_days(self):
        assert _clamp_days(5) == 5
        assert _clamp_days(1) == 1
        assert _clamp_days(0) == 1
        assert _clamp_days(-3) == 1
        assert _clamp_days(999) == 30
        assert _clamp_days("7") == 7
        assert _clamp_days("abc") == 5
        assert _clamp_days(None) == 5

    def test_date_normalizers(self):
        assert _date_from_yyyymmdd("20260710") == "2026-07-10"
        assert _date_from_yyyymmdd("2026-07-10") is None
        assert _date_from_yyyymmdd("") is None
        assert _date_from_yyyymmdd(None) is None

        assert _date_from_daum("2026-07-10 00:00:00") == "2026-07-10"
        assert _date_from_daum("2026-07-10") == "2026-07-10"
        assert _date_from_daum("20260710") is None
        assert _date_from_daum(None) is None

        assert _date_from_dot_yy("26.07.10") == "2026-07-10"
        assert _date_from_dot_yy("날짜") is None
        assert _date_from_dot_yy("") is None
        assert _date_from_dot_yy(None) is None
