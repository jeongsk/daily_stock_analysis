# -*- coding: utf-8 -*-
"""Unit tests for report language helpers."""

import unittest

from src.report_language import (
    get_bias_status_emoji,
    get_localized_stock_name,
    get_sentiment_label,
    get_signal_level,
    infer_decision_type_from_advice,
    localize_bias_status,
    localize_confidence_level,
    localize_operation_advice,
    localize_trend_prediction,
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

    def test_get_localized_stock_name_replaces_placeholder_for_english(self) -> None:
        self.assertEqual(
            get_localized_stock_name("股票AAPL", "AAPL", "en"),
            "Unnamed Stock",
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
        # 역방향 인식: LLM이 한국어로 출력해도 decision 분류/신호가 정상 동작해야 함
        self.assertEqual(infer_decision_type_from_advice("매수"), "buy")
        self.assertEqual(infer_decision_type_from_advice("매도"), "sell")
        self.assertEqual(infer_decision_type_from_advice("보유"), "hold")
        self.assertEqual(infer_decision_type_from_advice("비중 축소"), "sell")
        signal_text, _emoji, signal_tag = get_signal_level("매수", 60, "ko")
        self.assertEqual(signal_tag, "buy")
        self.assertEqual(signal_text, "매수")
        # 한국어 입력 -> 영어 출력 역방향 변환
        self.assertEqual(localize_operation_advice("매수", "en"), "Buy")
        self.assertEqual(localize_trend_prediction("횡보", "en"), "Sideways")
        self.assertEqual(localize_confidence_level("높음", "en"), "High")

    def test_get_sentiment_label_korean(self) -> None:
        self.assertEqual(get_sentiment_label(80, "ko"), "매우 낙관")
        self.assertEqual(get_sentiment_label(60, "ko"), "낙관")
        self.assertEqual(get_sentiment_label(40, "ko"), "중립")
        self.assertEqual(get_sentiment_label(20, "ko"), "비관")


if __name__ == "__main__":
    unittest.main()
