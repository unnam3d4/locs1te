"""Local-only Windows desktop management utility for ITSO DB.
No remote admin API: privileged actions require the local admin password.
"""
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import server

ROOT=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
CONFIG=ROOT/'settings.json'
DEFAULT={'host':'127.0.0.1','port':3000,'data_dir':'C:/ITSO_DB/data'}

def config():
    c=dict(DEFAULT)
    if CONFIG.exists():
        try:c.update(json.loads(CONFIG.read_text(encoding='utf-8')))
        except (ValueError,OSError):pass
    return c

class Panel:
    def __init__(self):
        self.root=tk.Tk();self.root.title('БАЗА ДАННЫХ ИТСО — Управление сервером')
        self.root.geometry('650x440');self.root.minsize(600,400)
        self.root.option_add('*Font','Tahoma 9')
        self.status=tk.StringVar(value='Для операций требуется пароль администратора')
        self.book=ttk.Notebook(self.root);self.book.pack(fill='both',expand=True,padx=10,pady=10)
        self.tabs={k:ttk.Frame(self.book,padding=12) for k in ('Сервер','Пользователи','Копии','Настройки')}
        for k,f in self.tabs.items():self.book.add(f,text=k)
        ttk.Label(self.root,textvariable=self.status,anchor='w',relief='sunken').pack(fill='x',side='bottom')
        self.make_server();self.make_users();self.make_backups();self.make_settings()

    def run(self):self.root.mainloop()

    def db(self):return server.DB(config()['data_dir'])

    def permitted(self):
        # Password prompt is local UI, not sent across HTTP.
        name=simpledialog.askstring('Администратор','Имя администратора:',parent=self.root)
        if name is None:return False
        pw=simpledialog.askstring('Администратор','Пароль:',parent=self.root,show='*')
        if pw is None:return False
        with self.db().tx() as c:r=c.execute("SELECT salt,password_hash FROM users WHERE name=? AND role='admin'",(name,)).fetchone()
        if not r or not server.check_password(pw,r['salt'],r['password_hash']):
            messagebox.showerror('Доступ','Неверные учётные данные');return False
        return True

    def action(self,work,auth=True):
        if auth and not self.permitted():return
        try:
            result=work();self.status.set(str(result or 'Операция выполнена'))
        except Exception as e:messagebox.showerror('Ошибка',str(e));self.status.set('Ошибка: '+str(e))

    def make_server(self):
        t=self.tabs['Сервер'];ttk.Label(t,text='ITSO_DB — служба Windows',font=('Tahoma',12,'bold')).pack(anchor='w',pady=6)
        for name,action in [('Проверить состояние службы','query'),('Запустить службу','start'),('Остановить службу','stop'),('Перезапустить службу','restart')]:
            ttk.Button(t,text=name,command=lambda a=action:self.control(a)).pack(fill='x',pady=4)
        ttk.Label(t,text='Сервер по умолчанию слушает только 127.0.0.1. Для доступа по LAN\nукажите IP сервера на вкладке «Настройки» и перезапустите службу.',wraplength=540).pack(anchor='w',pady=12)

    def control(self,mode):
        def work():
            if os.name!='nt':raise RuntimeError('Управление службой доступно только на Windows')
            verbs=['stop','start'] if mode=='restart' else [mode]
            results=[]
            for v in verbs:
                proc=subprocess.run(['sc.exe',v,'ITSO_DB'],capture_output=True,text=True,errors='replace',timeout=20)
                results.append(proc.stdout.strip() or proc.stderr.strip())
                if proc.returncode and v=='start':raise RuntimeError(results[-1])
            return '\n'.join(results)[-600:]
        self.action(work,auth=(mode!='query'))

    def make_users(self):
        t=self.tabs['Пользователи']
        ttk.Label(t,text='Добавление пользователей и смена пароля',font=('Tahoma',11,'bold')).pack(anchor='w',pady=6)
        ttk.Button(t,text='Список пользователей',command=lambda:self.action(self.user_list)).pack(fill='x',pady=5)
        ttk.Button(t,text='Добавить пользователя',command=self.add_user).pack(fill='x',pady=5)
        ttk.Button(t,text='Сменить пароль пользователя',command=self.reset_password).pack(fill='x',pady=5)

    def user_list(self):
        return ', '.join('{} ({})'.format(x['name'],x['role']) for x in self.db().list_users())

    def add_user(self):
        if not self.permitted():return
        name=simpledialog.askstring('Новый пользователь','Логин:',parent=self.root)
        if not name:return
        role=simpledialog.askstring('Роль','admin, editor или observer:',initialvalue='observer',parent=self.root)
        if role is None:return
        pw=simpledialog.askstring('Пароль','Пароль (не менее 8 символов):',show='*',parent=self.root)
        if pw is not None:self.action(lambda:self.db().create_user(name,pw,role),auth=False)

    def reset_password(self):
        if not self.permitted():return
        name=simpledialog.askstring('Пароль','Логин пользователя:',parent=self.root)
        if not name:return
        pw=simpledialog.askstring('Пароль','Новый пароль:',parent=self.root,show='*')
        if pw is None:return
        def work():
            if not 8<=len(pw)<=128:raise ValueError('Пароль должен быть от 8 до 128 символов')
            salt,digest=server.hashed(pw)
            with self.db().tx() as c:
                row=c.execute('SELECT id FROM users WHERE name=?',(name,)).fetchone()
                if not row:raise ValueError('Пользователь не найден')
                c.execute('UPDATE users SET salt=?,password_hash=? WHERE id=?',(salt,digest,row['id']))
                c.execute('DELETE FROM sessions WHERE user_id=?',(row['id'],))
            return 'Пароль изменён, сеансы пользователя завершены'
        self.action(work,auth=False)

    def make_backups(self):
        t=self.tabs['Копии']
        ttk.Label(t,text='Копии и перенос всей базы',font=('Tahoma',11,'bold')).pack(anchor='w',pady=6)
        ttk.Button(t,text='Создать локальную часовую копию сейчас',command=lambda:self.action(lambda:self.db().backup())).pack(fill='x',pady=5)
        ttk.Button(t,text='Экспорт полного архива ZIP',command=self.export).pack(fill='x',pady=5)
        ttk.Button(t,text='Восстановить ZIP (служба должна быть остановлена)',command=self.restore).pack(fill='x',pady=5)
        ttk.Label(t,text='Архив включает SQLite и вложения. Импорт проверяет SHA-256 и целостность базы.\nПеред восстановлением обязательно остановите сервер.',wraplength=550).pack(anchor='w',pady=15)

    def export(self):
        if not self.permitted():return
        path=filedialog.asksaveasfilename(defaultextension='.zip',filetypes=[('Архив ИТСО','*.zip')],parent=self.root)
        if path:self.action(lambda:self.db().export_archive(path),auth=False)

    def restore(self):
        if not self.permitted():return
        path=filedialog.askopenfilename(filetypes=[('Архив ИТСО','*.zip')],parent=self.root)
        if not path:return
        if not messagebox.askyesno('ОПАСНАЯ ОПЕРАЦИЯ','Вы остановили службу ITSO_DB? Восстановление ЗАМЕНИТ текущую базу и файлы. Продолжить?'):return
        self.action(lambda:self.db().restore_archive(path),auth=False)

    def make_settings(self):
        t=self.tabs['Настройки'];c=config()
        self.host=tk.StringVar(value=c['host']);self.port=tk.StringVar(value=str(c['port']));self.data=tk.StringVar(value=c['data_dir'])
        for caption,value in [('IP компьютера-сервера',self.host),('HTTP порт',self.port),('Папка данных',self.data)]:
            ttk.Label(t,text=caption).pack(anchor='w',pady=3);ttk.Entry(t,textvariable=value,width=60).pack(fill='x',pady=2)
        ttk.Label(t,text='HTTP НЕ шифруется. Доступ разрешайте только доверенной LAN,\nне пробрасывайте порт в Интернет.',wraplength=540).pack(anchor='w',pady=10)
        ttk.Button(t,text='Сохранить настройки (после перезапуска службы)',command=self.save_settings).pack(fill='x',pady=5)

    def save_settings(self):
        def work():
            import ipaddress
            host=self.host.get().strip()
            ipaddress.ip_address(host)
            port=int(self.port.get())
            if not 1<=port<=65535:raise ValueError('Неверный порт')
            folder=Path(self.data.get()).expanduser()
            if not folder.is_absolute():raise ValueError('Требуется абсолютный путь')
            payload={'host':host,'port':port,'data_dir':str(folder)}
            tmp=CONFIG.with_suffix('.writing');tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(str(tmp),str(CONFIG))
            return 'Настройки сохранены; перезапустите службу ITSO_DB'
        self.action(work)

if __name__=='__main__':Panel().run()
