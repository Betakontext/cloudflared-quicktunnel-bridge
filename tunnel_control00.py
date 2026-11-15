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
   """Process one FTPS control poll without coupling root and Comfy actions."""
   # Do not open a connection for a disabled or incompletely configured watcher.
   if not getattr(self, 'enabled', False):
    return
   host=getattr(self, 'host', '')
   user=getattr(self, 'user', '')
   password=getattr(self, 'password', '')
   remote_dir=getattr(self, 'remote_dir', '')
   if not all(isinstance(value, str) and value for value in (host, user, password, remote_dir)):
    return

   def consume(f, path, name):
    """Delete a detected flag, returning whether its acknowledgement succeeded."""
    try:
     f.cwd(path)
     f.delete(name)
     return True
    except Exception:
     LOG.warning('Failed to consume control flag %s', name)
     print('FTPS control flag consumption failed: %s' % name, flush=True)
     return False

   try:
    with verified_ftps_connect(host, user, password) as f:
     root=remote_dir
     action=getattr(self, 'action', 'cloudflared')
     root_stop=os.getenv('FTPS_TUNNEL_STOP_FLAG', 'stop.flag')
     root_restart=getattr(self, 'flag_name', '')

     # Root flags control cloudflared only. A root stop always pauses for safety,
     # even if its remote acknowledgement cannot be deleted.
     if action in ('cloudflared', 'both') and _plain(root_stop):
      has_root_stop=self._check_path_for_name(f, root, root_stop)
      has_root_restart=(_plain(root_restart) and
                        self._check_path_for_name(f, root, root_restart))
      if has_root_stop:
       print('FTPS control flag detected: root stop', flush=True)
       if has_root_restart and root_restart != root_stop:
        print('FTPS control flag detected: conflicting root restart', flush=True)
        consume(f, root, root_restart)
       # Never process a restart in a stop cycle, regardless of deletion results.
       consume(f, root, root_stop)
       print('FTPS control action: pausing tunnel', flush=True)
       self.tw.request_pause()
      elif has_root_restart:
       print('FTPS control flag detected: root restart', flush=True)
       if consume(f, root, root_restart):
        print('FTPS control action: restarting tunnel', flush=True)
        self.tw.request_restart(manual=True)

     # Comfy flags live under the configured subdirectory and are independent
     # from root tunnel flags. Stop wins when both are present.
     comfy_subdir=getattr(self, 'comfy_subdir', '')
     if action in ('comfy', 'both') and _subdir(comfy_subdir):
      comfy_path=posixpath.join(root.rstrip('/') or '/', comfy_subdir)
      comfy_restart='restart.flag'
      comfy_stop=getattr(self, 'comfy_stop_flag', 'stop.flag') or 'stop.flag'
      has_comfy_restart=self._check_path_for_name(f, comfy_path, comfy_restart)
      has_comfy_stop=(_plain(comfy_stop) and
                      self._check_path_for_name(f, comfy_path, comfy_stop))
      if has_comfy_stop:
       print('FTPS control flag detected: Comfy stop', flush=True)
       if has_comfy_restart and comfy_restart != comfy_stop:
        print('FTPS control flag detected: conflicting Comfy restart', flush=True)
        consume(f, comfy_path, comfy_restart)
       # Do not run a Comfy restart in a Comfy stop cycle.
       if consume(f, comfy_path, comfy_stop):
        print('FTPS control action: stopping Comfy', flush=True)
        self._handle_comfy_stop()
      elif has_comfy_restart:
       print('FTPS control flag detected: Comfy restart', flush=True)
       if consume(f, comfy_path, comfy_restart):
        print('FTPS control action: restarting Comfy', flush=True)
        self._handle_comfy_restart()
   except Exception:
    LOG.warning('FTPS flag poll failed')
  FW._one_cycle=one
 ns['_tunnel_control_installed']=True
