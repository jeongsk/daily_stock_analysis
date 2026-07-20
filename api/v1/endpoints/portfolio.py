# -*- coding: utf-8 -*-
"""Portfolio endpoints (P0 core account + snapshot workflow)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from api.v1.errors import api_error
from api.v1.schemas.analysis import DuplicateTaskErrorResponse, TaskAccepted
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.portfolio import (
    PortfolioAccountCreateRequest,
    PortfolioAccountItem,
    PortfolioAccountListResponse,
    PortfolioAccountUpdateRequest,
    PortfolioBrokerLinkCreatedResponse,
    PortfolioBrokerLinkItem,
    PortfolioBrokerLinkListResponse,
    PortfolioBrokerLinkTossRequest,
    PortfolioBrokerSyncResponse,
    PortfolioCashLedgerListResponse,
    PortfolioCashLedgerCreateRequest,
    PortfolioConditionalOrderApproveRequest,
    PortfolioConditionalOrderForceResolveRequest,
    PortfolioConditionalOrderProposalCreateRequest,
    PortfolioConditionalOrderProposalItem,
    PortfolioConditionalOrderProposalListResponse,
    PortfolioConditionalOrderSyncResponse,
    PortfolioCorporateActionListResponse,
    PortfolioCorporateActionCreateRequest,
    PortfolioDeleteResponse,
    PortfolioEventCreatedResponse,
    PortfolioFxRefreshResponse,
    PortfolioImportBrokerListResponse,
    PortfolioImportCommitResponse,
    PortfolioImportParseResponse,
    PortfolioImportTradeItem,
    PortfolioOrderCancelResponse,
    PortfolioOrderExecuteRequest,
    PortfolioOrderProposalCreateRequest,
    PortfolioOrderProposalItem,
    PortfolioOrderProposalListResponse,
    PortfolioOrderStatusResponse,
    PortfolioPositionAnalysisRequest,
    PortfolioRiskResponse,
    PortfolioSnapshotResponse,
    PortfolioTradeListResponse,
    PortfolioTradeCreateRequest,
)
from data_provider.toss_fetcher import TossOrderRejectedError
from src.services.task_queue import get_task_queue
from src.services.portfolio_broker_sync_service import (
    AmbiguousBrokerAccountError,
    BrokerLinkConflictError,
    BrokerLinkNotFoundError,
    PortfolioBrokerSyncService,
    TossNotConfiguredError,
    TossUpstreamError,
)
from src.services.portfolio_conditional_order_service import (
    ConditionalApprovalInProgressError,
    ConditionalProposalInProgressError,
    ConditionalProposalNotApprovableError,
    ConditionalProposalNotCancelableError,
    ConditionalProposalNotForceResolvableError,
    ConditionalProposalNotFoundError,
    ConditionalProposalNotReconcilableError,
    ExpireDateTooFarError,
    PortfolioConditionalOrderService,
)
from src.services.portfolio_import_service import PortfolioImportService
from src.services.portfolio_order_service import (
    ConfirmRequiredError,
    FxRateUnavailableError,
    HighValueOrderRejectedError,
    InsufficientBuyingPowerError,
    InsufficientSellableQuantityError,
    OrderAuditPersistFailedError,
    OrderIdempotencyConflictError,
    OrderLimitExceededError,
    OrderNotFoundError,
    OrderTypeNotAllowedError,
    PendingProposalLimitExceededError,
    PortfolioOrderService,
    ProposalInProgressError,
    ProposalNotExecutableError,
    ProposalNotFoundError,
    ProposalNotReconcilableError,
    ReferencePriceUnavailableError,
)
from src.services.portfolio_risk_service import PortfolioRiskService
from src.services.portfolio_service import (
    PortfolioBusyError,
    PortfolioConflictError,
    PortfolioOversellError,
    PortfolioService,
)
from src.auth import COOKIE_NAME, is_auth_enabled, verify_session

logger = logging.getLogger(__name__)

router = APIRouter()


def _bad_request(exc: Exception) -> HTTPException:
    return api_error(400, "validation_error", str(exc))


# Phase 5 additive filter on both proposal-list endpoints (design spec §3
# "출처 메타"). Codex review minor 2: an unrecognized value must be a loud
# 422, not silently treated as "no results" the way an unmatched status
# string effectively is today.
_VALID_GENERATION_SOURCES = {"manual", "auto"}


def _validate_generation_source_filter(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if value not in _VALID_GENERATION_SOURCES:
        raise api_error(
            422,
            "invalid_generation_source",
            f"generation_source must be one of {sorted(_VALID_GENERATION_SOURCES)}, got {value!r}",
        )
    return value


# Toss order-error codes that mean "conflicting/duplicate request" rather than
# a business-rule rejection — mapped to 409 instead of 422 so a client can
# tell "retry/inspect state" apart from "this order itself is invalid".
# idempotency-key-conflict is included per design spec §3: it is a defect
# (the client reused a clientOrderId with a different body), not a generic
# 500 to be swallowed.
_TOSS_ORDER_CONFLICT_CODES = {
    "request-in-progress",
    "already-filled",
    "already-canceled",
    "already-modified",
    "already-rejected",
    "already-processing",
    "idempotency-key-conflict",
}


def _map_toss_order_error(exc: TossOrderRejectedError) -> HTTPException:
    """Map one Toss order error code to its own distinguishable API error —
    design spec §2 "코드별로 명확한 4xx로 전달 — 뭉개기 금지": no code is ever
    collapsed into a generic message."""
    if exc.code in _TOSS_ORDER_CONFLICT_CODES:
        status_code = 409
    elif exc.status_code in (400, 404, 422, 429):
        status_code = exc.status_code
    else:
        status_code = 502
    return api_error(status_code, f"toss-{exc.code}", exc.message or str(exc), detail=exc.data or None)


# ----------------------------------------------------------------------
# Order-write auth (design spec v2 §3, Codex blocker 1): every order-write
# endpoint below — proposal create/execute/cancel, placed-order cancel,
# reconcile, dry-run included — requires ADMIN_AUTH_ENABLED=true *and* a
# verified session, independent of whether the global AuthMiddleware happens
# to be enforcing auth on /api/v1/* as a whole. Auth being disabled must
# itself be a 403 here, not an open door: unlike every other portfolio
# endpoint, "nobody is authenticated" cannot mean "everyone may place a real
# money order".
#
# v3 auth clarification (design spec §3 "인증 (필수, v3 명확화)", reviewer
# major 2): this is a single-shared-admin system with no per-session user
# identity — a session that passes this check manages *every* account, full
# stop. There is deliberately no additional caller-identity gate (a
# self-asserted request header compared against an account's own owner_id):
# an unverified header is not access control, so this module never accepts
# or checks one.
# ----------------------------------------------------------------------


def _require_order_auth(request: Request) -> None:
    if not is_auth_enabled():
        raise api_error(
            403,
            "order-auth-required",
            "Order endpoints require ADMIN_AUTH_ENABLED=true and a verified session",
        )
    cookie_val = request.cookies.get(COOKIE_NAME)
    if not cookie_val or not verify_session(cookie_val):
        raise api_error(403, "order-auth-required", "Order endpoints require a verified session")


def _internal_error(message: str, exc: Exception) -> HTTPException:
    logger.error(f"{message}: {exc}", exc_info=True)
    return api_error(500, "internal_error", f"{message}: {str(exc)}")


def _conflict_error(*, error: str, message: str) -> HTTPException:
    return api_error(409, error, message)


def _serialize_import_record(item: dict) -> PortfolioImportTradeItem:
    payload = dict(item)
    trade_date = payload.get("trade_date")
    if isinstance(trade_date, date):
        payload["trade_date"] = trade_date.isoformat()
    else:
        payload["trade_date"] = str(trade_date)
    return PortfolioImportTradeItem(**payload)


@router.post(
    "/accounts",
    response_model=PortfolioAccountItem,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Create portfolio account",
)
def create_account(request: PortfolioAccountCreateRequest) -> PortfolioAccountItem:
    service = PortfolioService()
    try:
        row = service.create_account(
            name=request.name,
            broker=request.broker,
            market=request.market,
            base_currency=request.base_currency,
            owner_id=request.owner_id,
        )
        return PortfolioAccountItem(**row)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create account failed", exc)


@router.get(
    "/accounts",
    response_model=PortfolioAccountListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="List portfolio accounts",
)
def list_accounts(
    include_inactive: bool = Query(False, description="Whether to include inactive accounts"),
) -> PortfolioAccountListResponse:
    service = PortfolioService()
    try:
        rows = service.list_accounts(include_inactive=include_inactive)
        return PortfolioAccountListResponse(accounts=[PortfolioAccountItem(**item) for item in rows])
    except Exception as exc:
        raise _internal_error("List accounts failed", exc)


@router.put(
    "/accounts/{account_id}",
    response_model=PortfolioAccountItem,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Update portfolio account",
)
def update_account(account_id: int, request: PortfolioAccountUpdateRequest) -> PortfolioAccountItem:
    service = PortfolioService()
    try:
        updated = service.update_account(
            account_id,
            name=request.name,
            broker=request.broker,
            market=request.market,
            base_currency=request.base_currency,
            owner_id=request.owner_id,
            is_active=request.is_active,
        )
        if updated is None:
            raise api_error(404, "not_found", f"Account not found: {account_id}")
        return PortfolioAccountItem(**updated)
    except HTTPException:
        raise
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Update account failed", exc)


@router.delete(
    "/accounts/{account_id}",
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Deactivate portfolio account",
)
def delete_account(account_id: int):
    service = PortfolioService()
    try:
        ok = service.deactivate_account(account_id)
        if not ok:
            raise api_error(404, "not_found", f"Account not found: {account_id}")
        return {"deleted": 1}
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Deactivate account failed", exc)


@router.post(
    "/trades",
    response_model=PortfolioEventCreatedResponse,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Record trade event",
)
def create_trade(request: PortfolioTradeCreateRequest) -> PortfolioEventCreatedResponse:
    service = PortfolioService()
    try:
        data = service.record_trade(
            account_id=request.account_id,
            symbol=request.symbol,
            trade_date=request.trade_date,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            fee=request.fee,
            tax=request.tax,
            market=request.market,
            currency=request.currency,
            trade_uid=request.trade_uid,
            note=request.note,
        )
        return PortfolioEventCreatedResponse(**data)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except PortfolioOversellError as exc:
        raise _conflict_error(error="portfolio_oversell", message=str(exc))
    except PortfolioConflictError as exc:
        raise _conflict_error(error="conflict", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create trade failed", exc)


@router.get(
    "/trades",
    response_model=PortfolioTradeListResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="List trade events",
)
def list_trades(
    account_id: Optional[int] = Query(None, description="Optional account id"),
    date_from: Optional[date] = Query(None, description="Trade date from"),
    date_to: Optional[date] = Query(None, description="Trade date to"),
    symbol: Optional[str] = Query(None, description="Optional stock symbol filter"),
    side: Optional[str] = Query(None, description="Optional side filter: buy/sell"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PortfolioTradeListResponse:
    service = PortfolioService()
    try:
        data = service.list_trade_events(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            symbol=symbol,
            side=side,
            page=page,
            page_size=page_size,
        )
        return PortfolioTradeListResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("List trade events failed", exc)


@router.delete(
    "/trades/{trade_id}",
    response_model=PortfolioDeleteResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Delete trade event",
)
def delete_trade(trade_id: int) -> PortfolioDeleteResponse:
    service = PortfolioService()
    try:
        ok = service.delete_trade_event(trade_id)
        if not ok:
            raise api_error(404, "not_found", f"Trade not found: {trade_id}")
        return PortfolioDeleteResponse(deleted=1)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Delete trade event failed", exc)


@router.post(
    "/cash-ledger",
    response_model=PortfolioEventCreatedResponse,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Record cash event",
)
def create_cash_ledger(request: PortfolioCashLedgerCreateRequest) -> PortfolioEventCreatedResponse:
    service = PortfolioService()
    try:
        data = service.record_cash_ledger(
            account_id=request.account_id,
            event_date=request.event_date,
            direction=request.direction,
            amount=request.amount,
            currency=request.currency,
            note=request.note,
        )
        return PortfolioEventCreatedResponse(**data)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create cash ledger event failed", exc)


@router.get(
    "/cash-ledger",
    response_model=PortfolioCashLedgerListResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="List cash ledger events",
)
def list_cash_ledger(
    account_id: Optional[int] = Query(None, description="Optional account id"),
    date_from: Optional[date] = Query(None, description="Cash event date from"),
    date_to: Optional[date] = Query(None, description="Cash event date to"),
    direction: Optional[str] = Query(None, description="Optional direction filter: in/out"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PortfolioCashLedgerListResponse:
    service = PortfolioService()
    try:
        data = service.list_cash_ledger_events(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            direction=direction,
            page=page,
            page_size=page_size,
        )
        return PortfolioCashLedgerListResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("List cash ledger events failed", exc)


@router.delete(
    "/cash-ledger/{entry_id}",
    response_model=PortfolioDeleteResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Delete cash ledger event",
)
def delete_cash_ledger(entry_id: int) -> PortfolioDeleteResponse:
    service = PortfolioService()
    try:
        ok = service.delete_cash_ledger_event(entry_id)
        if not ok:
            raise api_error(404, "not_found", f"Cash ledger entry not found: {entry_id}")
        return PortfolioDeleteResponse(deleted=1)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Delete cash ledger event failed", exc)


@router.post(
    "/corporate-actions",
    response_model=PortfolioEventCreatedResponse,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Record corporate action event",
)
def create_corporate_action(request: PortfolioCorporateActionCreateRequest) -> PortfolioEventCreatedResponse:
    service = PortfolioService()
    try:
        data = service.record_corporate_action(
            account_id=request.account_id,
            symbol=request.symbol,
            effective_date=request.effective_date,
            action_type=request.action_type,
            market=request.market,
            currency=request.currency,
            cash_dividend_per_share=request.cash_dividend_per_share,
            split_ratio=request.split_ratio,
            note=request.note,
        )
        return PortfolioEventCreatedResponse(**data)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create corporate action event failed", exc)


@router.get(
    "/corporate-actions",
    response_model=PortfolioCorporateActionListResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="List corporate action events",
)
def list_corporate_actions(
    account_id: Optional[int] = Query(None, description="Optional account id"),
    date_from: Optional[date] = Query(None, description="Corporate action effective date from"),
    date_to: Optional[date] = Query(None, description="Corporate action effective date to"),
    symbol: Optional[str] = Query(None, description="Optional stock symbol filter"),
    action_type: Optional[str] = Query(None, description="Optional action type filter"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PortfolioCorporateActionListResponse:
    service = PortfolioService()
    try:
        data = service.list_corporate_action_events(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            symbol=symbol,
            action_type=action_type,
            page=page,
            page_size=page_size,
        )
        return PortfolioCorporateActionListResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("List corporate action events failed", exc)


@router.delete(
    "/corporate-actions/{action_id}",
    response_model=PortfolioDeleteResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Delete corporate action event",
)
def delete_corporate_action(action_id: int) -> PortfolioDeleteResponse:
    service = PortfolioService()
    try:
        ok = service.delete_corporate_action_event(action_id)
        if not ok:
            raise api_error(404, "not_found", f"Corporate action not found: {action_id}")
        return PortfolioDeleteResponse(deleted=1)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Delete corporate action event failed", exc)


@router.get(
    "/snapshot",
    response_model=PortfolioSnapshotResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Get portfolio snapshot",
)
def get_snapshot(
    account_id: Optional[int] = Query(None, description="Optional account id, default returns all accounts"),
    as_of: Optional[date] = Query(None, description="Snapshot date, default today"),
    cost_method: str = Query("fifo", description="Cost method: fifo or avg"),
    include_realtime: bool = Query(
        True,
        description="Whether today's snapshot should try realtime quotes before historical close fallback",
    ),
) -> PortfolioSnapshotResponse:
    service = PortfolioService()
    try:
        data = service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of,
            cost_method=cost_method,
            include_realtime=include_realtime,
        )
        return PortfolioSnapshotResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Get snapshot failed", exc)


@router.post(
    "/positions/{symbol}/analysis",
    status_code=202,
    response_model=TaskAccepted,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": DuplicateTaskErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Submit manual analysis for a held portfolio position",
)
def analyze_position(symbol: str, request: PortfolioPositionAnalysisRequest) -> TaskAccepted | JSONResponse:
    service = PortfolioService()
    try:
        context = _resolve_position_analysis_context(service, symbol=symbol, account_id=request.account_id)
    except HTTPException:
        raise
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Resolve portfolio position failed", exc)

    queue = get_task_queue()
    accepted, duplicates = queue.submit_tasks_batch(
        [context["symbol"]],
        stock_name=None,
        original_query=context["symbol"],
        selection_source="manual",
        query_source="portfolio",
        portfolio_context=context,
        report_type="detailed",
        analysis_phase=request.analysis_phase,
        force_refresh=bool(request.force),
        report_language=request.report_language,
        notify=True,
    )
    if duplicates:
        dup = duplicates[0]
        error_response = DuplicateTaskErrorResponse(
            error="duplicate_task",
            message=str(dup),
            stock_code=dup.stock_code,
            existing_task_id=dup.existing_task_id,
        )
        return JSONResponse(status_code=409, content=error_response.model_dump())
    task = accepted[0]
    response = TaskAccepted(
        task_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status="pending",
        message=f"分析任务已加入队列: {task.stock_code}",
        analysis_phase=task.analysis_phase,
    )
    return response


def _resolve_position_analysis_context(
    service: PortfolioService,
    *,
    symbol: str,
    account_id: Optional[int],
) -> dict:
    target = service._normalize_symbol_for_position(symbol)
    if not target:
        raise ValueError("symbol must not be empty")

    snapshot = service.get_portfolio_snapshot(account_id=account_id, cost_method="fifo")
    matches = []
    for account in snapshot.get("accounts") or []:
        for position in account.get("positions") or []:
            position_symbol = service._normalize_symbol_for_position(
                str(position.get("symbol") or "")
            )
            if position_symbol != target:
                continue
            try:
                quantity = float(position.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0.0
            if quantity <= 0:
                continue
            matches.append((account, position, position_symbol))

    if not matches:
        raise api_error(404, "not_found", f"No non-zero portfolio position for {target}")
    if account_id is None:
        account_ids = {
            int(account.get("account_id"))
            for account, _, _ in matches
            if account.get("account_id") is not None
        }
        if len(account_ids) > 1:
            raise api_error(
                400,
                "ambiguous_position_account",
                f"{target} is held in multiple accounts; pass account_id",
            )

    account, position, position_symbol = matches[0]
    return {
        "account_id": account.get("account_id"),
        "account_name": account.get("account_name"),
        "symbol": position_symbol or target,
        "market": position.get("market"),
        "currency": position.get("currency"),
        "quantity": position.get("quantity"),
        "avg_cost": position.get("avg_cost"),
        "total_cost": position.get("total_cost"),
        "unrealized_pnl_base": position.get("unrealized_pnl_base"),
        "unrealized_pnl_pct": position.get("unrealized_pnl_pct"),
        "price_source": position.get("price_source"),
        "price_provider": position.get("price_provider"),
        "price_date": position.get("price_date"),
        "price_stale": bool(position.get("price_stale")),
        "price_available": bool(position.get("price_available", True)),
        "cost_method": snapshot.get("cost_method") or "fifo",
    }


@router.post(
    "/imports/csv/parse",
    response_model=PortfolioImportParseResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Parse broker CSV into normalized trade records",
)
def parse_csv_import(
    broker: str = Form(..., description="Broker id: huatai/citic/cmb"),
    file: UploadFile = File(...),
) -> PortfolioImportParseResponse:
    importer = PortfolioImportService()
    try:
        content = file.file.read()
        parsed = importer.parse_trade_csv(broker=broker, content=content)
        return PortfolioImportParseResponse(
            broker=parsed["broker"],
            record_count=parsed["record_count"],
            skipped_count=parsed["skipped_count"],
            error_count=parsed["error_count"],
            records=[_serialize_import_record(item) for item in parsed.get("records", [])],
            errors=list(parsed.get("errors", [])),
        )
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Parse CSV import failed", exc)


@router.get(
    "/imports/csv/brokers",
    response_model=PortfolioImportBrokerListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="List supported broker CSV parsers",
)
def list_csv_brokers() -> PortfolioImportBrokerListResponse:
    importer = PortfolioImportService()
    try:
        return PortfolioImportBrokerListResponse(brokers=importer.list_supported_brokers())
    except Exception as exc:
        raise _internal_error("List CSV brokers failed", exc)


@router.post(
    "/imports/csv/commit",
    response_model=PortfolioImportCommitResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Parse and commit broker CSV with dedup",
)
def commit_csv_import(
    account_id: int = Form(...),
    broker: str = Form(..., description="Broker id: huatai/citic/cmb"),
    dry_run: bool = Form(False),
    file: UploadFile = File(...),
) -> PortfolioImportCommitResponse:
    importer = PortfolioImportService()
    try:
        content = file.file.read()
        parsed = importer.parse_trade_csv(broker=broker, content=content)
        result = importer.commit_trade_records(
            account_id=account_id,
            broker=parsed["broker"],
            records=list(parsed.get("records", [])),
            dry_run=dry_run,
        )
        return PortfolioImportCommitResponse(**result)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Commit CSV import failed", exc)


@router.post(
    "/fx/refresh",
    response_model=PortfolioFxRefreshResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Refresh FX cache online with stale fallback",
)
def refresh_fx_rates(
    account_id: Optional[int] = Query(None, description="Optional account id"),
    as_of: Optional[date] = Query(None, description="Rate date, default today"),
) -> PortfolioFxRefreshResponse:
    service = PortfolioService()
    try:
        data = service.refresh_fx_rates(account_id=account_id, as_of=as_of)
        return PortfolioFxRefreshResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Refresh FX rates failed", exc)


@router.get(
    "/risk",
    response_model=PortfolioRiskResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Get portfolio risk report",
)
def get_risk_report(
    account_id: Optional[int] = Query(None, description="Optional account id"),
    as_of: Optional[date] = Query(None, description="Risk report date, default today"),
    cost_method: str = Query("fifo", description="Cost method: fifo or avg"),
    include_realtime: bool = Query(
        True,
        description="Whether today's risk snapshot should try realtime quotes before historical close fallback",
    ),
) -> PortfolioRiskResponse:
    service = PortfolioRiskService()
    try:
        data = service.get_risk_report(
            account_id=account_id,
            as_of=as_of,
            cost_method=cost_method,
            include_realtime=include_realtime,
        )
        return PortfolioRiskResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Get risk report failed", exc)


# ----------------------------------------------------------------------
# Broker link (Phase 2 hybrid sync — Toss Invest). Read-only against Toss:
# only GET /api/v1/accounts, /holdings, /orders are ever called; order
# create/modify/cancel endpoints are intentionally never used here.
# ----------------------------------------------------------------------


@router.post(
    "/links/toss",
    response_model=PortfolioBrokerLinkCreatedResponse,
    responses={
        400: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Link a Toss Invest brokerage account as a portfolio account",
)
def link_toss_account(request: PortfolioBrokerLinkTossRequest) -> PortfolioBrokerLinkCreatedResponse:
    service = PortfolioBrokerSyncService()
    try:
        data = service.link_toss_account(
            name=request.name,
            account_seq=request.account_seq,
            owner_id=request.owner_id,
        )
        return PortfolioBrokerLinkCreatedResponse(**data)
    except TossNotConfiguredError as exc:
        raise api_error(400, "toss-not-configured", str(exc))
    except AmbiguousBrokerAccountError as exc:
        raise api_error(
            400,
            "toss_account_ambiguous",
            str(exc),
            detail={"accounts": exc.accounts},
        )
    except BrokerLinkConflictError as exc:
        raise _conflict_error(error="broker_link_conflict", message=str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Link Toss account failed", exc)


@router.post(
    "/links/{account_id}/sync",
    response_model=PortfolioBrokerSyncResponse,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Sync a linked broker account: import new filled orders and reconcile drift",
)
def sync_broker_link(account_id: int) -> PortfolioBrokerSyncResponse:
    service = PortfolioBrokerSyncService()
    try:
        data = service.sync_linked_account(account_id)
        return PortfolioBrokerSyncResponse(**data)
    except TossNotConfiguredError as exc:
        raise api_error(400, "toss-not-configured", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Sync broker link failed", exc)


@router.get(
    "/links",
    response_model=PortfolioBrokerLinkListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="List linked broker accounts",
)
def list_broker_links() -> PortfolioBrokerLinkListResponse:
    service = PortfolioBrokerSyncService()
    try:
        rows = service.list_links()
        return PortfolioBrokerLinkListResponse(links=[PortfolioBrokerLinkItem(**item) for item in rows])
    except Exception as exc:
        raise _internal_error("List broker links failed", exc)


@router.delete(
    "/links/{account_id}",
    response_model=PortfolioDeleteResponse,
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Unlink a broker account (keeps the portfolio account and its ledger)",
)
def delete_broker_link(account_id: int) -> PortfolioDeleteResponse:
    service = PortfolioBrokerSyncService()
    try:
        ok = service.unlink(account_id)
        if not ok:
            raise api_error(404, "not_found", f"Broker link not found for account_id={account_id}")
        return PortfolioDeleteResponse(deleted=1)
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Unlink broker account failed", exc)


# ----------------------------------------------------------------------
# Manual-approval order proposals (Phase 3 — Toss Invest). Two-step flow:
# create a proposal, then a *separate* execute call with confirm=true. Default
# mode is dry-run (TOSS_ORDER_LIVE unset). Every endpoint below requires
# ADMIN_AUTH_ENABLED=true + a verified session (design spec v2 §3
# "인증 필수") via ``_require_order_auth`` — see
# docs/superpowers/specs/2026-07-17-toss-order-phase3-design.md.
# ----------------------------------------------------------------------


@router.post(
    "/links/{account_id}/orders/proposals",
    response_model=PortfolioOrderProposalItem,
    responses={
        400: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Create a manual-approval order proposal (validated, 10-minute TTL, not yet sent to Toss)",
)
def create_order_proposal(
    account_id: int, request: PortfolioOrderProposalCreateRequest, http_request: Request
) -> PortfolioOrderProposalItem:
    _require_order_auth(http_request)
    service = PortfolioOrderService()
    try:
        data = service.create_proposal(
            account_id=account_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            price=request.price,
        )
        return PortfolioOrderProposalItem(**data)
    except TossNotConfiguredError as exc:
        raise api_error(400, "toss-not-configured", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except OrderTypeNotAllowedError as exc:
        raise api_error(400, "order_type_not_allowed", str(exc))
    except HighValueOrderRejectedError as exc:
        raise api_error(422, "high_value_order_rejected", str(exc))
    except FxRateUnavailableError as exc:
        raise api_error(422, "fx_rate_unavailable", str(exc))
    except (InsufficientBuyingPowerError, InsufficientSellableQuantityError) as exc:
        raise api_error(422, "order_rejected", str(exc))
    except OrderLimitExceededError as exc:
        raise api_error(422, "order_limit_exceeded", str(exc), detail={"limit_type": exc.limit_type})
    except PendingProposalLimitExceededError as exc:
        raise _conflict_error(error="pending_proposal_limit_exceeded", message=str(exc))
    except ReferencePriceUnavailableError as exc:
        raise api_error(502, "reference_price_unavailable", str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create order proposal failed", exc)


@router.get(
    "/links/{account_id}/orders/proposals",
    response_model=PortfolioOrderProposalListResponse,
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="List order proposals for one linked account",
)
def list_order_proposals(
    account_id: int,
    status: Optional[str] = Query(None, description="Filter by proposal status"),
    generation_source: Optional[str] = Query(
        None, description="Filter by 'manual' or 'auto' (Phase 5 defensive-signal batch generator)"
    ),
) -> PortfolioOrderProposalListResponse:
    # Read-only — design spec §3 scopes the extra order-auth gate to *write*
    # endpoints (proposal create/execute/cancel, order cancel, reconcile);
    # this list is no more sensitive than any other read-only portfolio GET,
    # which already relies solely on the global AuthMiddleware.
    generation_source = _validate_generation_source_filter(generation_source)
    service = PortfolioOrderService()
    try:
        rows = service.list_proposals(account_id=account_id, status=status, generation_source=generation_source)
        return PortfolioOrderProposalListResponse(proposals=[PortfolioOrderProposalItem(**item) for item in rows])
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except Exception as exc:
        raise _internal_error("List order proposals failed", exc)


@router.post(
    "/links/{account_id}/orders/proposals/{proposal_uuid}/execute",
    response_model=PortfolioOrderProposalItem,
    responses={
        400: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Execute a pending proposal (confirm=true required, even in dry-run)",
)
def execute_order_proposal(
    account_id: int, proposal_uuid: str, request: PortfolioOrderExecuteRequest, http_request: Request
) -> PortfolioOrderProposalItem:
    _require_order_auth(http_request)
    service = PortfolioOrderService()
    try:
        data = service.execute_proposal(
            account_id=account_id,
            proposal_uuid=proposal_uuid,
            confirm=request.confirm,
        )
        return PortfolioOrderProposalItem(**data)
    except ConfirmRequiredError as exc:
        raise api_error(400, "confirm_required", str(exc))
    except ProposalNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except ProposalInProgressError as exc:
        raise _conflict_error(error="proposal_in_progress", message=str(exc))
    except ProposalNotExecutableError as exc:
        raise _conflict_error(error="proposal_not_executable", message=str(exc))
    except TossNotConfiguredError as exc:
        raise api_error(400, "toss-not-configured", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except HighValueOrderRejectedError as exc:
        raise api_error(422, "high_value_order_rejected", str(exc))
    except FxRateUnavailableError as exc:
        raise api_error(422, "fx_rate_unavailable", str(exc))
    except (InsufficientBuyingPowerError, InsufficientSellableQuantityError) as exc:
        raise api_error(422, "order_rejected", str(exc))
    except OrderLimitExceededError as exc:
        raise api_error(422, "order_limit_exceeded", str(exc), detail={"limit_type": exc.limit_type})
    except ReferencePriceUnavailableError as exc:
        raise api_error(502, "reference_price_unavailable", str(exc))
    except TossOrderRejectedError as exc:
        raise _map_toss_order_error(exc)
    except OrderAuditPersistFailedError as exc:
        # A real Toss order already happened (or its outcome could not be
        # determined); surface loudly with the full detail instead of a
        # generic message (design spec §7).
        raise api_error(500, "order_audit_persist_failed", str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Execute order proposal failed", exc)


@router.post(
    "/links/{account_id}/orders/proposals/{proposal_uuid}/reconcile",
    response_model=PortfolioOrderProposalItem,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Resolve an 'executing'/'outcome_unknown' proposal by re-POSTing its idempotent clientOrderId",
)
def reconcile_order_proposal(account_id: int, proposal_uuid: str, http_request: Request) -> PortfolioOrderProposalItem:
    _require_order_auth(http_request)
    service = PortfolioOrderService()
    try:
        data = service.reconcile_proposal(account_id=account_id, proposal_uuid=proposal_uuid)
        return PortfolioOrderProposalItem(**data)
    except ProposalNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except ProposalNotReconcilableError as exc:
        raise _conflict_error(error="proposal_not_reconcilable", message=str(exc))
    except TossNotConfiguredError as exc:
        raise api_error(400, "toss-not-configured", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except OrderIdempotencyConflictError as exc:
        raise api_error(409, "toss-idempotency-key-conflict", str(exc))
    except TossOrderRejectedError as exc:
        raise _map_toss_order_error(exc)
    except OrderAuditPersistFailedError as exc:
        raise api_error(500, "order_audit_persist_failed", str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except Exception as exc:
        raise _internal_error("Reconcile order proposal failed", exc)


@router.delete(
    "/links/{account_id}/orders/proposals/{proposal_uuid}",
    response_model=PortfolioOrderProposalItem,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Cancel a still-pending proposal (never reached Toss)",
)
def cancel_order_proposal(account_id: int, proposal_uuid: str, http_request: Request) -> PortfolioOrderProposalItem:
    _require_order_auth(http_request)
    service = PortfolioOrderService()
    try:
        data = service.cancel_proposal(account_id=account_id, proposal_uuid=proposal_uuid)
        return PortfolioOrderProposalItem(**data)
    except ProposalNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except ProposalNotExecutableError as exc:
        raise _conflict_error(error="proposal_not_executable", message=str(exc))
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except Exception as exc:
        raise _internal_error("Cancel order proposal failed", exc)


@router.post(
    "/links/{account_id}/orders/{toss_order_id}/cancel",
    response_model=PortfolioOrderCancelResponse,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Cancel an already-placed live order (self-issued orders only)",
)
def cancel_placed_order(account_id: int, toss_order_id: str, http_request: Request) -> PortfolioOrderCancelResponse:
    _require_order_auth(http_request)
    service = PortfolioOrderService()
    try:
        data = service.cancel_order(account_id=account_id, toss_order_id=toss_order_id)
        return PortfolioOrderCancelResponse(**data)
    except TossNotConfiguredError as exc:
        raise api_error(400, "toss-not-configured", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except OrderNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except ProposalNotReconcilableError as exc:
        raise _conflict_error(error="proposal_not_reconcilable", message=str(exc))
    except TossOrderRejectedError as exc:
        raise _map_toss_order_error(exc)
    except OrderAuditPersistFailedError as exc:
        raise api_error(500, "order_audit_persist_failed", str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except Exception as exc:
        raise _internal_error("Cancel placed order failed", exc)


@router.get(
    "/links/{account_id}/orders/{toss_order_id}",
    response_model=PortfolioOrderStatusResponse,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Get the current status of a self-issued order (read-only passthrough to Toss)",
)
def get_placed_order_status(account_id: int, toss_order_id: str) -> PortfolioOrderStatusResponse:
    # Read-only passthrough — see list_order_proposals for why this is not
    # gated behind the extra order-write auth requirement.
    service = PortfolioOrderService()
    try:
        data = service.get_order_status(account_id=account_id, toss_order_id=toss_order_id)
        return PortfolioOrderStatusResponse(**data)
    except TossNotConfiguredError as exc:
        raise api_error(400, "toss-not-configured", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except OrderNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except Exception as exc:
        raise _internal_error("Get order status failed", exc)


# ----------------------------------------------------------------------
# Server-side conditional-order proposals (Phase 4 — Toss Invest). Same
# two-step shape as the plain-order endpoints above (proposal -> separate
# approve call, confirm=true required), but "approve" registers a Toss-side
# SINGLE/STOP conditional order that Toss auto-executes once triggered —
# see docs/superpowers/specs/2026-07-19-toss-conditional-order-phase4-design.md.
# Every write endpoint below requires ADMIN_AUTH_ENABLED=true + a verified
# session via ``_require_order_auth``, same as Phase 3.
# ----------------------------------------------------------------------


@router.post(
    "/links/{account_id}/conditional-orders/proposals",
    response_model=PortfolioConditionalOrderProposalItem,
    responses={
        400: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Create a conditional-order proposal (SINGLE/STOP, validated, 10-minute TTL, not yet registered with Toss)",
)
def create_conditional_order_proposal(
    account_id: int, request: PortfolioConditionalOrderProposalCreateRequest, http_request: Request
) -> PortfolioConditionalOrderProposalItem:
    _require_order_auth(http_request)
    service = PortfolioConditionalOrderService()
    try:
        data = service.create_proposal(
            account_id=account_id,
            symbol=request.symbol,
            side=request.side,
            trigger_price=request.trigger_price,
            limit_price=request.limit_price,
            quantity=request.quantity,
            expire_date=request.expire_date,
        )
        return PortfolioConditionalOrderProposalItem(**data)
    except TossNotConfiguredError as exc:
        raise api_error(400, "toss-not-configured", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except ExpireDateTooFarError as exc:
        raise api_error(422, "expire_date_too_far", str(exc))
    except HighValueOrderRejectedError as exc:
        raise api_error(422, "high_value_order_rejected", str(exc))
    except FxRateUnavailableError as exc:
        raise api_error(422, "fx_rate_unavailable", str(exc))
    except (InsufficientBuyingPowerError, InsufficientSellableQuantityError) as exc:
        raise api_error(422, "order_rejected", str(exc))
    except OrderLimitExceededError as exc:
        raise api_error(422, "order_limit_exceeded", str(exc), detail={"limit_type": exc.limit_type})
    except PendingProposalLimitExceededError as exc:
        raise _conflict_error(error="pending_proposal_limit_exceeded", message=str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create conditional order proposal failed", exc)


@router.get(
    "/links/{account_id}/conditional-orders/proposals",
    response_model=PortfolioConditionalOrderProposalListResponse,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="List conditional-order proposals for one linked account",
)
def list_conditional_order_proposals(
    account_id: int,
    http_request: Request,
    status: Optional[str] = Query(None, description="Filter by proposal status"),
    generation_source: Optional[str] = Query(
        None, description="Filter by 'manual' or 'auto' (Phase 5 defensive-signal batch generator)"
    ),
) -> PortfolioConditionalOrderProposalListResponse:
    # Unlike Phase 3's read-only order-proposal list (which relies solely on
    # the global AuthMiddleware), every Phase 4 endpoint — reads included —
    # requires the extra order-write auth gate (Codex BLOCK review major 2):
    # a proposal exposes symbol/side/trigger/limit/quantity/status/Toss id,
    # which must not be visible to an unauthenticated caller when
    # ADMIN_AUTH_ENABLED=false.
    _require_order_auth(http_request)
    generation_source = _validate_generation_source_filter(generation_source)
    service = PortfolioConditionalOrderService()
    try:
        rows = service.list_proposals(account_id=account_id, status=status, generation_source=generation_source)
        return PortfolioConditionalOrderProposalListResponse(
            proposals=[PortfolioConditionalOrderProposalItem(**item) for item in rows]
        )
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except Exception as exc:
        raise _internal_error("List conditional order proposals failed", exc)


@router.get(
    "/links/{account_id}/conditional-orders/proposals/{proposal_uuid}",
    response_model=PortfolioConditionalOrderProposalItem,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Get one conditional-order proposal",
)
def get_conditional_order_proposal(
    account_id: int, proposal_uuid: str, http_request: Request
) -> PortfolioConditionalOrderProposalItem:
    _require_order_auth(http_request)
    service = PortfolioConditionalOrderService()
    try:
        data = service.get_proposal(account_id=account_id, proposal_uuid=proposal_uuid)
        return PortfolioConditionalOrderProposalItem(**data)
    except ConditionalProposalNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except Exception as exc:
        raise _internal_error("Get conditional order proposal failed", exc)


@router.post(
    "/links/{account_id}/conditional-orders/proposals/{proposal_uuid}/approve",
    response_model=PortfolioConditionalOrderProposalItem,
    responses={
        400: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Approve a pending conditional-order proposal (confirm=true required) — registers it with Toss",
)
def approve_conditional_order_proposal(
    account_id: int, proposal_uuid: str, request: PortfolioConditionalOrderApproveRequest, http_request: Request
) -> PortfolioConditionalOrderProposalItem:
    _require_order_auth(http_request)
    service = PortfolioConditionalOrderService()
    try:
        data = service.approve_proposal(
            account_id=account_id,
            proposal_uuid=proposal_uuid,
            confirm=request.confirm,
        )
        return PortfolioConditionalOrderProposalItem(**data)
    except ConfirmRequiredError as exc:
        raise api_error(400, "confirm_required", str(exc))
    except ConditionalProposalNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except ConditionalProposalInProgressError as exc:
        raise _conflict_error(error="proposal_in_progress", message=str(exc))
    except ConditionalProposalNotApprovableError as exc:
        raise _conflict_error(error="proposal_not_approvable", message=str(exc))
    except TossNotConfiguredError as exc:
        raise api_error(400, "toss-not-configured", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except ExpireDateTooFarError as exc:
        raise api_error(422, "expire_date_too_far", str(exc))
    except HighValueOrderRejectedError as exc:
        raise api_error(422, "high_value_order_rejected", str(exc))
    except FxRateUnavailableError as exc:
        raise api_error(422, "fx_rate_unavailable", str(exc))
    except (InsufficientBuyingPowerError, InsufficientSellableQuantityError) as exc:
        raise api_error(422, "order_rejected", str(exc))
    except OrderLimitExceededError as exc:
        raise api_error(422, "order_limit_exceeded", str(exc), detail={"limit_type": exc.limit_type})
    except TossOrderRejectedError as exc:
        raise _map_toss_order_error(exc)
    except OrderAuditPersistFailedError as exc:
        raise api_error(500, "order_audit_persist_failed", str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Approve conditional order proposal failed", exc)


@router.post(
    "/links/{account_id}/conditional-orders/proposals/{proposal_uuid}/reconcile",
    response_model=PortfolioConditionalOrderProposalItem,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Resolve an 'approving'/'registration_unknown' proposal via a best-effort Toss listing match",
)
def reconcile_conditional_order_proposal(
    account_id: int, proposal_uuid: str, http_request: Request
) -> PortfolioConditionalOrderProposalItem:
    _require_order_auth(http_request)
    service = PortfolioConditionalOrderService()
    try:
        data = service.reconcile_proposal(account_id=account_id, proposal_uuid=proposal_uuid)
        return PortfolioConditionalOrderProposalItem(**data)
    except ConditionalProposalNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except ConditionalApprovalInProgressError as exc:
        # Codex BLOCK review blocker 2: distinct from proposal_not_reconcilable
        # — the proposal *is* reconcilable in principle, but its approving
        # claim is still fresh enough that a real approve POST is plausibly
        # in flight, so reconcile must not preempt it right now.
        raise _conflict_error(error="approval-in-progress", message=str(exc))
    except ConditionalProposalNotReconcilableError as exc:
        raise _conflict_error(error="proposal_not_reconcilable", message=str(exc))
    except TossNotConfiguredError as exc:
        raise api_error(400, "toss-not-configured", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except Exception as exc:
        raise _internal_error("Reconcile conditional order proposal failed", exc)


@router.delete(
    "/links/{account_id}/conditional-orders/proposals/{proposal_uuid}",
    response_model=PortfolioConditionalOrderProposalItem,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Cancel a conditional-order proposal (pending: local only; approved/paused: cancels the Toss registration)",
)
def cancel_conditional_order_proposal(
    account_id: int, proposal_uuid: str, http_request: Request
) -> PortfolioConditionalOrderProposalItem:
    _require_order_auth(http_request)
    service = PortfolioConditionalOrderService()
    try:
        data = service.cancel_proposal(account_id=account_id, proposal_uuid=proposal_uuid)
        return PortfolioConditionalOrderProposalItem(**data)
    except ConditionalProposalNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except ConditionalProposalNotReconcilableError as exc:
        raise _conflict_error(error="proposal_not_reconcilable", message=str(exc))
    except ConditionalProposalNotCancelableError as exc:
        raise _conflict_error(error="proposal_not_cancelable", message=str(exc))
    except TossOrderRejectedError as exc:
        raise _map_toss_order_error(exc)
    except OrderAuditPersistFailedError as exc:
        raise api_error(500, "order_audit_persist_failed", str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except Exception as exc:
        raise _internal_error("Cancel conditional order proposal failed", exc)


@router.get(
    "/links/{account_id}/conditional-orders",
    response_model=PortfolioConditionalOrderProposalListResponse,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Observe conditional orders (local state + lazy per-row Toss status refresh)",
)
def list_conditional_orders(
    account_id: int,
    http_request: Request,
    status: Optional[str] = Query(None, description="Filter by local proposal status"),
) -> PortfolioConditionalOrderProposalListResponse:
    # Auth required (Codex BLOCK review major 2) — unlike a purely local
    # read, this also exposes each approved proposal's Toss-side status via
    # a lazy per-row refresh; the same order-data-exposure rationale as the
    # two endpoints above applies.
    _require_order_auth(http_request)
    service = PortfolioConditionalOrderService()
    try:
        rows = service.list_conditional_orders_with_lazy_refresh(account_id=account_id, status=status)
        return PortfolioConditionalOrderProposalListResponse(
            proposals=[PortfolioConditionalOrderProposalItem(**item) for item in rows]
        )
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except Exception as exc:
        raise _internal_error("List conditional orders failed", exc)


@router.post(
    "/links/{account_id}/conditional-orders/sync",
    response_model=PortfolioConditionalOrderSyncResponse,
    responses={
        400: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Bulk-refresh every 'approved'/'paused' conditional order's Toss status",
)
def sync_conditional_orders(account_id: int, http_request: Request) -> PortfolioConditionalOrderSyncResponse:
    _require_order_auth(http_request)
    service = PortfolioConditionalOrderService()
    try:
        data = service.sync_proposals(account_id=account_id)
        return PortfolioConditionalOrderSyncResponse(**data)
    except TossNotConfiguredError as exc:
        raise api_error(400, "toss-not-configured", str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except TossUpstreamError as exc:
        raise api_error(502, "toss-upstream-error", str(exc))
    except Exception as exc:
        raise _internal_error("Sync conditional orders failed", exc)


@router.post(
    "/links/{account_id}/conditional-orders/proposals/{proposal_uuid}/force-resolve",
    response_model=PortfolioConditionalOrderProposalItem,
    responses={
        400: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary=(
        "OPERATOR-ONLY manual resolution of a permanently 'registration_unknown' proposal "
        "(confirm Toss has no matching order first)"
    ),
)
def force_resolve_conditional_order_proposal(
    account_id: int, proposal_uuid: str, request: PortfolioConditionalOrderForceResolveRequest, http_request: Request
) -> PortfolioConditionalOrderProposalItem:
    _require_order_auth(http_request)
    service = PortfolioConditionalOrderService()
    try:
        data = service.force_resolve_proposal(
            account_id=account_id,
            proposal_uuid=proposal_uuid,
            confirm=request.confirm,
            reason=request.reason,
        )
        return PortfolioConditionalOrderProposalItem(**data)
    except ConfirmRequiredError as exc:
        raise api_error(400, "confirm_required", str(exc))
    except ConditionalProposalNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except ConditionalProposalNotForceResolvableError as exc:
        raise _conflict_error(error="proposal_not_force_resolvable", message=str(exc))
    except BrokerLinkNotFoundError as exc:
        raise api_error(404, "not_found", str(exc))
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Force-resolve conditional order proposal failed", exc)
