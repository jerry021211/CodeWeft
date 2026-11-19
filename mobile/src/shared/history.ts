import type { SQLiteDatabase } from 'expo-sqlite';
import type { Turn } from './provider';

export type Session = { id: number; title: string; base: string; model: string };
export type Message = Turn & { id: number; state: 'done' | 'streaming' | 'stopped' | 'interrupted' | 'failed'; note: string };
type DB = Pick<SQLiteDatabase, 'execSync' | 'runSync' | 'getAllSync' | 'getFirstSync' | 'withTransactionSync'>;

export class History {
  db: DB;
  constructor(db: DB) {
    this.db = db;
    db.execSync(`PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, title TEXT NOT NULL, base TEXT NOT NULL, model TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, chat INTEGER NOT NULL REFERENCES chats(id),
        role TEXT NOT NULL, content TEXT NOT NULL, state TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
      CREATE INDEX IF NOT EXISTS by_chat ON messages(chat,id);
      CREATE TABLE IF NOT EXISTS selection(slot INTEGER PRIMARY KEY CHECK(slot=1), chat INTEGER NOT NULL);
      UPDATE messages SET state='interrupted',note='上次生成已中断' WHERE state='streaming';`);
  }
  sessions() {
    return this.db.getAllSync<Session>('SELECT * FROM chats ORDER BY COALESCE((SELECT max(id) FROM messages WHERE chat=chats.id),0) DESC, id DESC');
  }
  select(id: number) {
    this.db.runSync('INSERT OR REPLACE INTO selection VALUES (1,?)', id);
  }
  current() {
    return this.db.getFirstSync<Session>('SELECT chats.* FROM chats JOIN selection ON chats.id=selection.chat') ?? null;
  }
  clearSelection() { this.db.runSync('DELETE FROM selection'); }
  messages(chat: number) { return this.db.getAllSync<Message>('SELECT id,role,content,state,note FROM messages WHERE chat=? ORDER BY id', chat); }
  begin(current: Session | null, base: string, model: string, question: string) {
    let chat = current;
    let reply = 0;
    this.db.withTransactionSync(() => {
      if (!chat) {
        const title = [...question].slice(0, 36).join('');
        const id = this.db.runSync('INSERT INTO chats(title,base,model) VALUES (?,?,?)', title, base, model).lastInsertRowId;
        chat = { id, title, base, model };
      }
      if (chat.base !== base || chat.model !== model) throw new Error('此会话属于其他模型配置，请新建对话。');
      this.select(chat.id);
      this.db.runSync("INSERT INTO messages(chat,role,content,state) VALUES (?,'user',?,'done')", chat.id, question);
      reply = this.db.runSync("INSERT INTO messages(chat,role,content,state) VALUES (?,'assistant','','streaming')", chat.id).lastInsertRowId;
    });
    return { chat: chat as Session, reply };
  }
  save(id: number, content: string, state: Message['state'], note = '') {
    this.db.runSync('UPDATE messages SET content=?,state=?,note=? WHERE id=?', content, state, note, id);
  }
}
