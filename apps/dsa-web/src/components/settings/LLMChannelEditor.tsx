import { useEffect, useMemo, useRef, useState } from 'react';
import type React from 'react';
import type { ParsedApiError } from '../../api/error';
import { getParsedApiError } from '../../api/error';
import { systemConfigApi } from '../../api/systemConfig';
import type { LLMCapabilityCheck, LLMCapabilityCheckResult } from '../../types/systemConfig';
import { ApiErrorAlert, Badge, Button, InlineAlert, Input, Select, StatusDot, Tooltip } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiLanguage } from '../../i18n/uiText';
import { LLM_CHANNEL_TEXT } from '../../locales/featureText';
import type { ChannelProtocol } from './llmProviderTemplates';
import {
  LLM_PROVIDER_TEMPLATES,
  MODEL_PLACEHOLDERS_BY_PROTOCOL,
  getProviderCapabilityText,
  getProviderConfigHint,
  getProviderTemplate,
  isKnownProviderTemplate,
} from './llmProviderTemplates';
import { SettingsHelpButton } from './SettingsHelpButton';

const PROTOCOL_OPTIONS: Array<{ value: ChannelProtocol; label: string }> = [
  { value: 'openai', label: 'OpenAI Compatible' },
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'gemini', label: 'Gemini' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'vertex_ai', label: 'Vertex AI' },
  { value: 'ollama', label: 'Ollama' },
];

const KNOWN_MODEL_PREFIXES = new Set([
  'openai',
  'anthropic',
  'gemini',
  'vertex_ai',
  'deepseek',
  'minimax',
  'ollama',
  'cohere',
  'huggingface',
  'bedrock',
  'sagemaker',
  'azure',
  'replicate',
  'together_ai',
  'palm',
  'text-completion-openai',
  'command-r',
  'groq',
  'cerebras',
  'fireworks_ai',
  'friendliai',
]);

const CHANNEL_FIELD_SUFFIXES = ['PROTOCOL', 'BASE_URL', 'API_KEY', 'API_KEYS', 'MODELS', 'EXTRA_HEADERS', 'ENABLED'] as const;
const CHANNEL_FIELD_KEY_PATTERN = /^LLM_([A-Z0-9_]+)_(PROTOCOL|BASE_URL|API_KEY|API_KEYS|MODELS|EXTRA_HEADERS|ENABLED)$/;
const FALSEY_VALUES = new Set(['0', 'false', 'no', 'off']);
const HERMES_CHANNEL_NAME = 'hermes';
const HERMES_DEFAULT_MODEL = 'hermes-agent';

const RUNTIME_CAPABILITY_OPTIONS: Array<{ value: LLMCapabilityCheck; label: string }> = [
  { value: 'json', label: 'JSON' },
  { value: 'tools', label: 'Tools' },
  { value: 'stream', label: 'Stream' },
  { value: 'vision', label: 'Vision' },
];

const RUNTIME_CAPABILITY_HINTS: Record<UiLanguage, Record<LLMCapabilityCheck, string>> = {
  zh: {
    json: '检测 response_format JSON 输出是否可用。',
    tools: '检测 function/tool calling 是否可用。',
    stream: '检测流式输出是否能返回有效 chunk。',
    vision: '检测当前模型是否接受 image_url 输入。',
  },
  en: {
    json: 'Checks whether response_format JSON output works.',
    tools: 'Checks whether function/tool calling works.',
    stream: 'Checks whether streaming output returns valid chunks.',
    vision: 'Checks whether the current model accepts image_url input.',
  },
  ko: {
    json: 'response_format JSON 출력이 가능한지 확인합니다.',
    tools: 'function/tool calling이 가능한지 확인합니다.',
    stream: '스트리밍 출력이 유효한 chunk를 반환하는지 확인합니다.',
    vision: '현재 모델이 image_url 입력을 받는지 확인합니다.',
  },
};

const CAPABILITY_STATUS_LABELS: Record<UiLanguage, Record<LLMCapabilityCheckResult['status'], string>> = {
  zh: { passed: '通过', failed: '失败', skipped: '跳过' },
  en: { passed: 'passed', failed: 'failed', skipped: 'skipped' },
  ko: { passed: '통과', failed: '실패', skipped: '건너뜀' },
};

const CHANNEL_LOCAL_TEXT = {
  zh: {
    invalidChannelName: '渠道名称不能为空，且只能包含字母、数字或下划线。',
    runtimeOnlyHermesSecret: '运行时注入的 Hermes Key 不会回传；如需在设置页测试，请重新输入 Key 或保存到 .env。',
    mixedHermesRoute: 'Mixed Hermes/non-Hermes route 暂不支持作为主生成或备选模型，请选择纯 Hermes 或纯非 Hermes route。',
    nonCanonicalRouteAlias: '当前运行时模型使用非规范 route alias，请从下拉框重新选择规范模型。',
    primaryModelUnavailable: '当前主模型不在已启用渠道的模型列表中，请重新选择。',
    agentPrimaryModelUnavailable: '当前 Agent 主模型没有 Agent-safe 非 Hermes deployment，请重新选择。',
    invalidFallbackModel: '存在无效的备选模型，请重新选择。',
    visionModelHermes: '当前 Vision 模型不能包含 Hermes deployment，请重新选择纯非 Hermes route。',
    aiConfigSaved: 'AI 配置已保存',
    channelConfigSaved: '渠道配置已保存',
    testingStatus: '测试中...',
    testFailed: '测试失败',
    connectionSuccess: '连接成功',
    discoveringModelsStatus: '正在获取模型列表...',
    modelsDiscovered: '已获取 {count} 个模型',
    discoverFailed: '获取模型失败',
    checkingCapabilitiesStatus: '正在检测运行时能力...',
    noCapabilityResults: '未返回能力检测结果',
    capabilityCheckFailed: '能力检测失败',
    currentConfiguredModel: '{model}（当前配置）',
  },
  en: {
    invalidChannelName: 'Channel name is required and can only contain letters, numbers, or underscores.',
    runtimeOnlyHermesSecret: 'The runtime-injected Hermes key is not sent back; to test from the settings page, re-enter the key or save it to .env.',
    mixedHermesRoute: 'Mixed Hermes/non-Hermes routes are not yet supported as the main or fallback model; choose a pure Hermes or pure non-Hermes route.',
    nonCanonicalRouteAlias: 'The current runtime model uses a non-canonical route alias; reselect a canonical model from the dropdown.',
    primaryModelUnavailable: 'The current main model is not in any enabled channel\'s model list; please reselect.',
    agentPrimaryModelUnavailable: 'The current Agent main model has no Agent-safe non-Hermes deployment; please reselect.',
    invalidFallbackModel: 'One or more fallback models are invalid; please reselect.',
    visionModelHermes: 'The current Vision model cannot include a Hermes deployment; reselect a pure non-Hermes route.',
    aiConfigSaved: 'AI config saved',
    channelConfigSaved: 'Channel config saved',
    testingStatus: 'Testing...',
    testFailed: 'Test failed',
    connectionSuccess: 'Connection successful',
    discoveringModelsStatus: 'Fetching model list...',
    modelsDiscovered: 'Fetched {count} models',
    discoverFailed: 'Failed to fetch models',
    checkingCapabilitiesStatus: 'Checking runtime capabilities...',
    noCapabilityResults: 'No capability check results returned',
    capabilityCheckFailed: 'Capability check failed',
    currentConfiguredModel: '{model} (currently configured)',
  },
  ko: {
    invalidChannelName: '채널명은 필수이며 문자, 숫자 또는 밑줄만 사용할 수 있습니다.',
    runtimeOnlyHermesSecret: '런타임에 주입된 Hermes Key는 다시 전달되지 않습니다. 설정 페이지에서 테스트하려면 Key를 다시 입력하거나 .env에 저장하세요.',
    mixedHermesRoute: 'Hermes/비 Hermes 혼합 route는 아직 주 생성 모델이나 대체 모델로 지원되지 않습니다. 순수 Hermes 또는 순수 비 Hermes route를 선택하세요.',
    nonCanonicalRouteAlias: '현재 런타임 모델이 비표준 route alias를 사용하고 있습니다. 드롭다운에서 표준 모델을 다시 선택하세요.',
    primaryModelUnavailable: '현재 주 모델이 활성화된 채널의 모델 목록에 없습니다. 다시 선택하세요.',
    agentPrimaryModelUnavailable: '현재 Agent 주 모델에 Agent-safe 비 Hermes deployment가 없습니다. 다시 선택하세요.',
    invalidFallbackModel: '유효하지 않은 대체 모델이 있습니다. 다시 선택하세요.',
    visionModelHermes: '현재 Vision 모델에는 Hermes deployment를 포함할 수 없습니다. 순수 비 Hermes route를 다시 선택하세요.',
    aiConfigSaved: 'AI 설정이 저장되었습니다',
    channelConfigSaved: '채널 설정이 저장되었습니다',
    testingStatus: '테스트 중...',
    testFailed: '테스트 실패',
    connectionSuccess: '연결 성공',
    discoveringModelsStatus: '모델 목록을 가져오는 중...',
    modelsDiscovered: '모델 {count}개를 가져왔습니다',
    discoverFailed: '모델 목록 가져오기 실패',
    checkingCapabilitiesStatus: '런타임 능력을 확인하는 중...',
    noCapabilityResults: '능력 검사 결과가 반환되지 않았습니다',
    capabilityCheckFailed: '능력 검사 실패',
    currentConfiguredModel: '{model} (현재 설정)',
  },
} as const;

const PROVIDER_LABELS_KO: Record<string, string> = {
  aihubmix: 'AIHubmix(통합 플랫폼)',
  anspire: 'Anspire Open(모델+검색 통합)',
  deepseek: 'DeepSeek 공식',
  dashscope: '통이첸원(Dashscope)',
  zhipu: 'Zhipu GLM',
  moonshot: 'Moonshot(Kimi)',
  minimax: 'MiniMax 공식',
  volcengine: 'Volcengine Ark(Doubao)',
  siliconflow: 'SiliconFlow',
  openrouter: 'OpenRouter',
  gemini: 'Gemini 공식',
  anthropic: 'Anthropic 공식',
  openai: 'OpenAI 공식',
  ollama: 'Ollama(로컬)',
  custom: '사용자 지정 채널',
};

const PROVIDER_LABELS_EN: Record<string, string> = {
  aihubmix: 'AIHubmix (aggregator)',
  anspire: 'Anspire Open (models + search)',
  deepseek: 'DeepSeek (official)',
  dashscope: 'Qwen (Dashscope)',
  zhipu: 'Zhipu GLM',
  moonshot: 'Moonshot (Kimi)',
  minimax: 'MiniMax (official)',
  volcengine: 'Volcengine Ark (Doubao)',
  siliconflow: 'SiliconFlow',
  openrouter: 'OpenRouter',
  gemini: 'Gemini (official)',
  anthropic: 'Anthropic (official)',
  openai: 'OpenAI (official)',
  ollama: 'Ollama (local)',
  custom: 'Custom channel',
};

const PROVIDER_LABEL_OVERRIDES: Partial<Record<UiLanguage, Record<string, string>>> = {
  en: PROVIDER_LABELS_EN,
  ko: PROVIDER_LABELS_KO,
};

function formatText(template: string, values: Record<string, string | number>): string {
  return Object.entries(values).reduce(
    (text, [key, value]) => text.replaceAll(`{${key}}`, String(value)),
    template,
  );
}

function getProviderDisplayLabel(channelId: string, fallback: string, language: UiLanguage): string {
  const overrides = PROVIDER_LABEL_OVERRIDES[language];
  // channelId 可能是任意用户渠道名，hasOwnProperty 防止命中原型键（如 constructor）
  if (overrides && Object.prototype.hasOwnProperty.call(overrides, channelId)) {
    return overrides[channelId];
  }
  return fallback;
}

const isHermesChannel = (channel: Pick<ChannelConfig, 'name'>): boolean => (
  channel.name.trim().toLowerCase() === HERMES_CHANNEL_NAME
);

function canonicalizeHermesRouteModel(model: string): string {
  const trimmed = model.trim() || HERMES_DEFAULT_MODEL;
  return trimmed.startsWith('openai/') ? trimmed : `openai/${trimmed}`;
}

function routeIdentityCandidates(model: string): Set<string> {
  const trimmed = model.trim();
  if (!trimmed) return new Set();
  const candidates = new Set<string>([trimmed]);
  if (!trimmed.startsWith('openai/') && !trimmed.includes('/')) {
    candidates.add(`openai/${trimmed}`);
  }
  return candidates;
}

function getRouteProvenance(
  routeProvenanceMap: Map<string, RouteProvenance>,
  model: string,
): RouteProvenance | undefined {
  for (const candidate of routeIdentityCandidates(model)) {
    const origin = routeProvenanceMap.get(candidate);
    if (origin) return origin;
  }
  return undefined;
}

const shouldUseSavedHermesSecret = (
  channel: Pick<ChannelConfig, 'name' | 'apiKey'>,
  maskToken: string,
  hasPersistedSecret: boolean,
): boolean => (
  isHermesChannel(channel) && channel.apiKey === maskToken && hasPersistedSecret
);

const hasRuntimeOnlyMaskedHermesSecret = (
  channel: Pick<ChannelConfig, 'name' | 'apiKey'>,
  maskToken: string,
  hasPersistedSecret: boolean,
): boolean => (
  isHermesChannel(channel) && channel.apiKey === maskToken && !hasPersistedSecret
);

interface ChannelConfig {
  id: string;
  name: string;
  protocol: ChannelProtocol;
  baseUrl: string;
  apiKey: string;
  models: string;
  enabled: boolean;
}

interface ChannelTestState {
  status: 'idle' | 'loading' | 'success' | 'error';
  text?: string;
  hint?: string;
}

interface ChannelDiscoveryState {
  status: 'idle' | 'loading' | 'success' | 'error';
  text?: string;
  hint?: string;
  models: string[];
}

interface ChannelCapabilityState {
  selected: LLMCapabilityCheck[];
  status: 'idle' | 'loading' | 'success' | 'error';
  text?: string;
  hint?: string;
  results: Partial<Record<LLMCapabilityCheck, LLMCapabilityCheckResult>>;
}

interface RuntimeConfig {
  primaryModel: string;
  agentPrimaryModel: string;
  fallbackModels: string[];
  visionModel: string;
  temperature: string;
}

interface LLMChannelEditorProps {
  items: Array<{ key: string; value: string; rawValueExists?: boolean }>;
  configVersion: string;
  maskToken: string;
  onSaved: (updatedItems: Array<{ key: string; value: string }>) => void | Promise<void>;
  onDraftItemsChange?: (items: Array<{ key: string; value: string }>) => void;
  disabled?: boolean;
}

interface ChannelRowProps {
  channel: ChannelConfig;
  index: number;
  busy: boolean;
  visibleKey: boolean;
  expanded: boolean;
  testState?: ChannelTestState;
  discoveryState?: ChannelDiscoveryState;
  capabilityState?: ChannelCapabilityState;
  onUpdate: (index: number, field: keyof ChannelConfig, value: string | boolean) => void;
  onRemove: (index: number) => void;
  onToggleExpand: (index: number) => void;
  onToggleKeyVisibility: (index: number, nextVisible: boolean) => void;
  onTest: (channel: ChannelConfig, index: number) => void;
  onDiscoverModels: (channel: ChannelConfig) => void;
  onToggleCapability: (channel: ChannelConfig, capability: LLMCapabilityCheck) => void;
  onCheckCapabilities: (channel: ChannelConfig) => void;
}

const LLM_CHANNEL_HELP_DOCS: Record<UiLanguage, Array<{ label: string; href: string }>> = {
  zh: [
    {
      label: 'LLM 配置指南',
      href: 'https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/docs/LLM_CONFIG_GUIDE.md',
    },
    {
      label: 'LLM 服务商配置速查',
      href: 'https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/docs/llm-providers.md',
    },
  ],
  en: [
    {
      label: 'LLM configuration guide',
      href: 'https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/docs/LLM_CONFIG_GUIDE.md',
    },
    {
      label: 'LLM provider quick reference',
      href: 'https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/docs/llm-providers.md',
    },
  ],
  ko: [
    {
      label: 'LLM 설정 가이드',
      href: 'https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/docs/LLM_CONFIG_GUIDE.md',
    },
    {
      label: 'LLM 제공자 설정 요약',
      href: 'https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/docs/llm-providers.md',
    },
  ],
};

function HelpLabel({
  htmlFor,
  label,
  fieldKey,
  helpKey,
  examples,
  compact = false,
}: {
  htmlFor?: string;
  label: string;
  fieldKey: string;
  helpKey: string;
  examples?: string[];
  compact?: boolean;
}) {
  const { language } = useUiLanguage();
  return (
    <div className={compact ? 'mb-1 flex items-center gap-1.5' : 'mb-2 flex items-center gap-1.5'}>
      <label
        htmlFor={htmlFor}
        className={compact ? 'text-xs text-muted-text' : 'text-sm font-medium text-foreground'}
      >
        {label}
      </label>
      <SettingsHelpButton
        fieldKey={fieldKey}
        title={label}
        helpKey={helpKey}
        examples={examples}
        docs={LLM_CHANNEL_HELP_DOCS[language]}
      />
    </div>
  );
}

function parseChannelFieldKeys(channel: ChannelConfig): string[] {
  const upperName = channel.name.trim().toUpperCase();
  return [
    `LLM_${upperName}_PROTOCOL`,
    `LLM_${upperName}_BASE_URL`,
    `LLM_${upperName}_ENABLED`,
    `LLM_${upperName}_API_KEY`,
    `LLM_${upperName}_API_KEYS`,
    `LLM_${upperName}_MODELS`,
    `LLM_${upperName}_EXTRA_HEADERS`,
  ];
}

function parseChannelFieldKeysFromName(name: string): string[] {
  const upperName = name.trim().toUpperCase();
  return CHANNEL_FIELD_SUFFIXES.map((suffix) => `LLM_${upperName}_${suffix}`);
}

function isChannelSecretFieldKey(key: string): boolean {
  const match = CHANNEL_FIELD_KEY_PATTERN.exec(key.toUpperCase());
  return match?.[2] === 'API_KEY' || match?.[2] === 'API_KEYS';
}

function resolveInitialChannelApiKeySource(
  channelName: string,
  initialItemValueByKey: Map<string, string>,
  initialItemSourceByKey: Map<string, boolean>,
): boolean | undefined {
  const upperName = channelName.trim().toUpperCase();
  const apiKeysKey = `LLM_${upperName}_API_KEYS`;
  const apiKeyKey = `LLM_${upperName}_API_KEY`;

  const apiKeysValue = (initialItemValueByKey.get(apiKeysKey) || '').trim();
  const apiKeyValue = (initialItemValueByKey.get(apiKeyKey) || '').trim();

  if (channelName.trim().toLowerCase() === HERMES_CHANNEL_NAME && apiKeyValue && initialItemSourceByKey.has(apiKeyKey)) {
    return initialItemSourceByKey.get(apiKeyKey);
  }
  if (apiKeysValue && initialItemSourceByKey.has(apiKeysKey)) {
    return initialItemSourceByKey.get(apiKeysKey);
  }
  if (apiKeyValue && initialItemSourceByKey.has(apiKeyKey)) {
    return initialItemSourceByKey.get(apiKeyKey);
  }

  if (apiKeyValue) {
    return initialItemSourceByKey.get(apiKeyKey);
  }
  if (apiKeysValue) {
    return initialItemSourceByKey.get(apiKeysKey);
  }
  return initialItemSourceByKey.get(apiKeysKey) ?? initialItemSourceByKey.get(apiKeyKey);
}

function resolveInitialChannelApiKeyValue(
  channelName: string,
  itemValueByKey: Map<string, string>,
  itemSourceByKey: Map<string, boolean>,
): string {
  const upperName = channelName.trim().toUpperCase();
  const apiKeysKey = `LLM_${upperName}_API_KEYS`;
  const apiKeyKey = `LLM_${upperName}_API_KEY`;

  const apiKeysValue = (itemValueByKey.get(apiKeysKey) || '').trim();
  const apiKeyValue = (itemValueByKey.get(apiKeyKey) || '').trim();

  if (channelName.trim().toLowerCase() === HERMES_CHANNEL_NAME && apiKeyValue) {
    return apiKeyValue;
  }
  if (apiKeysValue && itemSourceByKey.has(apiKeysKey)) {
    return apiKeysValue;
  }
  if (apiKeyValue && itemSourceByKey.has(apiKeyKey)) {
    return apiKeyValue;
  }
  if (apiKeysValue) {
    return apiKeysValue;
  }
  if (apiKeyValue) {
    return apiKeyValue;
  }
  return itemValueByKey.get(apiKeysKey) || itemValueByKey.get(apiKeyKey) || '';
}

function buildChangedItemKeys(
  channels: ChannelConfig[],
  initialChannels: ChannelConfig[],
  initialItemSourceByKey: Map<string, boolean>,
  initialItemValueByKey: Map<string, string>,
): Set<string> {
  const changedKeys = new Set<string>();
  const nextChannelNames = channels.map((channel) => channel.name.trim().toLowerCase()).join(',');
  const previousChannelNames = initialChannels.map((channel) => channel.name.trim().toLowerCase()).join(',');

  if (nextChannelNames !== previousChannelNames) {
    changedKeys.add('LLM_CHANNELS');
  }

  const maxLength = Math.max(channels.length, initialChannels.length);
  for (let index = 0; index < maxLength; index += 1) {
    const current = channels[index];
    const previous = initialChannels[index];
    if (!current && !previous) {
      continue;
    }

    if (!current) {
      const previousKeys = parseChannelFieldKeys(previous);
      for (const key of previousKeys) {
        if (initialItemSourceByKey.get(key.toUpperCase()) !== false) {
          changedKeys.add(key);
        }
      }
      continue;
    }

    if (!previous) {
      for (const key of parseChannelFieldKeys(current)) {
        changedKeys.add(key);
      }
      continue;
    }

    const currentName = current.name.trim().toUpperCase();
    const previousName = previous.name.trim().toUpperCase();
    if (currentName !== previousName) {
      const previousApiKeySource = resolveInitialChannelApiKeySource(
        previous.name,
        initialItemValueByKey,
        initialItemSourceByKey,
      );
      const preserveRuntimeOnlySecret = previousApiKeySource === false && current.apiKey === previous.apiKey;
      const previousKeys = parseChannelFieldKeys(previous);
      for (const key of previousKeys) {
        if (initialItemSourceByKey.get(key.toUpperCase()) !== false) {
          changedKeys.add(key);
        }
      }

      for (const key of parseChannelFieldKeys(current)) {
        if (preserveRuntimeOnlySecret && isChannelSecretFieldKey(key)) {
          continue;
        }
        changedKeys.add(key);
      }
      continue;
    }

    const prefix = `LLM_${currentName}`;
    if (current.protocol !== previous.protocol) {
      changedKeys.add(`${prefix}_PROTOCOL`);
    }
    if (current.baseUrl !== previous.baseUrl) {
      changedKeys.add(`${prefix}_BASE_URL`);
    }
    if (current.enabled !== previous.enabled) {
      changedKeys.add(`${prefix}_ENABLED`);
    }
    if (current.apiKey !== previous.apiKey) {
      changedKeys.add(`${prefix}_API_KEY`);
      changedKeys.add(`${prefix}_API_KEYS`);
    }
    if (current.models !== previous.models) {
      changedKeys.add(`${prefix}_MODELS`);
    }
  }

  return changedKeys;
}

const ChannelRow: React.FC<ChannelRowProps> = ({
  channel,
  index,
  busy,
  visibleKey,
  expanded,
  testState,
  discoveryState,
  capabilityState,
  onUpdate,
  onRemove,
  onToggleExpand,
  onToggleKeyVisibility,
  onTest,
  onDiscoverModels,
  onToggleCapability,
  onCheckCapabilities,
}) => {
  const { language } = useUiLanguage();
  const tx = LLM_CHANNEL_TEXT[language];
  const preset = getProviderTemplate(channel.name);
  const showProviderTemplateDetails = isKnownProviderTemplate(channel.name);
  const displayName = getProviderDisplayLabel(channel.name, preset?.label || channel.name, language);
  const providerCapabilities = showProviderTemplateDetails ? (preset?.capabilities || []) : [];
  const providerSources = showProviderTemplateDetails ? (preset?.officialSources || []) : [];
  const providerHint = showProviderTemplateDetails ? getProviderConfigHint(channel.name, language) : undefined;
  const selectedModels = splitModels(channel.models);
  const runtimeCapabilityOptions = isHermesChannel(channel)
    ? RUNTIME_CAPABILITY_OPTIONS.filter((option) => option.value === 'json')
    : RUNTIME_CAPABILITY_OPTIONS;
  const discoveredModels = discoveryState?.models || [];
  const manualOnlyModels = selectedModels.filter(
    (model) => !discoveredModels.some((discoveredModel) => areModelsEquivalent(model, discoveredModel, channel.protocol)),
  );
  const modelCount = selectedModels.length;
  const hasKey = channel.apiKey.length > 0;
  const statusVariant = testState?.status === 'success'
    ? 'success'
    : testState?.status === 'error'
      ? 'danger'
      : testState?.status === 'loading'
        ? 'warning'
        : 'default';
  const selectedCapabilities = capabilityState?.selected || [];
  const capabilityResults = capabilityState?.results || {};
  const capabilityBusy = capabilityState?.status === 'loading';
  const channelNameInputId = `llm-channel-${channel.id}-name`;
  const protocolInputId = `llm-channel-${channel.id}-protocol`;
  const baseUrlInputId = `llm-channel-${channel.id}-base-url`;
  const apiKeyInputId = `llm-channel-${channel.id}-api-key`;
  const modelsInputId = `llm-channel-${channel.id}-models`;

  return (
    <div className="mb-2 overflow-hidden rounded-xl border border-[var(--settings-border)] bg-[var(--settings-surface)] shadow-soft-card transition-[background-color,border-color,box-shadow] duration-200 hover:border-[var(--settings-border-strong)] hover:bg-[var(--settings-surface-hover)]">
      <div
        className="flex cursor-pointer select-none items-center gap-2.5 px-4 py-3 transition-colors"
        onClick={() => onToggleExpand(index)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onToggleExpand(index);
          }
        }}
        role="button"
        tabIndex={0}
      >
        <span className={`w-4 shrink-0 text-[11px] text-muted-text transition-transform ${expanded ? 'rotate-90' : ''}`}>▶</span>

        <input
          type="checkbox"
          checked={channel.enabled}
          disabled={busy}
          className="settings-input-checkbox h-4 w-4 shrink-0 rounded border-border/70 bg-base"
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => onUpdate(index, 'enabled', e.target.checked)}
        />

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-semibold text-foreground">{displayName}</span>
            <Badge variant="info" className="hidden sm:inline-flex">
              {channel.protocol}
            </Badge>
          </div>
          <p className="mt-0.5 truncate text-[11px] text-secondary-text">
            {modelCount > 0 ? formatText(tx.configuredModelCount, { count: modelCount }) : tx.noModelConfigured}
          </p>
        </div>

        <span className="flex shrink-0 items-center gap-2">
          {testState?.status === 'success' ? (
            <Tooltip content={tx.connectionOk}>
              <span className="inline-flex">
                <StatusDot tone="success" />
              </span>
            </Tooltip>
          ) : null}
          {testState?.status === 'error' ? (
            <Tooltip content={tx.connectionFailed}>
              <span className="inline-flex">
                <StatusDot tone="danger" />
              </span>
            </Tooltip>
          ) : null}
          {testState?.status === 'loading' ? (
            <Tooltip content={tx.testing}>
              <span className="inline-flex">
                <StatusDot tone="warning" pulse />
              </span>
            </Tooltip>
          ) : null}
          {!hasKey && channel.protocol !== 'ollama' ? <Badge variant="warning">{tx.missingKey}</Badge> : null}
          {testState?.status !== 'idle' ? (
            <Badge variant={statusVariant}>
              {testState?.status === 'success' ? tx.connectionOk : testState?.status === 'error' ? tx.connectionFailed : tx.testing}
            </Badge>
          ) : null}
        </span>

        <Tooltip content={tx.deleteChannel}>
          <span className="inline-flex">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-8 shrink-0 px-2 text-xs text-muted-text hover:text-rose-300"
              disabled={busy}
              onClick={(e) => {
                e.stopPropagation();
                onRemove(index);
              }}
            >
              ✕
            </Button>
          </span>
        </Tooltip>
      </div>

      {expanded ? (
        <div className="settings-surface-overlay-soft space-y-4 px-4 py-4">
          <div className="grid gap-2 sm:grid-cols-2">
            <div>
              <HelpLabel
                htmlFor={channelNameInputId}
                label={tx.channelName}
                fieldKey="LLM_CHANNEL_NAME"
                helpKey="settings.llm_channel.channel_name"
                examples={['LLM_CHANNELS=deepseek,aihubmix', 'LLM_DEEPSEEK_MODELS=deepseek-v4-flash,deepseek-v4-pro']}
              />
            <Input
              id={channelNameInputId}
              value={channel.name}
              disabled={busy}
              onChange={(e) => onUpdate(index, 'name', e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ''))}
              placeholder="primary"
            />
            </div>
            <div className="space-y-2">
              <HelpLabel
                htmlFor={protocolInputId}
                label={tx.protocol}
                fieldKey="LLM_CHANNEL_PROTOCOL"
                helpKey="settings.llm_channel.protocol"
                examples={['LLM_DEEPSEEK_PROTOCOL=deepseek', 'LLM_OPENROUTER_PROTOCOL=openai']}
              />
              <Select
                id={protocolInputId}
                value={channel.protocol}
                onChange={(v) => onUpdate(index, 'protocol', normalizeProtocol(v))}
                options={PROTOCOL_OPTIONS}
                disabled={busy}
                placeholder={tx.selectProtocol}
              />
            </div>
          </div>

          <div>
            <HelpLabel
              htmlFor={baseUrlInputId}
              label="Base URL"
              fieldKey="LLM_CHANNEL_BASE_URL"
              helpKey="settings.llm_channel.base_url"
              examples={['LLM_DEEPSEEK_BASE_URL=https://api.deepseek.com', 'LLM_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1']}
            />
          <Input
            id={baseUrlInputId}
            value={channel.baseUrl}
            disabled={busy}
            onChange={(e) => onUpdate(index, 'baseUrl', e.target.value)}
            placeholder={
              channel.protocol === 'gemini' || channel.protocol === 'anthropic'
                ? tx.officialBaseUrlEmpty
                : preset?.baseUrl || 'https://api.example.com/v1'
            }
          />
          </div>

          {showProviderTemplateDetails ? (
            <div className="space-y-2 rounded-xl border border-[var(--settings-border)] bg-[var(--settings-surface-hover)] p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[11px] font-medium text-muted-text">{tx.configReference}</span>
                {providerCapabilities.map((capability) => {
                  const capabilityMeta = getProviderCapabilityText(capability, language);
                  return (
                    <Tooltip key={capability} content={capabilityMeta.hint}>
                      <span className="inline-flex">
                        <Badge variant="default" className="border-[var(--settings-border)] bg-[var(--settings-surface)] text-secondary-text">
                          {capabilityMeta.label}
                        </Badge>
                      </span>
                    </Tooltip>
                  );
                })}
              </div>
              {providerHint ? (
                <p className="text-[11px] leading-5 text-secondary-text">{providerHint}</p>
              ) : null}
              {providerSources.length > 0 ? (
                <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] leading-5 text-secondary-text">
                  <span>{tx.officialSources}</span>
                  {providerSources.map((source) => (
                    <a
                      key={source.url}
                      href={source.url}
                      target="_blank"
                      rel="noreferrer"
                      className="settings-accent-text underline-offset-2 hover:underline"
                    >
                      {source.label}
                    </a>
                  ))}
                </p>
              ) : null}
              <p className="text-[11px] leading-5 text-muted-text">
                {tx.capabilityReferenceHint}
              </p>
            </div>
          ) : null}

          <div>
            <HelpLabel
              htmlFor={apiKeyInputId}
              label="API Key"
              fieldKey="LLM_CHANNEL_API_KEY"
              helpKey="settings.llm_channel.api_key"
              examples={['LLM_DEEPSEEK_API_KEY=sk-xxxx', 'LLM_OPENAI_API_KEYS=sk-key-1,sk-key-2']}
            />
          <Input
            id={apiKeyInputId}
            type="password"
            allowTogglePassword
            iconType="key"
            passwordVisible={visibleKey}
            onPasswordVisibleChange={(nextVisible) => onToggleKeyVisibility(index, nextVisible)}
            value={channel.apiKey}
            disabled={busy}
            onChange={(e) => onUpdate(index, 'apiKey', e.target.value)}
            placeholder={channel.protocol === 'ollama' ? tx.ollamaKeyHint : tx.multiKeyHint}
          />
          </div>

          <div className="space-y-3 rounded-xl border border-[var(--settings-border)] bg-[var(--settings-surface-hover)] p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="settings-secondary"
                size="sm"
                className="px-3 text-[11px] shadow-none"
                disabled={busy}
                onClick={() => onDiscoverModels(channel)}
              >
                {discoveryState?.status === 'loading' ? tx.discoveringModels : tx.discoverModels}
              </Button>
              <span className={`text-xs ${
                discoveryState?.status === 'success'
                  ? 'text-success'
                  : discoveryState?.status === 'error'
                    ? 'text-danger'
                    : 'text-muted-text'
              }`}
              >
                {discoveryState?.text || tx.discoveryDefaultHint}
              </span>
            </div>
            {discoveryState?.hint ? (
              <p className="text-[11px] text-secondary-text">
                {discoveryState.hint}
              </p>
            ) : null}

            {discoveredModels.length > 0 ? (
              <div>
                <HelpLabel
                  label={tx.optionalModels}
                  fieldKey="LLM_CHANNEL_DISCOVERED_MODELS"
                  helpKey="settings.llm_channel.models"
                  examples={['LLM_DEEPSEEK_MODELS=deepseek-v4-flash,deepseek-v4-pro']}
                />
                <div className="max-h-48 space-y-2 overflow-y-auto rounded-xl border border-[var(--settings-border)] bg-[var(--settings-surface)] p-3">
                  {discoveredModels.map((model) => (
                    <label key={model} className="flex items-center gap-2 text-sm text-secondary-text">
                      <input
                        type="checkbox"
                        checked={selectedModels.some((selectedModel) => (
                          areModelsEquivalent(selectedModel, model, channel.protocol)
                        ))}
                        disabled={busy}
                        onChange={() => onUpdate(index, 'models', toggleModelSelection(channel.models, model, channel.protocol))}
                        className="settings-input-checkbox h-4 w-4 rounded border-border/70 bg-base"
                      />
                      <span>{model}</span>
                    </label>
                  ))}
                </div>
              </div>
            ) : null}

            <div>
              <HelpLabel
                htmlFor={modelsInputId}
                label={discoveredModels.length > 0 ? tx.manualModel : tx.model}
                fieldKey="LLM_CHANNEL_MODELS"
                helpKey="settings.llm_channel.models"
                examples={['LLM_DEEPSEEK_MODELS=deepseek-v4-flash,deepseek-v4-pro', 'LLM_OLLAMA_MODELS=qwen3:8b,llama3.1:8b']}
              />
            <Input
              id={modelsInputId}
              value={channel.models}
              disabled={busy}
              onChange={(e) => onUpdate(index, 'models', e.target.value)}
              placeholder={preset?.placeholderModels || MODEL_PLACEHOLDERS_BY_PROTOCOL[channel.protocol]}
              hint={
                discoveredModels.length > 0
                  ? tx.customModelHint
                  : tx.manualModelHint
              }
            />
            </div>

            {manualOnlyModels.length > 0 ? (
              <p className="text-[11px] text-secondary-text">
                {formatText(tx.manualExtraModels, { models: manualOnlyModels.join(language === 'ko' || language === 'en' ? ', ' : '，') })}
              </p>
            ) : null}
          </div>

          <div className="flex items-center gap-2 pt-1">
            <Button
              type="button"
              variant="settings-secondary"
              size="sm"
              className="px-3 text-[11px] shadow-none"
              disabled={busy}
              onClick={() => onTest(channel, index)}
            >
              {testState?.status === 'loading' ? tx.testing : tx.testConnection}
            </Button>
            {testState?.text ? (
              <div className="space-y-1">
                <span className={`block text-xs ${
                  testState.status === 'success'
                    ? 'text-success'
                    : testState.status === 'error'
                      ? 'text-danger'
                      : 'text-muted-text'
                }`}
                >
                  {testState.text}
                </span>
                {selectedModels[0] ? (
                  <p className="text-[11px] text-secondary-text">
                    {formatText(tx.connectionTestModelHint, { model: selectedModels[0] })}
                  </p>
                ) : null}
                {testState.hint ? (
                  <p className="text-[11px] text-secondary-text">
                    {testState.hint}
                  </p>
                ) : null}
              </div>
            ) : null}
          </div>

          <div className="space-y-3 rounded-xl border border-[var(--settings-border)] bg-[var(--settings-surface-hover)] p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="flex items-center gap-1.5">
                  <p className="text-[11px] font-medium text-muted-text">{tx.capabilityOptional}</p>
                  <SettingsHelpButton
                    fieldKey="LLM_CHANNEL_CAPABILITY_CHECKS"
                    title={tx.capabilityCheck}
                    helpKey="settings.llm_channel.capability_checks"
                    examples={['JSON / Tools / Stream / Vision']}
                    docs={LLM_CHANNEL_HELP_DOCS[language]}
                  />
                </div>
                <p className="mt-0.5 text-[11px] text-secondary-text">
                  {tx.capabilityDescription}
                </p>
              </div>
              <Button
                type="button"
                variant="settings-secondary"
                size="sm"
                className="px-3 text-[11px] shadow-none"
                disabled={busy || capabilityBusy || selectedCapabilities.length === 0}
                onClick={() => onCheckCapabilities(channel)}
              >
                {capabilityBusy ? tx.checkingCapabilities : tx.checkCapabilities}
              </Button>
            </div>

            <div className="flex flex-wrap gap-2">
              {runtimeCapabilityOptions.map((option) => (
                <Tooltip key={option.value} content={RUNTIME_CAPABILITY_HINTS[language][option.value]}>
                  <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-[var(--settings-border)] bg-[var(--settings-surface)] px-2 py-1 text-[11px] text-secondary-text">
                    <input
                      type="checkbox"
                      checked={selectedCapabilities.includes(option.value)}
                      disabled={busy || capabilityBusy}
                      onChange={() => onToggleCapability(channel, option.value)}
                      className="settings-input-checkbox h-3.5 w-3.5 rounded border-border/70 bg-base"
                    />
                    <span>{option.label}</span>
                  </label>
                </Tooltip>
              ))}
            </div>

            {capabilityState?.text ? (
              <div className="space-y-1">
                <p className={`text-xs ${
                  capabilityState.status === 'success'
                    ? 'text-success'
                    : capabilityState.status === 'error'
                      ? 'text-danger'
                      : 'text-muted-text'
                }`}
                >
                  {capabilityState.text}
                </p>
                {capabilityState.hint ? (
                  <p className="text-[11px] text-secondary-text">{capabilityState.hint}</p>
                ) : null}
              </div>
            ) : null}

            {Object.keys(capabilityResults).length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {RUNTIME_CAPABILITY_OPTIONS.map((option) => {
                  const result = capabilityResults[option.value];
                  if (!result) return null;
                  return (
                    <Tooltip key={option.value} content={result.message}>
                      <span className="inline-flex">
                        <Badge variant={getCapabilityResultVariant(result.status)}>
                          {option.label} {CAPABILITY_STATUS_LABELS[language][result.status]}
                        </Badge>
                      </span>
                    </Tooltip>
                  );
                })}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
};

function normalizeProtocol(value: string): ChannelProtocol {
  const normalized = value.trim().toLowerCase().replace(/-/g, '_');
  if (normalized === 'vertex' || normalized === 'vertexai') {
    return 'vertex_ai';
  }
  if (normalized === 'claude') {
    return 'anthropic';
  }
  if (normalized === 'google') {
    return 'gemini';
  }
  if (normalized === 'deepseek') {
    return 'deepseek';
  }
  if (normalized === 'gemini') {
    return 'gemini';
  }
  if (normalized === 'anthropic') {
    return 'anthropic';
  }
  if (normalized === 'vertex_ai') {
    return 'vertex_ai';
  }
  if (normalized === 'ollama') {
    return 'ollama';
  }
  return 'openai';
}

function inferProtocol(protocol: string, baseUrl: string, models: string[]): ChannelProtocol {
  const explicit = normalizeProtocol(protocol);
  if (protocol.trim()) {
    return explicit;
  }

  const firstPrefixedModel = models.find((model) => model.includes('/'));
  if (firstPrefixedModel) {
    return normalizeProtocol(firstPrefixedModel.split('/', 1)[0]);
  }

  if (baseUrl.includes('127.0.0.1') || baseUrl.includes('localhost')) {
    return 'openai';
  }

  return 'openai';
}

function parseEnabled(value: string | undefined): boolean {
  if (!value) {
    return true;
  }
  return !FALSEY_VALUES.has(value.trim().toLowerCase());
}

function splitModels(models: string): string[] {
  return models
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);
}

interface ParsedModelRef {
  name: string;
  provider: string;
  hasProvider: boolean;
}

function parseModelRef(model: string): ParsedModelRef {
  const trimmed = model.trim();
  if (!trimmed) {
    return { name: '', provider: '', hasProvider: false };
  }

  const delimiterIndex = trimmed.indexOf('/');
  if (delimiterIndex < 0) {
    return { name: trimmed.toLowerCase(), provider: '', hasProvider: false };
  }

  const rawProvider = trimmed.slice(0, delimiterIndex).trim();
  const name = trimmed.slice(delimiterIndex + 1).trim();
  if (!rawProvider || !name) {
    return { name: '', provider: '', hasProvider: false };
  }

  const lowerProvider = rawProvider.toLowerCase();
  return {
    name: name.toLowerCase(),
    provider: PROTOCOL_ALIASES[lowerProvider] || lowerProvider,
    hasProvider: true,
  };
}

function getModelComparisonKey(model: string, protocol: ChannelProtocol): string {
  const normalizedModel = normalizeModelForRuntime(model, protocol).trim();
  const parsed = parseModelRef(normalizedModel);
  if (!parsed.name) {
    return '';
  }
  return `${parsed.provider}/${parsed.name}`;
}

function areModelsEquivalent(a: string, b: string, protocol: ChannelProtocol): boolean {
  const left = getModelComparisonKey(a, protocol);
  const right = getModelComparisonKey(b, protocol);
  return left !== '' && left === right;
}

function toggleModelSelection(models: string, targetModel: string, protocol: ChannelProtocol): string {
  const selectedModels = splitModels(models);
  const index = selectedModels.findIndex((model) => areModelsEquivalent(model, targetModel, protocol));
  if (index >= 0) {
    return selectedModels.filter((_, itemIndex) => itemIndex !== index).join(',');
  }
  return [...selectedModels, targetModel].join(',');
}

const PROTOCOL_ALIASES: Record<string, string> = {
  vertexai: 'vertex_ai',
  vertex: 'vertex_ai',
  claude: 'anthropic',
  google: 'gemini',
  openai_compatible: 'openai',
  openai_compat: 'openai',
};

function normalizeModelForRuntime(model: string, protocol: ChannelProtocol): string {
  const trimmedModel = model.trim();
  if (!trimmedModel) {
    return trimmedModel;
  }

  if (trimmedModel.includes('/')) {
    const rawPrefix = trimmedModel.split('/', 1)[0].trim();
    const lowerPrefix = rawPrefix.toLowerCase();
    const canonicalPrefix = PROTOCOL_ALIASES[lowerPrefix] || lowerPrefix;
    if (KNOWN_MODEL_PREFIXES.has(lowerPrefix) || KNOWN_MODEL_PREFIXES.has(canonicalPrefix)) {
      if (canonicalPrefix !== lowerPrefix && KNOWN_MODEL_PREFIXES.has(canonicalPrefix)) {
        return `${canonicalPrefix}/${trimmedModel.split('/').slice(1).join('/')}`;
      }
      return trimmedModel;
    }
    return `${protocol}/${trimmedModel}`;
  }

  return `${protocol}/${trimmedModel}`;
}

function resolveModelPreview(models: string, protocol: ChannelProtocol): string[] {
  return splitModels(models).map((model) => normalizeModelForRuntime(model, protocol));
}

interface RouteProvenance {
  routeName: string;
  hasHermes: boolean;
  hasNonHermes: boolean;
}

function resolveChannelRouteModels(channel: ChannelConfig): string[] {
  if (isHermesChannel(channel)) {
    const models = splitModels(channel.models);
    return (models.length > 0 ? models : [HERMES_DEFAULT_MODEL]).map(canonicalizeHermesRouteModel);
  }
  return resolveModelPreview(channel.models, channel.protocol);
}

function buildRouteProvenanceMap(channels: ChannelConfig[]): Map<string, RouteProvenance> {
  const provenance = new Map<string, RouteProvenance>();
  for (const channel of channels) {
    if (!channel.enabled || !channel.name.trim()) {
      continue;
    }
    const hermes = isHermesChannel(channel);
    for (const routeName of resolveChannelRouteModels(channel)) {
      if (!routeName) continue;
      const existing = provenance.get(routeName) || {
        routeName,
        hasHermes: false,
        hasNonHermes: false,
      };
      provenance.set(routeName, {
        ...existing,
        hasHermes: existing.hasHermes || hermes,
        hasNonHermes: existing.hasNonHermes || !hermes,
      });
    }
  }
  return provenance;
}

function buildModelOptions(
  models: string[],
  selectedModel: string,
  autoLabel: string,
  currentConfiguredLabel: string,
): Array<{ value: string; label: string }> {
  const options: Array<{ value: string; label: string }> = [{ value: '', label: autoLabel }];
  if (selectedModel && !models.includes(selectedModel)) {
    options.push({ value: selectedModel, label: formatText(currentConfiguredLabel, { model: selectedModel }) });
  }
  for (const model of models) {
    options.push({ value: model, label: model });
  }
  return options;
}

const LLM_STAGE_LABELS: Record<UiLanguage, Record<string, string>> = {
  zh: {
    model_discovery: '模型发现',
    chat_completion: '聊天调用',
    response_parse: '响应解析',
    capability_json: 'JSON 能力',
    capability_tools: 'Tools 能力',
    capability_stream: 'Stream 能力',
    capability_vision: 'Vision 能力',
  },
  en: {
    model_discovery: 'Model discovery',
    chat_completion: 'Chat completion',
    response_parse: 'Response parsing',
    capability_json: 'JSON capability',
    capability_tools: 'Tools capability',
    capability_stream: 'Stream capability',
    capability_vision: 'Vision capability',
  },
  ko: {
    model_discovery: '모델 목록 조회',
    chat_completion: '채팅 호출',
    response_parse: '응답 파싱',
    capability_json: 'JSON 능력',
    capability_tools: 'Tools 능력',
    capability_stream: 'Stream 능력',
    capability_vision: 'Vision 능력',
  },
};

const LLM_ERROR_LABELS: Record<UiLanguage, Record<string, string>> = {
  zh: {
    auth: '鉴权失败',
    timeout: '请求超时',
    quota: '额度或限流',
    model_not_found: '模型不可用',
    request_blocked: '请求被拦截',
    empty_response: '空响应',
    format_error: '格式异常',
    network_error: '网络异常',
    invalid_config: '配置无效',
    unsupported_protocol: '协议暂不支持',
    capability_unsupported: '能力不支持',
    skipped: '已跳过',
  },
  en: {
    auth: 'Authentication failed',
    timeout: 'Request timeout',
    quota: 'Quota or rate limited',
    model_not_found: 'Model unavailable',
    request_blocked: 'Request blocked',
    empty_response: 'Empty response',
    format_error: 'Format error',
    network_error: 'Network error',
    invalid_config: 'Invalid config',
    unsupported_protocol: 'Protocol not supported yet',
    capability_unsupported: 'Capability not supported',
    skipped: 'Skipped',
  },
  ko: {
    auth: '인증 실패',
    timeout: '요청 시간 초과',
    quota: '한도 또는 속도 제한',
    model_not_found: '모델 사용 불가',
    request_blocked: '요청 차단됨',
    empty_response: '빈 응답',
    format_error: '형식 오류',
    network_error: '네트워크 오류',
    invalid_config: '유효하지 않은 설정',
    unsupported_protocol: '프로토콜 미지원',
    capability_unsupported: '능력 미지원',
    skipped: '건너뜀',
  },
};

const LLM_TROUBLESHOOTING_HINTS: Record<UiLanguage, Record<string, string>> = {
  zh: {
    auth: '请检查 API Key 是否正确、是否有多余空格，以及当前渠道是否需要额外组织/项目权限。',
    timeout: '可重试；若持续超时，请检查 Base URL、网络代理、服务商可用区或本地防火墙。',
    quota: '请检查余额、套餐额度、RPM/TPM 限流或并发设置，必要时稍后重试。',
    model_not_found: '请确认模型名与渠道协议匹配，并先用“获取模型”核对该渠道实际可用模型列表。',
    empty_response: '渠道已连通但未返回正文；可尝试切换兼容模型、关闭额外响应模式后再测试。',
    network_error: '请检查 Base URL、代理、TLS/证书、中转网关或本地网络策略，并可稍后重试。',
    invalid_config: '先补齐协议、Base URL、API Key 和模型配置，再执行一键测试。',
    unsupported_protocol: '当前仅对 OpenAI Compatible / DeepSeek 渠道提供自动模型发现，请改为手动维护模型列表。',
  },
  en: {
    auth: 'Check that the API key is correct and has no extra whitespace, and whether this channel requires additional organization/project permissions.',
    timeout: 'You can retry; if timeouts persist, check the Base URL, network proxy, provider region, or local firewall.',
    quota: 'Check your balance, plan quota, RPM/TPM rate limits, or concurrency settings, and retry later if needed.',
    model_not_found: 'Confirm the model name matches the channel protocol, and use "Fetch models" first to verify the models actually available on this channel.',
    empty_response: 'The channel is reachable but returned no content; try switching to a compatible model or disabling extra response modes, then test again.',
    network_error: 'Check the Base URL, proxy, TLS/certificates, relay gateway, or local network policy, and retry later.',
    invalid_config: 'Fill in the protocol, Base URL, API key, and model config first, then run the one-click test.',
    unsupported_protocol: 'Automatic model discovery is currently only available for OpenAI Compatible / DeepSeek channels; maintain the model list manually instead.',
  },
  ko: {
    auth: 'API Key가 정확한지, 불필요한 공백이 없는지, 현재 채널에 추가 조직/프로젝트 권한이 필요한지 확인하세요.',
    timeout: '다시 시도할 수 있습니다. 시간 초과가 계속되면 Base URL, 네트워크 프록시, 제공자 리전, 로컬 방화벽을 확인하세요.',
    quota: '잔액, 요금제 한도, RPM/TPM 속도 제한, 동시성 설정을 확인하고 필요하면 잠시 후 다시 시도하세요.',
    model_not_found: '모델명이 채널 프로토콜과 일치하는지 확인하고, 먼저 "모델 가져오기"로 해당 채널에서 실제 사용 가능한 모델 목록을 확인하세요.',
    empty_response: '채널은 연결되었지만 본문이 반환되지 않았습니다. 호환 모델로 전환하거나 추가 응답 모드를 끈 뒤 다시 테스트해 보세요.',
    network_error: 'Base URL, 프록시, TLS/인증서, 중계 게이트웨이, 로컬 네트워크 정책을 확인하고 잠시 후 다시 시도하세요.',
    invalid_config: '프로토콜, Base URL, API Key, 모델 설정을 먼저 채운 뒤 원클릭 테스트를 실행하세요.',
    unsupported_protocol: '자동 모델 검색은 현재 OpenAI Compatible / DeepSeek 채널에서만 지원됩니다. 모델 목록을 수동으로 관리해 주세요.',
  },
};

const LLM_REASON_HINTS: Record<UiLanguage, Record<string, string>> = {
  zh: {
    missing_api_key: 'API Key 为空，或逗号分隔后没有任何可用 Key；请填入至少一个有效 Key 后再测试。',
    api_key_rejected: '服务商拒绝了当前 API Key；请检查 Key、组织/项目权限、区域和账号状态。',
    rate_limit: '服务商触发 RPM/TPM 或并发限流；请降低请求频率或稍后重试。',
    insufficient_balance: '服务商返回余额、账单或额度不足；请检查账户余额和套餐状态。',
    quota_exceeded: '服务商返回配额已耗尽；请确认账号套餐、余量和项目额度。',
    provider_blocked: '请求被服务商或中转网关拦截；请检查账号风控、地域限制、模型权限、代理商网关策略、内容安全策略或请求来源限制。',
    dns_error: '域名解析失败；请检查 Base URL 域名、网络代理和 DNS 配置。',
    tls_error: 'TLS/证书握手失败；请检查 HTTPS 证书、中转网关或公司代理策略。',
    connection_refused: '目标服务拒绝连接；请确认 Base URL 端口、服务进程和防火墙配置。',
    model_access_denied: '当前账号无法使用该模型；请确认模型是否已开通、账号是否可见，或模型是否已被禁用。',
    provider_prefix_mismatch: '模型 provider 前缀与当前渠道不匹配；请确认模型名是否应使用该渠道的 OpenAI-compatible 路由。',
    capability_unsupported: '当前模型或兼容层不支持该能力；这不影响基础文本连接，可换模型或关闭该能力依赖。',
  },
  en: {
    missing_api_key: 'The API key is empty, or no usable key remains after comma splitting; enter at least one valid key before testing.',
    api_key_rejected: 'The provider rejected the current API key; check the key, organization/project permissions, region, and account status.',
    rate_limit: 'The provider triggered RPM/TPM or concurrency rate limiting; reduce request frequency or retry later.',
    insufficient_balance: 'The provider reported insufficient balance, billing issues, or quota; check your account balance and plan status.',
    quota_exceeded: 'The provider reported the quota is exhausted; confirm your account plan, remaining credit, and project quota.',
    provider_blocked: 'The request was blocked by the provider or a relay gateway; check account risk controls, regional restrictions, model permissions, reseller gateway policies, content-safety policies, or request-origin restrictions.',
    dns_error: 'Domain resolution failed; check the Base URL domain, network proxy, and DNS configuration.',
    tls_error: 'TLS/certificate handshake failed; check the HTTPS certificate, relay gateway, or corporate proxy policy.',
    connection_refused: 'The target service refused the connection; confirm the Base URL port, service process, and firewall configuration.',
    model_access_denied: 'The current account cannot use this model; confirm the model is enabled, visible to the account, and not disabled.',
    provider_prefix_mismatch: 'The model\'s provider prefix does not match this channel; confirm whether the model name should use this channel\'s OpenAI-compatible route.',
    capability_unsupported: 'The current model or compatibility layer does not support this capability; basic text connectivity is unaffected — switch models or drop the dependency on this capability.',
  },
  ko: {
    missing_api_key: 'API Key가 비어 있거나 쉼표로 나눈 뒤 사용 가능한 Key가 없습니다. 유효한 Key를 하나 이상 입력한 뒤 테스트하세요.',
    api_key_rejected: '제공자가 현재 API Key를 거부했습니다. Key, 조직/프로젝트 권한, 리전, 계정 상태를 확인하세요.',
    rate_limit: '제공자의 RPM/TPM 또는 동시성 속도 제한에 걸렸습니다. 요청 빈도를 낮추거나 잠시 후 다시 시도하세요.',
    insufficient_balance: '제공자가 잔액, 결제 또는 한도 부족을 반환했습니다. 계정 잔액과 요금제 상태를 확인하세요.',
    quota_exceeded: '제공자가 할당량 소진을 반환했습니다. 계정 요금제, 잔여량, 프로젝트 한도를 확인하세요.',
    provider_blocked: '요청이 제공자 또는 중계 게이트웨이에서 차단되었습니다. 계정 리스크 관리, 지역 제한, 모델 권한, 대행 게이트웨이 정책, 콘텐츠 안전 정책, 요청 출처 제한을 확인하세요.',
    dns_error: '도메인 해석에 실패했습니다. Base URL 도메인, 네트워크 프록시, DNS 설정을 확인하세요.',
    tls_error: 'TLS/인증서 핸드셰이크에 실패했습니다. HTTPS 인증서, 중계 게이트웨이, 회사 프록시 정책을 확인하세요.',
    connection_refused: '대상 서비스가 연결을 거부했습니다. Base URL 포트, 서비스 프로세스, 방화벽 설정을 확인하세요.',
    model_access_denied: '현재 계정에서 이 모델을 사용할 수 없습니다. 모델 활성화 여부, 계정 가시성, 모델 비활성화 여부를 확인하세요.',
    provider_prefix_mismatch: '모델의 provider 접두사가 현재 채널과 일치하지 않습니다. 모델명이 이 채널의 OpenAI 호환 라우트를 사용해야 하는지 확인하세요.',
    capability_unsupported: '현재 모델 또는 호환 레이어가 이 능력을 지원하지 않습니다. 기본 텍스트 연결에는 영향이 없으며, 모델을 바꾸거나 이 능력 의존을 끄면 됩니다.',
  },
};

const LLM_DIAGNOSTIC_TEXT: Record<UiLanguage, {
  connectionTest: string;
  testFailed: string;
  discoveryFormatError: string;
  testFormatError: string;
  discoveryEmptyResponse: string;
  testedModel: string;
  scopeInfo: string;
  modelActionHint: string;
  failureWithRaw: string;
  failurePlain: string;
  capabilitySummary: string;
}> = {
  zh: {
    connectionTest: '连接测试',
    testFailed: '测试失败',
    discoveryFormatError: '该渠道返回的 /models 响应格式不兼容，请改为手动填写模型列表。',
    testFormatError: '返回结构与预期不一致，请确认该渠道兼容 Chat Completions 接口。',
    discoveryEmptyResponse: '该渠道的 /models 接口未返回可用模型 ID；请检查 Base URL 是否指向兼容的模型列表接口，或改为手动填写模型列表。',
    testedModel: '本次测试模型：{model}。',
    scopeInfo: '基础连接测试默认只测试模型列表中的第一个模型。',
    modelActionHint: '若该模型不可用，请调整模型顺序或移除不可用模型后重试。',
    failureWithRaw: '{prefix}：{summary}（原始摘要：{raw}）',
    failurePlain: '{prefix}：{summary}',
    capabilitySummary: '能力检测完成：{passed} 通过 / {failed} 失败 / {skipped} 跳过',
  },
  en: {
    connectionTest: 'Connection test',
    testFailed: 'Test failed',
    discoveryFormatError: 'This channel returned an incompatible /models response format; fill in the model list manually instead.',
    testFormatError: 'The response structure did not match expectations; confirm this channel is compatible with the Chat Completions API.',
    discoveryEmptyResponse: 'The channel\'s /models endpoint returned no usable model IDs; check that the Base URL points to a compatible model-list endpoint, or fill in the model list manually.',
    testedModel: 'Model tested this time: {model}.',
    scopeInfo: 'The basic connection test only tests the first model in the model list by default.',
    modelActionHint: 'If that model is unavailable, reorder the model list or remove unavailable models and retry.',
    failureWithRaw: '{prefix}: {summary} (raw summary: {raw})',
    failurePlain: '{prefix}: {summary}',
    capabilitySummary: 'Capability check finished: {passed} passed / {failed} failed / {skipped} skipped',
  },
  ko: {
    connectionTest: '연결 테스트',
    testFailed: '테스트 실패',
    discoveryFormatError: '이 채널의 /models 응답 형식이 호환되지 않습니다. 모델 목록을 수동으로 입력해 주세요.',
    testFormatError: '응답 구조가 예상과 다릅니다. 이 채널이 Chat Completions API와 호환되는지 확인하세요.',
    discoveryEmptyResponse: '이 채널의 /models 엔드포인트가 사용 가능한 모델 ID를 반환하지 않았습니다. Base URL이 호환되는 모델 목록 엔드포인트를 가리키는지 확인하거나 모델 목록을 수동으로 입력하세요.',
    testedModel: '이번 테스트 모델: {model}.',
    scopeInfo: '기본 연결 테스트는 기본적으로 모델 목록의 첫 번째 모델만 테스트합니다.',
    modelActionHint: '해당 모델을 사용할 수 없다면 모델 순서를 조정하거나 사용 불가 모델을 제거한 뒤 다시 시도하세요.',
    failureWithRaw: '{prefix}: {summary} (원본 요약: {raw})',
    failurePlain: '{prefix}: {summary}',
    capabilitySummary: '능력 검사 완료: 통과 {passed} / 실패 {failed} / 건너뜀 {skipped}',
  },
};

function getLlmStageLabel(language: UiLanguage, stage?: string | null): string {
  return LLM_STAGE_LABELS[language][stage || ''] || LLM_DIAGNOSTIC_TEXT[language].connectionTest;
}

function getLlmErrorCodeLabel(language: UiLanguage, code?: string | null): string {
  return LLM_ERROR_LABELS[language][code || ''] || LLM_DIAGNOSTIC_TEXT[language].testFailed;
}

function getLlmTroubleshootingHint(
  language: UiLanguage,
  code?: string | null,
  stage?: string | null,
  context: 'test' | 'discovery' = 'test',
  details?: Record<string, unknown>,
): string | undefined {
  const reason = typeof details?.reason === 'string' ? details.reason : '';
  if (reason && LLM_REASON_HINTS[language][reason]) {
    return LLM_REASON_HINTS[language][reason];
  }
  if (code === 'format_error') {
    return context === 'discovery' || stage === 'model_discovery'
      ? LLM_DIAGNOSTIC_TEXT[language].discoveryFormatError
      : LLM_DIAGNOSTIC_TEXT[language].testFormatError;
  }
  if (code === 'empty_response' && (context === 'discovery' || stage === 'model_discovery')) {
    return LLM_DIAGNOSTIC_TEXT[language].discoveryEmptyResponse;
  }
  return LLM_TROUBLESHOOTING_HINTS[language][code || ''];
}

function buildLlmTestHint(language: UiLanguage, result: {
  errorCode?: string | null;
  stage?: string | null;
  details?: Record<string, unknown>;
  resolvedModel?: string | null;
}): string | undefined {
  const text = LLM_DIAGNOSTIC_TEXT[language];
  const reason = typeof result.details?.reason === 'string' ? result.details.reason : '';
  const detailsModel = typeof result.details?.model === 'string' ? result.details.model : '';
  const testedModel = result.resolvedModel || detailsModel;
  const modelHint = testedModel ? formatText(text.testedModel, { model: testedModel }) : '';
  const scopeInfo = text.scopeInfo;
  const shouldSuggestModelListChange = reason === 'model_access_denied'
    || reason === 'model_not_found'
    || (result.errorCode === 'model_not_found' && !reason);
  const modelActionHint = shouldSuggestModelListChange ? text.modelActionHint : '';
  const troubleshootingHint = getLlmTroubleshootingHint(language, result.errorCode, result.stage, 'test', result.details);
  return [modelHint, scopeInfo, modelActionHint, troubleshootingHint].filter(Boolean).join(' ') || undefined;
}

function buildLlmFailureText(language: UiLanguage, result: {
  message: string;
  error?: string | null;
  stage?: string | null;
  errorCode?: string | null;
}): string {
  const text = LLM_DIAGNOSTIC_TEXT[language];
  const prefix = `${getLlmStageLabel(language, result.stage)} · ${getLlmErrorCodeLabel(language, result.errorCode)}`;
  const summary = result.message || text.testFailed;
  if (result.error && result.error !== result.message) {
    return formatText(text.failureWithRaw, { prefix, summary, raw: result.error });
  }
  return formatText(text.failurePlain, { prefix, summary });
}

function getCapabilityResultVariant(status: LLMCapabilityCheckResult['status']): 'success' | 'danger' | 'warning' {
  if (status === 'passed') return 'success';
  if (status === 'skipped') return 'warning';
  return 'danger';
}

function summarizeCapabilityResults(
  language: UiLanguage,
  results: Partial<Record<LLMCapabilityCheck, LLMCapabilityCheckResult>>,
): string {
  const values = Object.values(results);
  const passed = values.filter((result) => result?.status === 'passed').length;
  const failed = values.filter((result) => result?.status === 'failed').length;
  const skipped = values.filter((result) => result?.status === 'skipped').length;
  return formatText(LLM_DIAGNOSTIC_TEXT[language].capabilitySummary, { passed, failed, skipped });
}

function getFirstCapabilityHint(
  language: UiLanguage,
  results: Partial<Record<LLMCapabilityCheck, LLMCapabilityCheckResult>>,
): string | undefined {
  for (const result of Object.values(results)) {
    if (!result || result.status === 'passed') continue;
    const hint = getLlmTroubleshootingHint(language, result.errorCode, result.stage, 'test', result.details);
    if (hint) return hint;
  }
  return undefined;
}

const MANAGED_PROVIDERS = new Set(['gemini', 'vertex_ai', 'anthropic', 'openai', 'deepseek']);
const LEGACY_PROVIDER_KEYS: Record<string, string[]> = {
  gemini: ['GEMINI_API_KEYS', 'GEMINI_API_KEY'],
  vertex_ai: ['GEMINI_API_KEYS', 'GEMINI_API_KEY'],
  anthropic: ['ANTHROPIC_API_KEYS', 'ANTHROPIC_API_KEY'],
  openai: ['OPENAI_API_KEYS', 'AIHUBMIX_KEY', 'OPENAI_API_KEY'],
  deepseek: ['DEEPSEEK_API_KEYS', 'DEEPSEEK_API_KEY'],
};

function getRuntimeProvider(model: string): string {
  if (!model) return '';
  if (!model.includes('/')) return 'openai';
  return model.split('/', 1)[0].trim().toLowerCase();
}

function usesDirectEnvProvider(model: string): boolean {
  const provider = getRuntimeProvider(model);
  return Boolean(provider) && !MANAGED_PROVIDERS.has(provider);
}

function hasLegacyRuntimeSource(model: string, itemMap: Map<string, string>): boolean {
  const provider = PROTOCOL_ALIASES[getRuntimeProvider(model)] || getRuntimeProvider(model);
  if (!provider || !MANAGED_PROVIDERS.has(provider)) {
    return false;
  }
  return (LEGACY_PROVIDER_KEYS[provider] || []).some((key) => (itemMap.get(key) || '').trim().length > 0);
}

function isRuntimeModelAvailable(model: string, availableModels: string[], itemMap: Map<string, string>): boolean {
  const normalizedModel = model.trim();
  const matchesAvailableModel = normalizedModel.length > 0 && availableModels.includes(normalizedModel);
  return matchesAvailableModel
    || usesDirectEnvProvider(model)
    || (availableModels.length === 0 && hasLegacyRuntimeSource(model, itemMap));
}

function hasCanonicalRouteAliasMismatch(model: string, availableModels: string[]): boolean {
  const normalizedModel = model.trim();
  if (!normalizedModel || availableModels.includes(normalizedModel) || usesDirectEnvProvider(normalizedModel)) {
    return false;
  }
  for (const candidate of routeIdentityCandidates(normalizedModel)) {
    if (candidate !== normalizedModel && availableModels.includes(candidate)) {
      return true;
    }
  }
  return false;
}

function sanitizeRuntimeConfigForSave(
  runtimeConfig: RuntimeConfig,
  generationModels: string[],
  agentSafeModels: string[],
  visionSafeModels: string[],
  itemMap: Map<string, string>,
): RuntimeConfig {
  const primaryModel = runtimeConfig.primaryModel && !isRuntimeModelAvailable(runtimeConfig.primaryModel, generationModels, itemMap)
    ? ''
    : runtimeConfig.primaryModel;
  const agentPrimaryModel = runtimeConfig.agentPrimaryModel && !isRuntimeModelAvailable(runtimeConfig.agentPrimaryModel, agentSafeModels, itemMap)
    ? ''
    : runtimeConfig.agentPrimaryModel;
  const visionModel = runtimeConfig.visionModel && !isRuntimeModelAvailable(runtimeConfig.visionModel, visionSafeModels, itemMap)
    ? ''
    : runtimeConfig.visionModel;
  const fallbackModels = runtimeConfig.fallbackModels.filter((model) => isRuntimeModelAvailable(model, generationModels, itemMap));

  return {
    ...runtimeConfig,
    primaryModel,
    agentPrimaryModel,
    fallbackModels,
    visionModel,
  };
}

function runtimeConfigsAreEqual(left: RuntimeConfig, right: RuntimeConfig): boolean {
  return left.primaryModel === right.primaryModel
    && left.agentPrimaryModel === right.agentPrimaryModel
    && left.visionModel === right.visionModel
    && left.temperature === right.temperature
    && left.fallbackModels.join(',') === right.fallbackModels.join(',');
}

function runtimeConfigChangedKeys(left: RuntimeConfig, right: RuntimeConfig): Set<string> {
  const changed = new Set<string>();
  if (left.primaryModel !== right.primaryModel) {
    changed.add('LITELLM_MODEL');
  }
  if (left.agentPrimaryModel !== right.agentPrimaryModel) {
    changed.add('AGENT_LITELLM_MODEL');
  }
  if (left.fallbackModels.join(',') !== right.fallbackModels.join(',')) {
    changed.add('LITELLM_FALLBACK_MODELS');
  }
  if (left.temperature !== right.temperature) {
    changed.add('LLM_TEMPERATURE');
  }
  if (left.visionModel !== right.visionModel) {
    changed.add('VISION_MODEL');
  }
  return changed;
}

function resolveTemperatureFromItems(itemMap: Map<string, string>): string {
  const unified = itemMap.get('LLM_TEMPERATURE');
  if (unified) return unified;

  const primaryModel = itemMap.get('LITELLM_MODEL') || '';
  const provider = primaryModel.includes('/') ? primaryModel.split('/')[0] : (primaryModel ? 'openai' : '');
  const providerTemperatureEnv: Record<string, string> = {
    gemini: 'GEMINI_TEMPERATURE',
    vertex_ai: 'GEMINI_TEMPERATURE',
    anthropic: 'ANTHROPIC_TEMPERATURE',
    openai: 'OPENAI_TEMPERATURE',
    deepseek: 'OPENAI_TEMPERATURE',
  };
  const preferredEnv = providerTemperatureEnv[provider];
  if (preferredEnv) {
    const val = itemMap.get(preferredEnv);
    if (val) return val;
  }

  for (const envName of ['GEMINI_TEMPERATURE', 'ANTHROPIC_TEMPERATURE', 'OPENAI_TEMPERATURE']) {
    const val = itemMap.get(envName);
    if (val) return val;
  }

  return '0.7';
}

function normalizeAgentPrimaryModel(model: string): string {
  const trimmedModel = model.trim();
  if (!trimmedModel) {
    return '';
  }
  if (trimmedModel.includes('/')) {
    return trimmedModel;
  }
  return `openai/${trimmedModel}`;
}

function parseRuntimeConfigFromItems(items: Array<{ key: string; value: string }>): RuntimeConfig {
  const itemMap = new Map(items.map((item) => [item.key, item.value]));
  return {
    primaryModel: itemMap.get('LITELLM_MODEL') || '',
    agentPrimaryModel: normalizeAgentPrimaryModel(itemMap.get('AGENT_LITELLM_MODEL') || ''),
    fallbackModels: splitModels(itemMap.get('LITELLM_FALLBACK_MODELS') || ''),
    visionModel: itemMap.get('VISION_MODEL') || '',
    temperature: resolveTemperatureFromItems(itemMap),
  };
}

function parseChannelsFromItems(
  items: Array<{ key: string; value: string }>,
  itemSourceByKey: Map<string, boolean> = new Map(),
): ChannelConfig[] {
  const itemMap = new Map(items.map((item) => [item.key.toUpperCase(), item.value]));
  const channelNames = (itemMap.get('LLM_CHANNELS') || '')
    .split(',')
    .map((segment) => segment.trim())
    .filter(Boolean);

  return channelNames.map((name, index) => {
    const upperName = name.toUpperCase();
    const baseUrl = itemMap.get(`LLM_${upperName}_BASE_URL`) || '';
    const rawModels = itemMap.get(`LLM_${upperName}_MODELS`) || '';
    const models = splitModels(rawModels);

    return {
      id: `parsed:${index}:${upperName}`,
      name: name.toLowerCase(),
      protocol: inferProtocol(itemMap.get(`LLM_${upperName}_PROTOCOL`) || '', baseUrl, models),
      baseUrl,
      apiKey: resolveInitialChannelApiKeyValue(name, itemMap, itemSourceByKey),
      models: rawModels,
      enabled: parseEnabled(itemMap.get(`LLM_${upperName}_ENABLED`)),
    };
  });
}

function channelsToUpdateItems(
  channels: ChannelConfig[],
  previousChannelNames: string[],
  runtimeConfig: RuntimeConfig,
  includeRuntimeConfig: boolean,
): Array<{ key: string; value: string }> {
  const updates: Array<{ key: string; value: string }> = [];
  const activeNames = channels.map((channel) => channel.name.toUpperCase());

  updates.push({ key: 'LLM_CHANNELS', value: channels.map((channel) => channel.name).join(',') });
  if (includeRuntimeConfig) {
    updates.push({ key: 'LITELLM_MODEL', value: runtimeConfig.primaryModel });
    updates.push({ key: 'AGENT_LITELLM_MODEL', value: runtimeConfig.agentPrimaryModel });
    updates.push({ key: 'LITELLM_FALLBACK_MODELS', value: runtimeConfig.fallbackModels.join(',') });
    updates.push({ key: 'VISION_MODEL', value: runtimeConfig.visionModel });
    updates.push({ key: 'LLM_TEMPERATURE', value: runtimeConfig.temperature });
  }

  for (const channel of channels) {
    const prefix = `LLM_${channel.name.toUpperCase()}`;
    const isMultiKey = channel.apiKey.includes(',');
    updates.push({ key: `${prefix}_PROTOCOL`, value: channel.protocol });
    updates.push({ key: `${prefix}_BASE_URL`, value: channel.baseUrl });
    updates.push({ key: `${prefix}_ENABLED`, value: channel.enabled ? 'true' : 'false' });
    if (isHermesChannel(channel)) {
      updates.push({ key: `${prefix}_API_KEY`, value: channel.apiKey });
      updates.push({ key: `${prefix}_API_KEYS`, value: '' });
      updates.push({ key: `${prefix}_EXTRA_HEADERS`, value: '' });
    } else {
      updates.push({ key: `${prefix}_API_KEY${isMultiKey ? 'S' : ''}`, value: channel.apiKey });
      updates.push({ key: `${prefix}_API_KEY${isMultiKey ? '' : 'S'}`, value: '' });
    }
    updates.push({ key: `${prefix}_MODELS`, value: channel.models });
  }

  for (const oldName of previousChannelNames) {
    const upperName = oldName.toUpperCase();
    if (activeNames.includes(upperName)) {
      continue;
    }

    const prefix = `LLM_${upperName}`;
    updates.push({ key: `${prefix}_PROTOCOL`, value: '' });
    updates.push({ key: `${prefix}_BASE_URL`, value: '' });
    updates.push({ key: `${prefix}_ENABLED`, value: '' });
    updates.push({ key: `${prefix}_API_KEY`, value: '' });
    updates.push({ key: `${prefix}_API_KEYS`, value: '' });
    updates.push({ key: `${prefix}_MODELS`, value: '' });
    updates.push({ key: `${prefix}_EXTRA_HEADERS`, value: '' });
  }

  return updates;
}

function channelNamesAreSafe(channels: ChannelConfig[]): boolean {
  return channels.every((channel) => /^[a-z0-9_]+$/.test(channel.name.trim()));
}

function buildFilteredChannelUpdateItems({
  channels,
  initialChannels,
  initialNames,
  initialItemSourceByKey,
  savedItemMap,
  runtimeConfig,
  initialRuntimeConfig,
  managesRuntimeConfig,
}: {
  channels: ChannelConfig[];
  initialChannels: ChannelConfig[];
  initialNames: string[];
  initialItemSourceByKey: Map<string, boolean>;
  savedItemMap: Map<string, string>;
  runtimeConfig: RuntimeConfig;
  initialRuntimeConfig: RuntimeConfig;
  managesRuntimeConfig: boolean;
}): Array<{ key: string; value: string }> {
  const changedKeys = new Set<string>([
    ...buildChangedItemKeys(channels, initialChannels, initialItemSourceByKey, savedItemMap),
    ...runtimeConfigChangedKeys(runtimeConfig, initialRuntimeConfig),
  ]);
  return channelsToUpdateItems(channels, initialNames, runtimeConfig, managesRuntimeConfig).filter((item) => {
    const itemKey = item.key.toUpperCase();
    const initialItemSource = initialItemSourceByKey.get(itemKey);
    if (initialItemSource === false) {
      return changedKeys.has(itemKey);
    }
    if (isChannelSecretFieldKey(itemKey) && initialItemSource === undefined) {
      return changedKeys.has(itemKey);
    }
    return true;
  });
}

function buildChannelDraftItems({
  hasChanges,
  channels,
  initialChannels,
  initialNames,
  initialItemSourceByKey,
  savedItemMap,
  runtimeConfig,
  initialRuntimeConfig,
  managesRuntimeConfig,
}: {
  hasChanges: boolean;
  channels: ChannelConfig[];
  initialChannels: ChannelConfig[];
  initialNames: string[];
  initialItemSourceByKey: Map<string, boolean>;
  savedItemMap: Map<string, string>;
  runtimeConfig: RuntimeConfig;
  initialRuntimeConfig: RuntimeConfig;
  managesRuntimeConfig: boolean;
}): Array<{ key: string; value: string }> {
  if (!hasChanges || !channelNamesAreSafe(channels)) {
    return [];
  }
  return buildFilteredChannelUpdateItems({
    channels,
    initialChannels,
    initialNames,
    initialItemSourceByKey,
    savedItemMap,
    runtimeConfig,
    initialRuntimeConfig,
    managesRuntimeConfig,
  });
}

function channelsAreEqual(left: ChannelConfig, right: ChannelConfig): boolean {
  return (
    left.name === right.name
    && left.protocol === right.protocol
    && left.baseUrl === right.baseUrl
    && left.apiKey === right.apiKey
    && left.models === right.models
    && left.enabled === right.enabled
  );
}

export const LLMChannelEditor: React.FC<LLMChannelEditorProps> = ({
  items,
  configVersion,
  maskToken,
  onSaved,
  onDraftItemsChange,
  disabled = false,
}) => {
  const { language } = useUiLanguage();
  const tx = LLM_CHANNEL_TEXT[language];
  const localText = CHANNEL_LOCAL_TEXT[language];
  const initialItemSourceByKey = useMemo(() => {
    const sourceByKey = new Map<string, boolean>();
    for (const item of items) {
      sourceByKey.set(item.key.toUpperCase(), item.rawValueExists !== false);
    }
    for (const [key, hasSource] of sourceByKey) {
      if (hasSource) {
        continue;
      }
      const match = CHANNEL_FIELD_KEY_PATTERN.exec(key);
      if (!match) {
        continue;
      }
      const channelName = match[1];
      for (const channelKey of parseChannelFieldKeysFromName(channelName)) {
        if (!sourceByKey.has(channelKey)) {
          sourceByKey.set(channelKey, false);
        }
      }
    }
    return sourceByKey;
  }, [items]);
  const initialChannels = useMemo(
    () => parseChannelsFromItems(items, initialItemSourceByKey),
    [items, initialItemSourceByKey],
  );
  const initialNames = useMemo(() => initialChannels.map((channel) => channel.name), [initialChannels]);
  const initialRuntimeConfig = useMemo(() => parseRuntimeConfigFromItems(items), [items]);
  const savedItemMap = useMemo(() => new Map(items.map((item) => [item.key.toUpperCase(), item.value])), [items]);
  const hasPersistedHermesSecret = (channel: ChannelConfig): boolean => (
    isHermesChannel(channel) && initialItemSourceByKey.get('LLM_HERMES_API_KEY') === true
  );
  const hasLitellmConfig = useMemo(
    () => items.some((item) => item.key === 'LITELLM_CONFIG' && item.value.trim().length > 0),
    [items],
  );
  const managesRuntimeConfig = !hasLitellmConfig;

  const channelsFingerprint = useMemo(() => JSON.stringify(initialChannels), [initialChannels]);
  const runtimeFingerprint = useMemo(() => JSON.stringify(initialRuntimeConfig), [initialRuntimeConfig]);

  const [channels, setChannels] = useState<ChannelConfig[]>(initialChannels);
  const [runtimeConfig, setRuntimeConfig] = useState<RuntimeConfig>(initialRuntimeConfig);
  const [isSaving, setIsSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<
    | { type: 'success'; text: string }
    | { type: 'error'; error: ParsedApiError }
    | { type: 'local-error'; text: string }
    | null
  >(null);
  const [saveWarnings, setSaveWarnings] = useState<string[]>([]);
  const [visibleKeys, setVisibleKeys] = useState<Record<number, boolean>>({});
  const [testStates, setTestStates] = useState<Record<number, ChannelTestState>>({});
  const [discoveryStates, setDiscoveryStates] = useState<Record<string, ChannelDiscoveryState>>({});
  const [capabilityStates, setCapabilityStates] = useState<Record<string, ChannelCapabilityState>>({});
  const [expandedRows, setExpandedRows] = useState<Record<number, boolean>>({});
  const [isCollapsed, setIsCollapsed] = useState(false);
  const [addPreset, setAddPreset] = useState('aihubmix');
  const addChannelIdRef = useRef(0);
  const lastDraftFingerprintRef = useRef<string | null>(null);
  const onDraftItemsChangeRef = useRef(onDraftItemsChange);

  const prevChannelsRef = useRef(channelsFingerprint);
  const prevRuntimeRef = useRef(runtimeFingerprint);
  const pendingSaveFeedbackFingerprintRef = useRef<{ channels: string; runtime: string } | null>(null);
  const discoveryNonceRef = useRef<Record<string, number>>({});
  const discoveryRequestIdRef = useRef(0);
  const capabilityNonceRef = useRef<Record<string, number>>({});
  const capabilityRequestIdRef = useRef(0);

  useEffect(() => {
    if (prevChannelsRef.current === channelsFingerprint && prevRuntimeRef.current === runtimeFingerprint) {
      return;
    }
    prevChannelsRef.current = channelsFingerprint;
    prevRuntimeRef.current = runtimeFingerprint;
    const pendingSaveFeedbackFingerprint = pendingSaveFeedbackFingerprintRef.current;
    const preserveSaveFeedback = pendingSaveFeedbackFingerprint?.channels === channelsFingerprint
      && pendingSaveFeedbackFingerprint.runtime === runtimeFingerprint;
    pendingSaveFeedbackFingerprintRef.current = null;
    setChannels(initialChannels);
    setRuntimeConfig(initialRuntimeConfig);
    setVisibleKeys({});
    setTestStates({});
    setDiscoveryStates({});
    setCapabilityStates({});
    setExpandedRows({});
    discoveryNonceRef.current = {};
    capabilityNonceRef.current = {};
    if (!preserveSaveFeedback) {
      setSaveMessage(null);
      setSaveWarnings([]);
    }
    setIsCollapsed(false);
  }, [channelsFingerprint, runtimeFingerprint, initialChannels, initialRuntimeConfig]);

  const routeProvenanceMap = useMemo(() => {
    if (!managesRuntimeConfig) {
      return new Map<string, RouteProvenance>();
    }
    return buildRouteProvenanceMap(channels);
  }, [channels, managesRuntimeConfig]);

  const availableModels = useMemo(
    () => Array.from(routeProvenanceMap.values())
      .filter((origin) => !(origin.hasHermes && origin.hasNonHermes))
      .map((origin) => origin.routeName),
    [routeProvenanceMap],
  );

  const agentSafeModels = useMemo(
    () => Array.from(routeProvenanceMap.values())
      .filter((origin) => !origin.hasHermes || origin.hasNonHermes)
      .map((origin) => origin.routeName),
    [routeProvenanceMap],
  );

  const visionSafeModels = useMemo(
    () => Array.from(routeProvenanceMap.values())
      .filter((origin) => !origin.hasHermes)
      .map((origin) => origin.routeName),
    [routeProvenanceMap],
  );

  const agentSelectedModelForOptions = useMemo(() => {
    if (!runtimeConfig.agentPrimaryModel || agentSafeModels.includes(runtimeConfig.agentPrimaryModel)) {
      return runtimeConfig.agentPrimaryModel;
    }
    const origin = getRouteProvenance(routeProvenanceMap, runtimeConfig.agentPrimaryModel);
    return origin?.hasHermes && !origin.hasNonHermes ? '' : runtimeConfig.agentPrimaryModel;
  }, [agentSafeModels, routeProvenanceMap, runtimeConfig.agentPrimaryModel]);

  const visionSelectedModelForOptions = useMemo(() => {
    if (!runtimeConfig.visionModel || visionSafeModels.includes(runtimeConfig.visionModel)) {
      return runtimeConfig.visionModel;
    }
    const origin = getRouteProvenance(routeProvenanceMap, runtimeConfig.visionModel);
    return origin?.hasHermes ? '' : runtimeConfig.visionModel;
  }, [routeProvenanceMap, runtimeConfig.visionModel, visionSafeModels]);

  const hasChanges = useMemo(() => {
    const runtimeChanged = (
      runtimeConfig.primaryModel !== initialRuntimeConfig.primaryModel
      || runtimeConfig.agentPrimaryModel !== initialRuntimeConfig.agentPrimaryModel
      || runtimeConfig.visionModel !== initialRuntimeConfig.visionModel
      || runtimeConfig.temperature !== initialRuntimeConfig.temperature
      || runtimeConfig.fallbackModels.join(',') !== initialRuntimeConfig.fallbackModels.join(',')
    );

    if (runtimeChanged || channels.length !== initialChannels.length) {
      return true;
    }
    return channels.some((channel, index) => !channelsAreEqual(channel, initialChannels[index]));
  }, [channels, initialChannels, initialRuntimeConfig, runtimeConfig]);

  const draftItems = useMemo(() => buildChannelDraftItems({
    hasChanges,
    channels,
    initialChannels,
    initialNames,
    initialItemSourceByKey,
    savedItemMap,
    runtimeConfig,
    initialRuntimeConfig,
    managesRuntimeConfig,
  }), [
    channels,
    hasChanges,
    initialChannels,
    initialItemSourceByKey,
    initialNames,
    initialRuntimeConfig,
    managesRuntimeConfig,
    runtimeConfig,
    savedItemMap,
  ]);
  const draftFingerprint = useMemo(() => JSON.stringify(draftItems), [draftItems]);

  useEffect(() => {
    onDraftItemsChangeRef.current = onDraftItemsChange;
  }, [onDraftItemsChange]);

  useEffect(() => {
    if (!onDraftItemsChange || lastDraftFingerprintRef.current === draftFingerprint) {
      return;
    }
    lastDraftFingerprintRef.current = draftFingerprint;
    onDraftItemsChange(draftItems);
  }, [draftFingerprint, draftItems, onDraftItemsChange]);

  useEffect(() => () => {
    onDraftItemsChangeRef.current?.([]);
  }, []);

  const busy = disabled || isSaving;

  const updateChannel = (index: number, field: keyof ChannelConfig, value: string | boolean) => {
    const currentChannel = channels[index];
    setChannels((previous) => previous.map((channel, rowIndex) => {
      if (rowIndex !== index) return channel;
      const updated = { ...channel, [field]: value };

      if (field === 'name' && typeof value === 'string') {
        const newPreset = getProviderTemplate(value);
        if (newPreset) {
          const oldPreset = getProviderTemplate(channel.name);
          if (!updated.baseUrl || updated.baseUrl === (oldPreset?.baseUrl ?? '')) {
            updated.baseUrl = newPreset.baseUrl;
          }
          updated.protocol = newPreset.protocol;
          if (!updated.models || updated.models === (oldPreset?.placeholderModels ?? '')) {
            updated.models = newPreset.placeholderModels;
          }
        }
      }

      return updated;
    }));
    setTestStates((previous) => {
      if (!(index in previous)) {
        return previous;
      }
      const next = { ...previous };
      delete next[index];
      return next;
    });
    if (field !== 'models' && field !== 'enabled') {
      setDiscoveryStates((previous) => {
        const channel = channels.find((_, itemIndex) => itemIndex === index);
        if (!channel || !(channel.id in previous)) {
          return previous;
        }
        const next = { ...previous };
        delete next[channel.id];
        delete discoveryNonceRef.current[channel.id];
        return next;
      });
    }
    if (currentChannel) {
      delete capabilityNonceRef.current[currentChannel.id];
      setCapabilityStates((previous) => {
        const current = previous[currentChannel.id];
        if (!current) {
          return previous;
        }
        return {
          ...previous,
          [currentChannel.id]: {
            ...current,
            status: 'idle',
            text: undefined,
            hint: undefined,
            results: {},
          },
        };
      });
    }
  };

  const removeChannel = (index: number) => {
    const removedChannelId = channels[index]?.id || '';
    setChannels((previous) => previous.filter((_, rowIndex) => rowIndex !== index));
    setVisibleKeys({});
    setTestStates({});
    setDiscoveryStates((previous) => {
      if (!removedChannelId) {
        return previous;
      }
      const next = { ...previous };
      delete next[removedChannelId];
      return next;
    });
    setCapabilityStates((previous) => {
      if (!removedChannelId || !(removedChannelId in previous)) {
        return previous;
      }
      const next = { ...previous };
      delete next[removedChannelId];
      return next;
    });
    if (removedChannelId) {
      const nextNonce = { ...discoveryNonceRef.current };
      delete nextNonce[removedChannelId];
      discoveryNonceRef.current = nextNonce;
      delete capabilityNonceRef.current[removedChannelId];
    }
    setExpandedRows({});
  };

  const addChannel = () => {
    const preset = getProviderTemplate(addPreset) || getProviderTemplate('custom');
    if (!preset) {
      return;
    }
    setChannels((previous) => {
      const existingNames = new Set(previous.map((channel) => channel.name));
      const baseName = addPreset === 'custom' ? 'custom' : addPreset;
      let nextName = baseName;
      let counter = 2;
      while (existingNames.has(nextName)) {
        nextName = `${baseName}${counter}`;
        counter += 1;
      }

      return [
        ...previous,
        {
          id: `added:${addChannelIdRef.current += 1}`,
          name: nextName,
          protocol: preset.protocol,
          baseUrl: preset.baseUrl,
          apiKey: '',
          models: preset.placeholderModels || '',
          enabled: true,
        },
      ];
    });
    setTestStates({});
    setDiscoveryStates({});
    setCapabilityStates({});
    discoveryNonceRef.current = {};
    capabilityNonceRef.current = {};
    setExpandedRows((prev) => ({ ...prev, [channels.length]: true }));
    setIsCollapsed(false);
  };

  const handleSave = async () => {
    const hasEmptyName = channels.some((channel) => !channel.name.trim());
    if (hasEmptyName) {
      setSaveMessage({ type: 'local-error', text: localText.invalidChannelName });
      return;
    }

    if (managesRuntimeConfig) {
      const mixedPrimary = runtimeConfig.primaryModel
        && getRouteProvenance(routeProvenanceMap, runtimeConfig.primaryModel)?.hasHermes
        && getRouteProvenance(routeProvenanceMap, runtimeConfig.primaryModel)?.hasNonHermes;
      const mixedFallback = runtimeConfig.fallbackModels.find((model) => {
        const origin = getRouteProvenance(routeProvenanceMap, model);
        return origin?.hasHermes && origin.hasNonHermes;
      });
      if (mixedPrimary || mixedFallback) {
        setSaveMessage({ type: 'local-error', text: localText.mixedHermesRoute });
        return;
      }

      const nonCanonicalRouteAlias = (
        hasCanonicalRouteAliasMismatch(runtimeConfig.primaryModel, availableModels)
        || hasCanonicalRouteAliasMismatch(runtimeConfig.agentPrimaryModel, agentSafeModels)
        || hasCanonicalRouteAliasMismatch(runtimeConfig.visionModel, visionSafeModels)
        || runtimeConfig.fallbackModels.some((model) => hasCanonicalRouteAliasMismatch(model, availableModels))
      );
      if (nonCanonicalRouteAlias) {
        setSaveMessage({ type: 'local-error', text: localText.nonCanonicalRouteAlias });
        return;
      }
    }

    const runtimeConfigForSave = managesRuntimeConfig
      ? sanitizeRuntimeConfigForSave(runtimeConfig, availableModels, agentSafeModels, visionSafeModels, savedItemMap)
      : runtimeConfig;
    if (!runtimeConfigsAreEqual(runtimeConfigForSave, runtimeConfig)) {
      setRuntimeConfig(runtimeConfigForSave);
    }

    if (managesRuntimeConfig) {
      const invalidPrimaryModel = runtimeConfigForSave.primaryModel
        && !isRuntimeModelAvailable(runtimeConfigForSave.primaryModel, availableModels, savedItemMap);
      if (invalidPrimaryModel) {
        setSaveMessage({ type: 'local-error', text: localText.primaryModelUnavailable });
        return;
      }

      const invalidAgentPrimaryModel = runtimeConfigForSave.agentPrimaryModel
        && !isRuntimeModelAvailable(runtimeConfigForSave.agentPrimaryModel, agentSafeModels, savedItemMap);
      if (invalidAgentPrimaryModel) {
        setSaveMessage({ type: 'local-error', text: localText.agentPrimaryModelUnavailable });
        return;
      }

      const invalidFallbackModel = runtimeConfigForSave.fallbackModels.some(
        (model) => !isRuntimeModelAvailable(model, availableModels, savedItemMap),
      );
      if (invalidFallbackModel) {
        setSaveMessage({ type: 'local-error', text: localText.invalidFallbackModel });
        return;
      }

      const invalidVisionModel = runtimeConfigForSave.visionModel
        && !isRuntimeModelAvailable(runtimeConfigForSave.visionModel, visionSafeModels, savedItemMap);
      if (invalidVisionModel) {
        setSaveMessage({ type: 'local-error', text: localText.visionModelHermes });
        return;
      }
    }

    setIsSaving(true);
    setSaveMessage(null);
    setSaveWarnings([]);

    try {
      const updateItems = buildFilteredChannelUpdateItems({
        channels,
        initialChannels,
        initialNames,
        initialItemSourceByKey,
        savedItemMap,
        runtimeConfig: runtimeConfigForSave,
        initialRuntimeConfig,
        managesRuntimeConfig,
      });
      const response = await systemConfigApi.update({
        configVersion,
        maskToken,
        reloadNow: true,
        items: updateItems,
      });
      const responseWarnings = response.warnings || [];
      await onSaved(updateItems);
      pendingSaveFeedbackFingerprintRef.current = {
        channels: JSON.stringify(parseChannelsFromItems(updateItems)),
        runtime: JSON.stringify(parseRuntimeConfigFromItems(updateItems)),
      };
      setSaveWarnings(responseWarnings);
      setSaveMessage({ type: 'success', text: managesRuntimeConfig ? localText.aiConfigSaved : localText.channelConfigSaved });
    } catch (error: unknown) {
      setSaveWarnings([]);
      setSaveMessage({ type: 'error', error: getParsedApiError(error) });
    } finally {
      setIsSaving(false);
    }
  };

  const handleTest = async (channel: ChannelConfig, index: number) => {
    if (hasRuntimeOnlyMaskedHermesSecret(channel, maskToken, hasPersistedHermesSecret(channel))) {
      setTestStates((previous) => ({
        ...previous,
        [index]: { status: 'error', text: localText.runtimeOnlyHermesSecret },
      }));
      return;
    }

    setTestStates((previous) => ({
      ...previous,
      [index]: { status: 'loading', text: localText.testingStatus },
    }));

    try {
      const result = await systemConfigApi.testLLMChannel({
        name: channel.name,
        protocol: channel.protocol,
        baseUrl: channel.baseUrl,
        apiKey: channel.apiKey,
        models: splitModels(channel.models),
        enabled: channel.enabled,
        useSavedSecret: shouldUseSavedHermesSecret(channel, maskToken, hasPersistedHermesSecret(channel)),
      });

      const text = result.success
        ? `${localText.connectionSuccess}${result.resolvedModel ? ` · ${result.resolvedModel}` : ''}${result.latencyMs ? ` · ${result.latencyMs} ms` : ''}`
        : buildLlmFailureText(language, result);
      const hint = result.success ? undefined : buildLlmTestHint(language, result);

      setTestStates((previous) => ({
        ...previous,
        [index]: {
          status: result.success ? 'success' : 'error',
          text,
          hint,
        },
      }));
    } catch (error: unknown) {
      const parsed = getParsedApiError(error);
      setTestStates((previous) => ({
        ...previous,
        [index]: { status: 'error', text: parsed.message || localText.testFailed },
      }));
    }
  };

  const handleDiscoverModels = async (channel: ChannelConfig) => {
    if (hasRuntimeOnlyMaskedHermesSecret(channel, maskToken, hasPersistedHermesSecret(channel))) {
      setDiscoveryStates((previous) => ({
        ...previous,
        [channel.id]: {
          status: 'error',
          text: localText.runtimeOnlyHermesSecret,
          hint: undefined,
          models: previous[channel.id]?.models || [],
        },
      }));
      return;
    }

    const requestId = discoveryRequestIdRef.current + 1;
    discoveryRequestIdRef.current = requestId;
    discoveryNonceRef.current[channel.id] = requestId;
    const nonce = requestId;

    setDiscoveryStates((previous) => ({
      ...previous,
      [channel.id]: {
        status: 'loading',
        text: localText.discoveringModelsStatus,
        hint: undefined,
        models: previous[channel.id]?.models || [],
      },
    }));

    try {
      const result = await systemConfigApi.discoverLLMChannelModels({
        name: channel.name,
        protocol: channel.protocol,
        baseUrl: channel.baseUrl,
        apiKey: channel.apiKey,
        models: splitModels(channel.models),
        useSavedSecret: shouldUseSavedHermesSecret(channel, maskToken, hasPersistedHermesSecret(channel)),
      });

      if (discoveryNonceRef.current[channel.id] !== nonce) return;

      setDiscoveryStates((previous) => ({
        ...previous,
        [channel.id]: {
          status: result.success ? 'success' : 'error',
          text: result.success
            ? `${formatText(localText.modelsDiscovered, { count: result.models.length })}${result.latencyMs ? ` · ${result.latencyMs} ms` : ''}`
            : buildLlmFailureText(language, result),
          hint: result.success ? undefined : getLlmTroubleshootingHint(language, result.errorCode, result.stage, 'discovery', result.details),
          models: result.success ? result.models : (previous[channel.id]?.models || []),
        },
      }));
    } catch (error: unknown) {
      if (discoveryNonceRef.current[channel.id] !== nonce) return;

      const parsed = getParsedApiError(error);
      setDiscoveryStates((previous) => ({
        ...previous,
        [channel.id]: {
          status: 'error',
          text: parsed.message || localText.discoverFailed,
          hint: undefined,
          models: previous[channel.id]?.models || [],
        },
      }));
    }
  };

  const toggleCapability = (channel: ChannelConfig, capability: LLMCapabilityCheck) => {
    setCapabilityStates((previous) => {
      const current = previous[channel.id] || { selected: [], status: 'idle', results: {} };
      const selected = current.selected.includes(capability)
        ? current.selected.filter((item) => item !== capability)
        : [...current.selected, capability];
      return {
        ...previous,
        [channel.id]: {
          ...current,
          selected,
          status: current.status === 'loading' ? current.status : 'idle',
          text: current.status === 'loading' ? current.text : undefined,
          hint: current.status === 'loading' ? current.hint : undefined,
          results: current.status === 'loading' ? current.results : {},
        },
      };
    });
  };

  const handleCapabilityCheck = async (channel: ChannelConfig) => {
    const selected = (capabilityStates[channel.id]?.selected || []).filter(
      (capability) => !isHermesChannel(channel) || capability === 'json',
    );
    if (selected.length === 0) return;

    if (hasRuntimeOnlyMaskedHermesSecret(channel, maskToken, hasPersistedHermesSecret(channel))) {
      setCapabilityStates((previous) => ({
        ...previous,
        [channel.id]: {
          selected,
          status: 'error',
          text: localText.runtimeOnlyHermesSecret,
          hint: undefined,
          results: {},
        },
      }));
      return;
    }

    const requestId = capabilityRequestIdRef.current + 1;
    capabilityRequestIdRef.current = requestId;
    capabilityNonceRef.current[channel.id] = requestId;
    const nonce = requestId;

    setCapabilityStates((previous) => ({
      ...previous,
      [channel.id]: {
        selected,
        status: 'loading',
        text: localText.checkingCapabilitiesStatus,
        hint: undefined,
        results: {},
      },
    }));

    try {
      const result = await systemConfigApi.testLLMChannel({
        name: channel.name,
        protocol: channel.protocol,
        baseUrl: channel.baseUrl,
        apiKey: channel.apiKey,
        models: splitModels(channel.models),
        enabled: channel.enabled,
        capabilityChecks: selected,
        useSavedSecret: shouldUseSavedHermesSecret(channel, maskToken, hasPersistedHermesSecret(channel)),
      });

      if (capabilityNonceRef.current[channel.id] !== nonce) return;

      const capabilityResults = result.capabilityResults || {};
      const hasFailure = Object.values(capabilityResults).some((item) => item?.status === 'failed');
      const hasSkipped = Object.values(capabilityResults).some((item) => item?.status === 'skipped');
      setCapabilityStates((previous) => ({
        ...previous,
        [channel.id]: {
          selected,
          status: hasFailure || hasSkipped || !result.success ? 'error' : 'success',
          text: Object.keys(capabilityResults).length > 0
            ? summarizeCapabilityResults(language, capabilityResults)
            : result.success
              ? localText.noCapabilityResults
              : buildLlmFailureText(language, result),
          hint: getFirstCapabilityHint(language, capabilityResults)
            || (!result.success ? buildLlmTestHint(language, result) : undefined),
          results: capabilityResults,
        },
      }));
    } catch (error: unknown) {
      if (capabilityNonceRef.current[channel.id] !== nonce) return;

      const parsed = getParsedApiError(error);
      setCapabilityStates((previous) => ({
        ...previous,
        [channel.id]: {
          selected,
          status: 'error',
          text: parsed.message || localText.capabilityCheckFailed,
          hint: undefined,
          results: {},
        },
      }));
    }
  };

  const toggleKeyVisibility = (index: number, nextVisible: boolean) => {
    setVisibleKeys((previous) => ({ ...previous, [index]: nextVisible }));
  };

  const toggleExpand = (index: number) => {
    setExpandedRows((previous) => ({ ...previous, [index]: !previous[index] }));
  };

  const setPrimaryModel = (value: string) => {
    setRuntimeConfig((previous) => ({
      ...previous,
      primaryModel: value,
      fallbackModels: previous.fallbackModels.filter((model) => model !== value),
    }));
  };

  const toggleFallbackModel = (model: string) => {
    setRuntimeConfig((previous) => {
      const alreadySelected = previous.fallbackModels.includes(model);
      return {
        ...previous,
        fallbackModels: alreadySelected
          ? previous.fallbackModels.filter((item) => item !== model)
          : [...previous.fallbackModels, model],
      };
    });
  };

  return (
    <div className="space-y-4">
      <button
        type="button"
        className="flex w-full items-center justify-between rounded-[1.35rem] border border-[var(--settings-border)] bg-[var(--settings-surface)] px-5 py-4 text-left shadow-soft-card transition-[background-color,border-color,box-shadow] duration-200 hover:border-[var(--settings-border-strong)] hover:bg-[var(--settings-surface-hover)]"
        onClick={() => setIsCollapsed((previous) => !previous)}
      >
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <h3 className="text-base font-semibold text-foreground">{tx.configTitle}</h3>
            <Badge variant="info" className="settings-accent-badge">{tx.channelManagement}</Badge>
          </div>
          <p className="text-xs text-muted-text">
            {tx.configDescription}
          </p>
        </div>
        <span className="text-xs text-muted-text">{isCollapsed ? `▶ ${tx.expand}` : `▼ ${tx.collapse}`}</span>
      </button>

      {!isCollapsed ? (
        <div className="space-y-4 animate-in fade-in slide-in-from-top-2 duration-300">
          <div className="rounded-[1.35rem] border border-[var(--settings-border)] bg-[var(--settings-surface)] p-4 shadow-soft-card">
            <div className="mb-3 flex items-center justify-between">
              <div>
                <h4 className="text-sm font-medium text-foreground">{tx.quickAddTitle}</h4>
                <p className="mt-1 text-xs text-secondary-text">{tx.quickAddDescription}</p>
              </div>
              <Badge variant="default" className="border-[var(--settings-border)] bg-[var(--settings-surface-hover)] text-muted-text">
                {formatText(tx.channelCount, { count: channels.length })}
              </Badge>
            </div>
            <div className="flex items-center gap-2">
              <Button type="button" variant="settings-primary" className="whitespace-nowrap" disabled={busy} onClick={addChannel}>
                {tx.addChannel}
              </Button>
              <Select
                value={addPreset}
                onChange={setAddPreset}
                options={LLM_PROVIDER_TEMPLATES.map((preset) => ({
                  value: preset.channelId,
                  label: getProviderDisplayLabel(preset.channelId, preset.label, language),
                }))}
                disabled={busy}
                placeholder={tx.selectProvider}
                className="flex-1"
              />
            </div>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between px-1">
              <span className="text-xs font-medium uppercase tracking-wider text-muted-text">{tx.channelList}</span>
              {channels.length > 0 ? (
                <span className="text-[10px] text-muted-text">
                  {formatText(tx.enabledCount, { enabled: channels.filter((c) => c.enabled).length, total: channels.length })}
                </span>
              ) : null}
            </div>

            {channels.length === 0 ? (
              <div className="settings-surface-overlay-muted rounded-[1.35rem] border border-dashed settings-border-strong px-4 py-10 text-center">
                <p className="text-sm font-medium text-secondary-text">{tx.noChannelsTitle}</p>
                <p className="mt-1 text-xs text-muted-text">{tx.noChannelsDescription}</p>
              </div>
            ) : channels.map((channel, index) => (
              <ChannelRow
                key={channel.id}
                channel={channel}
                index={index}
                busy={busy}
                visibleKey={Boolean(visibleKeys[index])}
                expanded={Boolean(expandedRows[index])}
                testState={testStates[index]}
                discoveryState={discoveryStates[channel.id]}
                capabilityState={capabilityStates[channel.id]}
                onUpdate={updateChannel}
                onRemove={removeChannel}
                onToggleExpand={toggleExpand}
                onToggleKeyVisibility={toggleKeyVisibility}
                onTest={(ch, idx) => void handleTest(ch, idx)}
                onDiscoverModels={(channel) => void handleDiscoverModels(channel)}
                onToggleCapability={toggleCapability}
                onCheckCapabilities={(channel) => void handleCapabilityCheck(channel)}
              />
            ))}
          </div>

          {managesRuntimeConfig ? (
            <div className="rounded-[1.35rem] border border-[var(--settings-border)] bg-[var(--settings-surface)] p-4 shadow-soft-card">
              <div className="mb-4 flex items-center justify-between">
                <div>
                  <span className="settings-accent-text text-xs font-medium uppercase tracking-wider">{tx.runtimeParams}</span>
                  <p className="mt-1 text-[11px] text-muted-text">{tx.runtimeDescription}</p>
                </div>
                <Badge variant="default" className="border-[var(--settings-border)] bg-[var(--settings-surface-hover)] text-muted-text">Runtime</Badge>
              </div>
              <div className="mb-4">
                <HelpLabel
                  label="Temperature"
                  fieldKey="LLM_TEMPERATURE"
                  helpKey="settings.llm_channel.temperature"
                  examples={['LLM_TEMPERATURE=0.2', 'LLM_TEMPERATURE=0.7']}
                  compact
                />
                <div className="flex items-center gap-3">
                  <input
                    type="range"
                    min="0"
                    max="2"
                    step="0.1"
                    value={runtimeConfig.temperature}
                    disabled={busy}
                    onChange={(event) => setRuntimeConfig((previous) => ({ ...previous, temperature: event.target.value }))}
                    className="settings-input-checkbox h-1.5 flex-1 cursor-pointer rounded-full bg-border/60"
                  />
                  <span className="w-8 text-right text-sm text-secondary-text">{runtimeConfig.temperature}</span>
                </div>
                <p className="mt-1 text-[11px] text-secondary-text">
                  {tx.temperatureHint}
                </p>
              </div>

              {availableModels.length === 0 ? (
                <div className="rounded-xl border border-dashed settings-border-strong settings-surface-overlay-soft px-3 py-2 text-xs text-muted-text">
                  {tx.noRuntimeModelsHint}
                </div>
              ) : (
                <div className="space-y-4">
                  <div>
                    <HelpLabel
                      htmlFor="runtime-primary-model"
                      label={tx.mainModel}
                      fieldKey="LITELLM_MODEL"
                      helpKey="settings.llm_channel.primary_model"
                      examples={['LITELLM_MODEL=deepseek/deepseek-v4-flash']}
                      compact
                    />
                    <Select
                      id="runtime-primary-model"
                      value={runtimeConfig.primaryModel}
                      onChange={setPrimaryModel}
                      options={buildModelOptions(availableModels, runtimeConfig.primaryModel, tx.autoFirstModel, localText.currentConfiguredModel)}
                      disabled={busy}
                      placeholder=""
                    />
                  </div>

                  <div>
                    <HelpLabel
                      htmlFor="runtime-agent-primary-model"
                      label={tx.agentModel}
                      fieldKey="AGENT_LITELLM_MODEL"
                      helpKey="settings.llm_channel.agent_primary_model"
                      examples={['AGENT_LITELLM_MODEL=deepseek/deepseek-v4-pro']}
                      compact
                    />
                    <Select
                      id="runtime-agent-primary-model"
                      value={runtimeConfig.agentPrimaryModel}
                      onChange={(value) => setRuntimeConfig((previous) => ({
                        ...previous,
                        agentPrimaryModel: normalizeAgentPrimaryModel(value),
                      }))}
                      options={buildModelOptions(
                        agentSafeModels,
                        agentSelectedModelForOptions,
                        tx.autoInheritPrimaryModel,
                        localText.currentConfiguredModel,
                      )}
                      disabled={busy}
                      placeholder=""
                    />
                  </div>

                  <div>
                    <HelpLabel
                      label={tx.fallbackModel}
                      fieldKey="LITELLM_FALLBACK_MODELS"
                      helpKey="settings.llm_channel.fallback_models"
                      examples={['LITELLM_FALLBACK_MODELS=deepseek/deepseek-v4-pro,gemini/gemini-3-flash-preview']}
                      compact
                    />
                    <div className="space-y-2 rounded-xl border settings-border-strong settings-surface-overlay-soft p-3">
                      {availableModels.map((model) => (
                        <label key={model} className="flex items-center gap-2 text-sm text-secondary-text">
                          <input
                            type="checkbox"
                            checked={runtimeConfig.fallbackModels.includes(model)}
                            disabled={busy || model === runtimeConfig.primaryModel}
                            onChange={() => toggleFallbackModel(model)}
                            className="settings-input-checkbox h-4 w-4 rounded border-border/70 bg-base"
                          />
                          <span>{model}</span>
                        </label>
                      ))}
                    </div>
                    <p className="mt-1 text-[11px] text-secondary-text">
                      {tx.fallbackHint}
                    </p>
                  </div>

                  <div>
                    <HelpLabel
                      htmlFor="runtime-vision-model"
                      label={tx.visionModel}
                      fieldKey="VISION_MODEL"
                      helpKey="settings.llm_channel.vision_model"
                      examples={['VISION_MODEL=gemini/gemini-3.1-pro-preview']}
                      compact
                    />
                    <Select
                      id="runtime-vision-model"
                      value={runtimeConfig.visionModel}
                      onChange={(value) => setRuntimeConfig((previous) => ({ ...previous, visionModel: value }))}
                      options={buildModelOptions(
                        visionSafeModels,
                        visionSelectedModelForOptions,
                        tx.autoVisionDefault,
                        localText.currentConfiguredModel,
                      )}
                      disabled={busy}
                      placeholder=""
                    />
                  </div>
                </div>
              )}
            </div>
          ) : (
            <InlineAlert
              variant="warning"
              message={tx.advancedYamlWarning}
              className="rounded-[1.35rem] px-4 py-3 text-xs shadow-none"
            />
          )}

          <div className="flex flex-wrap items-center gap-3">
            <Button
              type="button"
              variant="settings-primary"
              glow
              disabled={busy || !hasChanges}
              onClick={() => void handleSave()}
            >
              {isSaving ? tx.saving : managesRuntimeConfig ? tx.saveAiConfig : tx.saveChannelConfig}
            </Button>
            {!hasChanges ? <span className="text-xs text-muted-text">{tx.noUnsavedChanges}</span> : null}
          </div>

          {saveMessage?.type === 'success' ? (
            <InlineAlert
              variant="success"
              message={saveMessage.text}
              className="rounded-lg px-3 py-2 text-sm shadow-none"
            />
          ) : null}

          {saveWarnings.length > 0 ? (
            <InlineAlert
              variant="warning"
              title={tx.saveHint}
              message={(
                <div className="space-y-1">
                  {saveWarnings.map((warning) => (
                    <p key={warning}>{warning}</p>
                  ))}
                </div>
              )}
              className="rounded-lg px-3 py-2 text-sm shadow-none"
            />
          ) : null}

          {saveMessage?.type === 'local-error' ? (
            <InlineAlert
              variant="danger"
              message={saveMessage.text}
              className="rounded-lg px-3 py-2 text-sm shadow-none"
            />
          ) : null}

          {saveMessage?.type === 'error' ? <ApiErrorAlert error={saveMessage.error} /> : null}
        </div>
      ) : null}
    </div>
  );
};
