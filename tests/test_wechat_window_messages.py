"""锁屏消息路径拒绝错进程、跨会话和越界输入；默认交互路径保持关闭。"""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from src.modules.window.wechat_window_messages import WindowMessageTransport
from src.modules.window.wechat_chat_adapter import WechatChatAdapter
from src.services.chat_attachment_service import ChatAutomationError


@pytest.fixture
def target():
    live = dict(pid=42, session=3, started=100, name='Weixin.exe', visible=True, iconic=False, exists=True)
    reader = Mock()
    reader.session.return_value = {'id': 3, 'info': {'locked': True}}
    reader.user32.IsWindow.side_effect = lambda h: live['exists']
    reader.user32.IsWindowVisible.side_effect = lambda h: live['visible']
    reader.user32.IsIconic.side_effect = lambda h: live['iconic']
    reader.user32.GetWindowThreadProcessId.side_effect = lambda h, p: setattr(p._obj, 'value', live['pid']) or 1
    reader.kernel32.ProcessIdToSessionId.side_effect = lambda pid, p: setattr(p._obj, 'value', live['session']) or True
    def rect(h, p):
        for k,v in zip(['left','top','right','bottom'],[100,200,400,600]):
            setattr(p._obj,k,v)
        return True
    reader.user32.GetWindowRect.side_effect = rect
    process = Mock()
    process.name.side_effect = lambda: live['name']
    process.create_time.side_effect = lambda: live['started']
    context = SimpleNamespace(window=SimpleNamespace(handle=123,control=SimpleNamespace(ProcessId=42)))
    with patch('src.modules.window.wechat_window_messages._Reader',return_value=reader), \
         patch('src.modules.window.wechat_window_messages.psutil.Process',return_value=process):
        yield WindowMessageTransport(),context,live,reader


def test_locked_session_window_validation_does_not_require_foreground(target):
    transport,context,live,reader=target
    reader.user32.GetForegroundWindow.side_effect=AssertionError('must not inspect global foreground')
    transport.check_point(context,100,200)
    transport.check_point(context,399,599)
    reader.user32.GetForegroundWindow.assert_not_called()


@pytest.mark.parametrize('field,value',[('pid',99),('session',4),('session',0),('name','LockApp.exe'),('visible',False),('iconic',True),('exists',False)])
def test_wrong_or_unusable_window_rejected(target,field,value):
    transport,context,live,_=target
    live[field]=value
    with pytest.raises(ChatAutomationError):transport.check_point(context,200,300)


def test_reused_process_identity_rejected(target):
    transport,context,live,_=target
    transport.check_point(context,200,300)
    live['started']=101
    with pytest.raises(ChatAutomationError):transport.check_point(context,200,300)


@pytest.mark.parametrize('x,y',[(99,300),(400,300),(200,199),(200,600)])
def test_outside_window_never_allowed(target,x,y):
    transport,context,_,_=target
    with pytest.raises(ChatAutomationError):transport.check_point(context,x,y)


def test_explicit_transport_delegates_without_foreground_activation():
    adapter=object.__new__(WechatChatAdapter)
    adapter._message_transport=Mock()
    with patch('ctypes.WinDLL',side_effect=AssertionError('physical activation forbidden')):
        adapter._activate('context')
    adapter._message_transport.activate.assert_called_once_with('context',force=False)


def test_collapsed_chat_returns_via_title_before_search():
    transport = object.__new__(WindowMessageTransport)
    title = SimpleNamespace(ClassName='mmui::ChatTitleBarMasterView')
    back = SimpleNamespace(ClassName='mmui::XButton', ControlTypeName='ButtonControl',
                           Name='返回', GetParentControl=lambda: title)
    edit = object()
    adapter = Mock()
    adapter._search_edits.side_effect = [[], [], [edit]]
    assert transport.chat_search(adapter, 'context', [back]) is edit
    adapter._post_click.assert_called_once_with('context', back)


def test_unrelated_back_button_cannot_be_used_for_chat_search():
    transport = object.__new__(WindowMessageTransport)
    parent = SimpleNamespace(ClassName='Browser', GetParentControl=lambda: None)
    back = SimpleNamespace(ClassName='Button', ControlTypeName='ButtonControl',
                           Name='返回', GetParentControl=lambda: parent)
    adapter = Mock()
    adapter._search_edits.return_value = []
    with pytest.raises(ChatAutomationError):
        transport.chat_search(adapter, 'context', [back])
    adapter._post_click.assert_not_called()


def test_folded_chat_list_returns_before_using_global_search():
    transport=object.__new__(WindowMessageTransport)
    folded=SimpleNamespace(AutomationId='session_list',Name='折叠的聊天')
    back=SimpleNamespace(AutomationId='button',ControlTypeName='ButtonControl',
                         ClassName='mmui::ChatBackwardView',Name='返回')
    edit=object();adapter=Mock()
    adapter._nodes.return_value=[];adapter._search_edits.return_value=[edit]
    assert transport.chat_search(adapter,'context',[folded,back]) is edit
    adapter._post_click.assert_called_once_with('context',back)
    adapter._search_edits.assert_called_once_with([])


def test_folded_chat_return_refuses_ambiguous_or_unrelated_back():
    transport=object.__new__(WindowMessageTransport)
    folded=SimpleNamespace(AutomationId='session_list',Name='折叠的聊天')
    unrelated=SimpleNamespace(AutomationId='button',ControlTypeName='ButtonControl',ClassName='Browser',Name='返回')
    adapter=Mock()
    with pytest.raises(ChatAutomationError):transport.chat_search(adapter,'context',[folded,unrelated])
    adapter._post_click.assert_not_called()


def test_message_click_does_not_fall_back_to_uia_mouse():
    adapter=object.__new__(WechatChatAdapter)
    adapter._message_transport=Mock();adapter._post_click=Mock()
    control=Mock();control.Click.side_effect=AssertionError('physical input forbidden')
    adapter._click('context',control)
    adapter._post_click.assert_called_once_with('context',control,cleanup=False,keep_parent_active=False)
    control.Click.assert_not_called()


@pytest.mark.parametrize('method,args',[('_bubble_point',(None,None,None)),('_open_bubble',(None,None,None)),('_download_file',(None,None,1)),('_file_menu',(None,None)),('_dismiss_menu',(None,))])
def test_unsupported_routes_fail_before_any_global_input(method,args):
    adapter=object.__new__(WechatChatAdapter);adapter._message_transport=Mock()
    with pytest.raises(ChatAutomationError,match='后台模式'):getattr(adapter,method)(*args)


def _search_fixture(list_after_inputs):
    """搜索列表只在第 N 次写入后出现；返回 transport、adapter、edit 和打补丁的 walk。"""
    transport = object.__new__(WindowMessageTransport)
    pattern = Mock()
    edit = Mock();edit.GetValuePattern.return_value = pattern
    row = SimpleNamespace(Name='180k拼团', ControlTypeName='ListItemControl', GetChildren=lambda: [])
    header = SimpleNamespace(Name='群聊', ControlTypeName='TextControl', GetChildren=lambda: [])
    listing = SimpleNamespace(AutomationId='search_list', GetChildren=lambda: [header, row])
    title = SimpleNamespace(AutomationId='current_chat_name_label', Name='180k拼团')
    adapter = Mock();adapter.diagnose.return_value = {'ready': True, 'detail': ''}
    transport.chat_search = Mock(return_value=edit)
    clicked = []
    adapter._post_click.side_effect = lambda context, control, **kw: clicked.append(control)
    def walk(control):
        if control is row:return [row]
        if row in clicked:return [title]
        inputs = sum(1 for c in pattern.SetValue.call_args_list if c.args[0])
        return [listing] if list_after_inputs and inputs >= list_after_inputs else []
    return transport, adapter, edit, pattern, walk


def test_search_list_missing_after_first_input_reclicks_and_retypes():
    transport, adapter, edit, pattern, walk = _search_fixture(2)
    with patch('src.modules.window.wechat_window_messages.walk', side_effect=walk):
        context = transport.open_chat(adapter, '180k拼团')
    assert context.name == '180k拼团'
    assert [c.args[1] for c in adapter._post_click.call_args_list][:2] == [edit, edit]
    assert [c.args[0] for c in pattern.SetValue.call_args_list] == ['', '180k拼团', '', '180k拼团']


def test_search_list_never_appears_fails_after_bounded_retries():
    transport, adapter, edit, pattern, walk = _search_fixture(0)
    with patch('src.modules.window.wechat_window_messages.walk', side_effect=walk):
        with pytest.raises(ChatAutomationError, match='搜索列表需要唯一匹配，实际找到 0 个'):
            transport.open_chat(adapter, '180k拼团')
    assert adapter._post_click.call_count == 3
    assert pattern.SetValue.call_count == 6
