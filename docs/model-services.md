# 多接口与按能力组合模型

模型按能力组合，主 Agent、子 Agent、记忆和摘要默认共用同一个对话模型。
语音识别和向量检索是两个独立服务，不从对话配置继承密钥或地址。
未使用设置页时，旧的 `SUMMARIZATION_MODEL_ID` / `SUMMARIZATION_API_KEY` 覆盖仍兼容；留空即可共用对话模型。

## 在网页中配置

从左侧会话栏底部进入 **模型设置**（窄屏先打开会话列表），或访问 `/#settings/models`。
Web 首次启动可以不设置 `MODEL_ID`，在页面完成配置后再执行任务。

- **对话模型**：选择 Anthropic Messages、OpenAI Chat Completions 或 OpenAI Responses，填写服务地址、密钥和模型名称。高级配置可声明图片、PDF、原生音频等输入能力，以及推理等级。
- **语音识别**：独立启用并设置转写模型、地址、密钥、语言和超时。
- **向量检索**：独立启用并设置向量模型、地址、密钥、输出维度和批量大小。

可以手动填写模型 ID，也可以点击 **获取模型列表** 从服务的 `/models` 接口选择。不支持模型列表的服务仍可手动配置。

**测试连接** 使用当前表单中的地址、密钥和模型发起一次真实调用，无需先保存，也不会修改保存的配置。测试显示成功/失败、耗时以及常见认证、权限、模型不存在或超时原因，不回显服务返回的敏感内容。
对话测试发送简短消息，最多输出 256 Token；向量测试生成一条向量并核对配置维度；语音测试上传内置的 1 秒静音 WAV 并检查转写响应。
测试可能产生少量费用，最长等待约 30 秒、不自动重试。基础连接成功不代表已验证工具调用、多模态能力或真实语音识别质量。

**保存并应用** 对下一次任务立即生效。运行中、排队中或未结束的团队任务会阻止保存；旧页面保存过期版本也会被拒绝，避免相互覆盖。
保存后主 Agent、子 Agent、摘要和记忆统一使用对话模型，并清除旧的独立摘要模型、摘要密钥与备用模型覆盖。

配置保存在 `CODEAGENT_DATA_DIR/settings/models.json`（未指定时使用系统 CodeAgent 数据目录），重启后自动恢复，优先于对应环境变量。
密钥存于该本地文件，未额外加密；API 只返回是否已配置，页面不回显密钥。留空保留已有密钥，**清除** 按钮明确移除密钥；更换服务地址时需重新填写或清除，防止误用旧服务的凭据。
首次保存前继续使用 `.env`；移走 `models.json` 并重启可恢复环境变量配置。

## 对话协议

| MODEL_PROTOCOL | BASE_URL 示例 | 请求路径 | 输入 |
| --- | --- | --- | --- |
| anthropic（默认） | `https://api.anthropic.com` | SDK Messages API | 文字、图片、PDF |
| openai_chat | `https://api.openai.com/v1` | `/chat/completions` | 文字、图片、PDF、WAV/MP3 原生音频 |
| openai_responses | `https://api.openai.com/v1` | `/responses` | 文字、图片、文件（当前附件入口接收 PDF） |

这是适配器的协议能力，具体模型仍须支持相应输入及工具调用。纯文本模型可配置
`MODEL_INPUT_MODALITIES=text`，提前拒绝不能处理的图片或文档。省略时不猜测模型能力，
由协议约束和服务端校验决定。UTF-8 文本、JSON、CSV、Markdown 附件会解码为文字，所有协议可用。

OpenAI-compatible 的 `BASE_URL` 包含服务商 API 前缀，代码只追加请求路径。
例如 Gemini 官方兼容入口可配置为 `https://generativelanguage.googleapis.com/v1beta/openai`，
协议使用 `openai_chat`；这不包含 Gemini 原生 File API、视频或服务商自有工具。

`API_KEY` / `BASE_URL` 优先；Anthropic 另支持 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`，
OpenAI 两种协议另支持 `OPENAI_API_KEY` / `OPENAI_BASE_URL`。不同协议不会误用另一组后备变量。
默认保留旧 Anthropic 行为。Python 推荐 `env.create_model_client()`；旧工厂名称仍兼容。

所有适配器支持流式文本、工具调用、取消检查、超时、错误恢复与用量事件。
Responses 使用 `store=false`，保存并回传必要的加密推理项；Chat 保存服务商返回的
`reasoning_content` 和 Gemini 工具签名供后续工具轮次使用，不把这些内容显示为回答。
缓存读取量从总输入量拆分统计，不重复计数。未返回用量时显示不可用。

OpenAI 协议默认不发送推理等级。明确知道模型支持的档位后，可配置
`MODEL_REASONING_LEVELS=none,low,medium,high`；这声明当前模型能力，不代表每个模型均支持。
`REASONING_EFFORT` 和 Web 逐次选择沿用现有机制。Chat 默认发送 `max_completion_tokens`；
旧兼容服务可配置 `OPENAI_CHAT_TOKEN_PARAMETER=max_tokens`。Responses 发送 `max_output_tokens`。

## 三种能力组合示例

以下模型 ID 为占位符，替换为各服务实际支持的型号：

```dotenv
# 对话模型：所有 Agent 角色默认共用
MODEL_PROTOCOL=openai_chat
MODEL_ID=your-chat-model
API_KEY=your-chat-key
BASE_URL=https://chat.example.com/v1
STREAMING=true
SUMMARIZATION_MODEL_ID=
SUMMARIZATION_API_KEY=

# 语音识别：接收音频，返回转写文字
CODEAGENT_SPEECH_ENABLED=true
CODEAGENT_SPEECH_MODEL=your-speech-to-text-model
CODEAGENT_SPEECH_BASE_URL=https://speech.example.com/v1
CODEAGENT_SPEECH_API_KEY=your-speech-key
CODEAGENT_SPEECH_LANGUAGE=zh
CODEAGENT_SPEECH_TIMEOUT=120

# 向量模型：复用已有代码检索 embedding 管线
CODEAGENT_EMBEDDING_ENABLED=true
CODEAGENT_EMBEDDING_MODEL=your-embedding-model
CODEAGENT_EMBEDDING_BASE_URL=https://embedding.example.com/v1
CODEAGENT_EMBEDDING_API_KEY=your-embedding-key
CODEAGENT_EMBEDDING_DIMENSIONS=1024
```

向量维度必须与服务实际返回值一致。现有 embedding 管线调用 `/embeddings`，仅用于代码检索，
不会自动将任意附件创建成知识库。语音模型调用 `/audio/transcriptions`，使用 multipart 文件上传。
配置启用后，WAV/MP3 附件先由语音模型转写，再把转写文字与其他附件交给对话模型。
因此纯文本对话模型也能处理音频内容；转写不会保留声纹、语调等音频信息。
关闭语音识别时，仅支持音频输入的 Chat Completions 模型可直接接收原始音频。
转写失败或取消会停止本次任务，不会把音频 base64 当作提示词，也不会自动切换供应商。
转写有独立开始/完成/失败事件，不伪造对话 token 计费数据。

## 输入入口与限制

Web 普通会话使用“添加附件”，可不输入文字直接提交附件。附件草稿按会话隔离，发送失败保留。
已发送消息显示附件名，原生媒体保存在运行 checkpoint 的消息块中；音频转写后保存的是转写文字。
队列中的附件在运行前保留于内存；服务重启后按既有规则中断任务，需要重新上传。
本版不提供附件下载、录音、视频处理、TTS、Team 附件传递或通用文档向量库。

```powershell
codeagent --attach screenshot.png --attach report.pdf "检查图表与报告是否一致"
codeagent --attach meeting.wav "根据录音整理待办"
```

API：`POST /api/conversations/{id}/runs`：

```json
{
  "content": "分析这些输入",
  "attachments": [
    {"name": "chart.png", "media_type": "image/png", "data": "BASE64_BYTES"}
  ]
}
```

最多 8 个附件，单个不超过 10 MiB，总计不超过 20 MiB；`data` 为纯 base64，不带 data URL 前缀。
服务端校验类型、base64、大小和 UTF-8 编码。模型服务自身可能还有更低的大小、图片数量或 PDF 页数限制。
图片支持 PNG/JPEG/GIF/WebP。接口不接受本机路径和任意 URL 作为上传附件。

上下文预算继续记录完整 `request_chars`；`media_payload_chars` 单独记录二进制编码字符数。
文本字符预算扣除 base64 媒体数据；token 估计按每个媒体块额外预留 8192，不将 base64 按文字计数。
这只是估计，不保证任意多页 PDF 都能装进模型窗口；长文件仍可能需要拆分。

协议参考：[OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、
[Responses](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)、
[语音转写](https://developers.openai.com/api/docs/guides/speech-to-text)、
[Gemini OpenAI 兼容层](https://ai.google.dev/gemini-api/docs/openai)、
[Claude PDF](https://docs.claude.com/en/docs/build-with-claude/pdf-support)。
