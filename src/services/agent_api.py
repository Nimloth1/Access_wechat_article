# encoding: UTF-8
"""仅供本机 Agent 客户端使用的桌面任务 API。"""
from __future__ import annotations

from contextlib import asynccontextmanager, ExitStack
from datetime import date
import secrets
import time
from typing import Literal

try:
    from fastapi import FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, ConfigDict, Field
except ImportError as exc:
    raise RuntimeError('请使用项目 .venv 安装的 FastAPI 和 Pydantic') from exc

from src.services.agent_jobs import AgentJobs, JobConflict, navigate_chat


class JobRequest(BaseModel):
    """拒绝未知参数，限制单次桌面任务时间。"""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    timeout_seconds: int = Field(default=600, ge=1, le=7200)


class ChatRequest(JobRequest):
    """定位日期所在可视页，再向历史方向滚动指定次数。"""
    chat: str = Field(min_length=1, max_length=200)
    date: date
    max_pages: int = Field(default=100, ge=1, le=1000)
    scroll_pages: int = Field(default=0, ge=0, le=100)
    compatibility: bool = False


class ArticleRequest(JobRequest):
    """V1 要求用户已打开目标公众号主页。"""
    account: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=1000)
    collect_comments: bool = False
    archive_offline: bool = False
    skip_collected: bool = True


class ArticleRequestV2(ArticleRequest):
    """默认自动打开公众号，也允许复用手动打开的主页。"""
    open_account: bool = True
    accessibility: Literal['auto', 'off', 'on'] = 'auto'


class ChatAction(JobRequest):
    """读取当前群、校验页面后继续向历史滚动或单击指定消息。"""
    chat: str = Field(min_length=1, max_length=200)
    action: Literal['read', 'scroll', 'click'] = 'read'
    page_token: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    message_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    scroll_pages: int = Field(default=1, ge=1, le=100)
    compatibility: bool = False


def desktop_runner(backend):
    """复用现有桌面适配器与文章主流程，所有 UIA 操作在线程内初始化。"""
    def run(kind, request, stop):
        """执行一个串行桌面任务并返回结构化结果。"""
        import uiautomation as auto
        from chat_attachments import task_lock
        from src.modules.window.wechat_chat_adapter import WechatChatAdapter
        from src.services.main_flow.main_flow_models import MainFlowCommand

        with task_lock(), auto.UIAutomationInitializerInThread(), ExitStack() as scope:
            if stop.is_set():
                return {'reason': 'cancelled'}
            if kind in {'chat', 'chat_v2', 'chat_action'}:
                adapter = WechatChatAdapter(stop)
                with adapter.compatibility(request['compatibility']):
                    if kind == 'chat_action':
                        return run_chat_action(adapter, request, stop)
                    return navigate_chat(adapter, request, stop)
            adapter = WechatChatAdapter(stop)
            diagnostic = None
            if 'accessibility' in request:
                diagnostic = scope.enter_context(adapter.account_accessibility(
                    request['accessibility'], activate=True))
                if not diagnostic['wechat_ready']:
                    raise RuntimeError(diagnostic['detail'])
            if request.get('open_account'):
                adapter.open_account(request['account'])
            deadline = time.monotonic() + (10 if request.get('open_account') else 0)
            while True:
                probe = backend.article_detail_card_probe_service.read_first_visible_card()
                if probe.get('ok') and probe.get('accountName') == request['account']:
                    break
                if stop.is_set() or time.monotonic() >= deadline:
                    break
                stop.wait(0.25)
            if not probe.get('ok'):
                raise RuntimeError(probe.get('message', '无法读取公众号主页'))
            if probe.get('accountName') != request['account']:
                raise RuntimeError(f"公众号不匹配：当前为 {probe.get('accountName')!r}")
            if stop.is_set():
                return {'reason': 'cancelled'}
            service = backend.main_flow_service
            snapshot = service.start(MainFlowCommand(
                target_count=request['limit'], collect_comments=request['collect_comments'],
                archive_offline=request['archive_offline'],
                skip_collected_records=request['skip_collected']))
            while snapshot.status in {'starting', 'running', 'stopping'}:
                if stop.is_set():
                    service.stop(snapshot.task_id)
                snapshot = service.wait(snapshot.task_id, timeout_seconds=0.2)
            if snapshot.status == 'failed':
                raise RuntimeError(snapshot.message)
            if snapshot.status == 'cancelled':
                stop.set()
            result = snapshot.to_payload()
            if diagnostic is not None:
                result['wechat_diagnostic'] = diagnostic
            return result
    return run


def run_chat_action(adapter, request, stop):
    """操作当前对话；继续操作不会搜索切换到其他群。"""
    context = adapter.open_chat(None)
    if adapter.chat_name(context) != request['chat']:
        raise RuntimeError('当前对话已改变，请重新定位')
    snapshot = adapter.page_snapshot(context)
    if request['action'] == 'read':
        return snapshot
    if snapshot['page_token'] != request['page_token']:
        raise RuntimeError('页面已变化，请重新读取')
    if request['action'] == 'click':
        return adapter.click_visible_message(context, request['message_id'], request['page_token'])
    from src.services.chat_attachment_service import older_prefix
    previous = adapter.read_page(context)
    for _ in range(request['scroll_pages']):
        if stop.is_set() or adapter.cancelled() or previous.at_start:
            break
        adapter.scroll_older(context)
        current = adapter.read_page(context)
        older_prefix(previous, current)
        previous = current
    return adapter.page_snapshot(context)


def desktop_diagnose(mode):
    """在桌面互斥和 COM 初始化内检测微信；不读取文章或聊天正文。"""
    import uiautomation as auto
    from chat_attachments import task_lock
    from src.modules.window.wechat_chat_adapter import WechatChatAdapter

    with task_lock(), auto.UIAutomationInitializerInThread():
        return WechatChatAdapter().account_diagnose(mode)


def create_agent_app(backend=None, *, token, runner=None, diagnoser=None):
    """创建独立 Agent API；诊断和任务共用互斥，不注册配置修改或删除接口。"""
    if not token:
        raise ValueError('AWA_AGENT_TOKEN 不能为空')
    jobs = AgentJobs(runner or desktop_runner(backend))

    @asynccontextmanager
    async def lifespan(app):
        """退出前停止桌面任务，再释放已有后端资源。"""
        try:
            yield
        finally:
            from starlette.concurrency import run_in_threadpool
            await run_in_threadpool(jobs.close)
            if backend is not None:
                from dev_server import shutdown_backend
                await run_in_threadpool(shutdown_backend, backend)

    app = FastAPI(title='WeChat Agent API', version='2.1', lifespan=lifespan)
    app.state.jobs = jobs

    @app.middleware('http')
    async def authenticate(request: Request, call_next):
        """所有端点均验证 Bearer，禁止浏览器跨源访问。"""
        value = request.headers.get('authorization', '')
        if request.headers.get('origin') or not secrets.compare_digest(
                value.encode(), ('Bearer ' + token).encode()):
            return JSONResponse({'detail': '需要有效的 Bearer token；不接受浏览器跨源请求'}, status_code=401)
        return await call_next(request)

    @app.exception_handler(JobConflict)
    async def conflict(request, exc):
        """占用和幂等冲突统一返回 409。"""
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.get('/agent/v1/health')
    @app.get('/agent/v2/health')
    def health():
        """说明服务可达，不宣称微信桌面已就绪。"""
        return {'status': 'ready', 'active_job': jobs.active, 'version': '2.1'}

    @app.get('/agent/v2/diagnose')
    def diagnose(accessibility: Literal['auto', 'off', 'on'] = 'auto'):
        """独立的微信就绪检查；任务占用时拒绝诊断，避免兼容标志并发修改。"""
        with jobs.lock:
            if jobs.active or jobs.closing:
                raise JobConflict('桌面任务正在运行或服务正在关闭，暂不诊断')
            return {'service_ready': True, **(diagnoser or desktop_diagnose)(accessibility)}

    @app.post('/agent/v2/chat-jobs', status_code=202)
    def chat_v2(payload: ChatRequest, idempotency_key: str | None = Header(default=None, max_length=200)):
        """定位日期并返回当前可见文本和操作凭据。"""
        return jobs.submit('chat_v2', {**payload.model_dump(mode='json'), 'include_content': True}, idempotency_key)

    @app.post('/agent/v2/chat-actions', status_code=202)
    def chat_action(payload: ChatAction, idempotency_key: str | None = Header(default=None, max_length=200)):
        """所有桌面动作复用同一任务锁与取消机制。"""
        if payload.action != 'read' and not payload.page_token:
            raise HTTPException(422, '滚动或点击必须提供 page_token')
        if payload.action == 'click' and not payload.message_id:
            raise HTTPException(422, '点击必须提供 message_id')
        return jobs.submit('chat_action', payload.model_dump(mode='json'), idempotency_key)

    @app.post('/agent/v2/article-jobs', status_code=202)
    def articles_v2(payload: ArticleRequestV2, idempotency_key: str | None = Header(default=None, max_length=200)):
        """自动打开精确匹配的公众号后采集。"""
        return jobs.submit('article', payload.model_dump(mode='json'), idempotency_key)

    @app.get('/agent/v2/articles/{article_id}/content')
    def content(article_id: int):
        """只返回归档正文，不执行 HTML，不读取任意文件。"""
        from src.services.agent_content import article_content
        return article_content(backend, article_id)

    @app.post('/agent/v1/chat-jobs', status_code=202)
    def chat(payload: ChatRequest, idempotency_key: str | None = Header(default=None, max_length=200)):
        """提交群聊日期定位任务。"""
        return jobs.submit('chat', payload.model_dump(mode='json'), idempotency_key)

    @app.post('/agent/v1/article-jobs', status_code=202)
    def articles(payload: ArticleRequest, idempotency_key: str | None = Header(default=None, max_length=200)):
        """提交已打开主页的文章采集任务。"""
        return jobs.submit('article', payload.model_dump(mode='json'), idempotency_key)

    @app.get('/agent/v1/jobs/{job_id}')
    @app.get('/agent/v2/jobs/{job_id}')
    @app.get('/agent/v1/chat-jobs/{job_id}')
    @app.get('/agent/v1/article-jobs/{job_id}')
    def status(job_id: str):
        """读取任务快照；任务仅保留至进程退出。"""
        try:
            return jobs.get(job_id)
        except KeyError:
            raise HTTPException(404, '任务不存在或服务已重启') from None

    @app.post('/agent/v1/jobs/{job_id}/cancel')
    @app.post('/agent/v2/jobs/{job_id}/cancel')
    @app.post('/agent/v1/chat-jobs/{job_id}/cancel')
    @app.post('/agent/v1/article-jobs/{job_id}/cancel')
    def cancel(job_id: str):
        """请求停止任务，调用方应继续查询至终态。"""
        try:
            return jobs.cancel(job_id)
        except KeyError:
            raise HTTPException(404, '任务不存在或服务已重启') from None

    @app.get('/agent/v1/accounts')
    @app.get('/agent/v2/accounts')
    def accounts():
        """列出已归档的公众号。"""
        from dev_server import _archive_account_items
        return {'items': _archive_account_items(backend)}

    @app.get('/agent/v1/accounts/{account_id}/articles')
    @app.get('/agent/v2/accounts/{account_id}/articles')
    def saved_articles(account_id: int, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        """返回文章链接、标题和 PW 归档路径，支持分页。"""
        from dev_server import _archive_account_articles_payload
        return _archive_account_articles_payload(backend, account_id, page, page_size)

    return app
