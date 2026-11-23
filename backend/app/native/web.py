"""Loopback-only first-run setup, then the unchanged application routes."""
import os
from pathlib import Path
import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.native.config import NativeConfig, atomic_json
from app.native.install import native_identity


class SetupInput(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    username: str = Field(min_length=3, max_length=80, pattern=r'^[a-z0-9][a-z0-9_-]+$')
    password: str = Field(min_length=12, max_length=256, repr=False)


HTML = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>天衍系统 · 首次设置</title><link rel="stylesheet" href="/native/setup.css">
<main><h1>欢迎使用天衍系统</h1><p>创建本机管理员。完成后可在数据中心连接你自己的数据源。</p>
<form id="setup"><label>管理员账号<input id="username" autocomplete="username" pattern="[a-z0-9][a-z0-9_-]{2,79}" required></label>
<label>密码（至少 12 位）<input id="password" type="password" autocomplete="new-password" minlength="12" maxlength="256" required></label>
<label>再次输入密码<input id="confirmation" type="password" autocomplete="new-password" required></label>
<button type="submit">完成设置</button><p id="result" role="status"></p></form>
<p>关闭此窗口后，后台服务仍会按交易时段运行。</p></main><script src="/native/setup.js" defer></script></html>'''
CSS = '''body{font:16px/1.6 system-ui,sans-serif;background:#0b1821;color:#e4edf4;margin:0}main{max-width:480px;margin:8vh auto;padding:32px}h1{font-size:26px}label{display:block;margin:20px 0}input,button{box-sizing:border-box;width:100%;padding:12px;border:1px solid #3e677b;border-radius:6px;background:#132b37;color:#fff}input{display:block;margin-top:8px}button{cursor:pointer;background:#116786}p{color:#a9c4d3}#result{color:#ffd768}'''
JS = '''let token=location.hash.slice(1);history.replaceState(null,'','/native/setup');
document.querySelector('#setup').addEventListener('submit',async event=>{event.preventDefault();const result=document.querySelector('#result');const password=document.querySelector('#password').value;
if(password!==document.querySelector('#confirmation').value){result.textContent='两次密码不一致。';return;}
if(!token){result.textContent='请从桌面“天衍系统”快捷方式重新打开首次设置。';return;}
const button=document.querySelector('button');button.disabled=true;
try{const response=await fetch('/native/setup',{method:'POST',headers:{'Content-Type':'application/json','X-Tianyan-Setup':token},body:JSON.stringify({username:document.querySelector('#username').value,password})});
if(response.ok){token='';location.replace('/');return;}result.textContent=response.status===409?'本机已经完成设置，请打开登录页。':'设置未完成，请检查账号格式或从桌面快捷方式重新打开。';}catch{result.textContent='服务暂不可用，请稍后重试。';}finally{button.disabled=false;}});'''


def create_app():
    from app.auth import hasher
    from app.db import engine
    from app.main import app as core
    from app.models import AuditEvent, User
    config = NativeConfig.read(os.environ['JL_NATIVE_CONFIG_FILE'])
    identity = native_identity(config)
    run_id = os.environ.get('JL_NATIVE_RUN_ID', '')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid_setup(request, error):
        # FastAPI's default validation details include input values, including
        # a short password. First-run errors never echo credentials.
        return JSONResponse({'error': 'setup_input_invalid'}, status_code=422)

    def admin_exists():
        with Session(engine()) as db:
            return db.scalar(select(User.id).where(User.role == 'admin').limit(1)) is not None

    @app.middleware('http')
    async def local_host(request: Request, call_next):
        if request.headers.get('host') != f'127.0.0.1:{config.http_port}':
            return JSONResponse({'error': 'local_host_required'}, status_code=403)
        if request.url.path == '/' and not admin_exists():
            return RedirectResponse('/native/setup')
        response = await call_next(request)
        response.headers['X-Tianyan-Native-Run'] = run_id
        response.headers['X-Tianyan-Native-Pid'] = str(os.getpid())
        for header, field in (('X-Tianyan-Native-Instance', 'instance_id'),
            ('X-Tianyan-Native-Config', 'configuration_id'), ('X-Tianyan-Native-Build', 'build_revision'),
            ('X-Tianyan-Native-Program', 'program_manifest_sha256')):
            response.headers[header] = identity[field]
        if request.url.path.startswith('/native/'):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'self'"
        return response

    @app.get('/native/setup/status')
    def setup_status():
        return {'setup_required': not admin_exists()}

    @app.get('/native/runtime-identity')
    def runtime_identity():
        return {**identity, 'run_id': run_id, 'pid': os.getpid(), 'parent_pid': os.getppid()}

    @app.get('/native/setup', response_class=HTMLResponse)
    def setup_page():
        return RedirectResponse('/') if admin_exists() else HTMLResponse(HTML)

    @app.get('/native/setup.js')
    def setup_js():
        return Response(JS, media_type='application/javascript')

    @app.get('/native/setup.css')
    def setup_css():
        return Response(CSS, media_type='text/css')

    @app.post('/native/setup')
    def setup(body: SetupInput, request: Request):
        expected = config.private().get('setup_token')
        supplied = request.headers.get('x-tianyan-setup', '')
        if (not expected or not supplied.isascii() or len(supplied) > 256 or not secrets.compare_digest(expected, supplied)
                or request.headers.get('origin') != config.origin
                or request.headers.get('sec-fetch-site') == 'cross-site'):
            return JSONResponse({'error': 'setup_authorization_required'}, status_code=403)
        with Session(engine()) as db, db.begin():
            db.execute(text('SELECT pg_advisory_xact_lock(796153746135)'))
            if db.scalar(select(User.id).limit(1)) is not None:
                return JSONResponse({'error': 'setup_already_complete'}, status_code=409)
            user = User(username=body.username, password_hash=hasher.hash(body.password), role='admin', enabled=True)
            db.add(user)
            db.flush()
            db.add(AuditEvent(actor_id=user.id, action='native.admin.initialized',
                              resource_type='user', resource_id=user.id, changed_field_names=['first_admin']))
        # The DB check is authoritative even if a crash delays token removal.
        private = config.private()
        private.pop('setup_token', None)
        atomic_json(config.data / 'private' / 'host.json', private)
        return {'status': 'created'}

    app.mount('/', core)
    return app
