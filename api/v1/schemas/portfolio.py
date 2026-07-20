# -*- coding: utf-8 -*-
"""Portfolio API schemas."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class PortfolioAccountCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    broker: Optional[str] = Field(None, max_length=64)
    market: Literal["cn", "hk", "us", "jp", "kr", "tw"] = "cn"
    base_currency: str = Field("CNY", min_length=3, max_length=8)
    owner_id: Optional[str] = Field(None, max_length=64)


class PortfolioAccountUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    broker: Optional[str] = Field(None, max_length=64)
    market: Optional[Literal["cn", "hk", "us", "jp", "kr", "tw"]] = None
    base_currency: Optional[str] = Field(None, min_length=3, max_length=8)
    owner_id: Optional[str] = Field(None, max_length=64)
    is_active: Optional[bool] = None


class PortfolioAccountItem(BaseModel):
    id: int
    owner_id: Optional[str] = None
    name: str
    broker: Optional[str] = None
    market: str
    base_currency: str
    is_active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PortfolioAccountListResponse(BaseModel):
    accounts: List[PortfolioAccountItem] = Field(default_factory=list)


class PortfolioTradeCreateRequest(BaseModel):
    account_id: int
    symbol: str = Field(..., min_length=1, max_length=16)
    trade_date: date
    side: Literal["buy", "sell"]
    quantity: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    fee: float = Field(0.0, ge=0)
    tax: float = Field(0.0, ge=0)
    market: Optional[Literal["cn", "hk", "us", "jp", "kr", "tw"]] = None
    currency: Optional[str] = Field(None, min_length=3, max_length=8)
    trade_uid: Optional[str] = Field(None, max_length=128)
    note: Optional[str] = Field(None, max_length=255)


class PortfolioCashLedgerCreateRequest(BaseModel):
    account_id: int
    event_date: date
    direction: Literal["in", "out"]
    amount: float = Field(..., gt=0)
    currency: Optional[str] = Field(None, min_length=3, max_length=8)
    note: Optional[str] = Field(None, max_length=255)


class PortfolioCorporateActionCreateRequest(BaseModel):
    account_id: int
    symbol: str = Field(..., min_length=1, max_length=16)
    effective_date: date
    action_type: Literal["cash_dividend", "split_adjustment"]
    market: Optional[Literal["cn", "hk", "us", "jp", "kr", "tw"]] = None
    currency: Optional[str] = Field(None, min_length=3, max_length=8)
    cash_dividend_per_share: Optional[float] = Field(None, ge=0)
    split_ratio: Optional[float] = Field(None, gt=0)
    note: Optional[str] = Field(None, max_length=255)


class PortfolioEventCreatedResponse(BaseModel):
    id: int


class PortfolioDeleteResponse(BaseModel):
    deleted: int


class PortfolioTradeListItem(BaseModel):
    id: int
    account_id: int
    trade_uid: Optional[str] = None
    symbol: str
    market: str
    currency: str
    trade_date: str
    side: str
    quantity: float
    price: float
    fee: float
    tax: float
    note: Optional[str] = None
    created_at: Optional[str] = None


class PortfolioTradeListResponse(BaseModel):
    items: List[PortfolioTradeListItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class PortfolioCashLedgerListItem(BaseModel):
    id: int
    account_id: int
    event_date: str
    direction: str
    amount: float
    currency: str
    note: Optional[str] = None
    created_at: Optional[str] = None


class PortfolioCashLedgerListResponse(BaseModel):
    items: List[PortfolioCashLedgerListItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class PortfolioCorporateActionListItem(BaseModel):
    id: int
    account_id: int
    symbol: str
    market: str
    currency: str
    effective_date: str
    action_type: str
    cash_dividend_per_share: Optional[float] = None
    split_ratio: Optional[float] = None
    note: Optional[str] = None
    created_at: Optional[str] = None


class PortfolioCorporateActionListResponse(BaseModel):
    items: List[PortfolioCorporateActionListItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class PortfolioPositionItem(BaseModel):
    symbol: str
    market: str
    currency: str
    quantity: float
    avg_cost: float
    total_cost: float
    last_price: float
    market_value_base: float
    unrealized_pnl_base: float
    unrealized_pnl_pct: Optional[float] = None
    valuation_currency: str
    price_source: str = "unknown"
    price_provider: Optional[str] = None
    price_date: Optional[str] = None
    price_stale: bool = False
    price_available: bool = True
    data_quality: str = "ok"
    limitations: List[str] = Field(default_factory=list)


class PortfolioPositionAnalysisRequest(BaseModel):
    account_id: Optional[int] = Field(None, description="Optional account id; required when a symbol is held in multiple accounts")
    analysis_phase: Literal["auto", "premarket", "intraday", "postmarket"] = "auto"
    force: bool = Field(False, description="Force refresh analysis inputs without bypassing duplicate in-flight tasks")
    report_language: Optional[Literal["zh", "en", "ko"]] = Field(None, description="Optional report language override for generated analysis output")


class PortfolioAccountSnapshot(BaseModel):
    account_id: int
    account_name: str
    owner_id: Optional[str] = None
    broker: Optional[str] = None
    market: str
    base_currency: str
    as_of: str
    cost_method: str
    total_cash: float
    total_market_value: float
    total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    fee_total: float
    tax_total: float
    fx_stale: bool
    data_quality: str = "ok"
    limitations: List[str] = Field(default_factory=list)
    positions: List[PortfolioPositionItem] = Field(default_factory=list)


class PortfolioSnapshotResponse(BaseModel):
    as_of: str
    cost_method: str
    currency: str
    account_count: int
    total_cash: float
    total_market_value: float
    total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    fee_total: float
    tax_total: float
    fx_stale: bool
    data_quality: str = "ok"
    limitations: List[str] = Field(default_factory=list)
    accounts: List[PortfolioAccountSnapshot] = Field(default_factory=list)


class PortfolioImportTradeItem(BaseModel):
    trade_date: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    price: float
    fee: float
    tax: float
    trade_uid: Optional[str] = None
    dedup_hash: str
    currency: Optional[str] = None


class PortfolioImportParseResponse(BaseModel):
    broker: str
    record_count: int
    skipped_count: int
    error_count: int
    records: List[PortfolioImportTradeItem] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class PortfolioImportCommitResponse(BaseModel):
    account_id: int
    record_count: int
    inserted_count: int
    duplicate_count: int
    failed_count: int
    dry_run: bool
    errors: List[str] = Field(default_factory=list)


class PortfolioImportBrokerItem(BaseModel):
    broker: str
    aliases: List[str] = Field(default_factory=list)
    display_name: Optional[str] = None


class PortfolioImportBrokerListResponse(BaseModel):
    brokers: List[PortfolioImportBrokerItem] = Field(default_factory=list)


class PortfolioFxRefreshResponse(BaseModel):
    as_of: str
    account_count: int
    refresh_enabled: bool
    disabled_reason: Optional[str] = None
    pair_count: int
    updated_count: int
    stale_count: int
    error_count: int


class PortfolioBrokerLinkTossRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=64, description="Optional new-account display name")
    account_seq: Optional[str] = Field(
        None,
        max_length=32,
        description="Toss accountSeq; required only when the credential has more than one brokerage account",
    )
    owner_id: Optional[str] = Field(None, max_length=64)


class PortfolioBrokerLinkCreatedResponse(BaseModel):
    account_id: int
    account_name: str
    provider: str
    external_account_seq: str
    external_account_no: Optional[str] = None
    snapshot_at: str
    imported: int
    skipped_duplicates: int
    reactivated: bool = False


class PortfolioBrokerLinkItem(BaseModel):
    account_id: int
    account_name: Optional[str] = None
    provider: str
    external_account_seq: str
    external_account_no: Optional[str] = None
    linked_at: Optional[str] = None
    last_synced_at: Optional[str] = None
    last_reconciled_at: Optional[str] = None


class PortfolioBrokerLinkListResponse(BaseModel):
    links: List[PortfolioBrokerLinkItem] = Field(default_factory=list)


class PortfolioBrokerDriftItem(BaseModel):
    type: Literal["quantity_mismatch"] = "quantity_mismatch"
    symbol: str
    ledger_qty: float
    broker_qty: float
    diff: float


class PortfolioBrokerFailedItem(BaseModel):
    type: Literal["missing_average_price", "oversell", "malformed_order"]
    symbol: Optional[str] = None
    order_id: Optional[str] = None
    filled_at: Optional[str] = None
    requested_quantity: Optional[float] = None
    available_quantity: Optional[float] = None
    reason: Optional[str] = None


class PortfolioBrokerSyncResponse(BaseModel):
    account_id: int
    imported: int
    skipped_duplicates: int
    failed: List[PortfolioBrokerFailedItem] = Field(default_factory=list)
    drift: List[PortfolioBrokerDriftItem] = Field(default_factory=list)
    last_synced_at: str
    last_reconciled_at: str


class PortfolioDecisionSignalRiskItem(BaseModel):
    account_id: Optional[int] = None
    symbol: str
    market: str
    signal: Dict[str, Any] = Field(default_factory=dict)


class PortfolioDecisionSignalRiskBlock(BaseModel):
    available: bool = True
    total: int = 0
    actions: Dict[str, int] = Field(default_factory=dict)
    items: List[PortfolioDecisionSignalRiskItem] = Field(default_factory=list)


class PortfolioRiskResponse(BaseModel):
    as_of: str
    account_id: Optional[int] = None
    cost_method: str
    currency: str
    thresholds: Dict[str, Any] = Field(default_factory=dict)
    concentration: Dict[str, Any] = Field(default_factory=dict)
    sector_concentration: Dict[str, Any] = Field(default_factory=dict)
    drawdown: Dict[str, Any] = Field(default_factory=dict)
    stop_loss: Dict[str, Any] = Field(default_factory=dict)
    decision_signal_risk: PortfolioDecisionSignalRiskBlock = Field(default_factory=PortfolioDecisionSignalRiskBlock)


# ----------------------------------------------------------------------
# Manual-approval order proposals (Phase 3 — Toss Invest). Two-step flow:
# create a proposal (validated, TTL'd), then a *separate* execute call with
# confirm=true (required even in dry-run) actually places (or dry-run
# simulates) the order. ``status`` (v2 state machine): pending -> executing ->
# executed | failed | outcome_unknown; pending -> canceled | expired |
# dry_run_executed. An 'executing'/'outcome_unknown' proposal must be resolved
# via POST .../proposals/{uuid}/reconcile before it can be canceled or
# re-executed. See
# docs/superpowers/specs/2026-07-17-toss-order-phase3-design.md.
# ----------------------------------------------------------------------


class PortfolioOrderProposalCreateRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16, description="KR 6-digit code/.KS/.KQ or US ticker")
    side: Literal["buy", "sell"]
    order_type: Literal["LIMIT", "MARKET"] = Field(
        "LIMIT", description="MARKET requires TOSS_ORDER_ALLOW_MARKET=true"
    )
    quantity: float = Field(..., gt=0)
    price: Optional[float] = Field(None, gt=0, description="Required for LIMIT, forbidden for MARKET")


class PortfolioOrderProposalItem(BaseModel):
    proposal_uuid: str
    account_id: int
    symbol: str
    storage_symbol: str
    market: str
    currency: str
    side: str
    order_type: str
    price: Optional[float] = None
    quantity: float
    est_amount_krw: float
    status: str
    toss_order_id: Optional[str] = None
    created_at: str
    expires_at: str
    executed_at: Optional[str] = None
    generation_source: str = Field(
        "manual", description="'manual' (human-created) or 'auto' (Phase 5 defensive-signal batch generator)"
    )
    source_signal_id: Optional[int] = Field(
        None, description="DecisionSignalRecord.id that produced this proposal; null for manual proposals"
    )
    mode: Optional[str] = Field(
        None, description="'dry_run' or 'live' preview/outcome; absent for list/cancel responses"
    )


class PortfolioOrderProposalListResponse(BaseModel):
    proposals: List[PortfolioOrderProposalItem] = Field(default_factory=list)


class PortfolioOrderExecuteRequest(BaseModel):
    confirm: bool = Field(..., description="Must be true — required even when the outcome will be dry-run")


class PortfolioOrderCancelResponse(BaseModel):
    toss_order_id: str
    canceled: bool


class PortfolioOrderExecutionItem(BaseModel):
    filledQuantity: Optional[str] = None
    averageFilledPrice: Optional[str] = None
    filledAmount: Optional[str] = None
    commission: Optional[str] = None
    tax: Optional[str] = None
    filledAt: Optional[str] = None
    settlementDate: Optional[str] = None


class PortfolioOrderStatusResponse(BaseModel):
    """Passthrough of Toss's own ``Order`` schema (``GET /orders/{orderId}``) —
    field names are kept camelCase to match the upstream payload exactly."""

    orderId: str
    symbol: str
    side: str
    orderType: str
    timeInForce: Optional[str] = None
    status: str
    price: Optional[str] = None
    quantity: str
    orderAmount: Optional[str] = None
    currency: str
    orderedAt: str
    canceledAt: Optional[str] = None
    execution: Optional[PortfolioOrderExecutionItem] = None


# ----------------------------------------------------------------------
# Server-side conditional-order proposals (Phase 4 — Toss Invest). Same
# two-step shape as the plain-order schemas above, but "approve" registers
# a Toss-side SINGLE/STOP conditional order that Toss auto-executes once
# triggered, with no further confirmation step. Local state machine:
# pending -> approving -> approved | registration_failed |
# registration_unknown; pending -> canceled | expired | dry_run_approved;
# approved -> triggered_completed | toss_expired | toss_canceled | paused;
# registration_unknown -> approved | registration_failed (reconcile). See
# docs/superpowers/specs/2026-07-19-toss-conditional-order-phase4-design.md.
#
# There is deliberately no `type`/`condition_type` field — this system only
# ever creates SINGLE + STOP conditional orders (design spec §3 "타입
# 스코프"); OCO/OTO/PROFIT_RATE are not representable at all. `extra="forbid"`
# additionally rejects any unrecognized field (e.g. a client attempting to
# smuggle in `type: "OCO"`) with a 422, matching the design spec §5 "스키마
# 레벨에서 422 거부(enum 미포함)" edge-case contract.
# ----------------------------------------------------------------------


class PortfolioConditionalOrderProposalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=16, description="KR 6-digit code/.KS/.KQ or US ticker")
    side: Literal["buy", "sell"]
    trigger_price: float = Field(..., gt=0, description="STOP watch price")
    limit_price: float = Field(..., gt=0, description="LIMIT leg order price (the only leg type supported)")
    quantity: float = Field(..., gt=0)
    expire_date: date = Field(..., description="Conditional-order expiry (<= today+7 days KST)")


class PortfolioConditionalOrderProposalItem(BaseModel):
    proposal_uuid: str
    account_id: int
    symbol: str
    storage_symbol: str
    market: str
    currency: str
    side: str
    trigger_price: float
    limit_price: float
    quantity: float
    est_amount_krw: float
    expire_date: str
    status: str
    toss_status: Optional[str] = None
    toss_conditional_order_id: Optional[str] = None
    created_at: str
    expires_at: str
    approved_at: Optional[str] = None
    generation_source: str = Field(
        "manual", description="'manual' (human-created) or 'auto' (Phase 5 defensive-signal batch generator)"
    )
    source_signal_id: Optional[int] = Field(
        None, description="DecisionSignalRecord.id that produced this proposal; null for manual proposals"
    )
    mode: Optional[str] = Field(
        None, description="'dry_run' or 'live' preview/outcome; absent for list/cancel responses"
    )


class PortfolioConditionalOrderProposalListResponse(BaseModel):
    proposals: List[PortfolioConditionalOrderProposalItem] = Field(default_factory=list)


class PortfolioConditionalOrderApproveRequest(BaseModel):
    confirm: bool = Field(
        ...,
        description=(
            "Must be true — required even when the outcome will be dry-run. Approving registers a "
            "Toss-side conditional order that auto-executes once triggered, with no further "
            "confirmation step."
        ),
    )


class PortfolioConditionalOrderSyncResponse(BaseModel):
    checked: int
    updated: int


class PortfolioConditionalOrderForceResolveRequest(BaseModel):
    """Manual escape hatch for a proposal permanently stuck
    ``registration_unknown`` (design spec §7 / Codex BLOCK review major 3) —
    OPERATOR-ONLY: use only after independently confirming on the Toss
    app/API that no matching conditional order actually exists. Calling
    this while an order is genuinely live on Toss would release the
    daily-cap reservation for an order that can still auto-execute."""

    model_config = ConfigDict(extra="forbid")

    confirm: bool = Field(..., description="Must be true.")
    reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Required: record why the operator confirmed on Toss that no matching order exists.",
    )
