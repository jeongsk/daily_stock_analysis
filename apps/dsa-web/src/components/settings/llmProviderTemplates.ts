import type { UiLanguage } from '../../i18n/uiText';

export type ChannelProtocol = 'openai' | 'deepseek' | 'gemini' | 'anthropic' | 'vertex_ai' | 'ollama';
export type LLMProviderCapability =
  | 'openai-compatible'
  | 'aggregator'
  | 'official-api'
  | 'model-discovery'
  | 'vision'
  | 'local-runtime';

export interface LLMProviderTemplate {
  channelId: string;
  label: string;
  protocol: ChannelProtocol;
  baseUrl: string;
  placeholderModels: string;
  capabilities: LLMProviderCapability[];
  configHint?: string;
  officialSources: Array<{
    label: string;
    url: string;
  }>;
}

export const LLM_PROVIDER_CAPABILITY_LABELS: Record<LLMProviderCapability, { label: string; hint: string }> = {
  'openai-compatible': {
    label: 'OpenAI 兼容',
    hint: '按 OpenAI-compatible endpoint 配置 Base URL，不额外拼接 /chat/completions。',
  },
  aggregator: {
    label: '聚合平台',
    hint: '模型可见性、路由和价格可能随账号权限与平台策略变化。',
  },
  'official-api': {
    label: '官方 API',
    hint: '使用服务商官方协议或官方兼容入口。',
  },
  'model-discovery': {
    label: '可获取模型',
    hint: '支持尝试通过 /models 获取模型列表；实际结果仍取决于账号权限和 API Key。',
  },
  vision: {
    label: 'Vision 提示',
    hint: '模板提示该 provider 常用于 Vision 场景；具体模型能力仍以账号和模型列表为准。',
  },
  'local-runtime': {
    label: '本地运行',
    hint: '需要当前运行环境能访问对应本地服务。',
  },
};

// 模板数据保持中文为规范值（含测试契约）；en/ko 仅在展示层通过 getter 覆盖。
const LLM_PROVIDER_CAPABILITY_LABEL_OVERRIDES: Partial<Record<UiLanguage, Record<LLMProviderCapability, { label: string; hint: string }>>> = {
  en: {
    'openai-compatible': {
      label: 'OpenAI compatible',
      hint: 'Configure the Base URL as an OpenAI-compatible endpoint; /chat/completions is not appended automatically.',
    },
    aggregator: {
      label: 'Aggregator',
      hint: 'Model visibility, routing, and pricing may change with account permissions and platform policies.',
    },
    'official-api': {
      label: 'Official API',
      hint: 'Uses the provider\'s official protocol or official compatible endpoint.',
    },
    'model-discovery': {
      label: 'Model discovery',
      hint: 'Supports fetching the model list via /models; actual results still depend on account permissions and the API key.',
    },
    vision: {
      label: 'Vision hint',
      hint: 'The template marks this provider as commonly used for Vision; actual model capability still depends on your account and model list.',
    },
    'local-runtime': {
      label: 'Local runtime',
      hint: 'Requires the current runtime environment to reach the local service.',
    },
  },
  ko: {
    'openai-compatible': {
      label: 'OpenAI 호환',
      hint: 'Base URL을 OpenAI 호환 엔드포인트로 설정하세요. /chat/completions는 추가로 붙이지 않습니다.',
    },
    aggregator: {
      label: '통합 플랫폼',
      hint: '모델 가시성, 라우팅, 가격은 계정 권한과 플랫폼 정책에 따라 달라질 수 있습니다.',
    },
    'official-api': {
      label: '공식 API',
      hint: '서비스 제공자의 공식 프로토콜 또는 공식 호환 엔드포인트를 사용합니다.',
    },
    'model-discovery': {
      label: '모델 목록 조회',
      hint: '/models로 모델 목록 조회를 지원합니다. 실제 결과는 계정 권한과 API Key에 따라 달라집니다.',
    },
    vision: {
      label: 'Vision 힌트',
      hint: '이 제공자가 Vision 용도로 자주 사용됨을 나타내는 템플릿 힌트입니다. 실제 모델 능력은 계정과 모델 목록 기준입니다.',
    },
    'local-runtime': {
      label: '로컬 실행',
      hint: '현재 실행 환경에서 해당 로컬 서비스에 접근할 수 있어야 합니다.',
    },
  },
};

export function getProviderCapabilityText(
  capability: LLMProviderCapability,
  language: UiLanguage,
): { label: string; hint: string } {
  return LLM_PROVIDER_CAPABILITY_LABEL_OVERRIDES[language]?.[capability] ?? LLM_PROVIDER_CAPABILITY_LABELS[capability];
}

export const LLM_PROVIDER_TEMPLATES: LLMProviderTemplate[] = [
  {
    channelId: 'aihubmix',
    label: 'AIHubmix（聚合平台）',
    protocol: 'openai',
    baseUrl: 'https://aihubmix.com/v1',
    placeholderModels: 'gpt-5.5,claude-sonnet-4-6,gemini-3.1-pro-preview',
    capabilities: ['openai-compatible', 'aggregator'],
    officialSources: [{ label: 'AIHubmix', url: 'https://aihubmix.com/' }],
  },
  {
    channelId: 'anspire',
    label: 'Anspire Open（一站式模型+搜索）',
    protocol: 'openai',
    baseUrl: 'https://open-gateway.anspire.cn/v6',
    placeholderModels: 'Doubao-Seed-2.0-lite,Doubao-Seed-2.0-pro,qwen3.5-flash,MiniMax-M2.7',
    capabilities: ['openai-compatible'],
    configHint:
      '同一 ANSPIRE_API_KEYS 可复用到搜索与 LLM 渠道。以下模型与网关为配置示例，实际可用性请以账号权限和控制台为准；建议先点“测试连接”确认。',
    officialSources: [
      { label: 'Anspire Open', url: 'https://open.anspire.cn/?share_code=QFBC0FYC' },
      {
        label: 'LiteLLM OpenAI-compatible',
        url: 'https://docs.litellm.ai/docs/providers/openai_compatible',
      },
    ],
  },
  {
    channelId: 'deepseek',
    label: 'DeepSeek 官方',
    protocol: 'deepseek',
    baseUrl: 'https://api.deepseek.com',
    placeholderModels: 'deepseek-v4-flash,deepseek-v4-pro',
    capabilities: ['official-api', 'openai-compatible'],
    officialSources: [{ label: 'DeepSeek API Docs', url: 'https://api-docs.deepseek.com/' }],
  },
  {
    channelId: 'dashscope',
    label: '通义千问（Dashscope）',
    protocol: 'openai',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    placeholderModels: 'qwen3.6-plus,qwen3.6-flash',
    capabilities: ['openai-compatible', 'model-discovery'],
    officialSources: [
      { label: 'DashScope Text Generation', url: 'https://help.aliyun.com/zh/model-studio/text-generation-model/' },
    ],
  },
  {
    channelId: 'zhipu',
    label: '智谱 GLM',
    protocol: 'openai',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    placeholderModels: 'glm-5.1,glm-4.7-flash',
    capabilities: ['openai-compatible'],
    officialSources: [{ label: 'Zhipu Model Overview', url: 'https://docs.bigmodel.cn/cn/guide/start/model-overview' }],
  },
  {
    channelId: 'moonshot',
    label: 'Moonshot（月之暗面）',
    protocol: 'openai',
    baseUrl: 'https://api.moonshot.cn/v1',
    placeholderModels: 'kimi-k2.6,kimi-k2.5',
    capabilities: ['openai-compatible'],
    officialSources: [{ label: 'Kimi Platform Docs', url: 'https://platform.kimi.com/docs/models' }],
  },
  {
    channelId: 'minimax',
    label: 'MiniMax 官方',
    protocol: 'openai',
    baseUrl: 'https://api.minimax.io/v1',
    placeholderModels: 'MiniMax-M3,MiniMax-M2.7,MiniMax-M2.7-highspeed',
    capabilities: ['openai-compatible'],
    officialSources: [
      { label: 'MiniMax OpenAI API', url: 'https://platform.minimax.io/docs/api-reference/text-chat' },
      { label: 'MiniMax Models', url: 'https://platform.minimax.io/docs/api-reference/models/openai/list-models' },
    ],
  },
  {
    channelId: 'volcengine',
    label: '火山方舟（豆包）',
    protocol: 'openai',
    baseUrl: 'https://ark.cn-beijing.volces.com/api/v3',
    placeholderModels: 'doubao-seed-1-6-251015,doubao-seed-1-6-thinking-251015',
    capabilities: ['openai-compatible'],
    configHint: '确认在线推理 endpoint / region 与 Coding Plan 专用入口不要混用。',
    officialSources: [
      { label: 'Volcengine Ark Inference', url: 'https://www.volcengine.com/docs/82379/2121998' },
      { label: 'Volcengine Ark Models', url: 'https://www.volcengine.com/docs/82379/1949118' },
    ],
  },
  {
    channelId: 'siliconflow',
    label: '硅基流动（SiliconFlow）',
    protocol: 'openai',
    baseUrl: 'https://api.siliconflow.cn/v1',
    placeholderModels: 'deepseek-ai/DeepSeek-V3.2,Qwen/Qwen3-235B-A22B-Thinking-2507',
    capabilities: ['openai-compatible', 'model-discovery'],
    configHint: '模型列表和模型可见性依赖账号权限与 API Key。',
    officialSources: [{ label: 'SiliconFlow Models', url: 'https://docs.siliconflow.cn/quickstart/models' }],
  },
  {
    channelId: 'openrouter',
    label: 'OpenRouter',
    protocol: 'openai',
    baseUrl: 'https://openrouter.ai/api/v1',
    placeholderModels: '~anthropic/claude-sonnet-latest,~openai/gpt-latest',
    capabilities: ['openai-compatible', 'aggregator', 'model-discovery'],
    configHint: '模型列表和模型可见性依赖账号权限与 API Key。',
    officialSources: [
      { label: 'OpenRouter Models API', url: 'https://openrouter.ai/docs/api/api-reference/models/get-models' },
    ],
  },
  {
    channelId: 'gemini',
    label: 'Gemini 官方',
    protocol: 'gemini',
    baseUrl: '',
    placeholderModels: 'gemini-3.1-pro-preview,gemini-3-flash-preview',
    capabilities: ['official-api', 'vision'],
    officialSources: [{ label: 'Gemini Models', url: 'https://ai.google.dev/gemini-api/docs/models' }],
  },
  {
    channelId: 'anthropic',
    label: 'Anthropic 官方',
    protocol: 'anthropic',
    baseUrl: '',
    placeholderModels: 'claude-sonnet-4-6,claude-opus-4-7',
    capabilities: ['official-api'],
    officialSources: [
      { label: 'Anthropic Models', url: 'https://docs.anthropic.com/en/docs/about-claude/models/all-models' },
    ],
  },
  {
    channelId: 'openai',
    label: 'OpenAI 官方',
    protocol: 'openai',
    baseUrl: 'https://api.openai.com/v1',
    placeholderModels: 'gpt-5.5,gpt-5.4-mini',
    capabilities: ['official-api', 'openai-compatible', 'model-discovery'],
    officialSources: [{ label: 'OpenAI Models', url: 'https://platform.openai.com/docs/models' }],
  },
  {
    channelId: 'ollama',
    label: 'Ollama（本地）',
    protocol: 'ollama',
    baseUrl: 'http://127.0.0.1:11434',
    placeholderModels: 'llama3.2,qwen2.5',
    capabilities: ['local-runtime'],
    configHint: '需要本机、Docker 或 self-hosted runner 能访问 Ollama 服务。',
    officialSources: [{ label: 'Ollama API', url: 'https://github.com/ollama/ollama/blob/main/docs/api.md' }],
  },
  {
    channelId: 'custom',
    label: '自定义渠道',
    protocol: 'openai',
    baseUrl: '',
    placeholderModels: 'model-name-1,model-name-2',
    capabilities: [],
    officialSources: [],
  },
];

export const LLM_PROVIDER_TEMPLATE_BY_ID: Record<string, LLMProviderTemplate> = Object.fromEntries(
  LLM_PROVIDER_TEMPLATES.map((template) => [template.channelId, template]),
);

export function getProviderTemplate(channelId: string): LLMProviderTemplate | undefined {
  if (!Object.prototype.hasOwnProperty.call(LLM_PROVIDER_TEMPLATE_BY_ID, channelId)) {
    return undefined;
  }
  return LLM_PROVIDER_TEMPLATE_BY_ID[channelId];
}

export function isKnownProviderTemplate(channelId: string): boolean {
  return channelId !== 'custom' && Boolean(getProviderTemplate(channelId));
}

const PROVIDER_CONFIG_HINT_OVERRIDES: Partial<Record<UiLanguage, Record<string, string>>> = {
  en: {
    anspire: 'The same ANSPIRE_API_KEYS can be reused for both search and LLM channels. The models and gateway below are configuration examples; actual availability depends on your account permissions and console. Run "Test connection" first to confirm.',
    volcengine: 'Make sure the online inference endpoint/region is not mixed up with the Coding Plan dedicated entry.',
    siliconflow: 'The model list and model visibility depend on account permissions and the API key.',
    openrouter: 'The model list and model visibility depend on account permissions and the API key.',
    ollama: 'Requires the local machine, Docker, or a self-hosted runner to reach the Ollama service.',
  },
  ko: {
    anspire: '동일한 ANSPIRE_API_KEYS를 검색과 LLM 채널에 함께 사용할 수 있습니다. 아래 모델과 게이트웨이는 설정 예시이며, 실제 사용 가능 여부는 계정 권한과 콘솔 기준입니다. 먼저 "연결 테스트"로 확인하는 것을 권장합니다.',
    volcengine: '온라인 추론 endpoint/region과 Coding Plan 전용 엔드포인트를 혼용하지 않도록 확인하세요.',
    siliconflow: '모델 목록과 모델 가시성은 계정 권한과 API Key에 따라 달라집니다.',
    openrouter: '모델 목록과 모델 가시성은 계정 권한과 API Key에 따라 달라집니다.',
    ollama: '로컬 머신, Docker 또는 self-hosted runner에서 Ollama 서비스에 접근할 수 있어야 합니다.',
  },
};

export function getProviderConfigHint(channelId: string, language: UiLanguage): string | undefined {
  const template = getProviderTemplate(channelId);
  if (!template?.configHint) return undefined;
  return PROVIDER_CONFIG_HINT_OVERRIDES[language]?.[channelId] ?? template.configHint;
}

export const MODEL_PLACEHOLDERS_BY_PROTOCOL: Record<ChannelProtocol, string> = {
  openai: 'gpt-5.5,qwen3.6-plus',
  deepseek: 'deepseek-v4-flash,deepseek-v4-pro',
  gemini: 'gemini-3.1-pro-preview,gemini-3-flash-preview',
  anthropic: 'claude-sonnet-4-6,claude-opus-4-7',
  vertex_ai: 'gemini-3.1-pro-preview',
  ollama: 'llama3.2,qwen2.5',
};
