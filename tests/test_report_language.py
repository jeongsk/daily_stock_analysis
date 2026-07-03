# -*- coding: utf-8 -*-
"""Unit tests for report language helpers."""

import unittest
import unittest.mock
from types import SimpleNamespace
from unittest.mock import patch

from src.report_language import (
    detect_report_script_mismatch,
    get_bias_status_emoji,
    get_localized_stock_name,
    get_sentiment_label,
    get_signal_level,
    infer_decision_type_from_advice,
    localize_bias_status,
    localize_confidence_level,
    localize_operation_advice,
    localize_trend_prediction,
    resolve_report_language,
)


class ReportLanguageTestCase(unittest.TestCase):
    def test_get_signal_level_handles_compound_sell_advice(self) -> None:
        signal_text, emoji, signal_tag = get_signal_level("卖出/观望", 60, "zh")

        self.assertEqual(signal_text, "卖出")
        self.assertEqual(emoji, "🔴")
        self.assertEqual(signal_tag, "sell")

    def test_get_signal_level_handles_compound_buy_advice_in_english(self) -> None:
        signal_text, emoji, signal_tag = get_signal_level("Buy / Watch", 40, "en")

        self.assertEqual(signal_text, "Buy")
        self.assertEqual(emoji, "🟢")
        self.assertEqual(signal_tag, "buy")

    def test_get_signal_level_score_fallback_uses_canonical_scale(self) -> None:
        self.assertEqual(get_signal_level("", 28, "zh"), ("减仓", "🟠", "reduce"))
        self.assertEqual(get_signal_level("", 38, "zh"), ("减仓", "🟠", "reduce"))
        self.assertEqual(get_signal_level("", 42, "zh"), ("观望", "⚪", "watch"))
        self.assertEqual(get_signal_level("", 55, "zh"), ("观望", "⚪", "watch"))
        self.assertEqual(get_signal_level("", 60, "zh"), ("买入", "🟢", "buy"))
        self.assertEqual(get_signal_level("", 66, "zh"), ("买入", "🟢", "buy"))
        self.assertEqual(get_signal_level("", 72, "zh"), ("买入", "🟢", "buy"))

    def test_get_localized_stock_name_replaces_placeholder_for_english(self) -> None:
        with unittest.mock.patch("src.report_language.get_index_stock_name", return_value=None):
            self.assertEqual(
                get_localized_stock_name("股票AAPL", "AAPL", "en"),
                "Unnamed Stock",
            )

    def test_get_localized_stock_name_uses_korean_index_name_for_korean_report(self) -> None:
        def fake_index_name(code, language=None):
            if code == "005930.KS" and language == "ko":
                return "삼성전자"
            if code == "005930.KS" and language == "zh":
                return "三星电子"
            return None

        with unittest.mock.patch(
            "src.report_language.get_index_stock_name",
            side_effect=fake_index_name,
        ):
            self.assertEqual(
                get_localized_stock_name("三星电子", "005930.KS", "ko"),
                "삼성전자",
            )

    def test_get_localized_stock_name_preserves_custom_non_placeholder_name(self) -> None:
        def fake_index_name(code, language=None):
            if code == "005930.KS" and language == "ko":
                return "삼성전자"
            if code == "005930.KS" and language == "zh":
                return "三星电子"
            return None

        with unittest.mock.patch("src.report_language.get_index_stock_name", side_effect=fake_index_name):
            self.assertEqual(
                get_localized_stock_name("自定义名称", "005930.KS", "ko"),
                "自定义名称",
            )

    def test_get_localized_stock_name_replaces_wrong_chinese_name_for_korean_symbol(self) -> None:
        def fake_index_name(code, language=None):
            if code == "000660.KS" and language == "ko":
                return "SK하이닉스"
            if code == "000660.KS" and language == "zh":
                return "SK海力士"
            return None

        with unittest.mock.patch("src.report_language.get_index_stock_name", side_effect=fake_index_name):
            self.assertEqual(
                get_localized_stock_name("*ST南华", "000660.KS", "ko"),
                "SK하이닉스",
            )

    def test_get_sentiment_label_preserves_higher_band_thresholds(self) -> None:
        self.assertEqual(get_sentiment_label(80, "en"), "Very Bullish")
        self.assertEqual(get_sentiment_label(60, "en"), "Bullish")
        self.assertEqual(get_sentiment_label(40, "zh"), "中性")
        self.assertEqual(get_sentiment_label(20, "zh"), "悲观")

    def test_localize_trend_prediction_preserves_fine_grain_zh_states(self) -> None:
        self.assertEqual(localize_trend_prediction("多头排列", "zh"), "多头排列")
        self.assertEqual(localize_trend_prediction("弱势空头", "zh"), "弱势空头")

    def test_localize_trend_prediction_still_translates_english_input_for_zh(self) -> None:
        self.assertEqual(localize_trend_prediction("bullish", "zh"), "看多")
        self.assertEqual(localize_trend_prediction("very bearish", "zh"), "强烈看空")

    def test_bias_status_helpers_support_english_values(self) -> None:
        self.assertEqual(localize_bias_status("Safe", "en"), "Safe")
        self.assertEqual(localize_bias_status("警戒", "en"), "Caution")
        self.assertEqual(get_bias_status_emoji("Safe"), "✅")
        self.assertEqual(get_bias_status_emoji("Caution"), "⚠️")

    def test_infer_decision_type_from_advice_matches_chinese_phrases(self) -> None:
        self.assertEqual(infer_decision_type_from_advice("建议买入"), "buy")
        self.assertEqual(infer_decision_type_from_advice("建议持有"), "hold")
        self.assertEqual(infer_decision_type_from_advice("建议减仓"), "sell")
        self.assertEqual(infer_decision_type_from_advice("继续持有"), "hold")
        self.assertEqual(infer_decision_type_from_advice("建议洗盘观察"), "hold")
        self.assertEqual(infer_decision_type_from_advice("洗盘观察", default=""), "hold")
        self.assertEqual(infer_decision_type_from_advice("观察", default=""), "hold")
        self.assertEqual(infer_decision_type_from_advice("不建议买入"), "hold")
        self.assertEqual(
            infer_decision_type_from_advice("当前不跌破支撑位继续持有"),
            "hold",
        )
        self.assertEqual(
            infer_decision_type_from_advice("不破支撑后仍可持有"),
            "hold",
        )

    def test_localize_helpers_translate_korean(self) -> None:
        self.assertEqual(localize_operation_advice("hold", "ko"), "보유")
        self.assertEqual(localize_operation_advice("buy", "ko"), "매수")
        self.assertEqual(localize_operation_advice("sell", "ko"), "매도")
        self.assertEqual(localize_trend_prediction("sideways", "ko"), "횡보")
        self.assertEqual(localize_trend_prediction("bullish", "ko"), "상승 전망")
        self.assertEqual(localize_confidence_level("low", "ko"), "낮음")
        self.assertEqual(localize_confidence_level("medium", "ko"), "보통")

    def test_canonical_maps_recognize_korean_source(self) -> None:
        self.assertEqual(infer_decision_type_from_advice("매수"), "buy")
        self.assertEqual(infer_decision_type_from_advice("매도"), "sell")
        self.assertEqual(infer_decision_type_from_advice("보유"), "hold")
        self.assertEqual(infer_decision_type_from_advice("비중 축소"), "sell")
        signal_text, _emoji, signal_tag = get_signal_level("매수", 60, "ko")
        self.assertEqual(signal_tag, "buy")
        self.assertEqual(signal_text, "매수")
        self.assertEqual(localize_operation_advice("매수", "en"), "Buy")
        self.assertEqual(localize_trend_prediction("횡보", "en"), "Sideways")
        self.assertEqual(localize_confidence_level("높음", "en"), "High")

    def test_get_sentiment_label_korean(self) -> None:
        self.assertEqual(get_sentiment_label(80, "ko"), "매우 낙관")
        self.assertEqual(get_sentiment_label(60, "ko"), "낙관")
        self.assertEqual(get_sentiment_label(40, "ko"), "중립")
        self.assertEqual(get_sentiment_label(20, "ko"), "비관")

    # ------------------------------------------------------------------
    # resolve_report_language
    # ------------------------------------------------------------------

    @patch("src.config.get_config")
    def test_resolve_report_language_uses_config_attribute(self, mock_get_config):
        config = SimpleNamespace(report_language="ko")
        result = resolve_report_language(config)
        self.assertEqual(result, "ko")
        mock_get_config.assert_not_called()

    @patch("src.config.get_config")
    def test_resolve_report_language_falls_back_to_global_config(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(report_language="ko")
        result = resolve_report_language(None)
        self.assertEqual(result, "ko")
        mock_get_config.assert_called_once()

    @patch("src.config.get_config")
    def test_resolve_report_language_falls_back_to_global_when_missing(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(report_language="en")
        result = resolve_report_language(SimpleNamespace(other="value"))
        self.assertEqual(result, "en")
        mock_get_config.assert_called_once()

    @patch("src.config.get_config")
    def test_resolve_report_language_uses_default_when_both_unset(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(report_language=None)
        result = resolve_report_language(None)
        self.assertEqual(result, "zh")

    @patch("src.config.get_config")
    def test_resolve_report_language_invalid_config_attr_falls_back(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(report_language="en")
        result = resolve_report_language(SimpleNamespace(report_language="invalid_lang"))
        self.assertEqual(result, "en")
        mock_get_config.assert_called_once()

    # ------------------------------------------------------------------
    # detect_report_script_mismatch
    # ------------------------------------------------------------------

    def test_detect_script_mismatch_ko_with_hangul_passes(self):
        text = "오늘 시장은 반등했습니다. 거래대금이 증가했습니다."
        self.assertFalse(detect_report_script_mismatch("ko", text))

    def test_detect_script_mismatch_ko_with_chinese_fails(self):
        text = "今日市场反弹，成交额放大，市场情绪回暖。"
        self.assertTrue(detect_report_script_mismatch("ko", text))

    def test_detect_script_mismatch_zh_with_chinese_passes(self):
        text = "今日市场反弹，成交额放大，市场情绪回暖。"
        self.assertFalse(detect_report_script_mismatch("zh", text))

    def test_detect_script_mismatch_zh_with_hangul_fails(self):
        text = "오늘 시장은 반등했습니다. 거래대금이 증가했습니다."
        self.assertTrue(detect_report_script_mismatch("zh", text))

    def test_detect_script_mismatch_en_never_fails(self):
        self.assertFalse(detect_report_script_mismatch("en", "今日市场反弹"))
        self.assertFalse(detect_report_script_mismatch("en", "오늘 시장은 반등"))

    def test_detect_script_mismatch_short_text_returns_false(self):
        self.assertFalse(detect_report_script_mismatch("ko", "안녕"))

    def test_detect_script_mismatch_mixed_content_ko_with_some_hanzi(self):
        text = "오늘 삼성전자와 SK하이닉스가 상승을 주도했습니다."
        self.assertFalse(detect_report_script_mismatch("ko", text))

    def test_detect_script_mismatch_zh_with_some_hangul_loanwords_fails(self):
        text = "今日市场由三星电子和SK하이닉스领涨。"
        self.assertFalse(detect_report_script_mismatch("zh", text))


if __name__ == "__main__":
    unittest.main()
