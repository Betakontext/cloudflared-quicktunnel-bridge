"""Additive, race-safe cloudflared control patch. Call install(globals()) after classes."""
import contextlib, ftplib, io, logging, os, posixpath, queue, re, ssl, subprocess, threading, time
from pathlib import PurePosixPath
LOG=logging.getLogger(__name__); URL_RE=re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com\b')
def _plain(s): return isinstance(s,str) and bool(s) and '/' not in s and '\\' not in s and s not in ('.','..') and '\x00' not in s and '\r' not in s and '\n' not in s
def _subdir(s):
 p=PurePosixPath(s or '')
 return bool(s) and not p.is_absolute() and '..' not in p.parts and str(p) not in ('.','') and '\x00' not in s and '\r' not in s and '\n' not in s
def _join(a,b): return posixpath.join(a.rstrip('/'),b)
@contextlib.contextmanager
def verified_ftps_connect(host,user,password,timeout=25):
 """Certificate-verifying FTPS connection; no credentials are logged."""
 f=None
 try:
  f=ftplib.FTP_TLS(context=ssl.create_default_context(),timeout=timeout); f.connect(host,21); f.login(user,password); f.prot_p(); f.set_pasv(True); yield f
 finally:
  if f:
   try: f.quit()
   except Exception:
    try: f.close()
    except Exception: pass
def install(ns):
 """Patch intact source classes in *ns*. Idempotent; never starts a service."""
 if ns.get('_tunnel_control_installed'): return
 TW=ns.get('TunnelWriter'); FW=ns.get('FtpsFlagWatcher')
 if TW is None: raise ValueError('TunnelWriter must be defined before install(globals())')
 ns['ftps_connect']=verified_ftps_connect
 oldinit=TW.__init__
 def init(self,*a,**kw):
  oldinit(self,*a,**kw); self._ctl=threading.Condition(threading.RLock()); self.desired_enabled=True; self.generation=0; self._pending=-1; self._empty_sent=False; self._reader_q=queue.Queue(); self._owner_proc=None
 def paused(self):
  with self._ctl:return not self.desired_enabled
 def pause(self):
  with self._ctl:self.desired_enabled=False;self._pending=-1;self.generation+=1;self._ctl.notify_all()
 def restart(self,manual=False):
  with self._ctl:
   if not self.desired_enabled and not manual:return False
   if manual:self.desired_enabled=True
   self.generation+=1;self._pending=self.generation;self._ctl.notify_all();return True
 def stop(self):
  self.stop_evt.set()
  with self._ctl:self.desired_enabled=False;self._ctl.notify_all()
 def running(self):
  p=getattr(self,'_owner_proc',None); return bool(p and p.poll() is None)
 def invalidate(self):
  if not self._empty_sent:
   self._empty_sent=True; self.current_url=''
   try:self.write_urls_local(''); self.upload_remote_if_enabled()
   except Exception: LOG.warning('URL invalidation publication failed')
 def endproc(self,p):
  if not p:return True
  if p.poll() is None:
   try:p.terminate();p.wait(timeout=3)
   except Exception:
    try:p.kill();p.wait(timeout=3)
    except Exception: LOG.error('child did not stop; refusing duplicate spawn');return False
  return True
 def reader(p,q):
  try:
   for line in iter(p.stdout.readline,''): q.put(line)
  except Exception: pass
 def loop(self):
  backoff=2
  while not self.stop_evt.is_set():
   with self._ctl:
    enabled=self.desired_enabled; gen=self.generation
   if not enabled:
    invalidate(self)
    with self._ctl:self._ctl.wait(0.5)
    continue
   try:
    p=subprocess.Popen([self.cf_bin,'tunnel','--no-autoupdate','--protocol',self.protocol,'--url',self.comfy_url],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')
   except Exception:
    LOG.warning('cloudflared spawn failed')
    with self._ctl:self._ctl.wait(backoff)
    backoff=min(30,backoff*2);continue
   self._owner_proc=p; self.proc=p; self._reader_q=queue.Queue(); threading.Thread(target=reader,args=(p,self._reader_q),daemon=True).start(); changed=False
   while not self.stop_evt.is_set():
    with self._ctl: changed=(not self.desired_enabled or self.generation!=gen)
    if changed: break
    try: line=self._reader_q.get(timeout=.25)
    except queue.Empty:
     if p.poll() is not None:break
     continue
    m=URL_RE.search(line)
    if m:
     with self._ctl:
      if self.desired_enabled and self.generation==gen:
       self.current_url=m.group(0);self._empty_sent=False
       try:self.write_urls_local(self.current_url);self.upload_remote_if_enabled()
       except Exception:LOG.warning('URL publication failed')
       backoff=2
   invalidate(self); ok=endproc(self,p);self._owner_proc=None;self.proc=None
   if not ok: return
   if not self.stop_evt.is_set() and self.desired_enabled:
    with self._ctl:
     if self.generation == gen: self._ctl.wait(backoff)
    backoff=min(30,backoff*2)
 TW.__init__=init;TW.request_pause=pause;TW.request_restart=restart;TW.stop=stop;TW.is_running=running;TW.is_paused=paused;TW.run_forever=loop
 if FW:
  def one(self):
   # Root controls affect tunnel only; Comfy controls retain their separate behavior.
   try:
    with verified_ftps_connect(self.host,self.user,self.password) as f:
     root=getattr(self,'remote_dir','') or '/'; action=getattr(self,'action','cloudflared'); stopn=os.getenv('FTPS_TUNNEL_STOP_FLAG','stop.flag'); rn=getattr(self,'flag_name','')
     if action in ('cloudflared','both') and _plain(stopn):
      hasstop=self._check_path_for_name(f,root,stopn); hasrestart=_plain(rn) and self._check_path_for_name(f,root,rn)
      if hasstop:
       # acknowledge only after deletion; stop wins and conflicting restart is consumed best effort
       try:self._delete_flag(f,root,stopn)
       except Exception: LOG.warning('failed to consume stop flag')
       else:
        self.tw.request_pause()
        if hasrestart:
         try:self._delete_flag(f,root,rn)
         except Exception:LOG.warning('failed to consume conflicting restart flag')
      elif hasrestart:
       try:self._delete_flag(f,root,rn)
       except Exception:LOG.warning('failed to consume restart flag')
       else:self.tw.request_restart(manual=True)
     if action in ('comfy','both') and _subdir(getattr(self,'comfy_subdir','')):
      # original separate semantics: restart then stop; root flags never invoke these.
      self._handle_comfy_restart(); self._handle_comfy_stop()
   except Exception: LOG.warning('FTPS flag poll failed')
  FW._one_cycle=one
 ns['_tunnel_control_installed']=True
