"""Database and HTTP smoke tests; no third-party dependencies."""
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = server.DB(self.temp.name)
        self.db.create_user('operator', 'pass123456', 'admin')

    def test_no_forced_objects_or_sheets(self):
        self.assertEqual(self.db.list_sheets(), [])
        self.assertEqual(self.db.list_maps(), [])

    def test_password_session_and_role(self):
        auth = self.db.auth('operator', 'pass123456')
        u = self.db.current(auth['token'])
        self.assertEqual(u['role'], 'admin')
        self.assertEqual(u['csrf'], auth['csrf'])
        self.db.logout(auth['token'])
        self.assertIsNone(self.db.current(auth['token']))
        with self.assertRaises(server.Failure): self.db.auth('operator','incorrect')

    def test_conflicting_cell_writes(self):
        sid = self.db.add_sheet('Свободная таблица')
        self.assertEqual(self.db.edit_cell(sid, 8, 2, '192.168.0.24', {'bold':True}, 0), 1)
        with self.assertRaises(server.Failure) as error:
            self.db.edit_cell(sid, 8, 2, 'конфликт', {}, 0)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.db.sheet(sid)['cells'][0]['value'], '192.168.0.24')

    def test_map_create_manual_ping_and_connections(self):
        mid = self.db.add_map('Этаж 1')
        obj1 = self.db.save_object(mid, {'x':15,'y':25,'properties':{'Название':'Камера 1','IP':'192.168.1.10'}}, 0)
        obj2 = self.db.save_object(mid, {'x':160,'y':25,'properties':{'Название':'Коммутатор'}}, 0)
        self.assertNotEqual(obj1['id'], obj2['id'])
        self.db.add_link(mid, obj1['id'], obj2['id'])
        m = self.db.map(mid)
        self.assertEqual(len(m['objects']), 2)
        self.assertEqual(len(m['links']), 1)
        self.assertEqual(self.db.search('камера')['total'], 1)
        with self.assertRaises(server.Failure) as conflict:
            self.db.save_object(mid, {'id':obj1['id'],'x':45,'y':45,'properties':{}}, 0)
        self.assertEqual(conflict.exception.status, 409)

    def test_upload_duplicate_names_and_backup(self):
        first = self.db.store_upload(1, 'Инструкция.pdf', io.BytesIO(b'first'), 5)
        second = self.db.store_upload(1, 'Инструкция.pdf', io.BytesIO(b'second'), 6)
        self.assertEqual(second['name'], 'Инструкция (2).pdf')
        self.assertEqual((self.db.files/first['id']).read_bytes(), b'first')
        snap = self.db.backup()
        self.assertTrue((self.db.backups/snap/'itso.db').exists())
        self.assertTrue((self.db.backups/snap/'uploads'/first['id']).exists())
        self.db.delete_file(first['id'])
        self.assertTrue((self.db.backups/snap/'uploads'/first['id']).exists())

    def test_resumable_upload_crash_retry_duplicate_name_and_owner(self):
        data=b'first-chunk and remaining-chunk'
        first=self.db.start_upload(1,'Фото камеры.jpg',len(data))
        fid=first['id']
        half=11
        self.assertEqual(self.db.append_upload(1,fid,0,io.BytesIO(data[:half]),half)['received'],half)
        with self.assertRaises(server.Failure) as conflict:
            self.db.append_upload(1,fid,0,io.BytesIO(b'wrong'),5)
        self.assertEqual(conflict.exception.status,409)
        with self.assertRaises(server.Failure):
            self.db.upload_state(200,fid)
        # Opening the database again simulates a server restart.
        reopened=server.DB(self.temp.name)
        self.assertEqual(reopened.upload_state(1,fid)['received'],half)
        with self.assertRaises(server.Failure):
            reopened.append_upload(1,fid,half,io.BytesIO(b'x'),4)
        self.assertEqual(reopened.upload_state(1,fid)['received'],half)
        reopened.append_upload(1,fid,half,io.BytesIO(data[half:]),len(data)-half)
        done=reopened.finish_upload(1,fid)
        self.assertEqual((reopened.files/fid).read_bytes(),data)
        self.assertEqual(done['name'],'Фото камеры.jpg')
        self.assertEqual(reopened.list_files()[0]['byte_length'],len(data))
        other=reopened.start_upload(1,'Фото камеры.jpg',0)
        second=reopened.finish_upload(1,other['id'])
        self.assertEqual(second['name'],'Фото камеры (2).jpg')
        completed=reopened.upload_state(1,fid)
        self.assertTrue(completed['complete'])
        self.assertEqual(reopened.finish_upload(1,fid)['id'],fid)
        self.assertEqual(len(reopened.list_files()),2)

    def test_resumable_upload_validation_cleanup_and_no_file_size_limit(self):
        for size in (-1,'4',True,2**63):
            with self.assertRaises(server.Failure):self.db.start_upload(1,'test.txt',size)
        big=self.db.start_upload(1,'very_large.bin',6*1024*1024*1024)
        self.assertEqual(big['size'],6*1024*1024*1024)
        with self.assertRaises(server.Failure):
            self.db.append_upload(1,big['id'],0,io.BytesIO(b'x'),server.UPLOAD_CHUNK+1)
        with self.assertRaises(server.Failure):self.db.finish_upload(1,big['id'])
        with self.assertRaises(server.Failure):self.db.cancel_upload(99,big['id'])
        self.db.cancel_upload(1,big['id'])
        self.assertFalse((self.db.files/(big['id']+'.part')).exists())
        expired=self.db.start_upload(1,'stale.dat',1)
        with self.db.tx() as c:
            c.execute('UPDATE uploads_pending SET updated=0 WHERE id=?',(expired['id'],))
        self.assertEqual(self.db.cleanup_pending_uploads(),1)
        self.assertFalse((self.db.files/(expired['id']+'.part')).exists())

    def test_chat_edit_author_only(self):
        self.db.create_user('viewer', 'password000', 'observer')
        user = {'id':2, 'role':'observer'}
        mid = self.db.send_chat(2,'Тест')
        self.db.edit_chat(user,mid,'Изменено')
        self.assertEqual(self.db.chat()[0]['body'],'Изменено')
        with self.assertRaises(server.Failure):
            self.db.edit_chat({'id':1},mid,'Нельзя')


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = server.DB(self.temp.name)
        self.db.create_user('admin', 'secretpassword', 'admin')
        self.httpd = server.Server(('127.0.0.1',0),self.db)
        self.port = self.httpd.server_address[1]
        self.th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.th.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self,method,path,data=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=6)
        hdr=headers or {}
        if isinstance(data,dict):
            data=json.dumps(data).encode('utf8')
            hdr.update({'Content-Type':'application/json'})
        conn.request(method,path,data,hdr)
        res=conn.getresponse()
        blob=res.read()
        result={'status':res.status,'headers':dict(res.getheaders()),'body':blob}
        conn.close()
        return result

    def test_resumable_upload_http_and_csrf(self):
        login=self.request('POST','/api/login',{'name':'admin','password':'secretpassword'})
        payload=json.loads(login['body']);cookie=login['headers']['Set-Cookie'].split(';',1)[0]
        h={'Cookie':cookie,'X-ITSO-CSRF':payload['csrf']}
        start=self.request('POST','/api/uploads',{'name':'photo.png','size':5},dict(h))
        self.assertEqual(start['status'],201)
        fid=json.loads(start['body'])['id']
        denied=self.request('PUT','/api/uploads/'+fid,b'abc',{'Cookie':cookie,'X-Upload-Offset':'0'})
        self.assertEqual(denied['status'],403)
        first=self.request('PUT','/api/uploads/'+fid,b'ab',dict(h,**{'X-Upload-Offset':'0'}))
        self.assertEqual(first['status'],200)
        self.assertEqual(json.loads(first['body'])['received'],2)
        fail=self.request('POST','/api/uploads/'+fid+'/finish',{},dict(h))
        self.assertEqual(fail['status'],409)
        second=self.request('PUT','/api/uploads/'+fid,b'cde',dict(h,**{'X-Upload-Offset':'2'}))
        self.assertEqual(second['status'],200)
        final=self.request('POST','/api/uploads/'+fid+'/finish',{},dict(h))
        self.assertEqual(final['status'],201)
        fetched=self.request('GET','/api/files/'+fid,None,{'Cookie':cookie})
        self.assertEqual(fetched['body'],b'abcde')

    def test_login_permissions_csrf_and_files(self):
        login=self.request('POST','/api/login',{'name':'admin','password':'secretpassword'})
        self.assertEqual(login['status'],200)
        csrf=json.loads(login['body'])['csrf']
        cookie=login['headers']['Set-Cookie'].split(';',1)[0]
        wrong=self.request('POST','/api/sheets',{'title':'X'},{'Cookie':cookie})
        self.assertEqual(wrong['status'],403)
        headers={'Cookie':cookie,'X-ITSO-CSRF':csrf}
        created=self.request('POST','/api/sheets',{'title':'Test'},dict(headers))
        self.assertEqual(created['status'],201)
        sid=json.loads(created['body'])['id']
        update=self.request('PUT','/api/sheets/{}/cell'.format(sid),{'row':0,'col':0,'value':'Тест','revision':0},dict(headers))
        self.assertEqual(update['status'],200)
        files=self.request('POST','/api/files',b'abc',dict(headers,**{'X-Filename':'test.txt'}))
        self.assertEqual(files['status'],201)
        fid=json.loads(files['body'])['id']
        downloaded=self.request('GET','/api/files/'+fid,None,{'Cookie':cookie,'Range':'bytes=1-2'})
        self.assertEqual(downloaded['status'],206)
        self.assertEqual(downloaded['body'],b'bc')
        self.assertIn('HttpOnly',login['headers']['Set-Cookie'])
        self.assertIn('SameSite=Strict',login['headers']['Set-Cookie'])


if __name__=='__main__':unittest.main()
