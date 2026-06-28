import type React from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { CheckCircle2, ChevronDown, CircleAlert, CircleDashed, Clock, Play, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { useAuth, useSystemConfig } from '../hooks';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import { createParsedApiError, getParsedApiError, type ParsedApiError } from '../api/error';
import { analysisApi } from '../api/analysis';
import { alphasiftApi, notifyAlphaSiftConfigChanged, notifySystemConfigChanged } from '../api/alphasift';
import { systemConfigApi } from '../api/systemConfig';
import { ApiErrorAlert, Button, ConfirmDialog, EmptyState } from '../components/common';
import {
  AuthSettingsCard,
  ChangePasswordCard,
  IntelligentImport,
  LLMChannelEditor,
  NotificationTestPanel,
  SettingsCategoryNav,
  SettingsAlert,
  SettingsField,
  SettingsLoading,
  SettingsPanelErrorBoundary,
  SettingsSectionCard,
} from '../components/settings';
import { WEB_BUILD_INFO } from '../utils/constants';
import { getCategoryDescription } from '../utils/systemConfigI18n';
import type {
  ConfigValidationIssue,
  SchedulerStatusResponse,
  SetupStatusCheck,
  SetupStatusResponse,
  SystemConfigCategory,
  SystemConfigItem,
  SystemConfigUpdateItem,
} from '../types/systemConfig';
import type { UiLanguage, UiTextKey } from '../i18n/uiText';

type DesktopWindow = Window & {
  dsaDesktop?: {
    version?: unknown;
    getUpdateState?: () => Promise<RawDesktopUpdateState>;
    checkForUpdates?: () => Promise<RawDesktopUpdateState>;
    installDownloadedUpdate?: () => Promise<boolean>;
    openReleasePage?: (releaseUrl?: string) => Promise<boolean>;
    onUpdateStateChange?: (listener: (state: RawDesktopUpdateState) => void) => (() => void) | void;
  };
};

type DesktopUpdateState = {
  status?: string;
  updateMode?: string;
  currentVersion?: string;
  latestVersion?: string;
  releaseUrl?: string;
  checkedAt?: string;
  publishedAt?: string;
  message?: string;
  releaseName?: string;
  tagName?: string;
  downloadPercent?: number | null;
  downloadedBytes?: number | null;
  totalBytes?: number | null;
};

type RawDesktopUpdateState = {
  status?: unknown;
  updateMode?: unknown;
  currentVersion?: unknown;
  latestVersion?: unknown;
  releaseUrl?: unknown;
  checkedAt?: unknown;
  publishedAt?: unknown;
  message?: unknown;
  releaseName?: unknown;
  tagName?: unknown;
  downloadPercent?: unknown;
  downloadedBytes?: unknown;
  totalBytes?: unknown;
};

type DesktopUpdateNotice = {
  title: string;
  message: string;
  variant: 'error' | 'success' | 'warning';
  actionLabel?: string;
  actionKind?: 'release' | 'install';
};

const PROMPT_CACHE_ADVANCED_SETTING_KEYS = new Set([
  'LLM_PROMPT_CACHE_TELEMETRY_ENABLED',
  'LLM_PROMPT_CACHE_HINTS_ENABLED',
  'LLM_PROMPT_CACHE_DIAGNOSTICS_LEVEL',
]);

function isPromptCacheAdvancedSetting(item: { key: string }) {
  return PROMPT_CACHE_ADVANCED_SETTING_KEYS.has(item.key);
}

function trimDesktopRuntimeString(value: unknown) {
  return typeof value === 'string' ? value.trim() : '';
}

function normalizeDesktopRuntimeNumber(value: unknown) {
  if (value === null || value === undefined || value === '') {
    return null;
  }
  const numberValue = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}

function getDesktopRuntimeApi() {
  if (typeof window === 'undefined') {
    return undefined;
  }

  return (window as DesktopWindow).dsaDesktop;
}

function getDesktopAppVersion() {
  return trimDesktopRuntimeString(getDesktopRuntimeApi()?.version);
}

function normalizeDesktopUpdateState(state: RawDesktopUpdateState | null | undefined) {
  if (!state || typeof state !== 'object') {
    return null;
  }

  return {
    status: trimDesktopRuntimeString(state.status) || 'idle',
    updateMode: trimDesktopRuntimeString(state.updateMode) || 'manual',
    currentVersion: trimDesktopRuntimeString(state.currentVersion),
    latestVersion: trimDesktopRuntimeString(state.latestVersion),
    releaseUrl: trimDesktopRuntimeString(state.releaseUrl),
    checkedAt: trimDesktopRuntimeString(state.checkedAt),
    publishedAt: trimDesktopRuntimeString(state.publishedAt),
    message: trimDesktopRuntimeString(state.message),
    releaseName: trimDesktopRuntimeString(state.releaseName),
    tagName: trimDesktopRuntimeString(state.tagName),
    downloadPercent: normalizeDesktopRuntimeNumber(state.downloadPercent),
    downloadedBytes: normalizeDesktopRuntimeNumber(state.downloadedBytes),
    totalBytes: normalizeDesktopRuntimeNumber(state.totalBytes),
  };
}

function getDesktopUpdateNotice(
  state: DesktopUpdateState | null,
  t: (key: UiTextKey, params?: Record<string, string | number>) => string,
): DesktopUpdateNotice | null {
  if (!state) {
    return null;
  }

  if (state.status === 'update-available') {
    const latestLabel = state.latestVersion || state.tagName || t('settings.desktopLatest');
    const currentLabel = state.currentVersion || getDesktopAppVersion() || WEB_BUILD_INFO.version;
    return {
      title: t('settings.desktopUpdateAvailable'),
      message: t('settings.desktopUpdateMessage', {
        current: currentLabel,
        latest: latestLabel,
        message: state.message || t('settings.desktopUpdateReleaseMessage'),
      }),
      variant: 'warning' as const,
      actionLabel: state.updateMode === 'auto' ? undefined : t('settings.desktopDownload'),
      actionKind: state.updateMode === 'auto' ? undefined : 'release',
    };
  }

  if (state.status === 'downloading') {
    const percentText = typeof state.downloadPercent === 'number' ? `（${state.downloadPercent}%）` : '';
    return {
      title: t('settings.desktopDownloading'),
      message: state.message || t('settings.desktopUpdateDownloadingMessage', { percent: percentText }),
      variant: 'warning' as const,
    };
  }

  if (state.status === 'update-downloaded') {
    return {
      title: t('settings.desktopDownloaded'),
      message: state.message || t('settings.desktopUpdateDownloadedMessage'),
      variant: 'success' as const,
      actionLabel: t('settings.desktopInstall'),
      actionKind: 'install',
    };
  }

  if (state.status === 'installing') {
    return {
      title: t('settings.desktopInstalling'),
      message: state.message || t('settings.desktopUpdateInstallingMessage'),
      variant: 'warning' as const,
    };
  }

  if (state.status === 'up-to-date') {
    return {
      title: t('settings.desktopUpToDate'),
      message: state.message || t('settings.desktopUpToDateMessage'),
      variant: 'success' as const,
    };
  }

  if (state.status === 'checking') {
    return {
      title: t('settings.desktopChecking'),
      message: state.message || t('settings.desktopUpdateCheckingMessage'),
      variant: 'warning' as const,
    };
  }

  if (state.status === 'error') {
    return {
      title: t('settings.desktopCheckError'),
      message: state.message || t('settings.desktopUpdateErrorMessage'),
      variant: 'error' as const,
      actionLabel: state.updateMode === 'auto' && state.releaseUrl ? t('settings.desktopDownload') : undefined,
      actionKind: state.updateMode === 'auto' && state.releaseUrl ? 'release' : undefined,
    };
  }

  return null;
}

function formatEnvBackupFilename(isDesktopRuntime: boolean) {
  const now = new Date();
  const pad = (value: number) => value.toString().padStart(2, '0');
  const date = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
  const time = `${pad(now.getHours())}${pad(now.getMinutes())}`;
  return `${isDesktopRuntime ? 'dsa-desktop-env' : 'dsa-env'}_${date}_${time}.env`;
}

const SCHEDULE_TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
const SCHEDULER_DEFAULT_TIME = '18:00';
const SCHEDULER_SETTING_KEYS = new Set([
  'SCHEDULE_ENABLED',
  'SCHEDULE_TIME',
  'SCHEDULE_TIMES',
  'SCHEDULE_RUN_IMMEDIATELY',
]);

function getConfigItem(items: SystemConfigItem[], key: string) {
  return items.find((item) => item.key === key);
}

function parseSetupStockList(value: unknown) {
  return String(value ?? '')
    .split(/[,\n\r;，、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function isEnabledConfigValue(value: unknown) {
  return String(value ?? '').trim().toLowerCase() === 'true';
}

function getSetupCheckIcon(check: SetupStatusCheck) {
  if (check.status === 'configured' || check.status === 'inherited') {
    return <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" aria-hidden="true" />;
  }
  if (check.status === 'needs_action') {
    return <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />;
  }
  return <CircleDashed className="mt-0.5 h-4 w-4 shrink-0 text-muted-text" aria-hidden="true" />;
}

function getSetupCheckStatusLabel(
  check: SetupStatusCheck,
  t: (key: UiTextKey, params?: Record<string, string | number>) => string,
) {
  if (check.status === 'configured') return t('settings.setupStatusConfigured');
  if (check.status === 'inherited') return t('settings.setupStatusInherited');
  if (check.status === 'needs_action') return t('settings.setupStatusNeedsAction');
  return t('settings.setupStatusOptional');
}

const setupCheckTitleMap: Partial<Record<UiLanguage, Record<string, string>>> = {
  en: {
    llm_primary: 'LLM primary channel',
    llm_agent: 'Agent channel',
    stock_list: 'Watchlist',
    notification: 'Notification channel',
    storage: 'Database / local storage',
  },
  ko: {
    llm_primary: 'LLM 주 채널',
    llm_agent: 'Agent 채널',
    stock_list: '관심 종목',
    notification: '알림 채널',
    storage: '데이터베이스 / 로컬 저장소',
  },
};

const setupMessageMap: Partial<Record<UiLanguage, Record<string, string>>> = {
  en: {
    '已启用 Codex CLI 本地生成 Backend（experimental/limited）。': 'Codex CLI local generation backend is enabled (experimental/limited).',
    '已选择 codex_cli，但未找到 codex 可执行文件。': 'codex_cli is selected, but the codex executable was not found.',
    '主模型未出现在当前 LiteLLM YAML model_list 中': 'The primary model is not in the current LiteLLM YAML model_list.',
    '主模型未出现在当前启用渠道模型列表中': 'The primary model is not in the enabled channel model list.',
    '主模型缺少可用渠道或匹配的 API Key': 'The primary model has no available channel or matching API key.',
    '尚未检测到主模型配置': 'No primary model configuration was detected.',
    'Agent 工具调用暂不支持 codex_cli text-only backend。': 'Agent tool calls do not support the codex_cli text-only backend yet.',
    'Agent 工具调用将继续使用 LiteLLM 渠道。': 'Agent tool calls will continue using the LiteLLM channel.',
    'AGENT_GENERATION_BACKEND 已选择 litellm，但未检测到可用 LiteLLM 模型配置。': 'AGENT_GENERATION_BACKEND is set to litellm, but no available LiteLLM model configuration was detected.',
    'Agent 工具调用需要 LiteLLM 模型配置；codex_cli 主生成方式不会被自动继承。': 'Agent tool calls require a LiteLLM model configuration; the codex_cli primary generation backend is not inherited automatically.',
    '未单独配置 Agent 主模型，将继承 LLM 主渠道。': 'No separate Agent primary model is configured; it will inherit the LLM primary channel.',
    'Agent 未配置独立模型，且 LLM 主渠道尚不可用。': 'No separate Agent model is configured, and the LLM primary channel is not available yet.',
    '当前 STOCK_LIST 为空。': 'STOCK_LIST is currently empty.',
    '已检测到至少一个通知渠道配置。': 'At least one notification channel configuration was detected.',
    '通知为可选项，未配置也不影响首次跑通。': 'Notifications are optional; leaving them unconfigured does not block the first run.',
  },
  ko: {
    '已启用 Codex CLI 本地生成 Backend（experimental/limited）。': 'Codex CLI 로컬 생성 백엔드가 활성화되었습니다(실험/제한 기능).',
    '已选择 codex_cli，但未找到 codex 可执行文件。': 'codex_cli가 선택되었지만 codex 실행 파일을 찾을 수 없습니다.',
    '主模型未出现在当前 LiteLLM YAML model_list 中': '주 모델이 현재 LiteLLM YAML model_list에 없습니다.',
    '主模型未出现在当前启用渠道模型列表中': '주 모델이 현재 활성화된 채널 모델 목록에 없습니다.',
    '主模型缺少可用渠道或匹配的 API Key': '주 모델에 사용할 수 있는 채널 또는 일치하는 API Key가 없습니다.',
    '尚未检测到主模型配置': '주 모델 설정이 아직 감지되지 않았습니다.',
    'Agent 工具调用暂不支持 codex_cli text-only backend。': 'Agent 도구 호출은 아직 codex_cli text-only 백엔드를 지원하지 않습니다.',
    'Agent 工具调用将继续使用 LiteLLM 渠道。': 'Agent 도구 호출은 계속 LiteLLM 채널을 사용합니다.',
    'AGENT_GENERATION_BACKEND 已选择 litellm，但未检测到可用 LiteLLM 模型配置。': 'AGENT_GENERATION_BACKEND가 litellm으로 설정되었지만 사용할 수 있는 LiteLLM 모델 설정을 감지하지 못했습니다.',
    'Agent 工具调用需要 LiteLLM 模型配置；codex_cli 主生成方式不会被自动继承。': 'Agent 도구 호출에는 LiteLLM 모델 설정이 필요합니다. codex_cli 주 생성 방식은 자동으로 상속되지 않습니다.',
    '未单独配置 Agent 主模型，将继承 LLM 主渠道。': 'Agent 주 모델을 별도로 설정하지 않아 LLM 주 채널을 상속합니다.',
    'Agent 未配置独立模型，且 LLM 主渠道尚不可用。': 'Agent 독립 모델이 설정되지 않았고 LLM 주 채널도 아직 사용할 수 없습니다.',
    '当前 STOCK_LIST 为空。': '현재 STOCK_LIST가 비어 있습니다.',
    '已检测到至少一个通知渠道配置。': '최소 하나 이상의 알림 채널 설정이 감지되었습니다.',
    '通知为可选项，未配置也不影响首次跑通。': '알림은 선택 사항이며, 설정하지 않아도 최초 실행에는 영향이 없습니다.',
  },
};

const setupNextStepMap: Partial<Record<UiLanguage, Record<string, string>>> = {
  en: {
    '请先安装并登录 Codex CLI，或将 GENERATION_BACKEND 设回 litellm。': 'Install and sign in to Codex CLI first, or set GENERATION_BACKEND back to litellm.',
    '请配置 LITELLM_MODEL、LLM_CHANNELS、LITELLM_CONFIG 或 legacy provider API Key。': 'Configure LITELLM_MODEL, LLM_CHANNELS, LITELLM_CONFIG, or a legacy provider API key.',
    '请将 AGENT_GENERATION_BACKEND 设为 auto 或 litellm，并配置 LiteLLM 工具调用渠道。': 'Set AGENT_GENERATION_BACKEND to auto or litellm, then configure a LiteLLM tool-calling channel.',
    '如需使用 Ask-Stock Agent，请配置 AGENT_LITELLM_MODEL、LITELLM_MODEL、LLM_CHANNELS 或 LITELLM_CONFIG。': 'To use the Ask-Stock Agent, configure AGENT_LITELLM_MODEL, LITELLM_MODEL, LLM_CHANNELS, or LITELLM_CONFIG.',
    '如需使用 Ask-Stock Agent，请配置 LiteLLM 模型，或将 AGENT_GENERATION_BACKEND 固定为 litellm 后补齐模型配置。': 'To use the Ask-Stock Agent, configure a LiteLLM model or set AGENT_GENERATION_BACKEND to litellm and complete the model setup.',
    '请先补齐 LLM 主渠道配置。': 'Complete the LLM primary channel configuration first.',
    '请调整 AGENT_LITELLM_MODEL 或补齐对应渠道配置。': 'Adjust AGENT_LITELLM_MODEL or complete the matching channel configuration.',
    '请至少添加 1 只股票用于首次试跑。': 'Add at least one stock for the first smoke run.',
    '需要推送时可稍后配置飞书、Telegram、邮件或其他通知渠道。': 'Configure Feishu, Telegram, email, or another notification channel later if delivery is needed.',
    '请检查 DATABASE_PATH 或上级目录权限。': 'Check DATABASE_PATH or parent directory permissions.',
    '请调整 DATABASE_PATH 或目录权限。': 'Adjust DATABASE_PATH or directory permissions.',
  },
  ko: {
    '请先安装并登录 Codex CLI，或将 GENERATION_BACKEND 设回 litellm。': '먼저 Codex CLI를 설치하고 로그인하거나 GENERATION_BACKEND를 litellm으로 되돌리세요.',
    '请配置 LITELLM_MODEL、LLM_CHANNELS、LITELLM_CONFIG 或 legacy provider API Key。': 'LITELLM_MODEL, LLM_CHANNELS, LITELLM_CONFIG 또는 legacy provider API Key를 설정하세요.',
    '请将 AGENT_GENERATION_BACKEND 设为 auto 或 litellm，并配置 LiteLLM 工具调用渠道。': 'AGENT_GENERATION_BACKEND를 auto 또는 litellm으로 설정하고 LiteLLM 도구 호출 채널을 설정하세요.',
    '如需使用 Ask-Stock Agent，请配置 AGENT_LITELLM_MODEL、LITELLM_MODEL、LLM_CHANNELS 或 LITELLM_CONFIG。': 'Ask-Stock Agent를 사용하려면 AGENT_LITELLM_MODEL, LITELLM_MODEL, LLM_CHANNELS 또는 LITELLM_CONFIG를 설정하세요.',
    '如需使用 Ask-Stock Agent，请配置 LiteLLM 模型，或将 AGENT_GENERATION_BACKEND 固定为 litellm 后补齐模型配置。': 'Ask-Stock Agent를 사용하려면 LiteLLM 모델을 설정하거나 AGENT_GENERATION_BACKEND를 litellm으로 고정한 뒤 모델 설정을 완료하세요.',
    '请先补齐 LLM 主渠道配置。': '먼저 LLM 주 채널 설정을 완료하세요.',
    '请调整 AGENT_LITELLM_MODEL 或补齐对应渠道配置。': 'AGENT_LITELLM_MODEL을 조정하거나 해당 채널 설정을 완료하세요.',
    '请至少添加 1 只股票用于首次试跑。': '최초 테스트 실행을 위해 최소 1개 종목을 추가하세요.',
    '需要推送时可稍后配置飞书、Telegram、邮件或其他通知渠道。': '전송이 필요하면 나중에 Feishu, Telegram, 이메일 또는 기타 알림 채널을 설정하세요.',
    '请检查 DATABASE_PATH 或上级目录权限。': 'DATABASE_PATH 또는 상위 디렉터리 권한을 확인하세요.',
    '请调整 DATABASE_PATH 或目录权限。': 'DATABASE_PATH 또는 디렉터리 권한을 조정하세요.',
  },
};

function getSetupCheckTitle(check: SetupStatusCheck, language: UiLanguage) {
  return setupCheckTitleMap[language]?.[check.key] || check.title;
}

function getSetupSourceLabel(source: string, language: UiLanguage) {
  const labels: Partial<Record<UiLanguage, Record<string, string>>> = {
    en: {
      显式主模型: 'explicit primary model',
      'LiteLLM YAML': 'LiteLLM YAML',
      'LLM 渠道': 'LLM channel',
      'legacy provider': 'legacy provider',
    },
    ko: {
      显式主模型: '명시적 주 모델',
      'LiteLLM YAML': 'LiteLLM YAML',
      'LLM 渠道': 'LLM 채널',
      'legacy provider': 'legacy provider',
    },
  };
  return labels[language]?.[source] || source;
}

function getSetupCheckMessage(check: SetupStatusCheck, language: UiLanguage) {
  if (language === 'zh') {
    return check.message;
  }

  const detectedModel = check.message.match(/^已检测到 (.+): (.+)$/);
  if (detectedModel) {
    const source = getSetupSourceLabel(detectedModel[1], language);
    return language === 'ko'
      ? `${source} 감지됨: ${detectedModel[2]}`
      : `Detected ${source}: ${detectedModel[2]}`;
  }

  const agentModel = check.message.match(/^已配置 Agent 主模型: (.+)$/);
  if (agentModel) {
    return language === 'ko'
      ? `Agent 주 모델 설정됨: ${agentModel[1]}`
      : `Agent primary model configured: ${agentModel[1]}`;
  }

  const agentModelMissing = check.message.match(/^Agent 主模型 (.+) 缺少可用渠道或匹配的 API Key。$/);
  if (agentModelMissing) {
    return language === 'ko'
      ? `Agent 주 모델 ${agentModelMissing[1]}에 사용할 수 있는 채널 또는 일치하는 API Key가 없습니다.`
      : `Agent primary model ${agentModelMissing[1]} has no available channel or matching API key.`;
  }

  const stockCount = check.message.match(/^已配置 (\d+) 只股票。$/);
  if (stockCount) {
    return language === 'ko'
      ? `${stockCount[1]}개 종목이 설정되었습니다.`
      : `${stockCount[1]} stock(s) configured.`;
  }

  const dbAvailable = check.message.match(/^数据库路径可用: (.+)$/);
  if (dbAvailable) {
    return language === 'ko'
      ? `데이터베이스 경로 사용 가능: ${dbAvailable[1]}`
      : `Database path available: ${dbAvailable[1]}`;
  }

  const dbParentCreatable = check.message.match(/^数据库上级目录可创建: (.+)$/);
  if (dbParentCreatable) {
    return language === 'ko'
      ? `데이터베이스 상위 디렉터리 생성 가능: ${dbParentCreatable[1]}`
      : `Database parent directory can be created: ${dbParentCreatable[1]}`;
  }

  const dbParentUnavailable = check.message.match(/^数据库路径父目录不可用: (.+)$/);
  if (dbParentUnavailable) {
    return language === 'ko'
      ? `데이터베이스 경로의 상위 디렉터리를 사용할 수 없습니다: ${dbParentUnavailable[1]}`
      : `Database parent directory is unavailable: ${dbParentUnavailable[1]}`;
  }

  const dbParentNotWritable = check.message.match(/^数据库路径上级目录不可写: (.+)$/);
  if (dbParentNotWritable) {
    return language === 'ko'
      ? `데이터베이스 경로의 상위 디렉터리에 쓸 수 없습니다: ${dbParentNotWritable[1]}`
      : `Database parent directory is not writable: ${dbParentNotWritable[1]}`;
  }

  return setupMessageMap[language]?.[check.message] || check.message;
}

function getSetupCheckNextStep(check: SetupStatusCheck, language: UiLanguage) {
  if (!check.nextStep || language === 'zh') {
    return check.nextStep;
  }
  return setupNextStepMap[language]?.[check.nextStep] || check.nextStep;
}

type FirstRunSetupCardProps = {
  status: SetupStatusResponse | null;
  isLoading: boolean;
  error: ParsedApiError | null;
  firstStockCode: string;
  isSaving: boolean;
  isRunningSmoke: boolean;
  smokeError: ParsedApiError | null;
  smokeSuccess: string;
  onRefresh: () => void | Promise<void>;
  onSelectCategory: (category: SystemConfigCategory) => void;
  onRunSmoke: () => void | Promise<void>;
  listSeparator: string;
  t: (key: UiTextKey, params?: Record<string, string | number>) => string;
  language: UiLanguage;
};

const FirstRunSetupCard: React.FC<FirstRunSetupCardProps> = ({
  status,
  isLoading,
  error,
  firstStockCode,
  isSaving,
  isRunningSmoke,
  smokeError,
  smokeSuccess,
  onRefresh,
  onSelectCategory,
  onRunSmoke,
  listSeparator,
  t,
  language,
}) => {
  const [isHidden, setIsHidden] = useState(false);
  const requiredMissing = status?.checks.filter((check) => check.required && check.status === 'needs_action') || [];
  const isComplete = Boolean(status?.isComplete);
  const canRunSmoke = Boolean(status?.readyForSmoke && firstStockCode);
  const summaryTitle = !status
    ? error
      ? t('settings.setupGuideUnknownTitle')
      : t('settings.setupGuideCheckingTitle')
    : isComplete
      ? t('settings.setupGuideCompleteTitle')
      : t('settings.setupGuideIncompleteTitle');
  const summaryMessage = !status
    ? error
      ? t('settings.setupGuideUnknownSummary')
      : t('settings.setupGuideCheckingSummary')
    : requiredMissing.length
      ? t('settings.setupGuideMissingSummary', {
        count: requiredMissing.length,
        labels: requiredMissing.slice(0, 3).map((check) => getSetupCheckTitle(check, language)).join(listSeparator),
      })
      : t('settings.setupGuideReadySummary');

  if (isHidden) {
    return (
      <div className="rounded-2xl border settings-border bg-card/90 px-4 py-3 shadow-soft-card">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm font-semibold text-foreground">{t('settings.setupGuideHiddenTitle')}</p>
            <p className="mt-1 text-xs leading-5 text-muted-text">{t('settings.setupGuideHiddenDescription')}</p>
          </div>
          <Button type="button" variant="settings-secondary" size="sm" onClick={() => setIsHidden(false)}>
            {t('settings.setupGuideOpen')}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <SettingsSectionCard
      title={t('settings.setupGuideTitle')}
      description={t('settings.setupGuideDescription')}
    >
      <div data-testid="first-run-setup-card" className="space-y-4">
        <div className="flex flex-col gap-3 rounded-2xl border settings-border bg-background/35 px-4 py-4 md:flex-row md:items-start md:justify-between">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-foreground">
              {summaryTitle}
            </p>
            <p className="mt-1 text-xs leading-6 text-muted-text">
              {summaryMessage}
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="settings-secondary"
              size="sm"
              disabled={isLoading}
              isLoading={isLoading}
              loadingText={t('settings.setupGuideRefreshing')}
              onClick={() => void onRefresh()}
            >
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              {t('settings.setupGuideRefresh')}
            </Button>
            <Button type="button" variant="settings-secondary" size="sm" onClick={() => setIsHidden(true)}>
              {t('settings.setupGuideHide')}
            </Button>
          </div>
        </div>

        {error ? <ApiErrorAlert error={error} /> : null}

        {isLoading && !status ? (
          <p className="text-sm text-muted-text">{t('common.loading')}</p>
        ) : null}

        {status ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {status.checks.map((check) => (
              <div
                key={check.key}
                className="rounded-2xl border settings-border bg-card/65 px-4 py-3"
              >
                <div className="flex items-start gap-3">
                  {getSetupCheckIcon(check)}
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="text-sm font-semibold text-foreground">{getSetupCheckTitle(check, language)}</p>
                      <span className="rounded-full border settings-border bg-background/60 px-2 py-0.5 text-[11px] font-medium text-muted-text">
                        {getSetupCheckStatusLabel(check, t)}
                      </span>
                    </div>
                    <p className="mt-1 text-xs leading-5 text-muted-text">{getSetupCheckMessage(check, language)}</p>
                    {check.nextStep ? (
                      <p className="mt-2 text-xs leading-5 text-secondary-text">{getSetupCheckNextStep(check, language)}</p>
                    ) : null}
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="settings-secondary" size="sm" onClick={() => onSelectCategory('ai_model')}>
            {t('settings.setupGuideConfigureLlm')}
          </Button>
          <Button type="button" variant="settings-secondary" size="sm" onClick={() => onSelectCategory('base')}>
            {t('settings.setupGuideAddStocks')}
          </Button>
          <Button type="button" variant="settings-secondary" size="sm" onClick={() => onSelectCategory('notification')}>
            {t('settings.setupGuideConfigureNotification')}
          </Button>
          <Button
            type="button"
            variant="settings-primary"
            size="sm"
            disabled={!canRunSmoke || isSaving || isRunningSmoke}
            isLoading={isRunningSmoke}
            loadingText={t('settings.setupGuideSmokeRunning')}
            title={!firstStockCode ? t('settings.setupGuideSmokeNeedsStock') : undefined}
            onClick={() => void onRunSmoke()}
          >
            <Play className="h-4 w-4" aria-hidden="true" />
            {t('settings.setupGuideRunSmoke')}
          </Button>
        </div>

        {!canRunSmoke && status ? (
          <p className="text-xs leading-6 text-muted-text">
            {firstStockCode ? t('settings.setupGuideSmokeNotReady') : t('settings.setupGuideSmokeNeedsStock')}
          </p>
        ) : null}
        {smokeError ? <ApiErrorAlert error={smokeError} /> : null}
        {!smokeError && smokeSuccess ? (
          <SettingsAlert title={t('settings.actionSuccess')} message={smokeSuccess} variant="success" />
        ) : null}
      </div>
    </SettingsSectionCard>
  );
};

function parseScheduleTimes(scheduleTimesValue?: string, fallbackValue?: string) {
  const values = String(scheduleTimesValue ?? '')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);

  if (values.length > 0) {
    return values;
  }

  const fallback = String(fallbackValue ?? '').trim();
  return fallback ? [fallback] : [SCHEDULER_DEFAULT_TIME];
}

function serializeScheduleTimes(times: string[]) {
  return times.map((time) => time.trim()).filter(Boolean).join(',');
}

function formatSchedulerTimestamp(value: string | null | undefined, language: UiLanguage) {
  if (!value) {
    return '-';
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat(language === 'en' ? 'en-US' : language === 'ko' ? 'ko-KR' : 'zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

type SchedulerSettingsCardProps = {
  items: SystemConfigItem[];
  disabled: boolean;
  issueByKey: Record<string, ConfigValidationIssue[]>;
  statusRefreshToken: number;
  onChange: (key: string, value: string) => void;
  onSchedulerStateChange?: (payload: {
    runtimeEnabled: boolean | null;
    overrideEnabled: boolean | null;
  }) => void;
  t: (key: UiTextKey, params?: Record<string, string | number>) => string;
  language: UiLanguage;
};

const SchedulerSettingsCard: React.FC<SchedulerSettingsCardProps> = ({
  items,
  disabled,
  issueByKey,
  statusRefreshToken,
  onChange,
  onSchedulerStateChange,
  t,
  language,
}) => {
  const scheduleEnabledItem = getConfigItem(items, 'SCHEDULE_ENABLED');
  const scheduleTimesItem = getConfigItem(items, 'SCHEDULE_TIMES');
  const scheduleTimeItem = getConfigItem(items, 'SCHEDULE_TIME');
  const hasSchedulerSettings = Boolean(scheduleEnabledItem || scheduleTimesItem || scheduleTimeItem);
  const [status, setStatus] = useState<SchedulerStatusResponse | null>(null);
  const [isRefreshingStatus, setIsRefreshingStatus] = useState(false);
  const [isRunningNow, setIsRunningNow] = useState(false);
  const [statusError, setStatusError] = useState<ParsedApiError | null>(null);
  const [runNowError, setRunNowError] = useState<ParsedApiError | null>(null);
  const [runNowSuccess, setRunNowSuccess] = useState('');
  const [scheduleEnabledOverride, setScheduleEnabledOverride] = useState<boolean | null>(null);

  const refreshSchedulerStatus = useCallback(async () => {
    setStatusError(null);
    setIsRefreshingStatus(true);
    try {
      const payload = await systemConfigApi.getSchedulerStatus();
      setStatus(payload);
    } catch (error: unknown) {
      setStatusError(getParsedApiError(error));
    } finally {
      setIsRefreshingStatus(false);
    }
  }, []);

  useEffect(() => {
    if (!hasSchedulerSettings) {
      return;
    }
    void refreshSchedulerStatus();
  }, [hasSchedulerSettings, refreshSchedulerStatus, statusRefreshToken]);

  useEffect(() => {
    const isRuntimeDerived = isEnabledConfigValue(scheduleEnabledItem?.value) === status?.enabled;
    if (!status) {
      return;
    }

    if (scheduleEnabledOverride === null && isRuntimeDerived) {
      setScheduleEnabledOverride(null);
    }
  }, [scheduleEnabledItem?.value, scheduleEnabledOverride, statusRefreshToken]);

  useEffect(() => {
    if (!onSchedulerStateChange) {
      return;
    }

    const runtimeEnabled = status?.enabled ?? null;
    onSchedulerStateChange({
      runtimeEnabled,
      overrideEnabled: scheduleEnabledOverride,
    });
  }, [onSchedulerStateChange, status?.enabled, scheduleEnabledOverride]);

  if (!hasSchedulerSettings) {
    return null;
  }

  const scheduleEnabled = isEnabledConfigValue(scheduleEnabledItem?.value);
  const scheduleTimes = parseScheduleTimes(
    String(scheduleTimesItem?.value ?? ''),
    String(scheduleTimeItem?.value ?? ''),
  );
  const timeTargetKey = scheduleTimesItem ? 'SCHEDULE_TIMES' : 'SCHEDULE_TIME';
  const statusEnabled = status?.enabled ?? scheduleEnabled;
  const displayedScheduleEnabled = scheduleEnabledOverride ?? statusEnabled;
  const effectiveStatusTimes = status?.scheduleTimes?.length ? status.scheduleTimes : scheduleTimes.filter(Boolean);
  const validationIssues = [
    ...(issueByKey.SCHEDULE_ENABLED || []),
    ...(issueByKey.SCHEDULE_TIMES || []),
    ...(issueByKey.SCHEDULE_TIME || []),
  ];

  const updateScheduleTimes = (nextTimes: string[]) => {
    if (timeTargetKey === 'SCHEDULE_TIME') {
      onChange(timeTargetKey, nextTimes[0] || '');
      return;
    }
    onChange(timeTargetKey, serializeScheduleTimes(nextTimes));
  };

  const runSchedulerNow = async () => {
    setRunNowError(null);
    setRunNowSuccess('');
    setIsRunningNow(true);
    try {
      await systemConfigApi.runSchedulerNow();
      setRunNowSuccess(t('settings.schedulerRunAccepted'));
      await refreshSchedulerStatus();
    } catch (error: unknown) {
      setRunNowError(getParsedApiError(error));
    } finally {
      setIsRunningNow(false);
    }
  };

  return (
    <SettingsSectionCard
      title={t('settings.schedulerTitle')}
      description={t('settings.schedulerDescription')}
    >
      <div data-testid="scheduler-settings-card" className="space-y-4">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(300px,360px)]">
          <div className="space-y-4 rounded-2xl border settings-border bg-background/35 px-4 py-4">
                <label className="flex items-start gap-3">
                  <input
                    type="checkbox"
                    className="mt-1 h-4 w-4 rounded border-border text-cyan focus:ring-cyan/20"
                    checked={displayedScheduleEnabled}
                    data-testid="scheduler-enabled-checkbox"
                    disabled={disabled || !scheduleEnabledItem?.schema?.isEditable}
                    onChange={(event) => {
                      const nextEnabled = Boolean(event.target.checked);
                      setScheduleEnabledOverride(nextEnabled);
                      onChange('SCHEDULE_ENABLED', nextEnabled ? 'true' : 'false');
                    }}
                  />
              <span>
                <span className="block text-sm font-semibold text-foreground">{t('settings.schedulerEnable')}</span>
                <span className="block text-xs leading-6 text-muted-text">{t('settings.schedulerEnableDescription')}</span>
              </span>
            </label>

            <div className="space-y-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
                <Clock className="h-4 w-4" aria-hidden="true" />
                {t('settings.schedulerTimes')}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {scheduleTimes.map((time, index) => (
                  <div
                    key={index}
                    className="inline-flex h-11 shrink-0 items-center gap-1 rounded-xl border settings-border bg-card/90 p-1 shadow-inner"
                  >
                    <input
                      data-testid={`scheduler-time-input-${index}`}
                      type="time"
                      value={SCHEDULE_TIME_PATTERN.test(time) ? time : ''}
                      aria-label={t('settings.schedulerTimeInputAria', { index: index + 1 })}
                      className="h-9 w-[8.75rem] rounded-lg border-none bg-transparent px-2 text-sm font-medium text-foreground outline-none transition focus:bg-background/60 focus:ring-2 focus:ring-cyan/20"
                      disabled={disabled}
                      onChange={(event) => {
                        const nextTimes = scheduleTimes.map((currentTime, currentIndex) => (
                          currentIndex === index ? event.target.value : currentTime
                        ));
                        updateScheduleTimes(nextTimes);
                      }}
                    />
                    {scheduleTimes.length > 1 ? (
                      <Button
                        type="button"
                        variant="settings-secondary"
                        size="sm"
                        className="h-8 w-8 rounded-lg px-0"
                        aria-label={t('settings.schedulerRemoveTime')}
                        title={t('settings.schedulerRemoveTime')}
                        disabled={disabled}
                        onClick={() => {
                          updateScheduleTimes(scheduleTimes.filter((_, currentIndex) => currentIndex !== index));
                        }}
                      >
                        <Trash2 className="h-4 w-4" aria-hidden="true" />
                      </Button>
                    ) : null}
                  </div>
                ))}
                <Button
                  type="button"
                  variant="settings-secondary"
                  size="sm"
                  className="h-11 shrink-0"
                  data-testid="scheduler-add-time-button"
                  disabled={disabled}
                  onClick={() => updateScheduleTimes([...scheduleTimes, SCHEDULER_DEFAULT_TIME])}
                >
                  <Plus className="h-4 w-4" aria-hidden="true" />
                  {t('settings.schedulerAddTime')}
                </Button>
              </div>
            </div>
          </div>

          <div className="space-y-3 rounded-2xl border settings-border bg-background/35 px-4 py-4">
            <div>
              <p className="text-sm font-semibold text-foreground">{t('settings.schedulerStatus')}</p>
              <p className="mt-1 text-xs leading-6 text-muted-text">
                {status?.running
                  ? t('settings.schedulerRunning')
                  : statusEnabled
                    ? t('settings.schedulerEnabled')
                    : t('settings.schedulerDisabled')}
              </p>
            </div>
            <dl className="grid grid-cols-1 gap-2 text-xs">
              <div className="rounded-xl border settings-border bg-card/60 px-3 py-2">
                <dt className="text-muted-text">{t('settings.schedulerEffectiveTimes')}</dt>
                <dd className="mt-1 font-medium text-foreground">{effectiveStatusTimes.join(', ') || '-'}</dd>
              </div>
              <div className="rounded-xl border settings-border bg-card/60 px-3 py-2">
                <dt className="text-muted-text">{t('settings.schedulerNextRun')}</dt>
                <dd className="mt-1 font-medium text-foreground">
                  {formatSchedulerTimestamp(status?.nextRunAt, language)}
                </dd>
              </div>
              <div className="rounded-xl border settings-border bg-card/60 px-3 py-2">
                <dt className="text-muted-text">{t('settings.schedulerLastSuccess')}</dt>
                <dd data-testid="scheduler-last-success" className="mt-1 font-medium text-foreground">
                  {formatSchedulerTimestamp(status?.lastSuccessAt, language)}
                </dd>
              </div>
              {status?.lastError ? (
                <div className="rounded-xl border border-danger/40 bg-danger/10 px-3 py-2">
                  <dt className="text-danger">{t('settings.schedulerLastError')}</dt>
                  <dd data-testid="scheduler-last-error" className="mt-1 break-words text-danger">{status.lastError}</dd>
                </div>
              ) : null}
            </dl>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="settings-secondary"
                size="sm"
                data-testid="scheduler-refresh-status-button"
                disabled={disabled || isRefreshingStatus}
                isLoading={isRefreshingStatus}
                loadingText={t('settings.schedulerRefreshing')}
                onClick={() => void refreshSchedulerStatus()}
              >
                <RefreshCw className="h-4 w-4" aria-hidden="true" />
                {t('settings.schedulerRefresh')}
              </Button>
              <Button
                type="button"
                variant="settings-primary"
                size="sm"
                data-testid="scheduler-run-now-button"
                disabled={disabled || isRunningNow}
                isLoading={isRunningNow}
                loadingText={t('settings.schedulerRunningNow')}
                onClick={() => void runSchedulerNow()}
              >
                <Play className="h-4 w-4" aria-hidden="true" />
                {t('settings.schedulerRunNow')}
              </Button>
            </div>
          </div>
        </div>

        {validationIssues.length ? (
          <div className="space-y-1 text-xs text-danger">
            {validationIssues.map((issue) => (
              <p key={`${issue.key}-${issue.code}`}>{issue.message}</p>
            ))}
          </div>
        ) : null}
        {statusError ? <ApiErrorAlert error={statusError} /> : null}
        {runNowError ? <ApiErrorAlert error={runNowError} /> : null}
        {!runNowError && runNowSuccess ? (
          <SettingsAlert title={t('settings.actionSuccess')} message={runNowSuccess} variant="success" />
        ) : null}
      </div>
    </SettingsSectionCard>
  );
};

const SettingsPage: React.FC = () => {
  const { authEnabled, passwordChangeable } = useAuth();
  const { language: uiLanguage, t } = useUiLanguage();
  const [envBackupActionError, setEnvBackupActionError] = useState<ParsedApiError | null>(null);
  const [envBackupActionSuccess, setEnvBackupActionSuccess] = useState<string>('');
  const [alphaSiftActionError, setAlphaSiftActionError] = useState<ParsedApiError | null>(null);
  const [alphaSiftActionSuccess, setAlphaSiftActionSuccess] = useState<string>('');
  const [isExportingEnv, setIsExportingEnv] = useState(false);
  const [isImportingEnv, setIsImportingEnv] = useState(false);
  const [isUpdatingAlphaSift, setIsUpdatingAlphaSift] = useState(false);
  const [showImportConfirm, setShowImportConfirm] = useState(false);
  const [desktopUpdateState, setDesktopUpdateState] = useState<DesktopUpdateState | null>(null);
  const [isCheckingDesktopUpdate, setIsCheckingDesktopUpdate] = useState(false);
  const [schedulerStatusRefreshToken, setSchedulerStatusRefreshToken] = useState(0);
  const [schedulerRuntimeEnabled, setSchedulerRuntimeEnabled] = useState<boolean | null>(null);
  const [schedulerOverrideFromUi, setSchedulerOverrideFromUi] = useState<boolean | null>(null);
  const [setupStatus, setSetupStatus] = useState<SetupStatusResponse | null>(null);
  const [isRefreshingSetupStatus, setIsRefreshingSetupStatus] = useState(false);
  const [setupStatusError, setSetupStatusError] = useState<ParsedApiError | null>(null);
  const [isRunningSetupSmoke, setIsRunningSetupSmoke] = useState(false);
  const [setupSmokeError, setSetupSmokeError] = useState<ParsedApiError | null>(null);
  const [setupSmokeSuccess, setSetupSmokeSuccess] = useState('');
  const envBackupImportRef = useRef<HTMLInputElement | null>(null);
  const setupStatusRequestIdRef = useRef(0);
  const desktopRuntimeApi = getDesktopRuntimeApi();
  const isDesktopRuntime = Boolean(desktopRuntimeApi);
  const canCheckDesktopUpdate = Boolean(
    desktopRuntimeApi?.getUpdateState && desktopRuntimeApi?.checkForUpdates && desktopRuntimeApi?.openReleasePage
  );
  const desktopAppVersion = getDesktopAppVersion();
  const shouldShowDesktopVersionCard = Boolean(desktopAppVersion);

  // Set page title
  useEffect(() => {
    document.title = t('settings.pageTitleDocument');
  }, [t]);

  const {
    categories,
    itemsByCategory,
    issueByKey,
    activeCategory,
    setActiveCategory,
    hasDirty,
    dirtyCount,
    toast,
    clearToast,
    isLoading,
    isSaving,
    loadError,
    saveError,
    retryAction,
    load,
    retry,
    save,
    resetDraft,
    setDraftValue,
    getChangedItems,
    refreshAfterExternalSave,
    configVersion,
    maskToken,
  } = useSystemConfig();

  const currentChangedItems = getChangedItems();

  const refreshSetupStatus = useCallback(async () => {
    const requestId = setupStatusRequestIdRef.current + 1;
    setupStatusRequestIdRef.current = requestId;
    setSetupStatusError(null);
    setIsRefreshingSetupStatus(true);
    try {
      const status = await systemConfigApi.getSetupStatus();
      if (setupStatusRequestIdRef.current !== requestId) {
        return;
      }
      setSetupStatus(status);
    } catch (error: unknown) {
      if (setupStatusRequestIdRef.current !== requestId) {
        return;
      }
      setSetupStatusError(getParsedApiError(error));
    } finally {
      if (setupStatusRequestIdRef.current === requestId) {
        setIsRefreshingSetupStatus(false);
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void refreshSetupStatus();
  }, [refreshSetupStatus]);

  useEffect(() => {
    if (!toast) {
      return;
    }

    const timer = window.setTimeout(() => {
      clearToast();
    }, 3200);

    return () => {
      window.clearTimeout(timer);
    };
  }, [clearToast, toast]);

  useEffect(() => {
    if (!canCheckDesktopUpdate) {
      setDesktopUpdateState(null);
      setIsCheckingDesktopUpdate(false);
      return;
    }

    let active = true;

    const syncDesktopUpdateState = async () => {
      try {
        const state = await desktopRuntimeApi?.getUpdateState?.();
        if (active) {
          setDesktopUpdateState(normalizeDesktopUpdateState(state));
        }
      } catch (error: unknown) {
        if (!active) {
          return;
        }
        setDesktopUpdateState({
          status: 'error',
          message: error instanceof Error ? error.message : t('settings.desktopUpdateErrorMessage'),
        });
      }
    };

    void syncDesktopUpdateState();

    const unsubscribe = desktopRuntimeApi?.onUpdateStateChange?.((state) => {
      if (!active) {
        return;
      }
      setDesktopUpdateState(normalizeDesktopUpdateState(state));
      setIsCheckingDesktopUpdate(false);
    });

    return () => {
      active = false;
      if (typeof unsubscribe === 'function') {
        unsubscribe();
      }
    };
  }, [canCheckDesktopUpdate, desktopRuntimeApi, t]);

  const rawActiveItems = itemsByCategory[activeCategory] || [];
  const rawActiveItemMap = new Map(rawActiveItems.map((item) => [item.key, String(item.value ?? '')]));
  const firstSetupStockCode = parseSetupStockList(getConfigItem(itemsByCategory.base || [], 'STOCK_LIST')?.value)[0] || '';
  const alphasiftItem = (itemsByCategory.data_source || []).find((item) => item.key === 'ALPHASIFT_ENABLED');
  const alphasiftEnabled = String(alphasiftItem?.value ?? '').trim().toLowerCase() === 'true';
  const hasConfiguredChannels = Boolean((rawActiveItemMap.get('LLM_CHANNELS') || '').trim());
  const hasLitellmConfig = Boolean((rawActiveItemMap.get('LITELLM_CONFIG') || '').trim());
  const hasRuntimeSchedulerMismatch =
    schedulerRuntimeEnabled !== null
    && schedulerOverrideFromUi !== null
    && schedulerOverrideFromUi !== schedulerRuntimeEnabled;
  const hasRuntimeSchedulerMismatchInDraft = hasRuntimeSchedulerMismatch
    && !currentChangedItems.some((item) => item.key === 'SCHEDULE_ENABLED');
  const effectiveHasDirty = hasDirty || hasRuntimeSchedulerMismatchInDraft;
  const effectiveDirtyCount = dirtyCount + (hasRuntimeSchedulerMismatchInDraft ? 1 : 0);

  const handleSchedulerRuntimeStateChange = useCallback(({ runtimeEnabled, overrideEnabled }: {
    runtimeEnabled: boolean | null;
    overrideEnabled: boolean | null;
  }) => {
    setSchedulerRuntimeEnabled(runtimeEnabled);
    setSchedulerOverrideFromUi(overrideEnabled);
  }, []);

  // UI rendering rule only: hide channel-managed and legacy provider-specific
  // LLM keys from generic fields when channel mode is active. This does not
  // alter save/refresh payloads or config migration/rollback behavior.
  const LLM_CHANNEL_KEY_RE = /^LLM_[A-Z0-9_]+_(PROTOCOL|BASE_URL|API_KEY|API_KEYS|MODELS|EXTRA_HEADERS|ENABLED)$/;
  const AI_MODEL_HIDDEN_KEYS = new Set([
    'LLM_CHANNELS',
    'LLM_TEMPERATURE',
    'LITELLM_MODEL',
    'AGENT_LITELLM_MODEL',
    'LITELLM_FALLBACK_MODELS',
    'AIHUBMIX_KEY',
    'DEEPSEEK_API_KEY',
    'DEEPSEEK_API_KEYS',
    'GEMINI_API_KEY',
    'GEMINI_API_KEYS',
    'GEMINI_MODEL',
    'GEMINI_MODEL_FALLBACK',
    'GEMINI_TEMPERATURE',
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_API_KEYS',
    'ANTHROPIC_MODEL',
    'ANTHROPIC_TEMPERATURE',
    'ANTHROPIC_MAX_TOKENS',
    'OPENAI_API_KEY',
    'OPENAI_API_KEYS',
    'OPENAI_BASE_URL',
    'OPENAI_MODEL',
    'OPENAI_VISION_MODEL',
    'OPENAI_TEMPERATURE',
    'VISION_MODEL',
  ]);
  const SYSTEM_HIDDEN_KEYS = new Set([
    'ADMIN_AUTH_ENABLED',
    ...SCHEDULER_SETTING_KEYS,
  ]);
  const DATA_SOURCE_HIDDEN_KEYS = new Set([
    'ALPHASIFT_ENABLED',
  ]);
  const AGENT_HIDDEN_KEYS = new Set<string>();
  const activeItems =
    activeCategory === 'ai_model'
      ? rawActiveItems.filter((item) => {
        if (hasConfiguredChannels && LLM_CHANNEL_KEY_RE.test(item.key)) {
          return false;
        }
        if (hasConfiguredChannels && !hasLitellmConfig && AI_MODEL_HIDDEN_KEYS.has(item.key)) {
          return false;
        }
        return true;
      })
      : activeCategory === 'system'
        ? rawActiveItems.filter((item) => !SYSTEM_HIDDEN_KEYS.has(item.key))
      : activeCategory === 'data_source'
        ? rawActiveItems.filter((item) => !DATA_SOURCE_HIDDEN_KEYS.has(item.key))
      : activeCategory === 'agent'
        ? rawActiveItems.filter((item) => !AGENT_HIDDEN_KEYS.has(item.key))
      : rawActiveItems;
  const promptCacheAdvancedItems = activeCategory === 'ai_model'
    ? activeItems.filter(isPromptCacheAdvancedSetting)
    : [];
  const visibleActiveItems = activeCategory === 'ai_model'
    ? activeItems.filter((item) => !isPromptCacheAdvancedSetting(item))
    : activeItems;
  const hasActiveConfigItems = visibleActiveItems.length > 0 || promptCacheAdvancedItems.length > 0;
  const isEnvBackupAllowed = isDesktopRuntime || authEnabled;
  const envBackupActionDisabled = isLoading || isSaving || isExportingEnv || isImportingEnv || !isEnvBackupAllowed;

  const downloadEnvBackup = async () => {
    setEnvBackupActionError(null);
    setEnvBackupActionSuccess('');
    setIsExportingEnv(true);
    try {
      const payload = await systemConfigApi.exportEnv();
      const blob = new Blob([payload.content], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = formatEnvBackupFilename(isDesktopRuntime);
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
      setEnvBackupActionSuccess(t('settings.envExported'));
    } catch (error: unknown) {
      setEnvBackupActionError(getParsedApiError(error));
    } finally {
      setIsExportingEnv(false);
    }
  };

  const beginEnvBackupImport = () => {
    setEnvBackupActionError(null);
    setEnvBackupActionSuccess('');
    if (hasDirty) {
      setShowImportConfirm(true);
      return;
    }
    envBackupImportRef.current?.click();
  };

  const handleEnvBackupImportFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    setShowImportConfirm(false);
    if (!file) {
      return;
    }

    setEnvBackupActionError(null);
    setEnvBackupActionSuccess('');
    setIsImportingEnv(true);
    try {
      const content = await file.text();
      const importResult = await systemConfigApi.importEnv({
        configVersion,
        content,
        reloadNow: true,
      });
      const reloaded = await load();
      if (!reloaded) {
        setEnvBackupActionError(createParsedApiError({
          title: t('settings.envImportedRefreshFailedTitle'),
          message: t('settings.envImportedRefreshFailedMessage'),
          rawMessage: t('settings.envImportedRefreshFailedRaw'),
          category: 'http_error',
        }));
        return;
      }
      if (importResult.updatedKeys.some((key) => SCHEDULER_SETTING_KEYS.has(key))) {
        setSchedulerStatusRefreshToken((current) => current + 1);
      }
      notifySystemConfigChanged();
      void refreshSetupStatus();
      setEnvBackupActionSuccess(t('settings.envImported'));
    } catch (error: unknown) {
      setEnvBackupActionError(getParsedApiError(error));
    } finally {
      setIsImportingEnv(false);
    }
  };

  const handleDesktopUpdateCheck = async () => {
    if (!desktopRuntimeApi?.checkForUpdates) {
      return;
    }

    setIsCheckingDesktopUpdate(true);
    setDesktopUpdateState((current) => ({
      ...(current || {}),
      status: 'checking',
      message: t('settings.desktopUpdateCheckingMessage'),
    }));

    try {
      const state = await desktopRuntimeApi.checkForUpdates();
      setDesktopUpdateState(normalizeDesktopUpdateState(state));
    } catch (error: unknown) {
      setDesktopUpdateState({
        status: 'error',
        message: error instanceof Error ? error.message : t('settings.desktopUpdateErrorMessage'),
      });
    } finally {
      setIsCheckingDesktopUpdate(false);
    }
  };

  const updateAlphaSiftEnabled = async (nextEnabled: boolean) => {
    setAlphaSiftActionError(null);
    setAlphaSiftActionSuccess('');
    setIsUpdatingAlphaSift(true);
    try {
      if (nextEnabled) {
        await alphasiftApi.enable();
        await refreshAfterExternalSave(['ALPHASIFT_ENABLED']);
        setAlphaSiftActionSuccess(t('settings.enabledAlphaSiftSuccess'));
        return;
      }

      await systemConfigApi.update({
        configVersion,
        maskToken,
        reloadNow: true,
        items: [{ key: 'ALPHASIFT_ENABLED', value: 'false' }],
      });
      notifyAlphaSiftConfigChanged();
      await refreshAfterExternalSave(['ALPHASIFT_ENABLED']);
      setAlphaSiftActionSuccess(t('settings.disabledAlphaSiftSuccess'));
    } catch (error: unknown) {
      setAlphaSiftActionError(getParsedApiError(error));
      await refreshAfterExternalSave(['ALPHASIFT_ENABLED']);
    } finally {
      setIsUpdatingAlphaSift(false);
    }
  };

  const handleSaveConfig = async () => {
    const changedItems = getChangedItems();
    const syncRuntimeSchedulerState =
      schedulerOverrideFromUi !== null
      && schedulerRuntimeEnabled !== null
      && schedulerOverrideFromUi !== schedulerRuntimeEnabled
      && !changedItems.some((item) => item.key === 'SCHEDULE_ENABLED');
    const schedulerSyncItem: SystemConfigUpdateItem[] = syncRuntimeSchedulerState
      ? [{ key: 'SCHEDULE_ENABLED', value: schedulerOverrideFromUi ? 'true' : 'false' }]
      : [];
    const changedItemsToSave = [...changedItems, ...schedulerSyncItem];
    const changedAlphaSiftItem = changedItems.find((item) => item.key === 'ALPHASIFT_ENABLED');
    const changedSchedulerSettings = changedItemsToSave.some((item) => SCHEDULER_SETTING_KEYS.has(item.key));
    const result = await save(changedItemsToSave);
    if (!result.success) {
      return;
    }
    notifySystemConfigChanged();
    if (changedSchedulerSettings) {
      setSchedulerStatusRefreshToken((current) => current + 1);
    }
    void refreshSetupStatus();
    if (!changedAlphaSiftItem) {
      return;
    }

    setAlphaSiftActionError(null);
    setAlphaSiftActionSuccess('');
    try {
      const isAlphaSiftEnabled = changedAlphaSiftItem.value.trim().toLowerCase() === 'true';
      if (isAlphaSiftEnabled) {
        await alphasiftApi.enable();
        await refreshAfterExternalSave(['ALPHASIFT_ENABLED']);
        setAlphaSiftActionSuccess(t('settings.enabledAlphaSiftSuccess'));
        return;
      }

      notifyAlphaSiftConfigChanged();
      setAlphaSiftActionSuccess(t('settings.disabledAlphaSiftSuccess'));
    } catch (error: unknown) {
      setAlphaSiftActionError(getParsedApiError(error));
      await refreshAfterExternalSave(['ALPHASIFT_ENABLED']);
    }
  };

  const openDesktopReleasePage = async () => {
    if (!desktopRuntimeApi?.openReleasePage) {
      return;
    }

    await desktopRuntimeApi.openReleasePage(desktopUpdateState?.releaseUrl);
  };

  const installDesktopUpdate = async () => {
    if (!desktopRuntimeApi?.installDownloadedUpdate) {
      setDesktopUpdateState((current) => ({
        ...(current || {}),
        status: 'error',
        message: t('settings.desktopManualUnsupported'),
      }));
      return;
    }

    try {
      setDesktopUpdateState((current) => ({
        ...(current || {}),
        status: 'installing',
        message: t('settings.desktopUpdateInstallingMessage'),
      }));
      await desktopRuntimeApi.installDownloadedUpdate();
    } catch (error: unknown) {
      setDesktopUpdateState((current) => ({
        ...(current || {}),
        status: 'error',
        message: error instanceof Error ? error.message : t('settings.desktopManualUnsupported'),
      }));
    }
  };

  const handleRunSetupSmoke = async () => {
    setSetupSmokeError(null);
    setSetupSmokeSuccess('');

    if (!setupStatus?.readyForSmoke) {
      setSetupSmokeError(createParsedApiError({
        title: t('settings.setupGuideSmokeUnavailableTitle'),
        message: t('settings.setupGuideSmokeNotReady'),
        rawMessage: t('settings.setupGuideSmokeNotReady'),
        category: 'missing_params',
      }));
      return;
    }

    if (!firstSetupStockCode) {
      setSetupSmokeError(createParsedApiError({
        title: t('settings.setupGuideSmokeUnavailableTitle'),
        message: t('settings.setupGuideSmokeNeedsStock'),
        rawMessage: t('settings.setupGuideSmokeNeedsStock'),
        category: 'missing_params',
      }));
      return;
    }

    setIsRunningSetupSmoke(true);
    try {
      const result = await analysisApi.analyzeAsync({
        stockCode: firstSetupStockCode,
        reportType: 'brief',
        asyncMode: true,
        notify: false,
        originalQuery: firstSetupStockCode,
        selectionSource: 'manual',
      });
      const taskId = 'taskId' in result ? result.taskId : result.accepted?.[0]?.taskId;
      setSetupSmokeSuccess(
        taskId
          ? t('settings.setupGuideSmokeAcceptedWithTask', { stock: firstSetupStockCode, taskId })
          : t('settings.setupGuideSmokeAccepted', { stock: firstSetupStockCode }),
      );
      void refreshSetupStatus();
    } catch (error: unknown) {
      setSetupSmokeError(getParsedApiError(error));
    } finally {
      setIsRunningSetupSmoke(false);
    }
  };

  const desktopUpdateNotice = getDesktopUpdateNotice(desktopUpdateState, t);
  const shouldGuardActiveConfigPanel = activeCategory === 'notification' || activeCategory === 'agent';
  const activeConfigPanelErrorTitle = activeCategory === 'agent' ? t('settings.agentSettings') : t('settings.notificationSettings');
  const settingsPanelDiagnosticHint = isDesktopRuntime
    ? uiLanguage === 'en'
      ? <>Check and provide the desktop log <code>desktop.log</code>, plus the release version, Windows version, and trigger path.</>
      : <>请查看并提供桌面端日志 <code>desktop.log</code>，同时补充 release 版本、Windows 版本和触发入口。</>
    : t('settings.diagnosticHintWeb');
  const activeConfigPanel = hasActiveConfigItems ? (
    <SettingsSectionCard
      title={t('settings.activePanelTitle')}
      description={getCategoryDescription(activeCategory as SystemConfigCategory, '', uiLanguage) || t('settings.activePanelDescription')}
    >
      {visibleActiveItems.map((item) => (
        <SettingsField
          key={item.key}
          item={item}
          value={item.value}
          disabled={isSaving}
          onChange={setDraftValue}
          issues={issueByKey[item.key] || []}
        />
      ))}
      {promptCacheAdvancedItems.length ? (
        <details className="group/prompt-cache rounded-[1.15rem] border border-[var(--settings-border)] bg-[var(--settings-surface)] p-4 shadow-soft-card transition-[background-color,border-color,box-shadow] duration-200 hover:border-[var(--settings-border-strong)] hover:bg-[var(--settings-surface-hover)]">
          <summary className="flex cursor-pointer list-none items-start justify-between gap-3 [&::-webkit-details-marker]:hidden">
            <div className="min-w-0 space-y-1">
              <p className="text-sm font-semibold text-foreground">
                {t('settings.promptCacheAdvancedTitle')}
              </p>
              <p className="text-xs leading-5 text-muted-text">
                {t('settings.promptCacheAdvancedDescription')}
              </p>
            </div>
            <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-muted-text transition-transform group-open/prompt-cache:rotate-180" aria-hidden="true" />
          </summary>
          <div className="mt-4 space-y-4">
            {promptCacheAdvancedItems.map((item) => (
              <SettingsField
                key={item.key}
                item={item}
                value={item.value}
                disabled={isSaving}
                onChange={setDraftValue}
                issues={issueByKey[item.key] || []}
              />
            ))}
          </div>
        </details>
      ) : null}
    </SettingsSectionCard>
  ) : (
    <EmptyState
      title={t('settings.currentCategoryEmptyTitle')}
      description={t('settings.currentCategoryEmptyDescription')}
      className="settings-surface-panel settings-border-strong border-none bg-transparent shadow-none"
    />
  );

  return (
    <div className="settings-page min-h-full px-4 pb-6 pt-4 md:px-6">
      <div className="mb-5 rounded-[1.5rem] border settings-border bg-card/94 px-5 py-5 shadow-soft-card-strong backdrop-blur-sm">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-foreground">{t('settings.pageTitle')}</h1>
            <p className="text-xs leading-6 text-muted-text">
              {t('settings.pageDescription')}
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="settings-secondary"
              onClick={resetDraft}
              disabled={isLoading || isSaving}
            >
              {t('settings.reset')}
            </Button>
              <Button
                type="button"
                variant="settings-primary"
                onClick={() => void handleSaveConfig()}
                disabled={!effectiveHasDirty || isSaving || isLoading}
                isLoading={isSaving}
                loadingText={t('settings.saving')}
              >
                {isSaving
                  ? t('settings.saving')
                  : effectiveDirtyCount
                    ? t('settings.saveConfigWithCount', { count: effectiveDirtyCount })
                    : t('settings.saveConfig')}
              </Button>
          </div>
        </div>

        {saveError ? (
          <ApiErrorAlert
            className="mt-3"
            error={saveError}
            actionLabel={retryAction === 'save' ? t('settings.saveRetry') : undefined}
            onAction={retryAction === 'save' ? () => void retry() : undefined}
          />
        ) : null}
      </div>

      {loadError ? (
        <ApiErrorAlert
          error={loadError}
          actionLabel={retryAction === 'load' ? t('common.retry') : t('settings.reload')}
          onAction={() => void retry()}
          className="mb-4"
        />
      ) : null}

      {isLoading ? (
        <SettingsLoading />
      ) : (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-[280px_1fr]">
          <aside className="lg:sticky lg:top-4 lg:self-start">
            <SettingsCategoryNav
              categories={categories}
              itemsByCategory={itemsByCategory}
              activeCategory={activeCategory}
              onSelect={setActiveCategory}
            />
          </aside>

          <section className="space-y-4">
            <FirstRunSetupCard
              status={setupStatus}
              isLoading={isRefreshingSetupStatus}
              error={setupStatusError}
              firstStockCode={firstSetupStockCode}
              isSaving={isSaving}
              isRunningSmoke={isRunningSetupSmoke}
              smokeError={setupSmokeError}
              smokeSuccess={setupSmokeSuccess}
              onRefresh={refreshSetupStatus}
              onSelectCategory={setActiveCategory}
              onRunSmoke={handleRunSetupSmoke}
              listSeparator={uiLanguage === 'en' ? ', ' : '、'}
              t={t}
              language={uiLanguage}
            />
            {alphasiftItem ? (
              <SettingsSectionCard
                title={t('settings.alphaSift')}
                description={t('settings.alphaSiftDescription')}
              >
                <div className="flex flex-col gap-4 rounded-2xl border settings-border bg-background/35 px-4 py-4 md:flex-row md:items-center md:justify-between">
                  <div>
                    <p className="text-sm font-semibold text-foreground">
                      {alphasiftEnabled ? t('settings.alphaSiftEnabled') : t('settings.alphaSiftDisabled')}
                    </p>
                    <p className="mt-1 text-xs leading-6 text-muted-text">
                      {t('settings.alphaSiftSummary')}
                    </p>
                    <p className="mt-2 text-xs leading-6 text-amber-700 dark:text-amber-300">
                      {t('settings.alphaSiftRisk')}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      type="button"
                      variant="settings-secondary"
                      onClick={() => setActiveCategory('data_source')}
                    >
                      {t('settings.viewConfigItems')}
                    </Button>
                    <Button
                      type="button"
                      variant={alphasiftEnabled ? 'settings-secondary' : 'settings-primary'}
                      onClick={() => void updateAlphaSiftEnabled(!alphasiftEnabled)}
                      disabled={isSaving || isLoading || isUpdatingAlphaSift}
                      isLoading={isUpdatingAlphaSift}
                      loadingText={alphasiftEnabled ? t('settings.disablingAlphaSift') : t('settings.enablingAlphaSift')}
                    >
                      {alphasiftEnabled ? t('settings.disableAlphaSift') : t('settings.enableAlphaSift')}
                    </Button>
                  </div>
                </div>
                {alphaSiftActionError ? (
                  <div className="mt-3">
                    <ApiErrorAlert error={alphaSiftActionError} />
                  </div>
                ) : null}
                {!alphaSiftActionError && alphaSiftActionSuccess ? (
                  <div className="mt-3">
                    <SettingsAlert title={t('settings.actionSuccess')} message={alphaSiftActionSuccess} variant="success" />
                  </div>
                ) : null}
              </SettingsSectionCard>
            ) : null}
            {activeCategory === 'system' ? <AuthSettingsCard /> : null}
            {activeCategory === 'system' ? (
              <SchedulerSettingsCard
                items={rawActiveItems}
                disabled={isSaving || isLoading}
                issueByKey={issueByKey}
                statusRefreshToken={schedulerStatusRefreshToken}
                onSchedulerStateChange={handleSchedulerRuntimeStateChange}
                onChange={setDraftValue}
                t={t}
                language={uiLanguage}
              />
            ) : null}
            {activeCategory === 'system' ? (
              <SettingsSectionCard
                title={t('settings.versionInfo')}
                description={t('settings.versionInfoDescription')}
              >
                <div
                  className={`grid grid-cols-1 gap-3 ${shouldShowDesktopVersionCard ? 'md:grid-cols-4' : 'md:grid-cols-3'}`}
                >
                  <div className="rounded-2xl border settings-border bg-background/40 px-4 py-3">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-text">
                      {t('settings.versionWebui')}
                    </p>
                    <p className="mt-2 break-all font-mono text-sm text-foreground">
                      {WEB_BUILD_INFO.version}
                    </p>
                  </div>
                  <div className="rounded-2xl border settings-border bg-background/40 px-4 py-3">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-text">
                      {t('settings.versionBuildId')}
                    </p>
                    <p className="mt-2 break-all font-mono text-sm text-foreground">
                      {WEB_BUILD_INFO.buildId}
                    </p>
                  </div>
                  <div className="rounded-2xl border settings-border bg-background/40 px-4 py-3">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-text">
                      {t('settings.versionBuildTime')}
                    </p>
                    <p className="mt-2 break-all font-mono text-sm text-foreground">
                      {WEB_BUILD_INFO.buildTime}
                    </p>
                  </div>
                  {shouldShowDesktopVersionCard ? (
                    <div className="rounded-2xl border settings-border bg-background/40 px-4 py-3">
                      <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-text">
                        {t('settings.versionDesktop')}
                      </p>
                      <p className="mt-2 break-all font-mono text-sm text-foreground">
                        {desktopAppVersion}
                      </p>
                    </div>
                  ) : null}
                </div>
                <p className="text-xs leading-6 text-muted-text">
                  {t('settings.updateBuildDescription')}
                </p>
                {canCheckDesktopUpdate ? (
                  <div className="mt-4 space-y-3 rounded-2xl border settings-border bg-background/30 px-4 py-4">
                    <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                      <div>
                        <p className="text-sm font-medium text-foreground">{t('settings.desktopUpdate')}</p>
                        <p className="text-xs leading-6 text-muted-text">
                          {t('settings.desktopUpdateDescription')}
                        </p>
                      </div>
                      <Button
                        type="button"
                        variant="settings-secondary"
                        onClick={() => void handleDesktopUpdateCheck()}
                        disabled={isCheckingDesktopUpdate}
                        isLoading={isCheckingDesktopUpdate}
                        loadingText={t('settings.checkingDesktopUpdate')}
                      >
                        {t('settings.checkDesktopUpdate')}
                      </Button>
                    </div>
                    {desktopUpdateNotice ? (
                      <SettingsAlert
                        title={desktopUpdateNotice.title}
                        message={desktopUpdateNotice.message}
                        variant={desktopUpdateNotice.variant}
                        actionLabel={desktopUpdateNotice.actionLabel}
                        onAction={desktopUpdateNotice.actionLabel ? () => {
                          if (desktopUpdateNotice.actionKind === 'install') {
                            void installDesktopUpdate();
                            return;
                          }
                          void openDesktopReleasePage();
                        } : undefined}
                      />
                    ) : (
                      <p className="text-xs leading-6 text-muted-text">
                        {t('settings.desktopCurrentNoStatus')}
                      </p>
                    )}
                  </div>
                ) : null}
                {WEB_BUILD_INFO.isFallbackVersion ? (
                  <p className="text-xs leading-6 text-amber-700 dark:text-amber-300">
                    {t('settings.fallbackVersionWarning')}
                  </p>
                ) : null}
              </SettingsSectionCard>
            ) : null}
            {activeCategory === 'system' ? (
              <SettingsSectionCard
                title={t('settings.configBackup')}
                description={t('settings.configBackupDescription')}
              >
                <div className="space-y-4">
                  {!isEnvBackupAllowed ? (
                    <p className="text-xs leading-6 text-amber-700 dark:text-amber-300">
                      {t('settings.disabledAuthBackupWarning')}
                    </p>
                  ) : null}
                  <div className="flex flex-wrap items-center gap-3">
                    <Button
                      type="button"
                      variant="settings-secondary"
                      onClick={() => void downloadEnvBackup()}
                      disabled={envBackupActionDisabled}
                      isLoading={isExportingEnv}
                      loadingText={t('settings.exportingEnv')}
                    >
                      {t('settings.exportEnv')}
                    </Button>
                    <Button
                      type="button"
                      variant="settings-primary"
                      onClick={beginEnvBackupImport}
                      disabled={envBackupActionDisabled}
                      isLoading={isImportingEnv}
                      loadingText={t('settings.importingEnv')}
                    >
                      {t('settings.importEnv')}
                    </Button>
                    <input
                      ref={envBackupImportRef}
                      type="file"
                      accept=".env,.txt"
                      className="hidden"
                      onChange={(event) => {
                        void handleEnvBackupImportFile(event);
                      }}
                    />
                  </div>
                  <p className="text-xs leading-6 text-muted-text">
                    {t('settings.envExportNote')}
                  </p>
                  <p className="text-xs leading-6 text-muted-text">
                    {t('settings.envDockerNote')}
                  </p>
                  {envBackupActionError ? (
                    <ApiErrorAlert
                      error={envBackupActionError}
                      actionLabel={envBackupActionError.status === 409 ? t('settings.reload') : undefined}
                      onAction={envBackupActionError.status === 409 ? () => void load() : undefined}
                    />
                  ) : null}
                  {!envBackupActionError && envBackupActionSuccess ? (
                    <SettingsAlert title={t('settings.actionSuccess')} message={envBackupActionSuccess} variant="success" />
                  ) : null}
                </div>
              </SettingsSectionCard>
            ) : null}
            {activeCategory === 'base' ? (
              <SettingsSectionCard
                title={t('settings.intelligentImport')}
                description={t('settings.intelligentImportDescription')}
              >
                <IntelligentImport
                  stockListValue={
                    (activeItems.find((i) => i.key === 'STOCK_LIST')?.value as string) ?? ''
                  }
                  configVersion={configVersion}
                  maskToken={maskToken}
                  onMerged={async () => {
                    await refreshAfterExternalSave(['STOCK_LIST']);
                    void refreshSetupStatus();
                  }}
                  disabled={isSaving || isLoading}
                />
              </SettingsSectionCard>
            ) : null}
            {activeCategory === 'ai_model' ? (
              <SettingsSectionCard
                title={t('settings.llmAccess')}
                description={t('settings.llmAccessDescription')}
              >
                <LLMChannelEditor
                  items={rawActiveItems}
                  configVersion={configVersion}
                  maskToken={maskToken}
                  onSaved={async (updatedItems) => {
                    await refreshAfterExternalSave(updatedItems.map((item) => item.key));
                    void refreshSetupStatus();
                  }}
                  disabled={isSaving || isLoading}
                />
              </SettingsSectionCard>
            ) : null}
            {activeCategory === 'system' && passwordChangeable ? (
              <ChangePasswordCard />
            ) : null}
            {activeCategory === 'notification' ? (
              <SettingsPanelErrorBoundary
                title={t('settings.notificationTest')}
                resetKey={`notification-test:${configVersion}`}
                diagnosticHint={settingsPanelDiagnosticHint}
              >
                <NotificationTestPanel
                  items={rawActiveItems.map((item) => ({ key: item.key, value: String(item.value ?? '') }))}
                  maskToken={maskToken}
                  disabled={isSaving || isLoading}
                />
              </SettingsPanelErrorBoundary>
            ) : null}
            {shouldGuardActiveConfigPanel && hasActiveConfigItems ? (
              <SettingsPanelErrorBoundary
                title={activeConfigPanelErrorTitle}
                resetKey={`${activeCategory}:${configVersion}`}
                diagnosticHint={settingsPanelDiagnosticHint}
              >
                {activeConfigPanel}
              </SettingsPanelErrorBoundary>
            ) : activeConfigPanel}
          </section>
        </div>
      )}

      {toast ? (
        <div className="fixed bottom-5 right-5 z-50 w-[320px] max-w-[calc(100vw-24px)]">
          {toast.type === 'success'
            ? (
                <SettingsAlert
                  title={t('settings.actionSuccess')}
                  message={toast.message}
                  variant="success"
                  presentation="toast"
                />
              )
            : <ApiErrorAlert error={toast.error} />}
        </div>
      ) : null}
      <ConfirmDialog
        isOpen={showImportConfirm}
        title={t('settings.importConfirmTitle')}
        message={t('settings.importConfirmMessage')}
        confirmText={t('settings.importConfirmContinue')}
        cancelText={t('common.cancel')}
        onConfirm={() => {
          setShowImportConfirm(false);
          envBackupImportRef.current?.click();
        }}
        onCancel={() => {
          setShowImportConfirm(false);
        }}
      />
    </div>
  );
};

export default SettingsPage;
