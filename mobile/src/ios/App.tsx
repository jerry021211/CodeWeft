import { useEffect, useRef, useState } from 'react';
import { Computer } from './Computer';
import { ActivityIndicator, AppState, FlatList, KeyboardAvoidingView, Pressable, StatusBar, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';
import { openDatabaseSync } from 'expo-sqlite';
import { fetch as nativeFetch } from 'expo/fetch';
import { History, type Message, type Session } from '../shared/history';
import { loadProfile, Settings } from './Settings';
import { streamChat, type Profile } from '../shared/provider';

export default function App() {
  return <SafeAreaProvider><SafeAreaView style={s.screen}><StatusBar barStyle="dark-content" /><Chat /></SafeAreaView></SafeAreaProvider>;
}

function Chat() {
  const store = useRef<History | null>(null);
  const active = useRef<AbortController | null>(null);
  const list = useRef<FlatList<Message>>(null);
  const [page, setPage] = useState<'chat' | 'history' | 'settings' | 'computer'>('chat');
  const [ready, setReady] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    (async () => {
      const saved = await loadProfile();
      if (!live) return;
      const history = new History(openDatabaseSync('codeweft-chat.db'));
      store.current = history;
      const current = history.current();
      setProfile(saved); setSession(current); setMessages(current ? history.messages(current.id) : []);
      setReady(true); setError('');
    })().catch(() => { if (live) setError('暂时无法打开手机存储，请解锁设备后重试。'); });
    return () => { live = false; active.current?.abort(); };
  }, [attempt]);

  function open(chat: Session | null) {
    if (active.current) return;
    const history = store.current!;
    if (chat) history.select(chat.id); else history.clearSelection();
    setSession(chat); setMessages(chat ? history.messages(chat.id) : []);
    setDraft(''); setError(''); setPage('chat');
  }
  async function send() {
    const question = draft.trim();
    if (!question || active.current || !store.current) return;
    if (!profile) { setPage('settings'); return; }
    const controller = new AbortController();
    const history = store.current;
    active.current = controller;
    setBusy(true); setError('');
    let id: number | undefined;
    let answer = '';
    let savedText = '';
    let tick: ReturnType<typeof setTimeout> | undefined;
    function flush() {
      if (tick) clearTimeout(tick);
      tick = undefined;
      if (id === undefined || savedText === answer) return;
      history.save(id, answer, 'streaming');
      savedText = answer;
      const snapshot = answer;
      setMessages(rows => rows.map(row => row.id === id ? { ...row, content: snapshot } : row));
    }
    function flushSafely() {
      try { flush(); } catch { setError('无法保存回复，请检查手机存储空间。'); controller.abort(); }
    }
    const listener = AppState.addEventListener('change', state => { if (state !== 'active') flushSafely(); });
    let state: Message['state'] = 'done';
    let note = '';
    let chat = session;
    try {
      const turn = history.begin(session, profile.base, profile.model, question);
      id = turn.reply; chat = turn.chat;
      const rows = history.messages(chat.id);
      setSession(chat); setMessages(rows); setDraft('');
      await streamChat(nativeFetch, profile, rows.filter(row => row.content).map(({ role, content }) => ({ role, content })),
        controller.signal, text => {
          if (controller.signal.aborted) return;
          answer += text;
          if (!tick) tick = setTimeout(flushSafely, 100);
        });
      if (!answer.trim()) throw new Error('模型没有返回文字。');
    } catch (cause) {
      state = 'failed';
      note = cause instanceof Error ? cause.message : '连接失败，请检查网络和模型配置。';
    } finally {
      if (tick) clearTimeout(tick);
      listener.remove();
      if (controller.signal.aborted) { state = 'stopped'; note = '已停止生成'; }
      try {
        if (id !== undefined && chat) {
          history.save(id, answer, state, note);
          setMessages(history.messages(chat.id));
        } else if (note) setError(note);
      } catch { setError('无法保存本轮回复，请检查手机存储空间。'); }
      active.current = null; setBusy(false);
    }
  }
  if (!ready) return <View style={s.center}>{error ? <><Text>{error}</Text><Button label="重试" onPress={() => setAttempt(n => n + 1)} /></> : <ActivityIndicator />}</View>;
  if (page === 'settings') return <Settings profile={profile} onClose={() => setPage('chat')} onSaved={next => {
    setProfile(next); setPage('chat');
    if (next && session && (session.base !== next.base || session.model !== next.model)) open(null);
  }} />;
  if (page === 'computer') return <Computer close={() => setPage('chat')} />;
  if (page === 'history') return <View style={s.screen}>
    <View style={s.bar}><Button label="‹ 返回" onPress={() => setPage('chat')} /><Text style={s.heading}>最近对话</Text><Button label="新建" onPress={() => open(null)} /></View>
    <FlatList data={store.current!.sessions()} keyExtractor={row => String(row.id)} ListEmptyComponent={<Text style={s.empty}>第一条消息发出后，会话会出现在这里。</Text>} renderItem={({ item }) =>
      <Pressable onPress={() => open(item)} style={s.history}><Text style={s.heading} numberOfLines={2}>{item.title}</Text><Text style={s.muted}>此 iPhone · {item.model}</Text></Pressable>} />
  </View>;
  const mismatch = !!session && !!profile && (session.base !== profile.base || session.model !== profile.model);
  return <KeyboardAvoidingView style={s.screen} behavior="padding">
    <View style={s.bar}><Button label="历史" disabled={busy} onPress={() => setPage('history')} /><View style={s.barTitle}>
      <Text style={s.heading} numberOfLines={1}>{session?.title ?? '新对话'}</Text><Pressable accessibilityRole="button" accessibilityLabel="切换到电脑项目" disabled={busy} hitSlop={5} onPress={() => setPage('computer')}><Text style={s.caption} numberOfLines={1}>此 iPhone · 切换电脑 ›</Text></Pressable>
    </View><Button label="设置" disabled={busy} onPress={() => setPage('settings')} /><Button label="＋" hint="新建对话" disabled={busy} onPress={() => open(null)} /></View>
    {messages.length === 0 ? <View style={s.center}><Text style={s.hero}>想聊点什么？</Text>
      {profile ? null : <Button label="配置模型" onPress={() => setPage('settings')} />}</View> :
      <FlatList ref={list} data={messages} keyExtractor={row => String(row.id)} keyboardShouldPersistTaps="handled" contentContainerStyle={s.timeline}
        onContentSizeChange={() => list.current?.scrollToEnd({ animated: false })} renderItem={({ item }) => <View style={item.role === 'user' ? s.question : s.answer}>
          <Text selectable style={s.body}>{item.content || (item.state === 'streaming' ? '正在回复…' : '')}</Text>
          {item.note ? <Text style={s.muted}>{item.note}</Text> : null}</View>} />}
    {error ? <Text style={s.error}>{error}</Text> : null}
    {mismatch ? <Button label="使用当前模型新建对话" onPress={() => open(null)} /> :
      <View style={s.composer}><TextInput multiline style={s.input} maxLength={64000} placeholder="输入消息…" accessibilityLabel="消息" value={draft} onChangeText={setDraft} />
        <Pressable accessibilityRole="button" accessibilityLabel={busy ? '停止生成' : '发送'} disabled={!busy && !draft.trim()} onPress={() => busy ? active.current?.abort() : void send()}
          style={[s.send, !busy && !draft.trim() && { opacity: .45 }]}><Text style={s.sendText}>{busy ? '■' : '↑'}</Text></Pressable></View>}
  </KeyboardAvoidingView>;
}

function Button({label, hint, disabled, onPress}: {label: string; hint?: string; disabled?: boolean; onPress: () => void}) {
  return <Pressable accessibilityRole="button" accessibilityLabel={hint ?? label} onPress={onPress} disabled={disabled} style={[s.button, disabled && {opacity:.4}]}><Text style={s.link}>{label}</Text></Pressable>;
}
const s = StyleSheet.create({
  screen: { flex: 1, backgroundColor: '#fafbf8' }, center: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 12 },
  bar: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 8, minHeight: 58, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: '#d9dfd9' },
  barTitle: { flex: 1, alignItems: 'center' }, heading: { fontSize: 17, fontWeight: '600', color: '#24362f' },
  caption: { color: '#61716a', fontSize: 12, marginTop: 2 }, muted: { color: '#61716a', marginTop: 6, fontSize: 13 },
  button: { paddingHorizontal: 10, minHeight: 44, justifyContent: 'center' }, link: { color: '#24594d', fontSize: 16 },
  empty: { padding: 24, color: '#61716a' }, history: { paddingHorizontal: 22, paddingVertical: 16, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: '#d9dfd9' },
  hero: { color: '#213e34', fontSize: 32, fontWeight: '600' }, timeline: { padding: 18, gap: 18 },
  question: { alignSelf: 'flex-end', maxWidth: '87%', borderRadius: 20, paddingHorizontal: 16, paddingVertical: 10, backgroundColor: '#e3eee5' },
  answer: { alignSelf: 'stretch', paddingVertical: 4 }, body: { fontSize: 17, lineHeight: 26, color: '#1d2b25' },
  composer: { flexDirection: 'row', alignItems: 'flex-end', margin: 12, padding: 7, borderWidth: 1, borderColor: '#c9d4ce', borderRadius: 24, backgroundColor: '#fff' },
  input: { flex: 1, fontSize: 17, padding: 10, maxHeight: 140, color: '#1d2b25' }, send: { backgroundColor: '#24594d', borderRadius: 20, width: 40, height: 40, alignItems: 'center', justifyContent: 'center' },
  sendText: { color: 'white', fontSize: 22 }, error: { color: '#963d38', paddingHorizontal: 18 },
});
