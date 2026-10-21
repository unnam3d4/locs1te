"""Windows Service entrypoint. Build on Windows using PyInstaller and pywin32.

The Windows service hosts exactly one SQLite-backed HTTP server and does not
create its own account or expose an unauthenticated management endpoint.
"""
import json
from pathlib import Path
import sys
import threading
import logging
from logging.handlers import RotatingFileHandler

import win32event
import win32service
import win32serviceutil
import servicemanager

import server

ROOT=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
CONFIG=ROOT/'settings.json'

def read_config():
    defaults={'host':'127.0.0.1','port':3000,'data_dir':'C:/ITSO_DB/data'}
    if CONFIG.is_file():
        with CONFIG.open('r',encoding='utf-8') as f:defaults.update(json.load(f))
    return defaults

class ItsoService(win32serviceutil.ServiceFramework):
    _svc_name_='ITSO_DB'
    _svc_display_name_='БАЗА ДАННЫХ ИТСО'
    _svc_description_='Локальный HTTP-сервер SQLite для рабочих мест ИТСО.'

    def __init__(self,args):
        win32serviceutil.ServiceFramework.__init__(self,args)
        self.stop_event=threading.Event()
        self.service_stop=win32event.CreateEvent(None,0,0,None)
        self.httpd=None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_event.set()
        if self.httpd:self.httpd.shutdown()
        win32event.SetEvent(self.service_stop)

    def SvcDoRun(self):
        config=read_config()
        db=server.DB(config['data_dir'])
        logfile=Path(config['data_dir'])/'logs'
        logfile.mkdir(parents=True,exist_ok=True)
        logger=logging.getLogger('itso')
        logger.addHandler(RotatingFileHandler(str(logfile/'errors.log'),maxBytes=2*1024*1024,backupCount=3,encoding='utf-8'))
        logger.setLevel(logging.INFO)
        if not db.admin_exists():
            logger.error('Первый администратор не настроен; служба остановлена')
            return
        try:
            worker=threading.Thread(target=server.hourly_backup,args=(db,self.stop_event),daemon=True)
            worker.start()
            with server.Server((config['host'],int(config['port'])),db) as self.httpd:
                self.httpd.serve_forever(poll_interval=.5)
        except Exception:
            logger.exception('Ошибка сервера ИТСО')
            raise
        finally:
            self.stop_event.set()

if __name__=='__main__':
    if len(sys.argv)==1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ItsoService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(ItsoService)
