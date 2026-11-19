export type Profile = { base: string; model: string; key: string };
export type Turn = { role: 'user' | 'assistant'; content: string };

export function baseURL(value: string): string {
  let url: URL;
  try { url = new URL(value.trim()); }
  catch { throw new Error('请填写完整的 HTTPS API 地址。'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash)
    throw new Error('API 地址须使用 HTTPS，且不能包含账号、查询参数或片段。');
  return url.href.replace(/\/+$/, '').replace(/\/chat\/completions$/, '');
}

// Assemble complete byte lines before decoding. A network chunk may end in
// the middle of a Chinese character, an SSE field, or a CRLF separator.
export async function readReply(body: ReadableStream<Uint8Array>, append: (text: string) => void) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let bytes: number[] = [];
  let fields: string[] = [];
  let finished = false;
  function dispatch() {
    const data = fields.join('\n');
    fields = [];
    if (!data) return;
    if (data.trim() === '[DONE]') { finished = true; return; }
    let chunk;
    try { chunk = JSON.parse(data); }
    catch { throw new Error('模型返回了无法解析的消息。'); }
    if (chunk?.error) throw new Error('模型服务中断了回复。');
    const content = chunk?.choices?.[0]?.delta?.content;
    if (typeof content === 'string' && content) append(content);
  }
  function line() {
    const value = decoder.decode(Uint8Array.from(bytes)).replace(/\r$/, '');
    bytes = [];
    if (!value) dispatch();
    else if (value.startsWith('data:')) fields.push(value.slice(5).replace(/^ /, ''));
  }
  try {
    while (!finished) {
      const { value, done } = await reader.read();
      if (done) {
        if (bytes.length) line();
        dispatch();
        if (!finished) throw new Error('连接提前结束，已保留收到的文字。');
        break;
      }
      for (const byte of value) {
        if (byte === 10) line();
        else bytes.push(byte);
        if (finished) break;
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

type Fetcher = (url: string, init: RequestInit) => Promise<Pick<Response, 'ok' | 'status' | 'body' | 'headers'>>;

export async function streamChat(fetcher: Fetcher, profile: Profile, messages: Turn[],
                                 signal: AbortSignal, append: (text: string) => void) {
  const response = await fetcher(baseURL(profile.base) + '/chat/completions', {
    method: 'POST', signal, redirect: 'error',
    headers: { Authorization: 'Bearer ' + profile.key, 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ model: profile.model, messages, stream: true }),
  });
  if (!response.ok) {
    const hint = response.status === 401 || response.status === 403 ? '请检查 API Key 和模型权限。'
      : response.status === 429 ? '请检查服务额度或稍后重试。' : '请核对服务地址和模型名称。';
    throw new Error('请求失败（HTTP ' + response.status + '）。' + hint);
  }
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream'))
    throw new Error('服务未返回 Chat Completions 流式回复，请核对接口。');
  await readReply(response.body, append);
}
