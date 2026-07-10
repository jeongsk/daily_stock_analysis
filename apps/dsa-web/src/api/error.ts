import axios from 'axios';
import type { UiLanguage } from '../i18n/uiText';
import { getRuntimeInitialLanguage } from '../utils/uiLanguage';

type ErrorCopyPair = { title: string; message: string };

type ErrorCopy = {
  separator: string;
  requestFailed: string;
  requestIncompleteRetry: string;
  requestIncompleteHttp: string;
  agentDisabled: ErrorCopyPair;
  missingParams: ErrorCopyPair;
  oversell: ErrorCopyPair;
  portfolioBusy: ErrorCopyPair;
  alphasiftInstallFailed: ErrorCopyPair;
  alphasiftSpecMissing: ErrorCopyPair;
  alphasiftSpecNotAllowed: ErrorCopyPair;
  alphasiftNotReady: string;
  alphasiftAdapterUnavailable: ErrorCopyPair;
  screenTaskNotFound: ErrorCopyPair;
  screenFailed: ErrorCopyPair;
  llmNotConfigured: ErrorCopyPair;
  modelToolIncompatible: ErrorCopyPair;
  invalidToolCall: ErrorCopyPair;
  upstreamTimeout: ErrorCopyPair;
  upstreamNetwork: ErrorCopyPair;
  upstreamLlm400: ErrorCopyPair;
  localConnectionFailed: ErrorCopyPair;
};

const ERROR_TEXT: Record<UiLanguage, ErrorCopy> = {
  zh: {
    separator: '：',
    requestFailed: '请求失败',
    requestIncompleteRetry: '请求未成功完成，请稍后重试。',
    requestIncompleteHttp: '请求未成功完成（HTTP {status}）。',
    agentDisabled: { title: 'Agent 模式未开启', message: '当前功能依赖 Agent 模式，请先开启后再重试。' },
    missingParams: { title: '请求缺少必要参数', message: '请先补充股票代码或必要输入后再试。' },
    oversell: { title: '卖出数量超过可用持仓', message: '卖出数量超过当前可用持仓，请删除或修正对应卖出流水后重试。' },
    portfolioBusy: { title: '持仓账本正忙', message: '持仓账本正在处理另一笔变更，请稍后重试。' },
    alphasiftInstallFailed: { title: 'AlphaSift 修复安装失败', message: 'DSA 已尝试修复安装 AlphaSift，但 pip 安装未成功。请检查 ALPHASIFT_INSTALL_SPEC、网络代理或后端 Python 环境。' },
    alphasiftSpecMissing: { title: 'AlphaSift 安装来源未配置', message: '请先确认后端依赖已安装；如需使用修复安装入口，请配置受信任的 ALPHASIFT_INSTALL_SPEC。' },
    alphasiftSpecNotAllowed: { title: 'AlphaSift 安装来源受限', message: '修复安装仅允许使用受信任的 AlphaSift GitHub 来源；如需本地路径或 wheel，请先手动安装到当前 Python 环境。' },
    alphasiftNotReady: 'AlphaSift 未就绪',
    alphasiftAdapterUnavailable: { title: 'AlphaSift 适配层不可用', message: '当前 AlphaSift 版本缺少 DSA 稳定适配层。请重新安装或升级 AlphaSift 后再试。' },
    screenTaskNotFound: { title: '选股任务不可恢复', message: '服务端没有找到这次选股任务，可能后端已重启或任务记录已清理，请重新运行选股。' },
    screenFailed: { title: 'AlphaSift 选股失败', message: 'AlphaSift 运行时访问外部行情、快照或模型服务失败，请稍后重试，或检查网络与代理设置。' },
    llmNotConfigured: { title: '系统没有配置可用的 LLM 模型', message: '请先在系统设置中配置主模型、可用渠道或相关 API Key 后再重试。' },
    modelToolIncompatible: { title: '当前模型不兼容工具调用', message: '当前模型不适合 Agent / 工具调用场景，请更换支持工具调用的模型后重试。' },
    invalidToolCall: { title: '上游模型返回的数据结构不完整', message: '上游模型返回的工具调用结构不符合要求，请更换模型或关闭相关推理模式后重试。' },
    upstreamTimeout: { title: '连接上游服务超时', message: '服务端访问外部依赖时超时，请稍后重试，或检查当前网络与代理设置。' },
    upstreamNetwork: { title: '服务端无法访问外部依赖', message: '页面已连接到本地服务，但本地服务访问外部模型或数据接口失败，请检查代理、DNS 或出网配置。' },
    upstreamLlm400: { title: '上游模型接口拒绝了当前请求', message: '本地服务正常，但上游模型接口拒绝了请求，请检查模型名称、参数格式或工具调用兼容性。' },
    localConnectionFailed: { title: '无法连接到本地服务', message: '浏览器当前无法连接到本地 Web 服务，请检查服务是否启动、监听地址是否正确、端口是否开放。' },
  },
  en: {
    separator: ': ',
    requestFailed: 'Request failed',
    requestIncompleteRetry: 'The request did not complete; please try again later.',
    requestIncompleteHttp: 'The request did not complete (HTTP {status}).',
    agentDisabled: { title: 'Agent mode is not enabled', message: 'This feature depends on agent mode; enable it first and try again.' },
    missingParams: { title: 'Missing required parameters', message: 'Please provide the stock code or other required input and try again.' },
    oversell: { title: 'Sell quantity exceeds available position', message: 'The sell quantity exceeds the currently available position; delete or fix the corresponding sell records and try again.' },
    portfolioBusy: { title: 'Portfolio ledger is busy', message: 'The portfolio ledger is processing another change; please try again later.' },
    alphasiftInstallFailed: { title: 'AlphaSift repair install failed', message: 'DSA tried to repair-install AlphaSift, but pip install did not succeed. Check ALPHASIFT_INSTALL_SPEC, your network proxy, or the backend Python environment.' },
    alphasiftSpecMissing: { title: 'AlphaSift install source not configured', message: 'Make sure the backend dependencies are installed first; to use the repair-install entry, configure a trusted ALPHASIFT_INSTALL_SPEC.' },
    alphasiftSpecNotAllowed: { title: 'AlphaSift install source restricted', message: 'Repair install only allows trusted AlphaSift GitHub sources; for a local path or wheel, install it into the current Python environment manually first.' },
    alphasiftNotReady: 'AlphaSift is not ready',
    alphasiftAdapterUnavailable: { title: 'AlphaSift adapter layer unavailable', message: 'The current AlphaSift version lacks the DSA stable adapter layer. Reinstall or upgrade AlphaSift and try again.' },
    screenTaskNotFound: { title: 'Screening task cannot be recovered', message: 'The server could not find this screening task; the backend may have restarted or the task record was cleaned up. Please run the screening again.' },
    screenFailed: { title: 'AlphaSift screening failed', message: 'The AlphaSift runtime failed to access external market data, snapshot, or model services; try again later, or check your network and proxy settings.' },
    llmNotConfigured: { title: 'No usable LLM model is configured', message: 'Configure the primary model, available channels, or the relevant API keys in system settings, then try again.' },
    modelToolIncompatible: { title: 'Current model does not support tool calls', message: 'The current model is not suitable for agent / tool-call scenarios; switch to a model that supports tool calls and try again.' },
    invalidToolCall: { title: 'Upstream model returned an incomplete data structure', message: 'The tool-call structure returned by the upstream model does not meet requirements; switch models or disable the related reasoning mode and try again.' },
    upstreamTimeout: { title: 'Connection to upstream service timed out', message: 'The server timed out while accessing external dependencies; try again later, or check your current network and proxy settings.' },
    upstreamNetwork: { title: 'Server cannot reach external dependencies', message: 'The page is connected to the local service, but the local service failed to reach external model or data APIs; check your proxy, DNS, or outbound network configuration.' },
    upstreamLlm400: { title: 'Upstream model API rejected the request', message: 'The local service is fine, but the upstream model API rejected the request; check the model name, parameter format, or tool-call compatibility.' },
    localConnectionFailed: { title: 'Cannot connect to the local service', message: 'The browser cannot connect to the local web service; check whether the service is running, the listen address is correct, and the port is open.' },
  },
  ko: {
    separator: ': ',
    requestFailed: '요청 실패',
    requestIncompleteRetry: '요청이 완료되지 않았습니다. 잠시 후 다시 시도해 주세요.',
    requestIncompleteHttp: '요청이 완료되지 않았습니다 (HTTP {status}).',
    agentDisabled: { title: 'Agent 모드가 꺼져 있습니다', message: '이 기능은 Agent 모드가 필요합니다. 먼저 켠 후 다시 시도해 주세요.' },
    missingParams: { title: '요청에 필수 매개변수가 없습니다', message: '종목 코드 또는 필수 입력을 채운 후 다시 시도해 주세요.' },
    oversell: { title: '매도 수량이 보유 수량을 초과합니다', message: '매도 수량이 현재 가용 보유 수량을 초과합니다. 해당 매도 내역을 삭제하거나 수정한 후 다시 시도해 주세요.' },
    portfolioBusy: { title: '포트폴리오 장부가 사용 중입니다', message: '포트폴리오 장부가 다른 변경을 처리하고 있습니다. 잠시 후 다시 시도해 주세요.' },
    alphasiftInstallFailed: { title: 'AlphaSift 복구 설치 실패', message: 'DSA가 AlphaSift 복구 설치를 시도했지만 pip 설치에 실패했습니다. ALPHASIFT_INSTALL_SPEC, 네트워크 프록시 또는 백엔드 Python 환경을 확인해 주세요.' },
    alphasiftSpecMissing: { title: 'AlphaSift 설치 소스가 설정되지 않았습니다', message: '먼저 백엔드 의존성이 설치되어 있는지 확인해 주세요. 복구 설치 기능을 사용하려면 신뢰할 수 있는 ALPHASIFT_INSTALL_SPEC을 설정해 주세요.' },
    alphasiftSpecNotAllowed: { title: 'AlphaSift 설치 소스가 제한되어 있습니다', message: '복구 설치는 신뢰할 수 있는 AlphaSift GitHub 소스만 허용합니다. 로컬 경로나 wheel이 필요하면 먼저 현재 Python 환경에 수동으로 설치해 주세요.' },
    alphasiftNotReady: 'AlphaSift가 준비되지 않았습니다',
    alphasiftAdapterUnavailable: { title: 'AlphaSift 어댑터 계층을 사용할 수 없습니다', message: '현재 AlphaSift 버전에는 DSA 안정 어댑터 계층이 없습니다. AlphaSift를 재설치하거나 업그레이드한 후 다시 시도해 주세요.' },
    screenTaskNotFound: { title: '스크리닝 작업을 복구할 수 없습니다', message: '서버에서 이 스크리닝 작업을 찾을 수 없습니다. 백엔드가 재시작되었거나 작업 기록이 정리되었을 수 있으니 스크리닝을 다시 실행해 주세요.' },
    screenFailed: { title: 'AlphaSift 스크리닝 실패', message: 'AlphaSift 런타임이 외부 시세, 스냅숏 또는 모델 서비스 접근에 실패했습니다. 잠시 후 다시 시도하거나 네트워크와 프록시 설정을 확인해 주세요.' },
    llmNotConfigured: { title: '사용 가능한 LLM 모델이 설정되어 있지 않습니다', message: '시스템 설정에서 기본 모델, 사용 가능한 채널 또는 관련 API Key를 먼저 설정한 후 다시 시도해 주세요.' },
    modelToolIncompatible: { title: '현재 모델이 도구 호출을 지원하지 않습니다', message: '현재 모델은 Agent/도구 호출 시나리오에 적합하지 않습니다. 도구 호출을 지원하는 모델로 교체한 후 다시 시도해 주세요.' },
    invalidToolCall: { title: '업스트림 모델이 불완전한 데이터 구조를 반환했습니다', message: '업스트림 모델이 반환한 도구 호출 구조가 요구 사항에 맞지 않습니다. 모델을 교체하거나 관련 추론 모드를 끈 후 다시 시도해 주세요.' },
    upstreamTimeout: { title: '업스트림 서비스 연결 시간 초과', message: '서버가 외부 의존성에 접근하는 중 시간이 초과되었습니다. 잠시 후 다시 시도하거나 네트워크와 프록시 설정을 확인해 주세요.' },
    upstreamNetwork: { title: '서버가 외부 의존성에 접근할 수 없습니다', message: '페이지는 로컬 서비스에 연결되었지만 로컬 서비스가 외부 모델 또는 데이터 API 접근에 실패했습니다. 프록시, DNS 또는 아웃바운드 네트워크 설정을 확인해 주세요.' },
    upstreamLlm400: { title: '업스트림 모델 API가 요청을 거부했습니다', message: '로컬 서비스는 정상이지만 업스트림 모델 API가 요청을 거부했습니다. 모델 이름, 매개변수 형식 또는 도구 호출 호환성을 확인해 주세요.' },
    localConnectionFailed: { title: '로컬 서비스에 연결할 수 없습니다', message: '브라우저가 로컬 웹 서비스에 연결할 수 없습니다. 서비스가 실행 중인지, 수신 주소가 올바른지, 포트가 열려 있는지 확인해 주세요.' },
  },
};

function getErrorText(): ErrorCopy {
  return ERROR_TEXT[getRuntimeInitialLanguage()] ?? ERROR_TEXT.zh;
}

export type ApiErrorCategory =
  | 'agent_disabled'
  | 'missing_params'
  | 'llm_not_configured'
  | 'model_tool_incompatible'
  | 'invalid_tool_call'
  | 'portfolio_oversell'
  | 'portfolio_busy'
  | 'upstream_llm_400'
  | 'upstream_timeout'
  | 'upstream_network'
  | 'local_connection_failed'
  | 'http_error'
  | 'unknown';

export interface ParsedApiError {
  title: string;
  message: string;
  rawMessage: string;
  status?: number;
  category: ApiErrorCategory;
}

type ResponseLike = {
  status?: number;
  data?: unknown;
  statusText?: string;
};

type ErrorCarrier = {
  response?: ResponseLike;
  code?: string;
  message?: string;
  parsedError?: ParsedApiError;
  cause?: unknown;
};

type CreateParsedApiErrorOptions = {
  title: string;
  message: string;
  rawMessage?: string;
  status?: number;
  category?: ApiErrorCategory;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function pickString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value.trim();
    }
  }
  return null;
}

function stringifyValue(value: unknown): string | null {
  if (value === null || value === undefined) {
    return null;
  }

  if (typeof value === 'string') {
    return value.trim() || null;
  }

  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }

  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function getResponse(error: unknown): ResponseLike | undefined {
  if (!isRecord(error)) {
    return undefined;
  }

  const response = (error as ErrorCarrier).response;
  return response && typeof response === 'object' ? response : undefined;
}

function getErrorCode(error: unknown): string | undefined {
  return isRecord(error) && typeof (error as ErrorCarrier).code === 'string'
    ? (error as ErrorCarrier).code
    : undefined;
}

function getErrorMessage(error: unknown): string | null {
  if (typeof error === 'string') {
    return error.trim() || null;
  }

  if (error instanceof Error && error.message.trim()) {
    return error.message.trim();
  }

  if (isRecord(error) && typeof (error as ErrorCarrier).message === 'string') {
    const message = (error as ErrorCarrier).message?.trim();
    return message || null;
  }

  return null;
}

function getCauseMessage(error: unknown): string | null {
  if (!isRecord(error)) {
    return null;
  }

  return getErrorMessage((error as ErrorCarrier).cause);
}

function buildMatchText(parts: Array<string | undefined | null>): string {
  return parts
    .filter((part): part is string => typeof part === 'string' && part.trim().length > 0)
    .join(' | ')
    .toLowerCase();
}

function includesAny(haystack: string, needles: string[]): boolean {
  return needles.some((needle) => haystack.includes(needle.toLowerCase()));
}

function extractValidationDetail(detail: unknown): string | null {
  if (!Array.isArray(detail)) {
    return null;
  }

  const parts = detail
    .map((item) => {
      if (!isRecord(item)) {
        return stringifyValue(item);
      }

      const location = Array.isArray(item.loc)
        ? item.loc.map((segment) => String(segment)).join('.')
        : null;
      const message = pickString(item.msg, item.message, item.error);
      if (!location && !message) {
        return stringifyValue(item);
      }
      return [location, message].filter(Boolean).join(': ');
    })
    .filter((entry): entry is string => Boolean(entry));

  return parts.length > 0 ? parts.join('; ') : null;
}

function extractErrorCode(data: unknown): string | null {
  if (!isRecord(data)) {
    return null;
  }

  const detail = data.detail;
  if (isRecord(detail)) {
    return pickString(detail.error, detail.code, data.error, data.code);
  }

  return pickString(data.error, data.code);
}

export function extractErrorPayloadText(data: unknown): string | null {
  if (typeof data === 'string') {
    return data.trim() || null;
  }

  if (Array.isArray(data)) {
    return extractValidationDetail(data) ?? stringifyValue(data);
  }

  if (!isRecord(data)) {
    return stringifyValue(data);
  }

  const detail = data.detail;
  if (isRecord(detail)) {
    return (
      pickString(detail.message, detail.error)
      ?? extractValidationDetail(detail.detail)
      ?? stringifyValue(detail)
    );
  }

  return (
    pickString(
      detail,
      data.message,
      data.error,
      data.title,
      data.reason,
      data.description,
      data.msg,
    )
    ?? extractValidationDetail(detail)
    ?? stringifyValue(data)
  );
}

export function createParsedApiError(options: CreateParsedApiErrorOptions): ParsedApiError {
  return {
    title: options.title,
    message: options.message,
    rawMessage: options.rawMessage?.trim() || options.message,
    status: options.status,
    category: options.category ?? 'unknown',
  };
}

export function isParsedApiError(value: unknown): value is ParsedApiError {
  return isRecord(value)
    && typeof value.title === 'string'
    && typeof value.message === 'string'
    && typeof value.rawMessage === 'string'
    && typeof value.category === 'string';
}

export function isApiRequestError(
  value: unknown,
): value is Error & ErrorCarrier & { parsedError: ParsedApiError } {
  return value instanceof Error
    && isRecord(value)
    && isParsedApiError((value as ErrorCarrier).parsedError);
}

export function formatParsedApiError(parsed: ParsedApiError): string {
  if (!parsed.title.trim()) {
    return parsed.message;
  }
  if (parsed.title === parsed.message) {
    return parsed.title;
  }
  return `${parsed.title}${getErrorText().separator}${parsed.message}`;
}

export function getParsedApiError(error: unknown): ParsedApiError {
  if (isParsedApiError(error)) {
    return error;
  }
  if (isRecord(error) && isParsedApiError((error as ErrorCarrier).parsedError)) {
    return (error as ErrorCarrier).parsedError as ParsedApiError;
  }
  return parseApiError(error);
}

export function createApiError(
  parsed: ParsedApiError,
  extra: { response?: ResponseLike; code?: string; cause?: unknown } = {},
): Error & ErrorCarrier & { status?: number; category: ApiErrorCategory; rawMessage: string } {
  const apiError = new Error(formatParsedApiError(parsed)) as Error & ErrorCarrier & {
    status?: number;
    category: ApiErrorCategory;
    rawMessage: string;
  };
  apiError.name = 'ApiRequestError';
  apiError.parsedError = parsed;
  apiError.response = extra.response;
  apiError.code = extra.code;
  apiError.status = parsed.status;
  apiError.category = parsed.category;
  apiError.rawMessage = parsed.rawMessage;
  if (extra.cause !== undefined) {
    apiError.cause = extra.cause;
  }
  return apiError;
}

export function attachParsedApiError(error: unknown): ParsedApiError {
  const parsed = parseApiError(error);
  if (isRecord(error)) {
    const carrier = error as ErrorCarrier;
    carrier.parsedError = parsed;
  }
  if (error instanceof Error) {
    error.name = 'ApiRequestError';
    error.message = formatParsedApiError(parsed);
  }
  return parsed;
}

export function isLocalConnectionFailure(error: unknown): boolean {
  return parseApiError(error).category === 'local_connection_failed';
}

export function parseApiError(error: unknown): ParsedApiError {
  const text = getErrorText();
  const response = getResponse(error);
  const status = response?.status;
  const payloadText = extractErrorPayloadText(response?.data);
  const errorCode = extractErrorCode(response?.data);
  const errorMessage = getErrorMessage(error);
  const causeMessage = getCauseMessage(error);
  const code = getErrorCode(error);
  const rawMessage = pickString(payloadText, response?.statusText, errorMessage, causeMessage, code)
    ?? text.requestIncompleteRetry;
  const matchText = buildMatchText([rawMessage, errorMessage, causeMessage, code, errorCode, response?.statusText]);

  if (includesAny(matchText, ['agent mode is not enabled', 'agent_mode'])) {
    return createParsedApiError({
      title: text.agentDisabled.title,
      message: text.agentDisabled.message,
      rawMessage,
      status,
      category: 'agent_disabled',
    });
  }

  const hasStockCodeField = includesAny(matchText, ['stock_code', 'stock_codes']);
  const hasMissingParamText = includesAny(matchText, ['必须提供 stock_code 或 stock_codes', 'missing', 'required']);
  if (hasStockCodeField && hasMissingParamText) {
    return createParsedApiError({
      title: text.missingParams.title,
      message: text.missingParams.message,
      rawMessage,
      status,
      category: 'missing_params',
    });
  }

  if (errorCode === 'portfolio_oversell' || includesAny(matchText, ['oversell detected'])) {
    return createParsedApiError({
      title: text.oversell.title,
      message: text.oversell.message,
      rawMessage,
      status,
      category: 'portfolio_oversell',
    });
  }

  if (errorCode === 'portfolio_busy' || includesAny(matchText, ['portfolio ledger is busy'])) {
    return createParsedApiError({
      title: text.portfolioBusy.title,
      message: text.portfolioBusy.message,
      rawMessage,
      status,
      category: 'portfolio_busy',
    });
  }

  if (errorCode === 'alphasift_install_failed') {
    return createParsedApiError({
      title: text.alphasiftInstallFailed.title,
      message: text.alphasiftInstallFailed.message,
      rawMessage,
      status,
      category: 'http_error',
    });
  }

  if (errorCode === 'alphasift_install_spec_missing') {
    return createParsedApiError({
      title: text.alphasiftSpecMissing.title,
      message: text.alphasiftSpecMissing.message,
      rawMessage,
      status,
      category: 'http_error',
    });
  }

  if (errorCode === 'alphasift_install_spec_not_allowed') {
    return createParsedApiError({
      title: text.alphasiftSpecNotAllowed.title,
      message: text.alphasiftSpecNotAllowed.message,
      rawMessage,
      status,
      category: 'http_error',
    });
  }

  if (errorCode === 'alphasift_unavailable' || includesAny(matchText, ['cannot import alphasift', 'alphasift.screen'])) {
    return createParsedApiError({
      title: text.alphasiftNotReady,
      message: rawMessage,
      rawMessage,
      status,
      category: 'http_error',
    });
  }

  if (errorCode === 'alphasift_adapter_unavailable') {
    return createParsedApiError({
      title: text.alphasiftAdapterUnavailable.title,
      message: text.alphasiftAdapterUnavailable.message,
      category: 'http_error',
      rawMessage,
      status,
    });
  }

  if (errorCode === 'alphasift_screen_task_not_found') {
    return createParsedApiError({
      title: text.screenTaskNotFound.title,
      message: text.screenTaskNotFound.message,
      rawMessage,
      status,
      category: 'http_error',
    });
  }

  if (errorCode === 'alphasift_screen_failed') {
    return createParsedApiError({
      title: text.screenFailed.title,
      message: text.screenFailed.message,
      rawMessage,
      status,
      category: 'upstream_network',
    });
  }

  const noConfiguredLlm = (
    includesAny(matchText, ['all llm models failed']) && includesAny(matchText, ['last error: none'])
  ) || includesAny(matchText, [
    'no llm configured',
    'no effective primary model configured',
    'litellm_model not configured',
    'ai analysis will be unavailable',
  ]);
  if (noConfiguredLlm) {
    return createParsedApiError({
      title: text.llmNotConfigured.title,
      message: text.llmNotConfigured.message,
      rawMessage,
      status,
      category: 'llm_not_configured',
    });
  }

  if (includesAny(matchText, [
    'tool call',
    'function call',
    'does not support tools',
    'tools is not supported',
    'reasoning',
  ])) {
    return createParsedApiError({
      title: text.modelToolIncompatible.title,
      message: text.modelToolIncompatible.message,
      rawMessage,
      status,
      category: 'model_tool_incompatible',
    });
  }

  if (includesAny(matchText, [
    'thought_signature',
    'missing function',
    'missing tool',
    'invalid tool call',
    'invalid function call',
  ])) {
    return createParsedApiError({
      title: text.invalidToolCall.title,
      message: text.invalidToolCall.message,
      rawMessage,
      status,
      category: 'invalid_tool_call',
    });
  }

  if (includesAny(matchText, ['timeout', 'timed out', 'read timeout', 'connect timeout']) || code === 'ECONNABORTED') {
    return createParsedApiError({
      title: text.upstreamTimeout.title,
      message: text.upstreamTimeout.message,
      rawMessage,
      status,
      category: 'upstream_timeout',
    });
  }

  if (
    status === 502
    || status === 503
    || includesAny(matchText, [
      'dns',
      'enotfound',
      'name or service not known',
      'temporary failure in name resolution',
      'proxy',
      'tunnel',
      '502',
      '503',
    ])
  ) {
    return createParsedApiError({
      title: text.upstreamNetwork.title,
      message: text.upstreamNetwork.message,
      rawMessage,
      status,
      category: 'upstream_network',
    });
  }

  const hasLlmProviderHint = includesAny(matchText, [
    'chat/completions',
    'generativelanguage',
    'openai',
    'gemini',
  ]);
  if (status === 400 && hasLlmProviderHint) {
    return createParsedApiError({
      title: text.upstreamLlm400.title,
      message: text.upstreamLlm400.message,
      rawMessage,
      status,
      category: 'upstream_llm_400',
    });
  }

  const localConnectionFailed = !response && (
    includesAny(matchText, ['fetch failed', 'failed to fetch', 'network error', 'connection refused', 'econnrefused'])
    || code === 'ERR_NETWORK'
    || code === 'ECONNREFUSED'
  );
  if (localConnectionFailed) {
    return createParsedApiError({
      title: text.localConnectionFailed.title,
      message: text.localConnectionFailed.message,
      rawMessage,
      status,
      category: 'local_connection_failed',
    });
  }

  if (payloadText || status) {
    return createParsedApiError({
      title: text.requestFailed,
      message: payloadText ?? text.requestIncompleteHttp.replace('{status}', String(status)),
      rawMessage,
      status,
      category: 'http_error',
    });
  }

  return createParsedApiError({
    title: text.requestFailed,
    message: rawMessage,
    rawMessage,
    status,
    category: 'unknown',
  });
}

export function toApiErrorMessage(error: unknown, fallback?: string): string {
  const parsed = getParsedApiError(error);
  const message = formatParsedApiError(parsed);
  return message.trim() || fallback || getErrorText().requestIncompleteRetry;
}

export function isAxiosApiError(error: unknown): boolean {
  return axios.isAxiosError(error);
}
