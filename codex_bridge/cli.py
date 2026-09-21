"""Local administration. Pairing/scope changes are deliberately not peer tools."""
from __future__ import annotations
import argparse
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

from .core import Bridge, BridgeError, canonical, identifier, now, windows_file_retry


def config_path():
    return Path(os.environ.get('CODEX_BRIDGE_CONFIG', str(Path.home()/'.codex-bridge'/'config.json'))).expanduser().resolve()


def protect(directory):
    directory.mkdir(parents=True,exist_ok=True)
    if os.name=='nt':
        query=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,check=True)
        import csv
        sid=next(csv.reader([query.stdout.strip()]))[1]
        subprocess.run(['icacls',str(directory),'/inheritance:r','/grant:r',f'*{sid}:(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True)
    else:
        directory.chmod(0o700)


def save(path,data):
    path=Path(path)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        private_invitation(temporary,data)
        windows_file_retry(lambda: os.replace(temporary,path))
    finally:
        if temporary.exists(): temporary.unlink()


def process_alive(pid):
    if os.name=='nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        handle=kernel.OpenProcess(0x1000,False,int(pid))
        if not handle: return False
        try:
            code=ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle),ctypes.byref(code))) and code.value==259
        finally: kernel.CloseHandle(ctypes.c_void_p(handle))
    try: os.kill(pid,0); return True
    except OSError: return False


def private_invitation(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as handle:
        if os.name=='nt':
            import csv
            query=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,check=True)
            sid=next(csv.reader([query.stdout.strip()]))[1]
            subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',f'*{sid}:F','*S-1-5-18:F'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True)
        else: path.chmod(0o600)
        json.dump(value,handle,indent=2)


def read(path):
    return json.loads(windows_file_retry(lambda: path.read_text(encoding='utf-8-sig')))


def read_config(path):
    cfg=read(path)
    if not isinstance(cfg,dict) or cfg.get('version')!=1:
        raise BridgeError('configuration','Expected a version 1 bridge configuration')
    identifier(cfg.get('peer_id'),'peer_id')
    port_number(cfg.get('listen_port'),'listen_port')
    if not isinstance(cfg.get('local_token'),str) or len(cfg['local_token'])<32:
        raise BridgeError('configuration','Initialize a local bridge credential before starting')
    for key in ('state_dir','codex_path'):
        if not isinstance(cfg.get(key),str) or not Path(cfg[key]).is_absolute():
            raise BridgeError('configuration',key+' must be an absolute local path')
    return cfg


def port_number(value,label):
    if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=65535:
        raise BridgeError('configuration',label+' must be an integer from 1 to 65535')
    return value


@contextmanager
def process_lock(path):
    """Hold an OS lock before opening the request journal or starting a supervisor."""
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell()==0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise BridgeError('already_running','This configuration already has a process running or stopping') from exc
        try:
            yield
        finally:
            if os.name=='nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def valid_response(response):
    if not isinstance(response,dict) or not isinstance(response.get('ok'),bool):
        raise BridgeError('protocol_error','The local endpoint did not return a bridge response')
    if not response['ok']:
        error=response.get('error',{})
        raise BridgeError(error.get('code','local_error'),error.get('message','Local bridge rejected the request'),error.get('retryable',False))
    return response


def call(path,method,params=None,timeout=40):
    cfg=read_config(path)
    req=urllib.request.Request('http://127.0.0.1:'+str(cfg['listen_port'])+'/rpc',
        data=canonical({'method':method,'params':params or {}}).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['local_token']})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req,timeout=timeout) as reply:
        return json.load(reply)


def launch(path,mode):
    cfg=read_config(path)
    state=Path(cfg['state_dir'])
    state.mkdir(parents=True,exist_ok=True)
    cmd=[sys.executable,'-m','codex_bridge.cli','--config',str(path),mode]
    source=str(Path(__file__).resolve().parents[1])
    env=os.environ.copy()
    env['PYTHONPATH']=source
    options={'cwd':source,'env':env,'stdin':subprocess.DEVNULL}
    if os.name=='nt':
        options['creationflags']=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_CLIENT'):
            # Windows sshd owns a job which terminates normal descendants at logout.
            options['creationflags'] |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    else: options['start_new_session']=True
    with (state/(mode+'.stdout.log')).open('ab') as out, (state/(mode+'.stderr.log')).open('ab') as err:
        proc=subprocess.Popen(cmd,stdout=out,stderr=err,**options)
    # Only the child which acquires the lifetime lock publishes its PID. A losing
    # simultaneous launcher must not overwrite the running owner's record.
    return proc


def powershell(script,timeout=30):
    executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
    encoded=base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result=subprocess.run([str(executable),'-NoProfile','-NonInteractive','-WindowStyle','Hidden',
        '-EncodedCommand',encoded],capture_output=True,text=True,timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise BridgeError('interactive_start_failed',(result.stderr or result.stdout or 'Windows scheduled task failed').strip()[:1500])
    return result.stdout


def ps_literal(value):
    return "'"+str(value).replace("'","''")+"'"


def launch_interactive(path):
    if os.name!='nt': raise BridgeError('configuration','Interactive desktop launch is supported only on Windows')
    cfg=read_config(path)
    state=Path(cfg['state_dir'])
    state.mkdir(parents=True,exist_ok=True)
    task_name='CodexBridge-'+hashlib.sha256(str(path).casefold().encode()).hexdigest()[:16]
    wrapper=state/'interactive-serve.ps1'
    source=str(Path(__file__).resolve().parents[1])
    arguments=subprocess.list2cmdline(['-m','codex_bridge.cli','--config',str(path),'serve'])
    wrapper.write_text("\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        '$env:PYTHONPATH = '+ps_literal(source),
        'try {',
        '  $child = Start-Process -FilePath '+ps_literal(sys.executable)+' -ArgumentList '+ps_literal(arguments)
        +' -WorkingDirectory '+ps_literal(source)+' -WindowStyle Hidden -PassThru'
        +' -RedirectStandardOutput '+ps_literal(state/'serve.stdout.log')
        +' -RedirectStandardError '+ps_literal(state/'serve.stderr.log'),
        '  $child.WaitForExit()',
        '} finally {',
        '  Unregister-ScheduledTask -TaskName '+ps_literal(task_name)+' -Confirm:$false -ErrorAction SilentlyContinue',
        '}',
    ])+'\n',encoding='utf-8-sig')
    action_args=subprocess.list2cmdline(['-NoProfile','-NonInteractive','-WindowStyle','Hidden','-File',str(wrapper)])
    script="\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        '$taskName = '+ps_literal(task_name),
        '$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue',
        "if ($existing -and $existing.State -eq 'Running') { throw 'This interactive bridge task is already running; inspect its logs or stop it before retrying.' }",
        'if ($existing) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }',
        '$identity = [Security.Principal.WindowsIdentity]::GetCurrent()',
        '$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited',
        '$action = New-ScheduledTaskAction -Execute '+ps_literal(Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe')
        +' -Argument '+ps_literal(action_args)+' -WorkingDirectory '+ps_literal(source),
        '$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries',
        "Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Description 'Codex Bridge on-demand local daemon; no password, elevation, or startup trigger.' | Out-Null",
        'Start-ScheduledTask -TaskName $taskName',
    ])
    powershell(script)
    save(state/'interactive-task.json',{'task_name':task_name,'config':str(path),'created_at':now()})
    return {'launch_mode':'interactive','task_name':task_name}


def wait_for_exit(pid_file,seconds=15):
    if not pid_file.exists(): return True
    pid=read(pid_file).get('pid')
    deadline=time.monotonic()+seconds
    while pid and process_alive(pid) and time.monotonic()<deadline:
        time.sleep(.1)
    return not pid or not process_alive(pid)


def start_daemon(path,interactive=None):
    cfg=read_config(path)
    if interactive is not None:
        if interactive and os.name!='nt': raise BridgeError('configuration','Interactive launch requires Windows')
        cfg['launch_mode']='interactive' if interactive else 'background'
        save(path,cfg)
    try:
        # This probe must never wait for peers or SSH; a healthy offline bridge is running.
        existing=valid_response(call(path,'session_list',{},timeout=2))
        return {'already_running':True,'launch_mode':cfg.get('launch_mode','background'),'status':existing}
    except OSError:
        pass
    mode=cfg.get('launch_mode','background')
    if mode not in ('background','interactive'): raise BridgeError('configuration','launch_mode must be background or interactive')
    process=None
    if mode=='interactive':
        result=launch_interactive(path)
    else:
        process=launch(path,'serve')
        result={'pid':process.pid,'launch_mode':'background'}
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        if process and process.poll() is not None:
            # A simultaneous start may have won the lifetime lock; use its healthy endpoint.
            try:
                valid_response(call(path,'session_list',{},timeout=2))
                return {'already_running':True,'launch_mode':mode}
            except OSError:
                raise BridgeError('startup_failed','Bridge exited; inspect state/serve.stderr.log')
        try:
            valid_response(call(path,'session_list',{},timeout=2))
            return {'started':True,'ready':True,**result}
        except OSError:
            time.sleep(.25)
    raise BridgeError('startup_timeout','Bridge did not become ready; inspect state/serve.stderr.log. Interactive mode requires this Windows user to be signed in.')


def transport_args(cfg):
    t=cfg['ssh_transport']
    if not isinstance(t,dict): raise BridgeError('configuration','ssh_transport must be an object')
    ssh=t.get('ssh_exe') or shutil.which('ssh')
    if not ssh: raise BridgeError('configuration','SSH executable not configured')
    for key in ('identity_file','known_hosts_file'):
        if not Path(t[key]).is_file(): raise BridgeError('configuration',key+' is missing')
    for key in ('ssh_port','local_peer_port','remote_bridge_port','remote_peer_port'):
        port_number(t.get(key),key)
    port_number(cfg.get('listen_port'),'listen_port')
    for key in ('ssh_host','username','host_key_alias'):
        value=t.get(key)
        if not isinstance(value,str) or not value or value.startswith('-') or any(char.isspace() or char=='\0' for char in value):
            raise BridgeError('configuration',key+' must be a nonempty SSH name without whitespace')
    if t['local_peer_port']==cfg['listen_port']:
        raise BridgeError('configuration','local_peer_port must differ from this daemon listen_port')
    if t['remote_peer_port']==t['remote_bridge_port']:
        raise BridgeError('configuration','remote_peer_port must differ from the remote daemon port')
    args=[ssh,'-F','none','-N','-T','-p',str(t['ssh_port']),'-l',t['username'],'-i',t['identity_file'],
        '-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes','-o','BatchMode=yes',
        '-o','UserKnownHostsFile='+t['known_hosts_file'],'-o','HostKeyAlias='+t['host_key_alias'],
        '-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=10','-o','ServerAliveCountMax=3',
        '-o','ConnectTimeout=10',
        '-L',f"127.0.0.1:{t['local_peer_port']}:127.0.0.1:{t['remote_bridge_port']}",
        '-R',f"127.0.0.1:{t['remote_peer_port']}:127.0.0.1:{cfg['listen_port']}",t['ssh_host']]
    return args


def supervise_transport(path):
    cfg=read_config(path)
    state=Path(cfg['state_dir'])
    stop=state/'transport.stop'
    if stop.exists(): raise BridgeError('stopped','Transport stop marker exists')
    delay=1
    while not stop.exists():
        cfg=read_config(path)
        if not cfg.get('ssh_transport',{}).get('enabled',False): break
        args=transport_args(cfg)
        print(canonical({'event':'transport_connecting','timestamp':now()}),flush=True)
        options={'stdin':subprocess.DEVNULL}
        if os.name=='nt': options['creationflags']=subprocess.CREATE_NO_WINDOW
        started=time.monotonic()
        child=subprocess.Popen(args,**options)
        save(state/'transport-child.pid.json',{'pid':child.pid,'created_at':now()})
        try:
            while child.poll() is None and not stop.exists(): time.sleep(0.25)
        finally:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        print(canonical({'event':'transport_closed','exit_code':child.returncode,'timestamp':now()}),flush=True)
        if time.monotonic()-started>30: delay=1
        deadline=time.monotonic()+delay
        while time.monotonic()<deadline and not stop.exists(): time.sleep(0.25)
        delay=min(30,delay*2)
    print(canonical({'event':'transport_stopped','timestamp':now()}),flush=True)


def transport_status(path):
    cfg=read_config(path)
    state=Path(cfg['state_dir'])
    result={'enabled':bool(cfg.get('ssh_transport',{}).get('enabled',False)),
        'stop_requested':(state/'transport.stop').exists(),
        'note':'Process state alone does not verify SSH forwarding; use peer_status for end-to-end reachability.'}
    for label,name in [('supervisor','transport-run.pid.json'),('ssh_child','transport-child.pid.json')]:
        record=read(state/name) if (state/name).exists() else {}
        pid=record.get('pid')
        result[label]={'pid':pid,'running':bool(pid and process_alive(pid))}
    return result


async def configure_tool_approvals(path,marketplace,mode):
    from .codex_adapter import CodexAdapter
    from .tools import TOOLS
    identifier(marketplace,'marketplace')
    adapter=CodexAdapter(read(path)['codex_path'])
    try:
        await adapter.start()
        prefix='plugins.codex-bridge@'+marketplace+'.mcp_servers.codex_bridge.tools.'
        result=await adapter._rpc('config/batchWrite',{'edits':[
            {'keyPath':prefix+tool['name']+'.approval_mode','value':mode,'mergeStrategy':'replace'}
            for tool in TOOLS], 'reloadUserConfig':True})
        return {'plugin':'codex-bridge@'+marketplace,'approval_mode':mode,'tool_count':len(TOOLS),
                'status':result.get('status'),'note':'Only this plugin tool policy changed; peer/project restrictions still apply.'}
    finally:
        await adapter.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description='Codex Bridge local setup and administration')
    parser.add_argument('--config',type=Path,default=config_path())
    sub=parser.add_subparsers(dest='command',required=True)
    setup=sub.add_parser('setup',help='Detect the local runtime and preserve or create this computer configuration')
    setup.add_argument('--peer-id'); setup.add_argument('--codex'); setup.add_argument('--port',type=int)
    setup.add_argument('--batch',action='store_true'); setup.add_argument('--register-mcp',action='store_true')
    setup.add_argument('--guided',action='store_true',help='Continue with pairing and project prompts after local setup')
    pair=sub.add_parser('pair-setup',help='Guided private invitation exchange; credential contents are never printed')
    pair.add_argument('--peer-id'); pair.add_argument('--url'); pair.add_argument('--export-file',type=Path)
    pair.add_argument('--import-file',type=Path); pair.add_argument('--consume-import',action='store_true'); pair.add_argument('--batch',action='store_true')
    select=sub.add_parser('project-select',help='Select or update a workspace while preserving unrelated settings and local actions')
    select.add_argument('--id'); select.add_argument('--name'); select.add_argument('--workspace',type=Path)
    select.add_argument('--peer-id'); select.add_argument('--export-root',type=Path); select.add_argument('--import-root',type=Path)
    select.add_argument('--policy',choices=['read-only','workspace-write']); select.add_argument('--operations'); select.add_argument('--peer-project-id')
    select.add_argument('--batch',action='store_true')
    route=sub.add_parser('transport-config',help='Select an existing SSH route without copying keys or changing SSH/firewall configuration')
    route.add_argument('--peer-id'); route.add_argument('--batch',action='store_true')
    for key in ('ssh-host','username','identity-file','known-hosts-file','host-key-alias','ssh-exe'):
        route.add_argument('--'+key)
    for key in ('ssh-port','local-peer-port','remote-bridge-port','remote-peer-port'):
        route.add_argument('--'+key,type=int)
    preflight=sub.add_parser('preflight',help='Safe read-only runtime, sign-in, SSH, pairing, and scope checks')
    preflight.add_argument('--peer-id'); preflight.add_argument('--project-id')
    chat=sub.add_parser('show-chat',help='Find or open this computer local project conversation')
    chat.add_argument('--session-id',required=True); chat.add_argument('--open',action='store_true')
    init=sub.add_parser('init'); init.add_argument('--peer-id',required=True); init.add_argument('--codex',required=True); init.add_argument('--port',type=int,default=47321)
    start=sub.add_parser('start')
    launch_mode=start.add_mutually_exclusive_group()
    launch_mode.add_argument('--interactive',action='store_true',help='Persist Windows signed-in desktop launch mode; no passwords or elevation')
    launch_mode.add_argument('--background',action='store_true',help='Persist normal detached process launch mode')
    for name in ('serve','stop','status','diagnostics'):
        sub.add_parser(name)
    for name in ('transport-start','transport-stop','transport-status','transport-run'):
        transport_parser=sub.add_parser(name)
        transport_parser.add_argument('--peer',required=(name=='transport-run'),help='Select one configured peer; omit to operate on all configured peers')
    export=sub.add_parser('pair-export'); export.add_argument('--peer-id',required=True); export.add_argument('--file',type=Path,required=True); export.add_argument('--url',required=True)
    imp=sub.add_parser('pair-import'); imp.add_argument('--file',type=Path,required=True); imp.add_argument('--url',required=True)
    revoke=sub.add_parser('revoke'); revoke.add_argument('--peer-id',required=True)
    approvals=sub.add_parser('tool-approvals'); approvals.add_argument('--marketplace',default='personal'); approvals.add_argument('--mode',choices=['approve','prompt'],required=True)
    project=sub.add_parser('project-add'); project.add_argument('--id',required=True); project.add_argument('--name',required=True); project.add_argument('--workspace',type=Path,required=True); project.add_argument('--peer-id',required=True); project.add_argument('--export-root',type=Path,required=True); project.add_argument('--import-root',type=Path,required=True); project.add_argument('--policy',choices=['read-only','workspace-write'],default='read-only'); project.add_argument('--operations',default='tasks,messages,artifacts,context'); project.add_argument('--peer-project-id')
    rpc=sub.add_parser('call'); rpc.add_argument('method'); rpc.add_argument('--json',default='{}'); rpc.add_argument('--json-file',type=Path)
    args=parser.parse_args(argv)
    path=args.config.expanduser().resolve()
    try:
        if args.command in ('setup','pair-setup','project-select','transport-config','preflight','show-chat'):
            from . import onboarding
            if args.command=='setup':
                result=onboarding.setup(path,peer_id=args.peer_id,codex=args.codex,port=args.port,
                    batch=args.batch,register=args.register_mcp)
                if args.guided and not args.batch:
                    result=onboarding.continue_setup(path,result)
            elif args.command=='pair-setup':
                result=onboarding.pair_setup(path,peer_id=args.peer_id,url=args.url,
                    export_file=args.export_file,import_file=args.import_file,
                    consume_import=args.consume_import,batch=args.batch)
            elif args.command=='project-select':
                result=onboarding.project_select(path,project_id=args.id,name=args.name,
                    workspace=args.workspace,peer_id=args.peer_id,export_root=args.export_root,
                    import_root=args.import_root,policy=args.policy,operations=args.operations,
                    peer_project_id=args.peer_project_id,batch=args.batch)
            elif args.command=='transport-config':
                keys=('ssh_host','username','identity_file','known_hosts_file','host_key_alias','ssh_exe',
                    'ssh_port','local_peer_port','remote_bridge_port','remote_peer_port')
                result=onboarding.transport_config(path,peer_id=args.peer_id,batch=args.batch,
                    settings={key:getattr(args,key) for key in keys if getattr(args,key) is not None})
            elif args.command=='preflight':
                result=onboarding.preflight(path,peer_id=args.peer_id,project_id=args.project_id)
            else:
                result=onboarding.show_chat(path,args.session_id,open_chat=args.open)
        elif args.command=='init':
            if path.exists(): raise BridgeError('already_initialized','Configuration exists; it was preserved')
            if not 1024<=args.port<=65535: raise BridgeError('configuration','Select a port between 1024 and 65535')
            if not Path(args.codex).is_file(): raise BridgeError('configuration','The selected Codex executable does not exist')
            state=path.parent/'state'
            if state.exists() and any(state.iterdir()):
                raise BridgeError('already_initialized','State already exists beside this configuration; use a new dedicated directory or restore the matching configuration')
            if not path.parent.exists(): protect(path.parent)
            protect(state)
            cfg={'version':1,'peer_id':identifier(args.peer_id),'listen_port':args.port,
                 'state_dir':str(path.parent/'state'),'codex_path':str(Path(args.codex).resolve()),
                 'local_token':secrets.token_urlsafe(32),'peers':{},'projects':{}}
            save(path,cfg)
            result={'initialized':True,'config':str(path),'peer_id':args.peer_id}
        elif args.command=='pair-export':
            cfg=read(path); peer_id=identifier(args.peer_id)
            peer=cfg['peers'].setdefault(peer_id,{'incoming_token':secrets.token_urlsafe(32),'outgoing_token':'','enabled':False,'url':args.url})
            cfg['peers'][peer_id]['url']=args.url
            save(path,cfg)
            private_invitation(args.file.resolve(),{'protocol':1,'peer_id':cfg['peer_id'],'receive_token':peer['incoming_token']})
            result={'invitation_file':str(args.file),'peer_id':peer_id,'contains':'Bridge-only pairing credential; transfer privately, then delete exchange copy'}
        elif args.command=='pair-import':
            cfg=read(path); invitation=read(args.file)
            if invitation.get('protocol')!=1 or len(invitation.get('receive_token',''))<32: raise BridgeError('configuration','Invalid pairing invitation')
            peer_id=identifier(invitation['peer_id'])
            peer=cfg['peers'].setdefault(peer_id,{'incoming_token':secrets.token_urlsafe(32)})
            peer.update(outgoing_token=invitation['receive_token'],url=args.url,enabled=True)
            save(path,cfg); result={'paired_peer':peer_id,'enabled':True}
        elif args.command=='project-add':
            cfg=read(path); identifier(args.id); identifier(args.peer_id)
            workspace=args.workspace.resolve()
            if not workspace.is_dir(): raise BridgeError('configuration','Workspace must already exist')
            roots=[args.export_root.resolve(),args.import_root.resolve()]
            if any(not r.is_relative_to(workspace) for r in roots): raise BridgeError('configuration','Transfer roots must be inside the selected workspace')
            for root in roots: root.mkdir(parents=True,exist_ok=True)
            operations=args.operations.split(',')
            if not set(operations)<= {'tasks','messages','artifacts','context'}: raise BridgeError('configuration','Unknown allowed operation')
            item={'name':args.name,'workspace':str(workspace),'export_root':str(roots[0]),'import_root':str(roots[1]),'allowed_peers':[args.peer_id],'allowed_ops':operations,'policy':args.policy}
            if args.peer_project_id: item['peer_project_id']=identifier(args.peer_project_id)
            cfg['projects'][args.id]=item; save(path,cfg); result={'project_id':args.id,**item}
        elif args.command=='serve':
            cfg=read_config(path)
            state=Path(cfg['state_dir'])
            with process_lock(state/'serve.lock'):
                save(state/'serve.pid.json',{'pid':os.getpid(),'created_at':now(),'config':str(path)})
                asyncio.run(Bridge(path).serve())
            return 0
        elif args.command=='start':
            result=start_daemon(path,True if args.interactive else False if args.background else None)
        elif args.command=='stop':
            cfg=read_config(path)
            try: result=valid_response(call(path,'shutdown'))
            except OSError:
                result={'ok':True,'already_unreachable':True}
            stopped=wait_for_exit(Path(cfg['state_dir'])/'serve.pid.json',seconds=15)
            result.update(stopped=stopped,stopping=not stopped)
        elif args.command=='status':
            result=call(path,'bridge_status')
            if isinstance(result,dict) and result.get('ok') is False:
                result={'ok':False,'error':{'code':'status_unavailable',
                    'message':'The local Bridge could not provide status. Run preflight to check this configuration.',
                    'retryable':False}}
        elif args.command=='diagnostics': result=call(path,'diagnostics')
        elif args.command=='revoke':
            try: result=call(path,'peer_revoke',{'peer_id':args.peer_id})
            except OSError:
                cfg=read(path); cfg['peers'][args.peer_id]['enabled']=False; save(path,cfg)
                result={'revoked':True,'peer_id':args.peer_id,'daemon_was_offline':True}
        elif args.command=='tool-approvals':
            result=asyncio.run(configure_tool_approvals(path,args.marketplace,args.mode))
        elif args.command in ('transport-run','transport-start','transport-stop','transport-status'):
            from . import transport
            if args.command=='transport-run':
                transport.supervise_transport(path,args.peer)
                return 0
            elif args.command=='transport-start': result=transport.start_transports(path,args.peer)
            elif args.command=='transport-stop': result=transport.stop_transports(path,args.peer)
            else: result=transport.transport_status(path,args.peer)
        elif args.command=='call':
            params=read(args.json_file) if args.json_file else json.loads(args.json)
            result=call(path,args.method,params)
        print(json.dumps(result,indent=2,ensure_ascii=False))
        return 1 if isinstance(result,dict) and result.get('ok') is False else 0
    except Exception as exc:
        safe_commands={'setup','pair-setup','project-select','transport-config','preflight','show-chat','status','transport-status'}
        if args.command in safe_commands and not isinstance(exc,BridgeError):
            error={'code':'local_check_failed','message':'The local operation could not complete. Verify the selected files and configuration, then run setup or preflight.', 'retryable':False}
        else:
            error=exc.as_dict() if isinstance(exc,BridgeError) else {'code':'local_error','message':str(exc),'retryable':False}
        print(json.dumps({'ok':False,'error':error},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())
