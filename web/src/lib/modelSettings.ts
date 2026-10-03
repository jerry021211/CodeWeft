import type { ModelServiceName, ModelSettings, ModelSettingsInput } from "@/types/api";

export const modelServiceLabels: Record<ModelServiceName, string> = { chat: "对话模型", speech: "语音识别", embedding: "向量检索" };

export function modelSettingsDraft(settings: ModelSettings): ModelSettingsInput {
  const withoutKey = <T extends { has_api_key: boolean }>(service: T) => {
    const { has_api_key: _, ...fields } = service;
    return { ...fields, api_key: null };
  };
  return { revision: settings.revision, services: {
    chat: withoutKey(settings.services.chat), speech: withoutKey(settings.services.speech), embedding: withoutKey(settings.services.embedding),
  } };
}

export function modelSettingsError(draft: ModelSettingsInput): string | undefined {
  for (const name of ["chat", "speech", "embedding"] as const) {
    const service = draft.services[name];
    const enabled = name === "chat" || ("enabled" in service && service.enabled);
    if (!enabled) continue;
    if (!service.model.trim()) return `请填写${modelServiceLabels[name]}的模型名称。`;
    try {
      const url = new URL(service.base_url);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error();
    } catch { return `请填写${modelServiceLabels[name]}的有效服务地址。`; }
  }
  if (!Number.isInteger(draft.services.chat.max_tokens) || draft.services.chat.max_tokens < 1) return "输出 Token 上限必须为正整数。";
  if (draft.services.embedding.enabled && (!Number.isInteger(draft.services.embedding.dimensions) || draft.services.embedding.dimensions < 1)) return "启用向量检索时，请填写模型的实际输出维度。";
}
