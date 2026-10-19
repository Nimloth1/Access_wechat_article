"""公众号后台模式不调用前台激活或全局快捷键；成功必须读到实际页变化。"""
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from src.modules.window.home_window_activation import WindowsHomeWindowGuard, HomeWindowNotClickableError
from src.modules.window.wechat_home_scroller import WechatHomeScroller
from src.modules.window.wechat_browser_tabs import UiaWechatBrowserTabAdapter
from src.modules.window.window_models import BrowserTabInfo


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setenv('WECHAT_WINDOW_MESSAGES', '1')


def test_home_guard_reuses_verified_transport_without_global_activation(locked):
    native = Mock()
    native.GetForegroundWindow.side_effect = AssertionError('global foreground forbidden')
    guard = WindowsHomeWindowGuard(user32=native)
    guard._message_transport = Mock()
    home = SimpleNamespace(handle=1)
    guard.activate(home, force=True)
    guard.ensure_target_clickable(home, SimpleNamespace(home_window_handle=1, click_x=10, click_y=20))
    guard._message_transport.activate.assert_called_once()
    guard._message_transport.check_point.assert_called_once()
    native.GetForegroundWindow.assert_not_called()
    native.SetForegroundWindow.assert_not_called()
    native.WindowFromPoint.assert_not_called()


def test_message_mode_still_rejects_wrong_home_handle(locked):
    guard = WindowsHomeWindowGuard()
    guard._message_transport = Mock()
    with pytest.raises(HomeWindowNotClickableError):
        guard.ensure_target_clickable(SimpleNamespace(handle=1), SimpleNamespace(home_window_handle=2))
    guard._message_transport.check_point.assert_not_called()


def test_home_scroller_does_not_require_foreground_in_message_mode(locked):
    native = Mock()
    native.FindWindowExW.return_value = 0
    native.ChildWindowFromPointEx.side_effect = lambda h, p, f: h
    native.GetWindowThreadProcessId.side_effect = lambda h, p: setattr(p._obj, 'value', 42) or 1
    native.ScreenToClient.return_value = True
    native.GetForegroundWindow.side_effect = AssertionError('foreground forbidden')
    native.PostMessageW.return_value = True
    scroller = WechatHomeScroller(user32=native, platform_name='Windows', sleep=lambda _: None)
    scroller._home_guard._message_transport = Mock()
    home = SimpleNamespace(handle=10, rect=(100, 200, 500, 600))
    assert scroller.scroll_down(home, visible_targets=[])
    scroller._home_guard._message_transport.check_point.assert_called_once()
    native.GetForegroundWindow.assert_not_called()
    assert native.PostMessageW.call_count == scroller.wheel_steps


def test_locked_close_requires_verified_button_and_actual_page_change(locked):
    adapter = UiaWechatBrowserTabAdapter()
    selected = BrowserTabInfo('document:1', 1, '文章甲', (0, 0, 500, 600), True)
    with patch.object(adapter, '_require_close_owner_window', return_value='owner'), \
         patch.object(adapter, '_close_message_button') as close, \
         patch.object(adapter, '_wait_until_document_title_changes', return_value=True), \
         patch('src.modules.window.wechat_browser_tabs._send_ctrl_w_to_control', side_effect=AssertionError('global input forbidden')):
        adapter.close_tab(selected, home_window_handle=1)
        close.assert_called_once_with(selected, 'owner')


def test_locked_close_failure_never_falls_back_to_keyboard(locked):
    adapter = UiaWechatBrowserTabAdapter()
    selected = BrowserTabInfo('document:1', 1, '文章甲', (0, 0, 500, 600), True)
    with patch.object(adapter, '_require_close_owner_window', return_value='owner'), \
         patch.object(adapter, '_close_message_button'), \
         patch.object(adapter, '_wait_until_document_title_changes', return_value=False), \
         patch('src.modules.window.wechat_browser_tabs._send_ctrl_w_to_control', side_effect=AssertionError('global input forbidden')):
        with pytest.raises(RuntimeError, match='未确认文章页变化'):
            adapter.close_tab(selected, home_window_handle=1)


def test_locked_document_without_button_fails_before_any_input(locked):
    adapter = UiaWechatBrowserTabAdapter()
    control = SimpleNamespace(GetChildren=lambda: [])
    with pytest.raises(RuntimeError, match='关闭按钮'):
        adapter._close_message_button(SimpleNamespace(control=control), None)


def test_background_tab_read_never_calls_uia_set_active(locked):
    from src.modules.window.wechat_browser_tabs import _activate_tab_control
    control = Mock()
    control.SetActive.side_effect = AssertionError('global activation forbidden')
    _activate_tab_control(control)
    control.SetActive.assert_not_called()


def test_serve_enables_only_its_own_process_mode(monkeypatch):
    import awa_agent
    monkeypatch.setenv('AWA_AGENT_TOKEN', 'test-only')
    monkeypatch.setenv('WECHAT_WINDOW_MESSAGES', '0')
    observed = []
    with patch.object(awa_agent, 'serve', side_effect=lambda _: observed.append(os.environ.get('WECHAT_WINDOW_MESSAGES'))):
        assert awa_agent.main(['serve', '--window-messages']) == 0
    assert observed == ['1']


def test_independent_account_window_is_verified_before_stale_main_tree(locked):
    from src.modules.window import wechat_window_messages as module
    edit = Mock(AutomationId='search')
    listing = Mock(AutomationId='search_list')
    main = SimpleNamespace(control=SimpleNamespace(ProcessId=42))
    popup = SimpleNamespace(title='account', visible=True, is_minimized=False, process_name='Weixin.exe')
    attached = SimpleNamespace(control=SimpleNamespace(ProcessId=42))
    adapter = Mock(main=main)
    adapter._search_edits.return_value = [edit]
    adapter._window.return_value = attached
    transport = object.__new__(module.WindowMessageTransport)
    transport.verify_window = Mock()
    with patch.object(module, 'walk', side_effect=[[edit], [listing], AssertionError('stale UIA must not be read')]), \
         patch.object(module, 'local_chat_matches', return_value=[Mock()]), \
         patch.object(module, 'enumerate_desktop_windows', return_value=[popup]):
        with pytest.raises(module.ChatAutomationError, match='未在等待时间内确认目标对话已打开'):
            transport.open_chat(adapter, 'account')
    transport.verify_window.assert_called_once()


def test_locked_click_hits_chromium_child_without_global_hit_test(locked):
    from src.modules.window.article_clicker import _resolve_click_message_target, _Point
    user = Mock()
    user.ScreenToClient.return_value = True
    user.ChildWindowFromPointEx.side_effect = [20, 20]
    user.IsChild.return_value = True
    user.GetWindowThreadProcessId.side_effect = lambda h, p: setattr(p._obj, 'value', 42) or 1
    user.WindowFromPoint.side_effect = AssertionError('lock screen hit test forbidden')
    assert _resolve_click_message_target(user, 10, _Point(300, 400)) == 20
    user.WindowFromPoint.assert_not_called()
    assert all(call.args[2] == 3 for call in user.ChildWindowFromPointEx.call_args_list)


def test_locked_child_of_other_process_is_never_clicked(locked):
    from src.modules.window.article_clicker import _resolve_click_message_target, _Point
    user = Mock()
    user.ScreenToClient.return_value = True
    user.ChildWindowFromPointEx.return_value = 20
    user.IsChild.return_value = True
    user.GetWindowThreadProcessId.side_effect = lambda h, p: setattr(p._obj, 'value', h) or 1
    with pytest.raises(RuntimeError, match='不属于'):
        _resolve_click_message_target(user, 10, _Point(300, 400))


def test_browser_uia_attachment_uses_active_render_not_stale_root(locked):
    from src.modules.window import wechat_home_window_finder as finder
    from src.modules.window.window_models import WindowInfo
    import uiautomation
    old = Mock(); current = Mock()
    w = WindowInfo(1, '微信', 'Chrome_WidgetWin_0', 'WeChatAppEx.exe', (0, 0, 500, 600), True, False, old)
    with patch.object(finder, '_active_message_render', return_value=20), \
         patch.object(uiautomation, 'ControlFromHandle', return_value=current) as attach:
        result = finder.attach_uia_control(w)
    attach.assert_called_once_with(20)
    assert result.handle == 1 and result.control is current


def test_browser_render_scope_rejects_unknown_native_child(locked):
    from src.modules.window import wechat_home_window_finder as finder
    native = Mock()
    native.GetClassNameW.side_effect = lambda h, name, size: setattr(name, 'value', 'LockApp') or 1
    with patch.object(finder, '_USER32', native), \
         patch('src.modules.window.article_clicker._resolve_click_message_target', return_value=20):
        with pytest.raises(RuntimeError, match='渲染窗口'):
            finder._active_message_render(SimpleNamespace(handle=1, rect=(0, 0, 500, 600)))


def test_locked_null_hit_never_falls_back_to_parent_click(locked):
    from src.modules.window.article_clicker import _resolve_click_message_target, _Point
    user = Mock()
    user.ScreenToClient.return_value = True
    user.ChildWindowFromPointEx.return_value = 0
    user.GetWindowThreadProcessId.side_effect = lambda h, p: setattr(p._obj, 'value', 42) or 1
    with pytest.raises(RuntimeError, match='命中失败'):
        _resolve_click_message_target(user, 10, _Point(300, 400))


def article_action_target(title='article', names=None, process=42):
    from src.domain.models import ArticleTarget
    leaf = SimpleNamespace(Name='阅读 2万', ProcessId=42, GetPattern=lambda _: None)
    invoke = Mock()
    labels = [SimpleNamespace(Name=n, GetChildren=lambda: []) for n in (names or [title])]
    group = SimpleNamespace(ProcessId=process, GetPattern=lambda _: invoke,
                            GetChildren=lambda: labels, Name='', BoundingRectangle=(0, 0, 200, 200),
                            GetParentControl=lambda: None)
    leaf.GetParentControl=lambda: group
    return ArticleTarget('account', title, 100, 100, 1, 'known', leaf), invoke


def test_locked_article_uses_exact_card_semantic_action(locked):
    from src.modules.window.article_clicker import ArticleClicker
    target, action = article_action_target()
    physical = Mock(side_effect=AssertionError('native click forbidden'))
    clicker = ArticleClicker(native_click=physical)
    clicker._article_message_transport = Mock()
    result = clicker.click(target)
    assert result.method == 'uia_invoke'
    action.Invoke.assert_called_once()
    physical.assert_not_called()


@pytest.mark.parametrize('names,process', [(['wrong title'],42), (['article','article'],42), (['article'],99)])
def test_locked_article_ambiguous_or_wrong_owner_action_is_rejected(locked,names,process):
    from src.modules.window.article_clicker import ArticleClicker
    target, action = article_action_target(names=names,process=process)
    clicker=ArticleClicker();clicker._article_message_transport=Mock()
    with pytest.raises(RuntimeError):clicker.click(target)
    action.Invoke.assert_not_called()


def test_queue_metadata_is_rebound_before_semantic_action(locked):
    from dataclasses import replace
    from src.modules.window.article_clicker import ArticleClicker
    target, action = article_action_target()
    clicker = ArticleClicker();clicker._article_message_transport=Mock()
    with patch.object(clicker, '_resolve_message_target', return_value=target) as bind:
        assert clicker.click(replace(target,control=None)).method == 'uia_invoke'
    bind.assert_called_once()
    action.Invoke.assert_called_once()


@pytest.mark.parametrize('account,count',[('other',1),('account',2),('account',0)])
def test_rebinding_rejects_changed_account_or_ambiguous_title(locked,account,count):
    from src.modules.window.article_clicker import ArticleClicker
    target,_ = article_action_target()
    with patch('src.modules.window.wechat_home_window_finder.enumerate_win32_windows',return_value=[SimpleNamespace(handle=1)]), \
         patch('src.modules.window.wechat_home_window_finder.attach_uia_control',return_value=Mock()), \
         patch('src.modules.window.wechat_home_reader.WechatHomeReader') as home, \
         patch('src.modules.window.article_card_reader.UiaArticleCardReader') as cards:
        home.return_value.read.return_value.account_name=account
        cards.return_value.read_viewport.return_value.targets=[target]*count
        with pytest.raises(RuntimeError):ArticleClicker()._resolve_message_target(target)


def test_explicit_legacy_press_action_preferred_to_chromium_invoke():
    from src.modules.window.article_clicker import invoke_accessible_action
    legacy=Mock(DefaultAction='按');invoke=Mock()
    control=SimpleNamespace(GetPattern=lambda p:legacy if p==10018 else invoke)
    assert invoke_accessible_action(control)=='uia_legacy_default_action'
    legacy.DoDefaultAction.assert_called_once();invoke.Invoke.assert_not_called()


def test_failed_legacy_action_is_not_replayed_through_another_interface():
    from src.modules.window.article_clicker import invoke_accessible_action
    legacy=Mock(DefaultAction='按');invoke=Mock()
    legacy.DoDefaultAction.side_effect=RuntimeError('uncertain delivery')
    control=SimpleNamespace(GetPattern=lambda p:legacy if p==10018 else invoke)
    with pytest.raises(RuntimeError):invoke_accessible_action(control)
    invoke.Invoke.assert_not_called()


@pytest.mark.parametrize('focus,attach_ok',[(20,True),(99,True),(20,False)])
def test_browser_focus_targets_only_owned_queue_and_always_detaches(locked,focus,attach_ok):
    from src.modules.window import wechat_window_messages as module
    reader=Mock();reader.kernel32.GetCurrentThreadId.return_value=7
    reader.user32.GetWindowThreadProcessId.return_value=9
    reader.user32.AttachThreadInput.return_value=attach_ok
    reader.user32.GetFocus.return_value=focus
    process=Mock();process.name.return_value='WeChatAppEx.exe'
    transport=object.__new__(module.WindowMessageTransport)
    transport.verify_window=Mock(return_value=(0,0,500,600))
    context=SimpleNamespace(window=SimpleNamespace(handle=10,control=SimpleNamespace(ProcessId=42)))
    with patch.object(module,'_Reader',return_value=reader), \
         patch.object(module.psutil,'Process',return_value=process), \
         patch('src.modules.window.wechat_home_window_finder._active_message_render',return_value=20):
        if focus==20 and attach_ok:transport._focus_browser(context)
        else:
            with pytest.raises(module.ChatAutomationError):transport._focus_browser(context)
    reader.user32.AttachThreadInput.assert_any_call(7,9,True)
    if attach_ok:reader.user32.AttachThreadInput.assert_any_call(7,9,False)
    else:reader.user32.SetFocus.assert_not_called()
    reader.user32.SetForegroundWindow.assert_not_called()
    reader.user32.GetForegroundWindow.assert_not_called()
    reader.user32.SendInput.assert_not_called()
