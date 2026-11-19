import { useEffect, useRef, useState } from 'react';
import { ActivityIndicator, FlatList, KeyboardAvoidingView, NativeModules, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import * as SecureStore from 'expo-secure-store';
import { randomUUID } from 'expo-crypto';
import { exchange, type Desktop, type DesktopChat, type DesktopHistory } from '../shared/desktop';

const slot = 'codeweft.mobile.computer.v1';
const problem = (cause: unknown) => cause instanceof Error ? cause.message : '连接失败，请检查电脑';
const name = (path: string) => path.split(/[\\/]/).filter(Boolean).pop() || path;

function call<T>(key: string, data: Record<string, unknown>, pairing = false) {
  const source = NativeModules.SourceCode;
  const url = new URL(source?.getConstants?.().scriptURL ?? source?.scriptURL);
  return exchange<T>(url.origin + '/codeweft/mobile-link', key, data, pairing);
}
function Button({ title, press, disabled }: {title: string; press: () => void; disabled?: boolean}) {
  return <Pressable accessibilityRole="button" disabled={disabled} onPress={press} style={s.button}><Text style={{color: disabled ? '#888' : '#24594d'}}>{title}</Text></Pressable>;
}

export function Computer({ close }: {close: () => void}) {
  const [desktop, setDesktop] = useState<Desktop | null>(null);
  const [busy, setBusy] = useState(true);
  const [code, setCode] = useState('');
  const [error, fail] = useState('');
  useEffect(() => {
    SecureStore.getItemAsync(slot).then(v => { if (v) setDesktop(JSON.parse(v)); })
      .catch(e => fail(problem(e))).finally(() => setBusy(false));
  }, []);
  async function pair() {
    setBusy(true); fail('');
    try {
      const device = await call<Desktop>(code.trim(), {}, true);
      await SecureStore.setItemAsync(slot, JSON.stringify(device), {keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY});
      setDesktop(device); setCode('');
    } catch (e) { fail(problem(e)); } finally { setBusy(false); }
  }
  async function forget() {
    try { await SecureStore.deleteItemAsync(slot); setDesktop(null); fail(''); }
    catch (e) { fail(problem(e)); }
  }
  return <View style={s.screen}><View style={s.bar}>
    <Button title="‹ 此 iPhone" press={close} /><Text style={s.heading}>{desktop?.computer ?? '连接电脑'}</Text>
    {desktop && <Button title="重新配对" press={() => void forget()} />}
  </View>
    {error ? <Text style={s.error}>{error}</Text> : null}
    {desktop ? <Projects desktop={desktop} /> : <View style={s.page}>
      <Text>在电脑工作台打开「设置 → 连接手机」，生成并复制配对码。</Text>
      <TextInput style={s.input} placeholder="粘贴一次性配对码" value={code} onChangeText={setCode} secureTextEntry autoCapitalize="none" autoCorrect={false} />
      <Button title={busy ? '请稍候…' : '连接这台电脑'} disabled={busy || !code.trim()} press={() => void pair()} />
    </View>}
  </View>;
}

function Projects({desktop}: {desktop: Desktop}) {
  const [items, setItems] = useState<DesktopChat[]>([]);
  const [project, selectProject] = useState('');
  const [chat, open] = useState<DesktopChat | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, fail] = useState('');
  const [refresh, reload] = useState(0);
  const createId = useRef('');
  useEffect(() => {
    if (chat) return;
    let live = true;
    setBusy(true); fail('');
    call<DesktopChat[]>(desktop.key, {action: 'catalog'}).then(rows => { if (live) setItems(rows); })
      .catch(e => { if (live) fail(problem(e)); }).finally(() => { if (live) setBusy(false); });
    return () => { live = false; };
  }, [desktop, refresh, chat]);
  async function create() {
    setBusy(true); fail('');
    try {
      createId.current ||= randomUUID();
      const item = await call<DesktopChat>(desktop.key, {action: 'create', project, requestId: createId.current});
      createId.current = ''; open(item);
    } catch (e) { fail(problem(e)); } finally { setBusy(false); }
  }
  if (chat) return <Thread key={chat.id} desktop={desktop} chat={chat} back={() => open(null)} />;
  const paths = [...new Set(items.map(i => i.project))];
  const rows = project ? items.filter(i => i.project === project) : paths.map(p => ({id:p,title:name(p),project:p}));
  return <View style={s.page}><View style={s.bar}>
    {project ? <Button title="‹ 项目" disabled={busy} press={() => {selectProject(''); createId.current = '';}} /> : null}
    <Text style={s.heading}>{project ? name(project) : '电脑上的项目'}</Text>
    <Button title="刷新" disabled={busy} press={() => reload(n => n + 1)} />
    {project ? <Button title="新对话" disabled={busy} press={() => void create()} /> : null}
  </View>{error ? <Text style={s.error}>{error}</Text> : null}
    {busy && <ActivityIndicator />}
    <FlatList data={rows} keyExtractor={r => r.id} ListEmptyComponent={!busy ? <Text>暂无可见的项目对话</Text> : undefined}
      renderItem={({item}) => <Pressable disabled={busy} style={s.row} onPress={() => project ? open(item) : selectProject(item.project)}>
        <Text style={s.heading}>{item.title}</Text>{!project && <Text>{item.project}</Text>}
      </Pressable>} />
  </View>;
}

function Thread({desktop, chat, back}: {desktop: Desktop; chat: DesktopChat; back: () => void}) {
  const [rows, setRows] = useState<DesktopHistory['messages']>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [draft, edit] = useState('');
  const [busy, setBusy] = useState(false);
  const [syncError, syncFail] = useState('');
  const [sendError, sendFail] = useState('');
  const attempt = useRef('');
  const sending = useRef(false);
  const list = useRef<FlatList<DesktopHistory['messages'][number]>>(null);
  useEffect(() => {
    let live = true;
    let offset = 0;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      let delay = 2500;
      try {
        const page = await call<DesktopHistory>(desktop.key, {action:'history', project:chat.project, chat:chat.id, offset});
        if (!live) return;
        offset = page.next;
        if (page.messages.length) setRows(old => [...old, ...page.messages]);
        setStatus(page.status); syncFail('');
        if (page.more) delay = 0;
      } catch (e) { if (live) syncFail(problem(e)); delay = 5000; }
      if (live) timer = setTimeout(poll, delay);
    }
    void poll();
    return () => { live = false; clearTimeout(timer); };
  }, [desktop, chat]);
  async function send() {
    if (sending.current || !draft.trim()) return;
    sending.current = true; setBusy(true); sendFail('');
    try {
      attempt.current ||= randomUUID();
      await call(desktop.key, {action:'send', project:chat.project, chat:chat.id, content:draft.trim(), requestId:attempt.current});
      edit(''); attempt.current = ''; setStatus('queued');
    } catch (e) { sendFail(problem(e)); } finally { sending.current = false; setBusy(false); }
  }
  const labels: Record<string,string> = {queued:'排队中', running:'电脑正在处理…', failed:'执行失败，请查看电脑工作台', cancelled:'已停止', interrupted:'执行中断', waiting_approval:'等待电脑审批'};
  return <KeyboardAvoidingView behavior="padding" style={s.page}><View style={s.bar}>
    <Button title={'‹ ' + name(chat.project)} press={back} /><Text style={s.heading} numberOfLines={1}>{chat.title}</Text>
  </View><FlatList data={rows} ref={list} keyExtractor={r => r.id} onContentSizeChange={() => list.current?.scrollToEnd({animated:false})}
    renderItem={({item}) => <Text selectable style={[s.message, item.role === 'user' && s.user]}>{item.text}</Text>} />
    {status && labels[status] ? <Text>{labels[status]}</Text> : null}
    {syncError || sendError ? <Text style={s.error}>{sendError || syncError}</Text> : null}
    <View style={s.bar}><TextInput multiline maxLength={20000} style={[s.input,{flex:1}]} placeholder="询问这个项目（只读）" value={draft} editable={!busy}
      onChangeText={v => {attempt.current = ''; edit(v);}} />
      <Button title="发送" disabled={busy || !draft.trim()} press={() => void send()} /></View>
  </KeyboardAvoidingView>;
}

const s = StyleSheet.create({
  screen:{ flex:1, backgroundColor:'#fafbf8' }, page:{ flex:1, padding:12, gap:12 },
  bar:{flexDirection:'row', alignItems:'center', gap:6}, heading:{fontSize:17, fontWeight:'600', flexShrink:1},
  button:{paddingHorizontal:8,minHeight:44,justifyContent:'center'}, error:{color:'#963d38',padding:8},
  input:{padding:12,borderWidth:1,borderColor:'#ccd8d2',borderRadius:14,maxHeight:130},
  row:{padding:14,gap:6,borderBottomWidth:StyleSheet.hairlineWidth,borderColor:'#ced9d1'},
  message:{fontSize:16,lineHeight:24,padding:12},user:{alignSelf:'flex-end',backgroundColor:'#e3eee5',borderRadius:14,marginVertical:8},
});
