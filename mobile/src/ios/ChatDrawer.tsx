import { useState } from 'react';
import { FlatList, Modal, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import type { Session } from '../shared/history';

type Props = {
  sessions: Session[]; selected?: number; close: () => void;
  select: (item: Session | null) => void; computer: () => void; settings: () => void;
};

export function ChatDrawer({sessions, selected, close, select, computer, settings}: Props) {
  const [search, filter] = useState('');
  const rows = sessions.filter(item => item.title.toLowerCase().includes(search.trim().toLowerCase()));
  return <Modal transparent animationType="fade" onRequestClose={close} statusBarTranslucent>
    <View style={s.overlay}>
      <Pressable accessibilityRole="button" accessibilityLabel="关闭菜单" onPress={close} style={StyleSheet.absoluteFill} />
      <SafeAreaView style={s.sheet} accessibilityViewIsModal>
        <View style={s.head}><Text style={s.brand}>CodeWeft</Text><Pressable accessibilityLabel="关闭菜单" onPress={close} style={s.close}><Text>✕</Text></Pressable></View>
        <TextInput accessibilityLabel="搜索本机对话" value={search} onChangeText={filter} placeholder="搜索对话" style={s.search} clearButtonMode="while-editing" />
        <Pressable accessibilityRole="button" onPress={() => select(null)} style={s.primary}><Text style={s.primaryText}>＋　新对话</Text></Pressable>
        <Pressable accessibilityRole="button" onPress={computer} style={s.line}><Text style={s.label}>电脑项目与工作台　›</Text></Pressable>
        <Text style={s.caption}>此 iPhone 的对话</Text>
        <FlatList data={rows} keyboardShouldPersistTaps="handled" keyExtractor={row => String(row.id)}
          ListEmptyComponent={<Text style={s.caption}>{search ? '没有匹配的对话' : '暂无历史对话'}</Text>}
          renderItem={({item}) => <Pressable accessibilityRole="button" style={[s.line, item.id === selected && s.active]} onPress={() => select(item)}>
            <Text numberOfLines={1} style={s.label}>{item.title}</Text></Pressable>} />
        <Pressable accessibilityRole="button" style={s.line} onPress={settings}><Text style={s.label}>模型设置　›</Text></Pressable>
      </SafeAreaView>
    </View>
  </Modal>;
}
const s = StyleSheet.create({
  overlay:{flex:1,backgroundColor:'#11132355'},sheet:{flex:1,width:'84%',maxWidth:365,backgroundColor:'#f8f8fb',paddingHorizontal:16},
  head:{flexDirection:'row',alignItems:'center',justifyContent:'space-between',marginVertical:10},brand:{fontSize:23,fontWeight:'700',color:'#242430'},
  close:{width:44,height:44,alignItems:'center',justifyContent:'center'},search:{backgroundColor:'white',borderRadius:18,paddingHorizontal:16,height:46,fontSize:16},
  primary:{backgroundColor:'#262635',borderRadius:18,padding:14,marginVertical:14},primaryText:{color:'white',fontWeight:'600',fontSize:16},
  line:{paddingHorizontal:12,paddingVertical:14,borderRadius:14},active:{backgroundColor:'#e8e8f2'},label:{fontSize:16,color:'#292936'},
  caption:{fontWeight:'500',fontSize:13,color:'#696a78',margin:12},
});
