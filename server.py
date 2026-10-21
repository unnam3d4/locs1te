"""ITSO LAN database prototype. Python 3.8+; standard library only.
HTTP is intentionally unencrypted per project decision. NEVER expose to the Internet.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

HERE = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
COOKIE = 'itso_session'
SESSION_SECONDS = 2 * 3600
ROLES = ('admin', 'editor', 'observer')
MAX_JSON = 1024 * 1024
UPLOAD_CHUNK = 2 * 1024 * 1024  # no file-size limit: limit only per HTTP chunk
MIME = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
        '.js': 'application/javascript; charset=utf-8', '.ico': 'image/x-icon'}


def now():
    return int(time.time())


def hashed(password, salt=None):
    salt = salt if salt is not None else secrets.token_bytes(16)
    return salt.hex(), hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 230000).hex()


def check_password(password, salt, expected):
    try:
        return hmac.compare_digest(hashed(password, bytes.fromhex(salt))[1], expected)
    except (ValueError, TypeError):
        return False


def check_name(s):
    return isinstance(s, str) and 1 <= len(s.strip()) <= 80 and not any(ord(c) < 32 for c in s)


def clean_filename(name):
    name = str(name).replace('\\', '/').split('/')[-1].strip().strip('.')
    name = ''.join(ch for ch in name if ord(ch) >= 32 and ch not in '<>:"/\\|?*')
    return (name[:180] or 'Файл')


def dump(v):
    return json.dumps(v, ensure_ascii=False, separators=(',', ':'))


class Failure(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class DB:
    def __init__(self, data):
        self.root = Path(data).resolve()
        self.files = self.root / 'uploads'
        self.backups = self.root / 'backups'
        self.files.mkdir(parents=True, exist_ok=True)
        self.backups.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'itso.db'
        self.lock = threading.RLock()
        self.upload_lock = threading.Lock()
        with self.tx() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
            CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                role TEXT NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                csrf TEXT NOT NULL, expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sheets (id INTEGER PRIMARY KEY, title TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '', created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS cells (sheet_id INTEGER NOT NULL, row_no INTEGER NOT NULL, col_no INTEGER NOT NULL,
                value TEXT NOT NULL DEFAULT '', style TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(sheet_id,row_no,col_no));
            CREATE TABLE IF NOT EXISTS maps (id INTEGER PRIMARY KEY, title TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '', created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS map_objects (id TEXT PRIMARY KEY, map_id INTEGER NOT NULL,
                x REAL NOT NULL, y REAL NOT NULL, properties TEXT NOT NULL DEFAULT '{}',
                appearance TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS map_links (id TEXT PRIMARY KEY, map_id INTEGER NOT NULL,
                from_id TEXT NOT NULL, to_id TEXT NOT NULL, options TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                byte_length INTEGER NOT NULL, owner INTEGER NOT NULL, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS uploads_pending (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                name TEXT NOT NULL, byte_length INTEGER NOT NULL, received INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                body TEXT NOT NULL, created INTEGER NOT NULL, edited INTEGER);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires);
            CREATE INDEX IF NOT EXISTS idx_chat_created ON chat(id DESC);
            CREATE INDEX IF NOT EXISTS idx_obj_map ON map_objects(map_id);
            CREATE INDEX IF NOT EXISTS idx_links_map ON map_links(map_id);
            ''')

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            c = sqlite3.connect(str(self.path), timeout=20)
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA foreign_keys=ON')
            c.execute('PRAGMA busy_timeout=20000')
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    def admin_exists(self):
        with self.tx() as c:
            return c.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is not None

    def create_user(self, name, password, role):
        if not check_name(name) or role not in ROLES or not 8 <= len(password) <= 128:
            raise Failure('Проверьте имя, роль и пароль (не менее 8 символов)')
        salt, secret = hashed(password)
        with self.tx() as c:
            try:
                c.execute('INSERT INTO users(name,role,salt,password_hash) VALUES(?,?,?,?)', (name.strip(),role,salt,secret))
            except sqlite3.IntegrityError:
                raise Failure('Пользователь уже существует',409)

    def auth(self, name, password):
        with self.tx() as c:
            row = c.execute('SELECT * FROM users WHERE name=?',(name,)).fetchone()
            if not row or not check_password(password,row['salt'],row['password_hash']):
                raise Failure('Неверный логин или пароль',401)
            token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            c.execute('INSERT INTO sessions(token_hash,user_id,csrf,expires) VALUES(?,?,?,?)',
                      (hashlib.sha256(token.encode()).hexdigest(),row['id'],csrf,now()+SESSION_SECONDS))
            return {'token':token,'csrf':csrf,'user':{'id':row['id'],'name':row['name'],'role':row['role']}}

    def current(self, token):
        if not token:
            return None
        with self.tx() as c:
            c.execute('DELETE FROM sessions WHERE expires<?', (now(),))
            r = c.execute('''SELECT u.id,u.name,u.role,s.csrf,s.expires FROM sessions s
                  JOIN users u ON u.id=s.user_id WHERE s.token_hash=?''',
                  (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            return dict(r) if r else None

    def logout(self, token):
        with self.tx() as c:
            c.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def list_users(self):
        with self.tx() as c:
            return [dict(r) for r in c.execute('SELECT id,name,role FROM users ORDER BY id')]

    def list_sheets(self):
        with self.tx() as c:
            return [dict(r) for r in c.execute('SELECT id,title,folder FROM sheets ORDER BY folder,title,id')]

    def add_sheet(self, title, folder=''):
        if not check_name(title) or len(folder)>150: raise Failure('Недопустимое название')
        with self.tx() as c:
            return c.execute('INSERT INTO sheets(title,folder,created) VALUES(?,?,?)',(title.strip(),folder,now())).lastrowid

    def sheet(self, sheet_id):
        with self.tx() as c:
            s=c.execute('SELECT id,title,folder FROM sheets WHERE id=?',(sheet_id,)).fetchone()
            if not s: raise Failure('Таблица не найдена',404)
            cells=[dict(r) for r in c.execute('SELECT row_no,col_no,value,style,revision FROM cells WHERE sheet_id=? ORDER BY row_no,col_no',(sheet_id,))]
            for cell in cells:cell['style']=json.loads(cell['style'])
            return {'sheet':dict(s),'cells':cells}

    def edit_cell(self, sheet_id, row, col, value, style, expected):
        if not (0<=row<100000 and 0<=col<16384) or not isinstance(value,str) or len(value)>20000:
            raise Failure('Некорректная ячейка')
        if not isinstance(style,dict) or len(dump(style))>2048: raise Failure('Некорректный стиль')
        with self.tx() as c:
            if not c.execute('SELECT 1 FROM sheets WHERE id=?',(sheet_id,)).fetchone(): raise Failure('Нет таблицы',404)
            old=c.execute('SELECT revision FROM cells WHERE sheet_id=? AND row_no=? AND col_no=?',(sheet_id,row,col)).fetchone()
            rev=old['revision'] if old else 0
            if rev!=expected: raise Failure('Ячейка изменена другим пользователем. Обновите данные.',409)
            c.execute('''INSERT INTO cells(sheet_id,row_no,col_no,value,style,revision) VALUES(?,?,?,?,?,?)
                 ON CONFLICT(sheet_id,row_no,col_no) DO UPDATE SET value=excluded.value,
                 style=excluded.style,revision=excluded.revision''', (sheet_id,row,col,value,dump(style),rev+1))
            return rev+1

    def list_maps(self):
        with self.tx() as c:
            return [dict(r) for r in c.execute('SELECT id,title,folder FROM maps ORDER BY folder,title,id')]

    def add_map(self,title,folder=''):
        if not check_name(title) or len(folder)>150:raise Failure('Недопустимое название')
        with self.tx() as c:
            return c.execute('INSERT INTO maps(title,folder,created) VALUES(?,?,?)',(title.strip(),folder,now())).lastrowid

    def map(self,map_id):
        with self.tx() as c:
            m=c.execute('SELECT id,title,folder FROM maps WHERE id=?',(map_id,)).fetchone()
            if not m:raise Failure('Карта не найдена',404)
            obj=[dict(r) for r in c.execute('SELECT * FROM map_objects WHERE map_id=?',(map_id,))]
            for o in obj:o['properties']=json.loads(o['properties']);o['appearance']=json.loads(o['appearance'])
            links=[dict(r) for r in c.execute('SELECT * FROM map_links WHERE map_id=?',(map_id,))]
            for l in links:l['options']=json.loads(l['options'])
            return {'map':dict(m),'objects':obj,'links':links}

    def save_object(self,map_id,obj,expected):
        oid=obj.get('id') or secrets.token_hex(12)
        if not isinstance(oid, str) or not re.fullmatch(r'[a-f0-9]{24}', oid):
            raise Failure('Некорректный идентификатор объекта')
        x,y=obj.get('x'),obj.get('y')
        props=obj.get('properties',{})
        appearance=obj.get('appearance',{})
        if not isinstance(x,(int,float)) or not isinstance(y,(int,float)) or abs(x)>1e7 or abs(y)>1e7 or not isinstance(props,dict) or not isinstance(appearance,dict) or len(dump(props))>20000 or len(dump(appearance))>2048:
            raise Failure('Некорректные данные объекта')
        with self.tx() as c:
            if not c.execute('SELECT 1 FROM maps WHERE id=?',(map_id,)).fetchone():raise Failure('Нет карты',404)
            old=c.execute('SELECT revision,map_id FROM map_objects WHERE id=?',(oid,)).fetchone()
            if old and old['map_id']!=map_id:raise Failure('Неверная карта',409)
            rev=old['revision'] if old else 0
            if rev!=expected:raise Failure('Объект уже изменён другим пользователем',409)
            if old:
                c.execute('UPDATE map_objects SET x=?,y=?,properties=?,appearance=?,revision=? WHERE id=?',
                          (x,y,dump(props),dump(appearance),rev+1,oid))
            else:
                c.execute('INSERT INTO map_objects(id,map_id,x,y,properties,appearance,revision) VALUES(?,?,?,?,?,?,1)',
                          (oid,map_id,x,y,dump(props),dump(appearance)))
        return {'id':oid,'revision':rev+1}

    def add_link(self,map_id,source,target):
        if source==target:raise Failure('Соединение должно связывать разные объекты')
        with self.tx() as c:
            count=c.execute('SELECT COUNT(*) AS n FROM map_objects WHERE map_id=? AND id IN (?,?)',(map_id,source,target)).fetchone()['n']
            if count!=2:raise Failure('Объект не найден',404)
            lid=secrets.token_hex(12)
            c.execute('INSERT INTO map_links(id,map_id,from_id,to_id) VALUES(?,?,?,?)',(lid,map_id,source,target))
        return lid

    def delete_object(self,map_id,oid,delete_links):
        with self.tx() as c:
            r=c.execute('SELECT 1 FROM map_objects WHERE id=? AND map_id=?',(oid,map_id)).fetchone()
            if not r:raise Failure('Объект не найден',404)
            # In this MVP preserve links only as unbound ends is not implemented; require delete_links.
            if not delete_links and c.execute('SELECT 1 FROM map_links WHERE from_id=? OR to_id=?',(oid,oid)).fetchone():
                raise Failure('Сохранение свободных концов линий пока не реализовано',501)
            c.execute('DELETE FROM map_links WHERE from_id=? OR to_id=?',(oid,oid))
            c.execute('DELETE FROM map_objects WHERE id=?',(oid,))

    def list_files(self):
        with self.tx() as c:
            return [dict(r) for r in c.execute('''SELECT f.id,f.name,f.byte_length,f.created,u.name AS owner
                    FROM files f JOIN users u ON f.owner=u.id ORDER BY f.created DESC, f.name''')]

    def store_upload(self, user_id, name, stream, length):
        if length < 0:
            raise Failure('Некорректный размер')
        name = clean_filename(name)
        fid = secrets.token_hex(20)
        tmp = self.files / (fid + '.part')
        try:
            # Stream outside the database lock, so multi-gigabyte uploads do not
            # block edits, reads or chat for the second workstation.
            with open(str(tmp), 'xb') as f:
                remaining = length
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        raise Failure('Загрузка оборвана')
                    f.write(block)
                    remaining -= len(block)
                f.flush()
                os.fsync(f.fileno())
            with self.tx() as c:
                original = Path(name)
                choice = name
                count = 2
                while c.execute('SELECT 1 FROM files WHERE name=?', (choice,)).fetchone():
                    choice = '{} ({}){}'.format(original.stem, count, original.suffix)
                    count += 1
                os.replace(str(tmp), str(self.files / fid))
                c.execute('INSERT INTO files(id,name,byte_length,owner,created) VALUES(?,?,?,?,?)',
                          (fid, choice, length, user_id, now()))
                return {'id': fid, 'name': choice, 'byte_length': length}
        finally:
            if tmp.exists():
                tmp.unlink()

    def start_upload(self, user_id, name, size):
        # Durable resumable upload, with no product limit on total file size.
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size < 2**63:
            raise Failure('Некорректный размер загружаемого файла')
        name = clean_filename(name)
        fid = secrets.token_hex(20)
        path = self.files / (fid + '.part')
        with self.upload_lock:
            with open(str(path), 'xb'):
                pass
            try:
                with self.tx() as c:
                    c.execute('''INSERT INTO uploads_pending(id,user_id,name,byte_length,received,updated)
                                 VALUES(?,?,?,?,0,?)''', (fid,user_id,name,size,now()))
            except Exception:
                path.unlink()
                raise
        return {'id':fid,'name':name,'size':size,'received':0}

    def upload_state(self, user_id, fid):
        with self.tx() as c:
            row = c.execute('SELECT * FROM uploads_pending WHERE id=? AND user_id=?',
                            (fid,user_id)).fetchone()
        if not row:
            with self.tx() as c:
                completed = c.execute('SELECT name,byte_length FROM files WHERE id=? AND owner=?',
                                      (fid,user_id)).fetchone()
            if completed:
                return {'id':fid,'name':completed['name'],'size':completed['byte_length'],
                        'received':completed['byte_length'],'complete':True}
            raise Failure('Загрузка не найдена',404)
        if not (self.files / (fid + '.part')).is_file():
            raise Failure('Незавершённая загрузка больше недоступна',404)
        return {'id':fid,'name':row['name'],'size':row['byte_length'],
                'received':row['received'],'complete':False}

    def append_upload(self, user_id, fid, offset, stream, size):
        if isinstance(offset,bool) or not isinstance(offset,int) or offset < 0 or not 0 < size <= UPLOAD_CHUNK:
            raise Failure('Некорректный диапазон блока',400)
        with self.upload_lock:
            state = self.upload_state(user_id,fid)
            if state['complete']:
                raise Failure('Файл уже загружен',409)
            if offset != state['received']:
                raise Failure('Смещение загрузки изменилось, запросите актуальное положение',409)
            if offset + size > state['size']:
                raise Failure('Блок превышает объявленный размер файла',400)
            path = self.files / (fid + '.part')
            if not path.is_file():
                raise Failure('Временный файл загрузки утрачен',409)
            # Truncate any uncommitted bytes left by a crash after fsync but before DB commit.
            with open(str(path), 'r+b') as f:
                if os.fstat(f.fileno()).st_size < offset:
                    raise Failure('Временный файл загрузки повреждён',409)
                f.truncate(offset)
                f.seek(offset)
                remaining = size
                try:
                    while remaining:
                        block = stream.read(min(65536,remaining))
                        if not block:
                            raise Failure('Передача блока прервана, попробуйте продолжить',400)
                        f.write(block)
                        remaining -= len(block)
                    f.flush()
                    os.fsync(f.fileno())
                except Exception:
                    f.truncate(offset)
                    raise
            with self.tx() as c:
                c.execute('''UPDATE uploads_pending SET received=?,updated=?
                             WHERE id=? AND user_id=? AND received=?''',
                          (offset+size,now(),fid,user_id,offset))
                if c.total_changes != 1:
                    raise Failure('Состояние загрузки изменилось',409)
        return {'id':fid,'received':offset+size,'size':state['size']}

    def finish_upload(self,user_id,fid):
        with self.upload_lock:
            state = self.upload_state(user_id,fid)
            if state['complete']:
                return {'id':fid,'name':state['name'],'byte_length':state['size']}
            if state['received'] != state['size']:
                raise Failure('Загрузка ещё не завершена',409)
            part = self.files / (fid + '.part')
            if not part.is_file() or part.stat().st_size != state['size']:
                raise Failure('Размер загруженного файла не совпадает',409)
            with self.tx() as c:
                original = Path(state['name'])
                choice = state['name']
                suffix = 2
                while c.execute('SELECT 1 FROM files WHERE name=?',(choice,)).fetchone():
                    choice = '{} ({}){}'.format(original.stem,suffix,original.suffix)
                    suffix += 1
                final = self.files / fid
                os.replace(str(part),str(final))
                try:
                    c.execute('INSERT INTO files(id,name,byte_length,owner,created) VALUES(?,?,?,?,?)',
                              (fid,choice,state['size'],user_id,now()))
                    c.execute('DELETE FROM uploads_pending WHERE id=?',(fid,))
                except Exception:
                    os.replace(str(final),str(part))
                    raise
        return {'id':fid,'name':choice,'byte_length':state['size']}

    def cancel_upload(self,user_id,fid):
        with self.upload_lock:
            state = self.upload_state(user_id,fid)
            if state['complete']:
                raise Failure('Файл уже загружен и не может быть отменён',409)
            with self.tx() as c:
                c.execute('DELETE FROM uploads_pending WHERE id=? AND user_id=?',(fid,user_id))
            try:(self.files/(fid+'.part')).unlink()
            except FileNotFoundError:pass

    def cleanup_pending_uploads(self,max_age=7*86400):
        # Only expired *incomplete* files are garbage; never touch completed uploads.
        with self.upload_lock:
            with self.tx() as c:
                ids=[r['id'] for r in c.execute('SELECT id FROM uploads_pending WHERE updated<?',(now()-max_age,))]
                for fid in ids:
                    c.execute('DELETE FROM uploads_pending WHERE id=?',(fid,))
                    try:(self.files/(fid+'.part')).unlink()
                    except FileNotFoundError:pass
            return len(ids)

    def file(self,fid):
        with self.tx() as c:
            r=c.execute('SELECT id,name,byte_length FROM files WHERE id=?',(fid,)).fetchone()
            if not r:raise Failure('Файл не найден',404)
            return dict(r)

    def delete_file(self,fid):
        with self.tx() as c:
            if not c.execute('SELECT 1 FROM files WHERE id=?',(fid,)).fetchone():raise Failure('Файл не найден',404)
            c.execute('DELETE FROM files WHERE id=?',(fid,))
            try:(self.files/fid).unlink()
            except FileNotFoundError:pass

    def send_chat(self,user_id,body):
        if not isinstance(body,str) or not body.strip() or len(body)>6000:raise Failure('Сообщение должно содержать от 1 до 6000 символов')
        with self.tx() as c:
            return c.execute('INSERT INTO chat(user_id,body,created) VALUES(?,?,?)',(user_id,body.strip(),now())).lastrowid

    def chat(self,before=0,limit=60):
        with self.tx() as c:
            where='WHERE ch.id<?' if before else ''
            params=(before,limit) if before else (limit,)
            return [dict(r) for r in c.execute('''SELECT ch.id,u.name AS username,ch.user_id,ch.body,ch.created,ch.edited FROM chat ch
                    JOIN users u ON ch.user_id=u.id {} ORDER BY ch.id DESC LIMIT ?'''.format(where),params)][::-1]

    def edit_chat(self,user,mid,body):
        if not isinstance(body,str) or not body.strip() or len(body)>6000:raise Failure('Некорректное сообщение')
        with self.tx() as c:
            r=c.execute('SELECT user_id,created FROM chat WHERE id=?',(mid,)).fetchone()
            if not r:raise Failure('Сообщение не найдено',404)
            if r['user_id']!=user['id'] or now()-r['created']>900:raise Failure('Редактирование доступно автору в течение 15 минут',403)
            c.execute('UPDATE chat SET body=?,edited=? WHERE id=?',(body.strip(),now(),mid))

    def delete_chat(self,mid):
        with self.tx() as c:c.execute('DELETE FROM chat WHERE id=?',(mid,))

    def search(self,q,page=1,section='all',scope='',exact=False):
        """Small-scale search MVP; future milestone replaces scans with indexed paging."""
        if not 1<=len(q.strip())<=160: return {'total':0,'counts':{},'items':[]}
        terms=q.strip().casefold().split()
        # Exact mode compares a whole field, not a substring of serialized JSON.
        hit=lambda hay: hay.casefold()==q.strip().casefold() if exact else all(t in hay.casefold() for t in terms)
        found=[];counts={'maps':0,'tables':0,'files':0,'chat':0}
        with self.tx() as c:
            if section in ('all','maps'):
                for r in c.execute('SELECT o.id,o.map_id,o.properties,m.title,m.folder FROM map_objects o JOIN maps m ON m.id=o.map_id'):
                    props=json.loads(r['properties']); hay=dump(props)
                    matched=(any(hit(str(v)) for v in props.values()) if exact else hit(hay))
                    if matched and (not scope or scope in (r['folder'],str(r['map_id']))):
                        found.append({'section':'maps','label':str(props.get('Название',props.get('name','Объект'))),'location':r['folder']+' / '+r['title'], 'detail':hay[:200],'ref':r['map_id'],'object_id':r['id']})
            if section in ('all','tables'):
                for r in c.execute('''SELECT s.id,s.title,s.folder,c.row_no,c.col_no,c.value FROM cells c JOIN sheets s ON s.id=c.sheet_id'''):
                    if hit(r['value']) and (not scope or scope in (r['folder'],str(r['id']))):
                        found.append({'section':'tables','label':r['title'],'location':'{} / R{}C{}'.format(r['folder'],r['row_no']+1,r['col_no']+1),'detail':r['value'][:200],'ref':r['id']})
            if section in ('all','files'):
                for r in c.execute('SELECT id,name FROM files'):
                    if hit(r['name']):found.append({'section':'files','label':r['name'],'location':'Обменник','detail':r['name'],'ref':r['id']})
            if section in ('all','chat'):
                for r in c.execute('SELECT id,body FROM chat'):
                    if hit(r['body']):found.append({'section':'chat','label':'Сообщение #'+str(r['id']),'location':'Чат','detail':r['body'][:200],'ref':r['id']})
        for r in found:counts[r['section']]+=1
        found.sort(key=lambda x:(x['section'],x['label'].casefold()))
        return {'total':len(found),'counts':counts,'items':found[(page-1)*50:page*50]}

    def backup(self):
        stamp=dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        target=self.backups/stamp
        with self.lock:
            target.mkdir()
            try:
                with contextlib.closing(sqlite3.connect(str(self.path))) as src, \
                     contextlib.closing(sqlite3.connect(str(target/'itso.db'))) as dest:
                    src.backup(dest)
                attach=target/'uploads';attach.mkdir()
                with self.tx() as c:ids=[r['id'] for r in c.execute('SELECT id FROM files')]
                for fid in ids:
                    origin=self.files/fid
                    if origin.exists():
                        try:os.link(str(origin),str(attach/fid))
                        except OSError:shutil.copyfile(str(origin),str(attach/fid))
            except Exception:
                shutil.rmtree(str(target),ignore_errors=True)
                raise
        cutoff=time.time()-14*86400
        for folder in self.backups.iterdir():
            if folder.is_dir() and folder.stat().st_mtime<cutoff:
                shutil.rmtree(str(folder),ignore_errors=True)
        return stamp


class Handler(BaseHTTPRequestHandler):
    server_version='ITSO/0.1'
    def log_message(self,fmt,*args):
        # Log technical errors and requests, not message contents or credentials.
        if args and str(args[1] if len(args)>1 else '').startswith(('4','5')):
            sys.stderr.write('%s %s\n'%(self.address_string(),fmt%args))

    def json_response(self,data,status=200,headers=None):
        b=dump(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.send_header('Content-Length',str(len(b)))
        self.end_headers();self.wfile.write(b)

    def body(self):
        n=int(self.headers.get('Content-Length','0'))
        if n<=0 or n>MAX_JSON:raise Failure('Некорректный размер запроса',413)
        if 'application/json' not in self.headers.get('Content-Type',''):raise Failure('Ожидается JSON',415)
        try:return json.loads(self.rfile.read(n).decode('utf-8'))
        except (UnicodeError,ValueError):raise Failure('Некорректный JSON')

    def token(self):
        for part in self.headers.get('Cookie','').split(';'):
            if part.strip().startswith(COOKIE+'='):return part.strip().split('=',1)[1]
        return ''

    def user(self,write=False,editor=False,admin=False):
        u=self.server.db.current(self.token())
        if not u:raise Failure('Войдите в систему',401)
        if write and not hmac.compare_digest(self.headers.get('X-ITSO-CSRF',''),u['csrf']):raise Failure('Ошибка защиты запроса',403)
        if admin and u['role']!='admin':raise Failure('Недостаточно прав',403)
        if editor and u['role'] not in ('admin','editor'):raise Failure('Недостаточно прав',403)
        return u

    def handle_api(self,method,path,qs):
        db=self.server.db
        if path=='/api/login' and method=='POST':
            body=self.body(); result=db.auth(str(body.get('name','')),str(body.get('password','')))
            token=result.pop('token')
            return self.json_response(result,headers={'Set-Cookie':'{}={}; HttpOnly; SameSite=Strict; Path=/; Max-Age={}'.format(COOKIE,token,SESSION_SECONDS)})
        if path=='/api/me' and method=='GET':
            u=self.user();return self.json_response({'id':u['id'],'name':u['name'],'role':u['role'],'csrf':u['csrf'],'expires':u['expires']})
        u=self.user(write=method in ('POST','PUT','DELETE'))
        if path=='/api/logout' and method=='POST':
            db.logout(self.token());return self.json_response({'ok':True},headers={'Set-Cookie':COOKIE+'=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
        if path=='/api/users' and method=='GET':self.user(admin=True);return self.json_response(db.list_users())
        if path=='/api/sheets' and method=='GET':return self.json_response(db.list_sheets())
        if path=='/api/sheets' and method=='POST':
            self.user(editor=True);d=self.body();return self.json_response({'id':db.add_sheet(d.get('title'),d.get('folder',''))},201)
        m=re.fullmatch(r'/api/sheets/(\d+)',path)
        if m and method=='GET':return self.json_response(db.sheet(int(m.group(1))))
        m=re.fullmatch(r'/api/sheets/(\d+)/cell',path)
        if m and method=='PUT':
            self.user(editor=True);d=self.body()
            rev=db.edit_cell(int(m.group(1)),int(d.get('row',-1)),int(d.get('col',-1)),d.get('value'),d.get('style',{}),d.get('revision',0))
            return self.json_response({'revision':rev})
        if path=='/api/maps' and method=='GET':return self.json_response(db.list_maps())
        if path=='/api/maps' and method=='POST':
            self.user(editor=True);d=self.body();return self.json_response({'id':db.add_map(d.get('title'),d.get('folder',''))},201)
        m=re.fullmatch(r'/api/maps/(\d+)',path)
        if m and method=='GET':return self.json_response(db.map(int(m.group(1))))
        m=re.fullmatch(r'/api/maps/(\d+)/objects',path)
        if m and method=='PUT':
            self.user(editor=True);d=self.body();return self.json_response(db.save_object(int(m.group(1)),d.get('object',{}),d.get('revision',0)))
        m=re.fullmatch(r'/api/maps/(\d+)/objects/([a-z0-9]+)',path)
        if m and method=='DELETE':
            self.user(editor=True);d=self.body();db.delete_object(int(m.group(1)),m.group(2),d.get('delete_links',False));return self.json_response({'ok':True})
        m=re.fullmatch(r'/api/maps/(\d+)/links',path)
        if m and method=='POST':
            self.user(editor=True);d=self.body();return self.json_response({'id':db.add_link(int(m.group(1)),d.get('from'),d.get('to'))},201)
        # Resumable uploads: small binary chunks, no limit on the complete file.
        if path=='/api/uploads' and method=='POST':
            d=self.body()
            return self.json_response(db.start_upload(u['id'],d.get('name'),d.get('size')),201)
        m=re.fullmatch(r'/api/uploads/([a-f0-9]{40})',path)
        if m and method=='GET':return self.json_response(db.upload_state(u['id'],m.group(1)))
        if m and method=='PUT':
            length=int(self.headers.get('Content-Length','-1'))
            offset=int(self.headers.get('X-Upload-Offset','-1'))
            return self.json_response(db.append_upload(u['id'],m.group(1),offset,self.rfile,length))
        if m and method=='DELETE':
            db.cancel_upload(u['id'],m.group(1));return self.json_response({'ok':True})
        m=re.fullmatch(r'/api/uploads/([a-f0-9]{40})/finish',path)
        if m and method=='POST':return self.json_response(db.finish_upload(u['id'],m.group(1)),201)
        if path=='/api/files' and method=='GET':return self.json_response(db.list_files())
        if path=='/api/files' and method=='POST':
            n=int(self.headers.get('Content-Length','-1'))
            from urllib.parse import unquote
            name=unquote(self.headers.get('X-Filename',''))
            if n<0:raise Failure('Требуется Content-Length',411)
            if not name:raise Failure('Укажите имя файла')
            # Raw binary is streamed and never buffered fully in RAM.
            return self.json_response(db.store_upload(u['id'],name,self.rfile,n),201)
        m=re.fullmatch(r'/api/files/([a-f0-9]{40})',path)
        if m and method=='DELETE':
            self.user(admin=True);db.delete_file(m.group(1));return self.json_response({'ok':True})
        if m and method=='GET':return self.download(m.group(1))
        if path=='/api/chat' and method=='GET':
            before=int(qs.get('before',['0'])[0]);return self.json_response(db.chat(before))
        if path=='/api/chat' and method=='POST':
            d=self.body();return self.json_response({'id':db.send_chat(u['id'],d.get('body'))},201)
        m=re.fullmatch(r'/api/chat/(\d+)',path)
        if m and method=='PUT':d=self.body();db.edit_chat(u,int(m.group(1)),d.get('body'));return self.json_response({'ok':True})
        if m and method=='DELETE':self.user(admin=True);db.delete_chat(int(m.group(1)));return self.json_response({'ok':True})
        if path=='/api/search' and method=='GET':
            q=qs.get('q',[''])[0];page=max(1,min(100000,int(qs.get('page',['1'])[0])))
            return self.json_response(db.search(q,page,qs.get('section',['all'])[0],qs.get('scope',[''])[0],qs.get('exact',['0'])[0]=='1'))
        if path=='/api/ping' and method=='POST':
            d=self.body();target=str(d.get('ip',''))
            import ipaddress
            try:
                ip=ipaddress.ip_address(target)
                if ip.version!=4 or not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                    raise ValueError()
            except ValueError:raise Failure('Разрешён только внутренний IPv4-адрес')
            cmd=['ping','-n','1','-w','1000',target] if os.name=='nt' else ['ping','-c','1','-W','1',target]
            try:
                p=subprocess.run(cmd,capture_output=True,timeout=3)
                return self.json_response({'reachable':p.returncode==0,'note':'Ответ ICMP не подтверждает исправность оборудования'})
            except (OSError,subprocess.TimeoutExpired):raise Failure('Проверка ICMP не выполнена',503)
        if path=='/api/backup' and method=='POST':
            self.user(admin=True);return self.json_response({'backup':db.backup()})
        raise Failure('Адрес API не найден',404)

    def download(self,fid):
        obj=self.server.db.file(fid)
        path=self.server.db.files/fid
        if not path.is_file():raise Failure('Файл на диске не найден',404)
        size=obj['byte_length'];start=0;end=size-1;status=200
        header=self.headers.get('Range','')
        if header:
            m=re.fullmatch(r'bytes=(\d+)-(\d*)',header.strip())
            if not m:raise Failure('Неподдерживаемый диапазон',416)
            start=int(m.group(1));end=int(m.group(2)) if m.group(2) else size-1
            if start>=size or end<start or end>=size:raise Failure('Неверный диапазон',416)
            status=206
        self.send_response(status)
        self.send_header('Content-Type','application/octet-stream')
        self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(obj['name']))
        self.send_header('Content-Length',str(max(0,end-start+1)))
        self.send_header('Accept-Ranges','bytes')
        self.send_header('Cache-Control','no-store')
        if status==206:self.send_header('Content-Range','bytes {}-{}/{}'.format(start,end,size))
        self.end_headers()
        with open(str(path),'rb') as f:
            f.seek(start);remain=end-start+1
            while remain>0:
                chunk=f.read(min(65536,remain))
                if not chunk:break
                self.wfile.write(chunk);remain-=len(chunk)

    def static(self,path):
        target='index.html' if path=='/' else path.lstrip('/')
        if target not in ('index.html','app.css','app.js'):
            raise Failure('Страница не найдена',404)
        file=HERE/'web'/target
        if not file.is_file():raise Failure('Статический файл не найден',404)
        b=file.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type',MIME.get(file.suffix,'application/octet-stream'))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(b)))
        self.end_headers();self.wfile.write(b)

    def dispatch(self,method):
        try:
            u=urlparse(self.path)
            if u.path.startswith('/api/'):
                self.handle_api(method,u.path,parse_qs(u.query))
            elif method=='GET':self.static(u.path)
            else:raise Failure('Метод недоступен',405)
        except Failure as e:self.json_response({'error':str(e)},e.status)
        except (ValueError,TypeError,KeyError,OverflowError):self.json_response({'error':'Некорректные параметры запроса'},400)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as e:
            print('ITSO server error:',type(e).__name__,str(e),file=sys.stderr)
            try:self.json_response({'error':'Внутренняя ошибка сервера'},500)
            except (BrokenPipeError,ConnectionResetError):pass

    def do_GET(self):self.dispatch('GET')
    def do_POST(self):self.dispatch('POST')
    def do_PUT(self):self.dispatch('PUT')
    def do_DELETE(self):self.dispatch('DELETE')


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,db):
        super().__init__(address,Handler)
        self.db=db


def hourly_backup(db,stop):
    while not stop.wait(3600):
        try:
            db.backup()
            db.cleanup_pending_uploads()
        except Exception as e:print('Ошибка резервного копирования или очистки загрузок:',e,file=sys.stderr)


def main(argv=None):
    p=argparse.ArgumentParser(description='БАЗА ДАННЫХ ИТСО — локальный сервер')
    p.add_argument('--data-dir',default=str(Path('C:/ITSO_DB/data') if os.name=='nt' else Path.cwd()/'data'))
    p.add_argument('--host',default='127.0.0.1',help='Use the LAN IP of this PC to enable LAN access')
    p.add_argument('--port',type=int,default=3000)
    p.add_argument('--init-admin',action='store_true',help='Create the first admin interactively')
    p.add_argument('--add-user',action='store_true',help='Add a user after checking administrator credentials')
    p.add_argument('--backup-now',action='store_true',help='Create an immediate backup')
    args=p.parse_args(argv)
    db=DB(args.data_dir)
    if args.init_admin:
        if db.admin_exists():raise SystemExit('Администратор уже создан')
        import getpass
        print('Настройка первого администратора')
        name=input('Имя пользователя [admin]: ').strip() or 'admin'
        password=getpass.getpass('Пароль (8+ символов): ')
        db.create_user(name,password,'admin')
        print('Администратор создан. Теперь запустите server.py без --init-admin')
        return
    if not db.admin_exists():
        raise SystemExit('Сначала создайте администратора: python server.py --init-admin')
    if args.add_user:
        import getpass
        admin_name = input('Администратор: ').strip()
        admin_pw = getpass.getpass('Пароль администратора: ')
        with db.tx() as c:
            r = c.execute("SELECT salt,password_hash FROM users WHERE name=? AND role='admin'", (admin_name,)).fetchone()
        if not r or not check_password(admin_pw, r['salt'], r['password_hash']):
            raise SystemExit('Неверные учётные данные администратора')
        name = input('Новый пользователь: ').strip()
        role = input('Роль (admin / editor / observer): ').strip()
        password = getpass.getpass('Пароль нового пользователя: ')
        db.create_user(name, password, role)
        print('Пользователь создан')
        return
    if args.backup_now:
        print('Резервная копия:', db.backup())
        return
    stop=threading.Event()
    worker=threading.Thread(target=hourly_backup,args=(db,stop),daemon=True)
    worker.start()
    with Server((args.host,args.port),db) as srv:
        print('ITSO HTTP http://{}:{} | HTTP без шифрования, только доверенная LAN'.format(args.host,args.port))
        try:srv.serve_forever(poll_interval=.5)
        except KeyboardInterrupt:pass
        finally:stop.set()


if __name__=='__main__':main()
