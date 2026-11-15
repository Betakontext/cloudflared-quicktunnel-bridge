"""Send FTPS control flags; requests are informational, not execution confirmation."""
import argparse,contextlib,ftplib,io,json,logging,math,os,posixpath,ssl,sys
from pathlib import Path,PurePosixPath
LOG=logging.getLogger(__name__)
def plain(x): return isinstance(x,str) and bool(x) and '/' not in x and '\\' not in x and x not in ('.','..') and not any(c in x for c in '\r\n\0')
def directory(x):
 p=PurePosixPath(x or '');return isinstance(x,str) and p.is_absolute() and '..' not in p.parts and not any(c in x for c in '\r\n\0')
def dotenv(path):
 d={}
 for raw in Path(path).read_text(encoding='utf8').splitlines():
  s=raw.strip()
  if not s or s.startswith('#') or '=' not in s:continue
  k,v=s.split('=',1);v=v.strip()
  if len(v)>1 and v[0]==v[-1] and v[0] in "'\"":v=v[1:-1]
  d[k.strip()]=v
 return d
def config(path,action,override=None):
 d={**os.environ,**dotenv(path)} # profile values deliberately win process environment
 fn=override or d.get('FTPS_TUNNEL_STOP_FLAG','stop.flag') if action=='stop' else override or d.get('FTPS_RESTART_FLAG','restart.flag')
 other=d.get('FTPS_RESTART_FLAG','restart.flag') if action=='stop' else d.get('FTPS_TUNNEL_STOP_FLAG','stop.flag')
 for k in ('FTPS_HOST','FTPS_USER','FTPS_PASS','FTPS_DIR'):
  if not d.get(k) or any(c in d[k] for c in '\r\n\0'):raise ValueError(f'{path}: missing/unsafe {k}')
 if not directory(d['FTPS_DIR']) or not plain(fn) or not plain(other) or fn==other:raise ValueError(f'{path}: unsafe directory or flag name')
 def number(k,default):
  try:v=float(d.get(k,default));assert math.isfinite(v) and v>0;return v
  except:raise ValueError(f'{path}: invalid {k}')
 return dict(host=d['FTPS_HOST'],user=d['FTPS_USER'],password=d['FTPS_PASS'],directory=d['FTPS_DIR'],flag=fn,opposite=other,retries=int(number('FTPS_RETRIES',5)),timeout=number('FTPS_TIMEOUT',30))
@contextlib.contextmanager
def ftps(c):
 f=None
 try:
  f=ftplib.FTP_TLS(context=ssl.create_default_context(),timeout=c['timeout']);f.connect(c['host'],21);f.login(c['user'],c['password']);f.prot_p();f.set_pasv(True);f.cwd(c['directory']);yield f
 finally:
  if f:
   try:f.quit()
   except Exception:
    try:f.close()
    except:pass
def transfer(c,action,message=''):
 payload=json.dumps({'requested_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'action':action,'message':message}).encode()
 with ftps(c) as f:
  names={posixpath.basename(x.rstrip('/')) for x in f.nlst()}
  if c['opposite'] in names:f.delete(c['opposite']) # deletion errors are real transfer failures
  f.storbinary('STOR '+c['flag'],io.BytesIO(payload))
def main(action,argv=None):
 ap=argparse.ArgumentParser();ap.add_argument('--env-file',action='append');ap.add_argument('--retries',type=int);ap.add_argument('--ftps-timeout',type=float);ap.add_argument('--message',default='');ap.add_argument('--flag-name');a=ap.parse_args(argv)
 base=Path(__file__).resolve().parent; files=[base/'.env',base/'.env.files'] if not a.env_file else [Path(x) for x in a.env_file]
 try:
  cs=[config(x,action,a.flag_name) for x in files]
  if a.retries is not None:
   if a.retries<1:raise ValueError('invalid --retries')
   for c in cs:c['retries']=a.retries
  if a.ftps_timeout is not None:
   if not math.isfinite(a.ftps_timeout) or a.ftps_timeout<=0:raise ValueError('invalid --ftps-timeout')
   for c in cs:c['timeout']=a.ftps_timeout
  seen={}; unique=[]
  for c in cs:
   key=(c['host'],c['user'],c['directory'])
   if key in seen:
    if seen[key]['flag']!=c['flag']:raise ValueError('conflicting filenames for same destination')
    continue # identical target: one unambiguous request only
   seen[key]=c; unique.append(c)
  cs=unique
 except Exception as e:print('configuration error:',e,file=sys.stderr);return 2
 bad=False
 for c in cs:
  ok=False
  for _ in range(c['retries']):
   try:transfer(c,action,a.message);ok=True;break
   except Exception:pass
  if not ok:bad=True;print('FTPS transfer failed for configured target',file=sys.stderr)
 return 1 if bad else 0
if __name__=='__main__':raise SystemExit(main('stop' if Path(sys.argv[0]).stem.startswith('stop') else 'restart'))
