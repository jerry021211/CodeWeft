import { useEffect, useRef, useState } from 'react';
import { ActivityIndicator, FlatList, KeyboardAvoidingView, NativeModules, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import * as SecureStore from 'expo-secure-store';
import { randomUUID } from 'expo-crypto';
import { openBrowserAsync } from 'expo-web-browser';
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
  async function openWorkbench(chat?: string) {
    if (!desktop || busy) return;
    setBusy(true); fail('');
    try {
      const {url} = await call<{url: string}>(desktop.key, {action:'open_workbench', chat});
      if (new URL(url).protocol !== 'https:') throw new Error('工作台需要 HTTPS 连接');
      await openBrowserAsync(url, {dismissButtonStyle:'close', toolbarColor:'#ffffff', controlsColor:'#272934'});
    } catch (e) { fail(problem(e)); } finally { setBusy(false); }
  }
  return <View style={s.screen}><View style={s.bar}>
    <Button title="‹ 此 iPhone" press={close} /><Text style={s.heading}>{desktop?.computer ?? '连接电脑'}</Text>
    {desktop && <Button title="重新配对" press={() => void forget()} />}
  </View>
    {error ? <Text style={s.error}>{error}</Text> : null}
    {desktop ? <Projects desktop={desktop} openWorkbench={openWorkbench} opening={busy} /> : <View style={s.page}>
      <Text>在电脑工作台打开「设置 → 连接手机」，生成并复制配对码。</Text>
      <TextInput style={s.input} placeholder="粘贴一次性配对码" value={code} onChangeText={setCode} secureTextEntry autoCapitalize="none" autoCorrect={false} />
      <Button title={busy ? '请稍候…' : '连接这台电脑'} disabled={busy || !code.trim()} press={() => void pair()} />
    </View>}
  </View>;
}

function Projects({desktop, openWorkbench, opening}: {desktop: Desktop; openWorkbench: (id?: string) => void; opening: boolean}) {
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
  if (chat) return <Thread key={chat.id} desktop={desktop} chat={chat} back={() => open(null)} openWorkbench={() => openWorkbench(chat.id)} opening={opening} />;
  const paths = [...new Set(items.map(i => i.project))];
  const rows = project ? items.filter(i => i.project === project) : paths.map(p => ({id:p,title:name(p),project:p}));
  return <View style={s.page}><View style={s.bar}>
    {project ? <Button title="‹ 项目" disabled={busy} press={() => {selectProject(''); createId.current = '';}} /> : null}
    <Text style={s.heading}>{project ? name(project) : '电脑上的项目'}</Text>
    <Button title="刷新" disabled={busy} press={() => reload(n => n + 1)} />
    {project ? <Button title="新对话" disabled={busy} press={() => void create()} /> : null}
  </View><Pressable accessibilityRole="button" disabled={opening} style={s.workbench} onPress={() => openWorkbench()}>
    <Text style={s.workbenchTitle}>完整工作台　↗</Text><Text style={s.workbenchHint}>执行任务、审批、工具与设置</Text>
  </Pressable>{error ? <Text style={s.error}>{error}</Text> : null}
    {busy && <ActivityIndicator />}
    <FlatList data={rows} keyExtractor={r => r.id} ListEmptyComponent={!busy ? <Text>暂无可见的项目对话</Text> : undefined}
      renderItem={({item}) => <Pressable disabled={busy} style={s.row} onPress={() => project ? open(item) : selectProject(item.project)}>
        <Text style={s.heading}>{item.title}</Text>{!project && <Text>{item.project}</Text>}
      </Pressable>} />
  </View>;
}

function Thread({desktop, chat, back, openWorkbench, opening}: {desktop: Desktop; chat: DesktopChat; back: () => void; openWorkbench: () => void; opening: boolean}) {
  const [rows, setRows] = useState<DesktopHistory['messages']>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [controls, setControls] = useState<Pick<DesktopHistory, 'control' | 'run' | 'approvals'>>({});
  const [execute, setExecute] = useState(false);
  const [draft, edit] = useState('');
  const [busy, setBusy] = useState(false);
  const [syncError, syncFail] = useState('');
  const [sendError, sendFail] = useState('');
  const attempt = useRef('');
  const sending = useRef(false);
  const list = useRef<FlatList<DesktopHistory['messages'][number]>>(null);
  const followLatest = useRef(true);
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
        setControls({control:page.control, run:page.run, approvals:page.approvals});
        if (!page.control) setExecute(false);
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
    followLatest.current = true;
    try {
      attempt.current ||= randomUUID();
      await call(desktop.key, {action:'send', project:chat.project, chat:chat.id, execute, content:draft.trim(), requestId:attempt.current});
      edit(''); attempt.current = ''; setStatus('queued');
    } catch (e) { sendFail(problem(e)); } finally { sending.current = false; setBusy(false); }
  }
  async function act(command: Record<string, string>) {
    if (sending.current) return;
    sending.current = true; setBusy(true); sendFail('');
    try {
      await call(desktop.key, {...command, project:chat.project, chat:chat.id});
      if (command.approval) setControls(old => ({...old, approvals: old.approvals?.filter(a => a.id !== command.approval)}));
    } catch (e) { sendFail(problem(e)); } finally { sending.current = false; setBusy(false); }
  }
  const labels: Record<string,string> = {queued:'排队中', running:'电脑正在处理…', failed:'执行失败，请查看电脑工作台', cancelled:'已停止', interrupted:'执行中断', waiting_approval:'等待电脑审批'};
  return <KeyboardAvoidingView behavior="padding" style={s.page}><View style={s.bar}>
    <Button title={'‹ ' + name(chat.project)} press={back} /><Text style={s.heading} numberOfLines={1}>{chat.title}</Text>
  </View><FlatList data={rows} ref={list} keyExtractor={r => r.id}
    onScroll={({nativeEvent:e}) => { followLatest.current = e.contentSize.height - e.contentOffset.y - e.layoutMeasurement.height < 96; }} scrollEventThrottle={32}
    onContentSizeChange={() => { if (followLatest.current) list.current?.scrollToEnd({animated:false}); }}
    renderItem={({item}) => <Text selectable style={[s.message, item.role === 'user' && s.user]}>{item.text}</Text>} />
    {status && labels[status] ? <Text>{labels[status]}</Text> : null}
    {syncError || sendError ? <Text style={s.error}>{sendError || syncError}</Text> : null}
    {controls.approvals?.length ? <ScrollView style={s.approvals} keyboardShouldPersistTaps="handled">{controls.approvals.map(a =>
      <View key={a.id} style={s.row}><Text style={s.heading}>请求授权 · {a.tool}</Text><Text>{a.reason}</Text>
        <Text selectable>{JSON.stringify(a.input, null, 2)}</Text><View style={s.bar}>
          {(['deny','allow'] as const).map(d => <Button key={d} title={d === 'allow' ? '允许本次' : '拒绝'} disabled={busy}
            press={() => void act({action:'decision', run:a.run, approval:a.id, decision:d})} />)}
        </View></View>)}</ScrollView> : null}
    <View style={s.mode}>{controls.control ? <Button title={execute ? '执行模式　⌄' : '只读模式　⌄'} disabled={busy}
      press={() => {attempt.current = ''; setExecute(v => !v);}} /> : <Text style={s.hint}>快速提问 · 只读</Text>}
      {controls.control && controls.run && <Button title="停止任务" disabled={busy} press={() => void act({action:'cancel', run:controls.run!})} />}
      <Button title="完整工作台　↗" disabled={opening} press={openWorkbench} /></View>
    <View style={s.bar}><TextInput multiline maxLength={20000} style={[s.input,{flex:1}]} placeholder={execute ? "让电脑完成任务…" : "询问这个项目…"} value={draft} editable={!busy}
      onChangeText={v => {attempt.current = ''; edit(v);}} />
      <Button title="发送" disabled={busy || !draft.trim()} press={() => void send()} /></View>
  </KeyboardAvoidingView>;
}

const s = StyleSheet.create({
  approvals:{maxHeight:230,flexGrow:0,backgroundColor:'#f6f4ed',borderRadius:16},
  workbench:{padding:18, borderRadius:22, backgroundColor:'#252634', gap:5}, workbenchTitle:{color:'white',fontWeight:'600',fontSize:17},
  workbenchHint:{color:'#d2d3e2',fontSize:13}, hint:{fontSize:13,color:'#60616b'},mode:{flexDirection:'row',alignItems:'center',justifyContent:'space-between'},
  screen:{ flex:1, backgroundColor:'#ffffff' }, page:{ flex:1, padding:14, gap:10 },
  bar:{flexDirection:'row', alignItems:'center', gap:6}, heading:{fontSize:17, fontWeight:'600', flexShrink:1},
  button:{paddingHorizontal:8,minHeight:44,justifyContent:'center'}, error:{color:'#963d38',padding:8},
  input:{padding:12,borderWidth:1,borderColor:'#ccd8d2',borderRadius:14,maxHeight:130},
  row:{padding:14,gap:6,borderBottomWidth:StyleSheet.hairlineWidth,borderColor:'#ced9d1'},
  message:{fontSize:16,lineHeight:24,padding:12},user:{alignSelf:'flex-end',backgroundColor:'#e3eee5',borderRadius:14,marginVertical:8},
});
