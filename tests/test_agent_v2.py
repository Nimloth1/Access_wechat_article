# encoding: UTF-8
"""V2 协议与桌面安全边界测试，不操作真实微信。"""
from contextlib import nullcontext
from datetime import date
import sqlite3
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.services.agent_api import create_agent_app, run_chat_action, desktop_runner
from src.services.agent_content import article_content
from src.services.chat_attachment_service import ChatMessage, ChatPage, ChatAutomationError
from src.modules.window.wechat_chat_adapter import WechatChatAdapter

AUTH = {'Authorization': 'Bearer test'}
TOKEN = 'a' * 64


def test_v2_validation_auth_and_routes():
    """动作凭据必填，V1/V2 共享任务生命周期。"""
    runner = Mock(return_value={'ok': True})
    app = create_agent_app(token='test', runner=runner)
    with TestClient(app) as client:
        assert client.get('/agent/v2/articles/1/content').status_code == 401
        assert client.post('/agent/v2/chat-actions', headers=AUTH,
                           json={'chat': '群', 'action': 'scroll'}).status_code == 422
        assert client.post('/agent/v2/chat-actions', headers=AUTH,
                           json={'chat': '群', 'action': 'click', 'page_token': TOKEN}).status_code == 422
        for path, payload in [('chat-jobs', {'chat': '群', 'date': '2026-09-20'}),
                              ('article-jobs', {'account': '号'}),
                              ('chat-actions', {'chat': '群'})]:
            response = client.post('/agent/v2/' + path, headers=AUTH, json=payload)
            assert response.status_code == 202
            job_id = response.json()['id']
            app.state.jobs.jobs[job_id]['thread'].join(2)
            assert client.get('/agent/v2/jobs/' + job_id, headers=AUTH).json()['status'] == 'completed'
            assert client.get('/agent/v1/jobs/' + job_id, headers=AUTH).status_code == 200
        assert runner.call_args_list[0].args[1]['include_content'] is True
        assert runner.call_args_list[1].args[1]['open_account'] is True


def test_actions_reject_changed_chat_and_page():
    """旧凭据和其他对话不能触发滚动或点击。"""
    adapter = Mock()
    adapter.chat_name.return_value = '群'
    adapter.page_snapshot.return_value = {'page_token': TOKEN}
    request = dict(chat='群', action='click', page_token='b' * 64, message_id=TOKEN)
    with pytest.raises(RuntimeError, match='页面已变化'):
        run_chat_action(adapter, request, Event())
    adapter.click_visible_message.assert_not_called()
    adapter.chat_name.return_value = '其他群'
    with pytest.raises(RuntimeError, match='当前对话'):
        run_chat_action(adapter, request, Event())
    adapter.scroll_older.assert_not_called()


def test_action_scroll_and_click():
    """核验后只滚动请求次数，点击使用指定 ID。"""
    adapter = Mock()
    adapter.cancelled.return_value = False
    adapter.chat_name.return_value = '群'
    adapter.page_snapshot.return_value = {'page_token': TOKEN}
    newer = ChatMessage('new', 'text', None)
    older = ChatMessage('old', 'text', None)
    adapter.read_page.side_effect = [ChatPage((newer,)), ChatPage((older, newer))]
    request = dict(chat='群', action='scroll', page_token=TOKEN, scroll_pages=1)
    assert run_chat_action(adapter, request, Event())['page_token'] == TOKEN
    adapter.scroll_older.assert_called_once()
    request.update(action='click', message_id='b' * 64)
    run_chat_action(adapter, request, Event())
    adapter.click_visible_message.assert_called_once_with(adapter.open_chat.return_value, 'b' * 64, TOKEN)


def test_snapshot_distinguishes_identical_messages_and_dates():
    """重复正文仍有独立 ID；日期缓存丢失不影响动作凭据。"""
    adapter = WechatChatAdapter.__new__(WechatChatAdapter)
    adapter._page_position = Mock(return_value=((1, (0, 0, 20, 20)),))
    adapter.read_page = Mock(return_value=ChatPage(tuple(
        ChatMessage('same', 'text', None, ((i,), None), '正文') for i in (1, 2))))
    context = SimpleNamespace(name='群', window=SimpleNamespace(handle=123))
    first = adapter.page_snapshot(context)
    assert first['visible_messages'][0]['text'] == '正文'
    assert first['visible_messages'][0]['id'] != first['visible_messages'][1]['id']
    adapter.read_page.return_value = ChatPage(tuple(
        ChatMessage('same', 'text', date(2026, 9, 20), ((i,), None), '正文') for i in (1, 2)))
    assert first['page_token'] == adapter.page_snapshot(context)['page_token']
    adapter._page_position.return_value = ((1, (0, 1, 20, 21)),)
    assert first['page_token'] != adapter.page_snapshot(context)['page_token']


def test_click_rejects_stale_unknown_and_file():
    """点击前拒绝不存在的消息和可执行关联文件。"""
    adapter = WechatChatAdapter.__new__(WechatChatAdapter)
    adapter.page_snapshot = Mock(return_value={'page_token': TOKEN, 'visible_messages': [{'id': TOKEN}]})
    adapter.read_page = Mock(return_value=ChatPage((ChatMessage('sig', 'file', None),)))
    adapter._click = Mock()
    for message_id, page_token, error in [(TOKEN, 'b' * 64, '页面已变化'),
                                          ('b' * 64, TOKEN, '不在当前'), (TOKEN, TOKEN, '文件')]:
        with pytest.raises(ChatAutomationError, match=error):
            adapter.click_visible_message(None, message_id, page_token)
    adapter._click.assert_not_called()


def test_click_fresh_unique_bubble_only():
    """新鲜消息 ID 只单击对应的唯一气泡，半屏气泡不能误点其他区域。"""
    adapter = WechatChatAdapter.__new__(WechatChatAdapter)
    bubble = SimpleNamespace(ClassName='mmui::ChatBubbleItemView', IsOffscreen=False,
                             BoundingRectangle=(10, 10, 80, 80), GetChildren=lambda: [])
    row = SimpleNamespace(ClassName='row', GetRuntimeId=lambda: [1], GetChildren=lambda: [bubble])
    listing = SimpleNamespace(GetChildren=lambda: [row], BoundingRectangle=(0, 0, 100, 100))
    context = SimpleNamespace(identities={}, name='群')
    adapter._list = Mock(return_value=listing)
    adapter.page_snapshot = Mock(return_value={'page_token': TOKEN, 'visible_messages': [{'id': TOKEN}]})
    adapter.read_page = Mock(return_value=ChatPage((ChatMessage('sig', 'text', None, ((1,), None)),)))
    adapter._click = Mock()
    assert adapter.click_visible_message(context, TOKEN, TOKEN)['clicked'] is True
    adapter._click.assert_called_once_with(context, bubble)
    bubble.BoundingRectangle = (10, 90, 80, 200)
    with pytest.raises(ChatAutomationError, match='视口'):
        adapter.click_visible_message(context, TOKEN, TOKEN)
    assert adapter._click.call_count == 1


def test_auto_account_open_before_verified_collection(monkeypatch):
    """自动导航完成后仍核对账号，错误主页绝不采集。"""
    import chat_attachments
    import uiautomation
    monkeypatch.setattr(chat_attachments, 'task_lock', nullcontext)
    monkeypatch.setattr(uiautomation, 'UIAutomationInitializerInThread', nullcontext)
    opener = Mock()
    monkeypatch.setattr(WechatChatAdapter, 'open_account', opener)
    backend = SimpleNamespace(article_detail_card_probe_service=Mock(), main_flow_service=Mock())
    backend.article_detail_card_probe_service.read_first_visible_card.return_value = {'ok': True, 'accountName': '其他号'}
    with pytest.raises(RuntimeError, match='公众号不匹配'):
        desktop_runner(backend)('article', {'account': '目标号', 'open_account': True}, Event())
    opener.assert_called_once_with('目标号')
    backend.main_flow_service.start.assert_not_called()


def test_account_entry_exact_and_unambiguous():
    """只选明确的历史消息按钮；重名入口报错。"""
    adapter = WechatChatAdapter.__new__(WechatChatAdapter)
    adapter.open_chat = Mock(return_value='context')
    node = SimpleNamespace(ControlTypeName='ButtonControl', Name='查看历史消息', IsOffscreen=False)
    adapter._nodes = Mock(return_value=[node])
    adapter._click, adapter._pause = Mock(), Mock()
    adapter.open_account('目标号')
    adapter.open_chat.assert_called_once_with('目标号')
    adapter._click.assert_called_once_with('context', node)
    adapter._nodes.return_value = [node, node]
    with pytest.raises(ChatAutomationError, match='唯一匹配'):
        adapter.open_account('目标号')


def test_archived_body_and_path_boundaries(tmp_path):
    """从受控归档读取正文，缺失和数据库路径越界不会泄露文件。"""
    db = tmp_path / 'index.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE awa_public_articles (id INTEGER, article_title TEXT, article_link TEXT, archive_dir TEXT)')
        conn.executemany('INSERT INTO awa_public_articles VALUES (?, ?, ?, ?)',
                         [(1, '标题', 'https://example.com', 'one'), (2, '越界', '', '../outside')])
    root = tmp_path / 'archive'
    origin = root / 'one' / 'origin'
    origin.mkdir(parents=True)
    html = origin / 'main.html'
    html.write_text('<div>无关</div><div id="js_content"><p>第一段 &amp; <b>加粗</b></p>'
                    '<script>secret()</script><p>第二段<br/>尾行</p></div>不属于正文', encoding='utf-8')
    backend = SimpleNamespace(db_path=db, runtime=SimpleNamespace(config=SimpleNamespace(
        storage=SimpleNamespace(article_storage_root=root))))
    assert article_content(backend, 1)['content'] == '第一段 & 加粗\n第二段\n尾行'
    for article_id in (2, 999):
        with pytest.raises(HTTPException) as error:
            article_content(backend, article_id)
        assert error.value.status_code == 404
    html.write_text('<html>没有正文</html>', encoding='utf-8')
    with pytest.raises(HTTPException) as error:
        article_content(backend, 1)
    assert error.value.status_code == 422


def test_v2_cli(monkeypatch, capsys):
    """新命令使用 V2，旧命令仍走 V1。"""
    import awa_agent
    monkeypatch.setenv('AWA_AGENT_TOKEN', 'test')
    call = Mock(return_value={'id': 'one', 'status': 'completed'})
    monkeypatch.setattr(awa_agent, 'call_api', call)
    assert awa_agent.main(['articles-v2', '--account', '号']) == 0
    assert call.call_args.kwargs['version'] == 2
    assert call.call_args.kwargs['payload']['open_account'] is True
    assert awa_agent.main(['chat-action', '--chat', '群']) == 0
    assert call.call_args.args[0] == 'chat-actions'
    assert awa_agent.main(['content', '1']) == 0
    assert call.call_args.args[0] == 'articles/1/content'


def test_diagnose_auth_readiness_busy_and_cli(monkeypatch):
    """服务可达不等于微信就绪；占用时不允许触碰读屏标志。"""
    import awa_agent
    probe = Mock(return_value={'wechat_ready': False, 'search_edit_count': 0, 'main_window_count': 1})
    app = create_agent_app(token='test', runner=Mock(), diagnoser=probe)
    with TestClient(app) as client:
        assert client.get('/agent/v2/diagnose').status_code == 401
        result = client.get('/agent/v2/diagnose', headers=AUTH).json()
        assert result['service_ready'] and not result['wechat_ready']
        assert client.get('/agent/v2/diagnose?accessibility=bad', headers=AUTH).status_code == 422
        app.state.jobs.active = 'busy'
        assert client.get('/agent/v2/diagnose', headers=AUTH).status_code == 409
        assert probe.call_count == 1
        app.state.jobs.active = None
    monkeypatch.setenv('AWA_AGENT_TOKEN', 'test')
    monkeypatch.setattr(awa_agent, 'call_api', Mock(return_value=result))
    assert awa_agent.main(['diagnose']) == 1
    result['wechat_ready'] = True
    assert awa_agent.main(['diagnose', '--accessibility', 'on']) == 0
    assert awa_agent.call_api.call_args.args[0] == 'diagnose?accessibility=on'


def test_article_compatibility_restores_on_navigation_failure(monkeypatch):
    """导航错误不能启动采集，并必须退出兼容上下文恢复微信状态。"""
    import chat_attachments
    import uiautomation
    from contextlib import contextmanager
    states = []
    @contextmanager
    def compatibility(self, mode, *, activate):
        """模拟已核验的运行时兼容开关。"""
        states.append(('enabled', mode, activate))
        try:
            yield {'wechat_ready': True}
        finally:
            states.append('restored')
    monkeypatch.setattr(chat_attachments, 'task_lock', nullcontext)
    monkeypatch.setattr(uiautomation, 'UIAutomationInitializerInThread', nullcontext)
    monkeypatch.setattr(WechatChatAdapter, 'account_accessibility', compatibility)
    monkeypatch.setattr(WechatChatAdapter, 'open_account', Mock(side_effect=RuntimeError('导航失败')))
    backend = SimpleNamespace(main_flow_service=Mock())
    with pytest.raises(RuntimeError, match='导航失败'):
        desktop_runner(backend)('article', {'account': '号', 'open_account': True, 'accessibility': 'auto'}, Event())
    assert states == [('enabled', 'auto', True), 'restored']
    backend.main_flow_service.start.assert_not_called()


def test_independent_account_window_requires_exact_name_and_process(monkeypatch):
    """仅搜索唯一命中后打开的同进程、同名称公众号窗口可进入主页。"""
    import src.modules.window.wechat_chat_adapter as module
    adapter = WechatChatAdapter.__new__(WechatChatAdapter)
    adapter.main = SimpleNamespace(control=SimpleNamespace(ProcessId=123))
    adapter.open_chat = Mock(side_effect=ChatAutomationError('未在等待时间内确认目标对话已打开'))
    window = SimpleNamespace(title='404K', process_name='Weixin.exe', visible=True,
                             is_minimized=False, control=SimpleNamespace(ProcessId=123))
    monkeypatch.setattr(module, 'enumerate_desktop_windows', lambda: [window])
    monkeypatch.setattr(module, 'attach_uia_control', lambda w: w)
    button = SimpleNamespace(Name='公众号主页', ControlTypeName='ButtonControl', IsOffscreen=False)
    adapter._nodes, adapter._post_click, adapter._pause = Mock(return_value=[button]), Mock(), Mock()
    adapter.open_account('404K')
    assert adapter._post_click.call_args.args[0].window is window
    adapter._post_click.reset_mock()
    window.control.ProcessId = 456
    with pytest.raises(ChatAutomationError, match='不属于当前微信'):
        adapter.open_account('404K')
    adapter._post_click.assert_not_called()
    adapter.open_chat.side_effect = ChatAutomationError('对话搜索框需要唯一匹配，实际找到 0 个')
    with pytest.raises(ChatAutomationError, match='搜索框'):
        adapter.open_account('404K')
    adapter._post_click.assert_not_called()


def test_auto_accessibility_precedes_activation_and_restores(monkeypatch):
    """空壳窗口激活前先启用兼容；异常退出必须恢复原值。"""
    import src.modules.window.wechat_chat_adapter as module
    from contextlib import contextmanager
    enabled = [False]
    @contextmanager
    def temporary(pid):
        """模拟兼容读屏位及正常异常恢复。"""
        enabled[0] = True
        try:
            yield
        finally:
            enabled[0] = False
    window = SimpleNamespace(control=SimpleNamespace(ProcessId=123), is_minimized=False)
    adapter = WechatChatAdapter.__new__(WechatChatAdapter)
    adapter.main = window
    adapter._locate_main = Mock()
    adapter._window = Mock(return_value=window)
    adapter._search_edits = lambda nodes: nodes
    adapter._activate = lambda context: enabled[0] or pytest.fail('空壳激活前未启用兼容')
    monkeypatch.setattr(module, 'walk', lambda control: [object()] if enabled[0] else [])
    monkeypatch.setattr(module, 'temporary_accessibility', temporary)
    with pytest.raises(RuntimeError, match='任务失败'):
        with adapter.account_accessibility('auto', activate=True) as diagnostic:
            assert diagnostic['wechat_ready'] and diagnostic['search_edit_count'] == 1
            assert diagnostic['compatibility_used']
            raise RuntimeError('任务失败')
    assert not enabled[0]


def test_diagnose_external_desktop_lock_is_409(monkeypatch):
    """另一个进程占用共享桌面锁时明确返回 409，不误报服务或微信就绪。"""
    import chat_attachments
    monkeypatch.setattr(chat_attachments, 'task_lock', Mock(
        side_effect=RuntimeError('另一个附件任务正在运行，请先停止它')))
    with TestClient(create_agent_app(token='test', runner=Mock())) as client:
        assert client.get('/agent/v2/health', headers=AUTH).json()['active_job'] is None
        response = client.get('/agent/v2/diagnose', headers=AUTH)
        assert response.status_code == 409
        assert '另一个附件任务' in response.json()['detail']


def test_real_windows_lock_conflict_before_marker_read(monkeypatch, tmp_path):
    """真实跨进程锁验证：锁标记读取失败也必须归为共享桌面冲突。"""
    import os
    import subprocess
    import sys
    import chat_attachments
    if os.name != 'nt':
        pytest.skip('需要 Windows msvcrt 文件锁')
    monkeypatch.setattr(chat_attachments, 'LOG_DIR', tmp_path)
    code = '''import sys
from pathlib import Path
import chat_attachments
chat_attachments.LOG_DIR = Path(sys.argv[1])
try:
    with chat_attachments.task_lock():
        raise AssertionError('不应取得第二把锁')
except RuntimeError as exc:
    print(str(exc))
'''
    with chat_attachments.task_lock():
        child = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                               capture_output=True, text=True, encoding='utf-8', timeout=10,
                               env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    assert child.returncode == 0, child.stderr
    assert '另一个附件任务正在运行' in child.stdout
