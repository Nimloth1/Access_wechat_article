"""验证 Agent 的互斥、取消、验证和定位行为，不依赖真实微信。"""
from datetime import date
from threading import Event
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.services.agent_api import create_agent_app, desktop_runner
from src.services.agent_jobs import AgentJobs, JobConflict, navigate_chat
from src.services.chat_attachment_service import ChatMessage, ChatPage


AUTH = {'Authorization': 'Bearer test-token'}
CHAT = {'chat': '测试群', 'date': '2026-09-20', 'timeout_seconds': 30}


def test_auth_validation_and_idempotency():
    """拒绝未授权和错误参数；重复提交不会重复操作桌面。"""
    release = Event()
    app = create_agent_app(token='test-token', runner=lambda *args: release.wait(3))
    with TestClient(app) as client:
        try:
            assert client.get('/agent/v1/health').status_code == 401
            assert client.get('/agent/v1/health', headers={**AUTH, 'Origin': 'https://example.com'}).status_code == 401
            assert client.post('/agent/v1/chat-jobs', headers=AUTH, json={**CHAT, 'date': 'bad'}).status_code == 422
            assert client.post('/agent/v1/article-jobs', headers=AUTH, json={'account': ' ', 'limit': 0}).status_code == 422
            headers = {**AUTH, 'Idempotency-Key': 'one'}
            first = client.post('/agent/v1/chat-jobs', headers=headers, json=CHAT)
            assert first.status_code == 202
            job_id = first.json()['id']
            assert client.post('/agent/v1/chat-jobs', headers=headers, json=CHAT).json()['id'] == job_id
            assert client.post('/agent/v1/chat-jobs', headers=headers, json={**CHAT, 'chat': '另一群'}).status_code == 409
            assert client.post('/agent/v1/article-jobs', headers=AUTH, json={'account': '测试号'}).status_code == 409
            assert client.post(f'/agent/v1/jobs/{job_id}/cancel', headers=AUTH).json()['status'] == 'stopping'
            assert client.post('/agent/v1/chat-jobs', headers=AUTH, json=CHAT).status_code == 409
            assert client.get('/agent/v1/jobs/missing', headers=AUTH).status_code == 404
        finally:
            release.set()


def test_timeout_retains_lock_until_runner_returns():
    """超时只是取消请求，清理完成前禁止下一个任务进入。"""
    entered, release = Event(), Event()
    def runner(kind, request, stop):
        """模拟收到取消但仍在清理的底层任务。"""
        assert stop.wait(2)
        entered.set()
        release.wait(3)
        return {'cleaned': True}
    jobs = AgentJobs(runner)
    job = jobs.submit('chat', {'timeout_seconds': 0.02})
    try:
        assert entered.wait(2)
        assert jobs.get(job['id'])['status'] == 'stopping'
        with pytest.raises(JobConflict):
            jobs.submit('article', {'timeout_seconds': 1})
    finally:
        release.set()
        jobs.jobs[job['id']]['thread'].join(3)
    assert jobs.get(job['id'])['status'] == 'timed_out'
    assert jobs.active is None


def test_failure_is_queryable_and_releases_slot():
    """底层异常成为任务失败记录，服务仍可接收后续任务。"""
    def runner(*args):
        """模拟窗口缺失。"""
        raise RuntimeError('没有找到窗口')
    jobs = AgentJobs(runner)
    job = jobs.submit('chat', {'timeout_seconds': 10})
    jobs.jobs[job['id']]['thread'].join(2)
    assert jobs.get(job['id'])['status'] == 'failed'
    assert '没有找到窗口' in jobs.get(job['id'])['error']
    assert jobs.active is None


class FakeChat:
    """提供带重叠消息的历史页。"""
    def __init__(self, pages):
        self.pages, self.index, self.scrolls = pages, 0, 0
    def open_chat(self, name):
        return name
    def chat_name(self, context):
        return context
    def prepare_dates(self, context):
        pass
    def cancelled(self):
        return False
    def read_page(self, context):
        return self.pages[self.index]
    def resolve_day(self, context, message):
        return message.day
    def scroll_older(self, context):
        self.scrolls += 1
        self.index += 1


def test_chat_date_navigation_and_scroll():
    """定位目标日期后只执行请求指定的额外滚动。"""
    newer = ChatMessage('new', 'text', date(2026, 9, 20))
    target = ChatMessage('target', 'text', date(2026, 9, 19))
    older = ChatMessage('old', 'text', date(2026, 9, 18))
    adapter = FakeChat([ChatPage((newer,)), ChatPage((target, newer)), ChatPage((older, target))])
    request = dict(chat='群', date='2026-09-19', max_pages=10, scroll_pages=1)
    result = navigate_chat(adapter, request, Event())
    assert result['found'] and result['scrolls'] == 2
    assert result['visible_messages'][0]['signature'] == 'old'


def test_chat_budget_does_not_claim_not_found():
    """达到扫描预算不等于历史中不存在目标日期。"""
    adapter = FakeChat([ChatPage((ChatMessage('a', 'text', date(2026, 9, 20)),))])
    result = navigate_chat(adapter, dict(chat='群', date='2026-09-19', max_pages=1, scroll_pages=0), Event())
    assert not result['found'] and result['reason'] == 'scan_limit'
    assert adapter.scrolls == 0


@pytest.mark.parametrize('matches', [True, False])
def test_article_account_verification_and_result(monkeypatch, matches):
    """只有主页名称精确匹配才启动主流程，完成后保留结果。"""
    import chat_attachments
    import uiautomation
    monkeypatch.setattr(chat_attachments, 'task_lock', nullcontext)
    monkeypatch.setattr(uiautomation, 'UIAutomationInitializerInThread', nullcontext)
    probe = Mock()
    probe.read_first_visible_card.return_value = {'ok': True, 'accountName': '目标号' if matches else '别的号'}
    service = Mock()
    service.start.return_value = SimpleNamespace(task_id='main-1', status='running')
    service.wait.return_value = SimpleNamespace(status='completed', to_payload=lambda: {'saved': 1})
    backend = SimpleNamespace(article_detail_card_probe_service=probe, main_flow_service=service)
    payload = dict(account='目标号', limit=1, collect_comments=False, archive_offline=True, skip_collected=True)
    if matches:
        assert desktop_runner(backend)('article', payload, Event()) == {'saved': 1}
        assert service.start.call_args.args[0].target_count == 1
        assert service.start.call_args.args[0].archive_offline
    else:
        with pytest.raises(RuntimeError, match='公众号不匹配'):
            desktop_runner(backend)('article', payload, Event())
        service.start.assert_not_called()


def test_cli_payload_and_failure_exit(monkeypatch, capsys):
    """CLI 正确转换参数，失败任务返回非零退出码和 JSON。"""
    import json
    import awa_agent
    monkeypatch.setenv('AWA_AGENT_TOKEN', 'test-token')
    call = Mock(return_value={'id': 'one', 'status': 'running'})
    monkeypatch.setattr(awa_agent, 'call_api', call)
    assert awa_agent.main(['articles', '--account', '公众号', '--limit', '1']) == 0
    assert call.call_args.args == ('article-jobs', 'test-token')
    assert call.call_args.kwargs['payload']['skip_collected'] is True
    assert json.loads(capsys.readouterr().out)['id'] == 'one'
    call.return_value = {'id': 'one', 'status': 'failed'}
    assert awa_agent.main(['status', 'one']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'
