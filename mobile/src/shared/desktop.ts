import nacl from 'tweetnacl';
import { fromByteArray, toByteArray } from 'base64-js';
import { getRandomBytesAsync } from 'expo-crypto';

export type Desktop = { computer: string; key: string };
export type DesktopChat = { id: string; project: string; title: string };
export type DesktopHistory = {
  messages: { id: string; role: 'user' | 'assistant'; text: string }[];
  status: string | null;
  next: number;
  more: boolean;
};

export async function exchange<T>(endpoint: string, secret: string, command: Record<string, unknown>, pairing = false): Promise<T> {
  if (!/^[A-Za-z0-9+/]{43}=$/.test(secret)) throw new Error('请粘贴电脑显示的完整配对码');
  const key = toByteArray(secret);
  const nonce = await getRandomBytesAsync(nacl.secretbox.nonceLength);
  const plain = new TextEncoder().encode(JSON.stringify({ ...command, direction: 'request', time: Math.floor(Date.now() / 1000) }));
  const cipher = nacl.secretbox(plain, nonce, key);
  const wire = new Uint8Array(nonce.length + cipher.length);
  wire.set(nonce);
  wire.set(cipher, nonce.length);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 22000);
  try {
    const res = await fetch(endpoint, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
      body: JSON.stringify({ pairing, box: fromByteArray(wire) }),
    });
    if (res.status === 401) throw new Error('配对已失效，请重新配对');
    if (res.status === 400) throw new Error('请检查电脑和手机的系统时间');
    if (!res.ok) throw new Error('连接失败，请检查电脑工作台和 Expo 是否已启动');
    const reply = toByteArray((await res.json()).box);
    const size = nacl.secretbox.nonceLength;
    const decoded = nacl.secretbox.open(reply.subarray(size), reply.subarray(0, size), key);
    if (!decoded) throw new Error('无法验证电脑回复');
    const body = JSON.parse(new TextDecoder().decode(decoded));
    // 同一设备的多个请求共用密钥，校验回传的 nonce 才能确认这条回复属于当前请求。
    if (body.request !== fromByteArray(nonce)) throw new Error('电脑回复与当前请求不匹配');
    if (body.error) throw new Error(body.error);
    return body.result;
  } finally {
    clearTimeout(timer);
  }
}
