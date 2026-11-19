import { useState } from 'react';
import { KeyboardAvoidingView, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import * as SecureStore from 'expo-secure-store';
import { baseURL, type Profile } from '../shared/provider';

const slot = 'codeweft.mobile.provider.v1';

export async function loadProfile(): Promise<Profile | null> {
  const value = await SecureStore.getItemAsync(slot);
  return value ? JSON.parse(value) : null;
}

export function Settings({ profile, onClose, onSaved }: {
  profile: Profile | null; onClose: () => void; onSaved: (value: Profile | null) => void;
}) {
  const [base, setBase] = useState(profile?.base ?? '');
  const [model, setModel] = useState(profile?.model ?? '');
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function save(remove = false) {
    setBusy(true);
    try {
      if (remove) {
        await SecureStore.deleteItemAsync(slot);
        onSaved(null);
        return;
      }
      const endpoint = baseURL(base);
      const token = key.trim() || (profile?.base === endpoint ? profile.key : '');
      if (!model.trim()) throw new Error('请填写模型 ID。');
      if (!token) throw new Error('请填写这个服务的 API Key。');
      const next = { base: endpoint, model: model.trim(), key: token };
      await SecureStore.setItemAsync(slot, JSON.stringify(next), { keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY });
      onSaved(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法保存配置。');
    } finally { setBusy(false); }
  }
  return <KeyboardAvoidingView behavior="padding" style={{ flex: 1 }}>
    <ScrollView contentContainerStyle={s.page} keyboardShouldPersistTaps="handled">
      <Pressable disabled={busy} onPress={onClose}><Text style={s.link}>‹ 返回聊天</Text></Pressable>
      <Text style={s.title}>模型设置</Text>
      <Text style={s.description}>使用支持流式 Chat Completions 的服务。会话内容将直接发往下方地址。</Text>
      <View><Text style={s.label}>服务地址</Text><TextInput accessibilityLabel="服务地址" style={s.input} value={base} onChangeText={setBase} placeholder="https://你的服务域名/v1" keyboardType="url" autoCapitalize="none" autoCorrect={false} editable={!busy} /></View>
      <View><Text style={s.label}>模型 ID</Text><TextInput accessibilityLabel="模型 ID" style={s.input} value={model} onChangeText={setModel} placeholder="服务商提供的模型名称" autoCapitalize="none" autoCorrect={false} editable={!busy} /></View>
      <View><Text style={s.label}>API Key</Text><TextInput accessibilityLabel="API Key" style={s.input} value={key} onChangeText={setKey} placeholder={profile ? '同一地址留空可保留原 Key' : '输入个人 API Key'} secureTextEntry autoCapitalize="none" autoCorrect={false} editable={!busy} /></View>
      {error ? <Text accessibilityRole="alert" style={s.error}>{error}</Text> : null}
      <Pressable disabled={busy} onPress={() => void save()} style={s.primary}><Text style={s.primaryText}>{busy ? '保存中…' : '保存配置'}</Text></Pressable>
      {profile && <Pressable disabled={busy} onPress={() => void save(true)}><Text style={s.link}>移除此模型配置和密钥</Text></Pressable>}
    </ScrollView>
  </KeyboardAvoidingView>;
}

const s = StyleSheet.create({
  page: { padding: 24, gap: 20 }, link: { color: '#245a51', fontSize: 16, paddingVertical: 8 },
  title: { fontWeight: '700', fontSize: 30, color: '#202b29' }, description: { fontSize: 15, lineHeight: 23, color: '#536460' },
  label: { fontSize: 14, marginBottom: 8, color: '#35443f' },
  input: { borderWidth: 1, borderColor: '#cbd7d1', borderRadius: 14, padding: 14, backgroundColor: '#fff', color: '#192620' },
  primary: { backgroundColor: '#22564d', padding: 15, borderRadius: 16, alignItems: 'center' },
  primaryText: { fontWeight: '600', color: 'white' }, error: { color: '#993d36' },
});
