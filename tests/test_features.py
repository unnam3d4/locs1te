"""Expanded database, copy/import/export/backup and role API tests."""
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=server.DB(Path(self.temp.name)/'data')
        self.db.create_user('admin','password123','admin')
        self.db.create_user('viewer','password123','observer')

    def test_sheet_clone_move_delete_and_csv(self):
        sid=self.db.add_sheet('Источник','Корпус 1')
        self.db.edit_cell(sid,0,0,'Камера, 1',{'bold':True},0)
        self.db.edit_cell(sid,2,3,'Строка\nс переносом',{},0)
        text=self.db.csv_export(sid).decode('utf-8-sig')
        self.assertEqual(list(csv.reader(io.StringIO(text)))[0][0],'Камера, 1')
        copied=self.db.clone_sheet(sid,'Копия','Корпус 2')
        self.assertEqual(len(self.db.sheet(copied)['cells']),2)
        self.db.update_sheet(copied,'Копия 2','Другое')
        self.assertEqual(self.db.sheet(copied)['sheet']['title'],'Копия 2')
        self.db.csv_import(copied,b'new,text\nhello,world')
        self.assertEqual([x['value'] for x in self.db.sheet(copied)['cells']],['new','text','hello','world'])
        self.db.delete_sheet(copied)
        self.assertEqual(len(self.db.sheet(sid)['cells']),2)
        with self.assertRaises(server.Failure):self.db.sheet(copied)

    def test_map_copy_with_links_and_attachment(self):
        mid=self.db.add_map('Карта')
        a=self.db.save_object(mid,{'x':1,'y':2,'properties':{'Название':'a'}},0)['id']
        b=self.db.save_object(mid,{'x':3,'y':4,'properties':{'Название':'b'}},0)['id']
        self.db.add_link(mid,a,b)
        f=self.db.store_upload(1,'file.txt',io.BytesIO(b'x'),1)
        self.db.attach_file(mid,a,f['id'])
        self.assertEqual(len(self.db.object_files(mid,a)),1)
        self.db.detach_file(mid,a,f['id'])
        self.assertEqual(self.db.object_files(mid,a),[])
        self.db.attach_file(mid,a,f['id'])
        cloned=self.db.clone_map(mid,'Копия')
        self.assertEqual(len(self.db.map(cloned)['objects']),2)
        self.assertEqual(len(self.db.map(cloned)['links']),1)
        self.assertEqual(sum(len(self.db.object_files(cloned,o['id'])) for o in self.db.map(cloned)['objects']),0)
        self.db.update_map(cloned,'Копия исправлена','Другое')
        self.db.delete_map(mid)
        self.assertEqual(len(self.db.list_files()),1)
        with self.db.tx() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM file_refs').fetchone()[0],0)

    def test_chat_file_unread_and_history(self):
        fid=self.db.store_upload(1,'chat.txt',io.BytesIO(b'hello'),5)['id']
        mid=self.db.send_chat(1,'Привет',fid)
        self.assertEqual(self.db.unread_chat(2),1)
        self.db.mark_chat_read(2,mid)
        self.assertEqual(self.db.unread_chat(2),0)
        with self.db.tx() as c:
            self.assertEqual(c.execute('SELECT file_id FROM file_refs WHERE chat_id=?',(mid,)).fetchone()[0],fid)
        self.assertEqual(self.db.chat()[0]['file_name'],'chat.txt')
        self.db.delete_chat(mid)
        self.assertEqual(self.db.list_files()[0]['id'],fid)
        for i in range(25):self.db.save_search(1,'query {}'.format(i))
        self.assertEqual(len(self.db.history(1)),20)
        self.db.delete_history(1)
        self.assertEqual(self.db.history(1),[])

    def test_full_archive_roundtrip_and_tampering(self):
        sid=self.db.add_sheet('Original')
        self.db.edit_cell(sid,0,0,'data',{},0)
        saved=self.db.store_upload(1,'file.pdf',io.BytesIO(b'%PDF'),4)
        archive=Path(self.temp.name)/'backup.zip'
        self.db.export_archive(archive)
        self.db.delete_sheet(sid)
        self.db.delete_file(saved['id'])
        self.db.restore_archive(archive)
        fresh=server.DB(Path(self.temp.name)/'data')
        self.assertEqual(fresh.sheet(sid)['cells'][0]['value'],'data')
        self.assertEqual((fresh.files/saved['id']).read_bytes(),b'%PDF')
        archive_bad=Path(self.temp.name)/'tampered.zip'
        with zipfile.ZipFile(archive) as zi,zipfile.ZipFile(archive_bad,'w') as zo:
            for i in zi.infolist():
                data=zi.read(i.filename)
                if i.filename.startswith('uploads/'):data=b'evil'
                zo.writestr(i.filename,data)
        with self.assertRaises(server.Failure):fresh.restore_archive(archive_bad)
        self.assertEqual((fresh.files/saved['id']).read_bytes(),b'%PDF')

    def test_invalid_csv_rolls_back(self):
        sid=self.db.add_sheet('Test');self.db.edit_cell(sid,0,0,'Keep',{},0)
        with self.assertRaises(server.Failure):self.db.csv_import(sid,b'\xff\xfe\xff')
        self.assertEqual(self.db.sheet(sid)['cells'][0]['value'],'Keep')

if __name__=='__main__':unittest.main()

class MapImportSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.db=server.DB(Path(self.folder.name)/'data')
        self.db.create_user('admin','password123','admin')

    def test_snapshot_restore_and_itmap_import(self):
        mid=self.db.add_map('1 этаж')
        obj1=self.db.save_object(mid,{'x':5,'y':6,'properties':{'Название':'Дверь'},'appearance':{'kind':'door'}},0)['id']
        obj2=self.db.save_object(mid,{'x':90,'y':6,'properties':{'Название':'Камера'}},0)['id']
        self.db.add_link(mid,obj1,obj2)
        snap=self.db.snapshot_map(mid,'До изменений')
        doc=self.db.map_export(mid)
        self.assertEqual(doc['format'],'itmap')
        copy=self.db.import_map(doc,'Импорт')
        self.assertEqual(len(self.db.map(copy)['links']),1)
        self.assertNotEqual(self.db.map(copy)['objects'][0]['id'],obj1)
        self.db.delete_object(mid,obj2,True)
        self.assertEqual(len(self.db.map(mid)['objects']),1)
        self.db.restore_map_snapshot(mid,snap)
        self.assertEqual(len(self.db.map(mid)['objects']),2)
        self.assertEqual(len(self.db.map(mid)['links']),1)
        with self.assertRaises(server.Failure):self.db.import_map({'format':'itmap','version':1,'objects':[{'id':'../escape'}]})
        self.db.delete_map(mid)
        with self.db.tx() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM map_snapshots').fetchone()[0],0)

class HttpFeatureTests(unittest.TestCase):
    def setUp(self):
        import http.client
        self.http_client=http.client
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.db=server.DB(Path(self.folder.name)/'data')
        self.db.create_user('admin','password123','admin')
        self.db.create_user('viewer','password123','observer')
        self.srv=server.Server(('127.0.0.1',0),self.db)
        import threading
        self.thread=threading.Thread(target=self.srv.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.srv.server_close);self.addCleanup(self.srv.shutdown)

    def request(self,method,path,body=None,headers=None):
        client=self.http_client.HTTPConnection('127.0.0.1',self.srv.server_address[1],timeout=6)
        h=dict(headers or {})
        if isinstance(body,dict):body=json.dumps(body).encode();h['Content-Type']='application/json'
        client.request(method,path,body,h)
        r=client.getresponse();data=r.read();status=r.status;client.close()
        return status,data,dict(r.getheaders())

    def login(self,name):
        status,body,headers=self.request('POST','/api/login',{'name':name,'password':'password123'})
        obj=json.loads(body)
        return {'Cookie':headers['Set-Cookie'].split(';')[0],'X-ITSO-CSRF':obj['csrf']}

    def test_roles_and_endpoints(self):
        admin=self.login('admin');observer=self.login('viewer')
        status,data,_=self.request('POST','/api/sheets',{'title':'Импорт'},admin)
        sid=json.loads(data)['id'];self.assertEqual(status,201)
        status,data,_=self.request('POST','/api/sheets/'+str(sid)+'/csv',b'a,b\n1,2',{'Cookie':admin['Cookie'],'X-ITSO-CSRF':admin['X-ITSO-CSRF'],'Content-Type':'text/csv'})
        self.assertEqual(status,200)
        status,data,_=self.request('GET','/api/sheets/'+str(sid)+'/csv',headers=observer)
        self.assertEqual(status,200);self.assertIn(b'a,b',data)
        status,_,_=self.request('DELETE','/api/sheets/'+str(sid),{},observer)
        self.assertEqual(status,403)
        status,_,_=self.request('DELETE','/api/sheets/'+str(sid),{},admin)
        self.assertEqual(status,200)
        status,_,_=self.request('POST','/api/chat/read',{'last_id':0},observer)
        self.assertEqual(status,200)
        status,body,_=self.request('GET','/api/chat/unread',headers=observer)
        self.assertEqual(json.loads(body)['count'],0)
        status,_,_=self.request('POST','/api/maps/import',{'format':'itmap','version':1,'objects':[],'links':[]},observer)
        self.assertEqual(status,403)
        status,data,_=self.request('POST','/api/maps/import',{'format':'itmap','version':1,'objects':[],'links':[]},admin)
        self.assertEqual(status,201)
        mid=json.loads(data)['id']
        status,data,_=self.request('GET','/api/maps/'+str(mid)+'/export',headers=observer)
        self.assertEqual(json.loads(data)['format'],'itmap')

class BindingTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.db=server.DB(Path(self.folder.name)/'data')
        self.db.create_user('admin','password123','admin')
    def test_bidirectional_properties_and_conflicts(self):
        mid=self.db.add_map('Схема');sid=self.db.add_sheet('Таблица IP')
        self.db.edit_cell(sid,0,1,'192.168.1.2',{},0)
        a=self.db.save_object(mid,{'x':0,'y':0,'properties':{'Название':'А'}},0)['id']
        b=self.db.save_object(mid,{'x':10,'y':10,'properties':{'Название':'Б'}},0)['id']
        self.db.bind_property(mid,a,'IP',sid,0,1,'table')
        self.db.bind_property(mid,b,'IP',sid,0,1,'table')
        objs={o['id']:o for o in self.db.map(mid)['objects']}
        self.assertEqual(objs[a]['properties']['IP'],'192.168.1.2')
        self.assertEqual(len(self.db.bindings(mid,a)),1)
        self.db.edit_cell(sid,0,1,'192.168.1.3',{},1)
        objs={o['id']:o for o in self.db.map(mid)['objects']}
        self.assertEqual(objs[b]['properties']['IP'],'192.168.1.3')
        with self.assertRaises(server.Failure) as err:
            self.db.save_object(mid,{'id':a,'x':15,'y':2,'properties':{'IP':'x'}},1)
        self.assertEqual(err.exception.status,409)
        current=objs[a]
        newprop=dict(current['properties']);newprop['IP']='192.168.1.4'
        self.db.save_object(mid,{'id':a,'x':2,'y':1,'properties':newprop,'appearance':{}},current['revision'])
        self.assertEqual(self.db.sheet(sid)['cells'][0]['value'],'192.168.1.4')
        peer={o['id']:o for o in self.db.map(mid)['objects']}[b]
        self.assertEqual(peer['properties']['IP'],'192.168.1.4')
        self.db.unbind_property(mid,a,'IP')
        self.db.delete_sheet(sid)
        self.assertEqual(self.db.bindings(mid,b),[])
