# encoding: UTF-8
# version: 1.0 @20261018: 锁屏下向同会话微信的已核验窗口投递消息;
# version: 1.1 @20261026: 搜索列表未出现时重新点击搜索框并重输，有限重试;
"""只操作同会话微信 HWND，不激活系统前台、不模拟鼠标键盘。"""
import ctypes
from ctypes import wintypes
import time
import psutil
from src.modules.window.wechat_chat_adapter import ChatContext,walk,unique,local_chat_matches
from src.modules.window.wechat_home_window_finder import rect_to_tuple
from src.modules.window.window_diagnostics import _Reader
from src.services.chat_attachment_service import ChatAutomationError


class WindowMessageTransport:
    def __init__(self):
        """记录调用者会话及目标进程身份，拒绝跨会话操作。"""
        self._message_owners = {}
        self._message_session = _Reader().session()['id']

    def verify_window(self, context):
        """核验活动句柄、微信进程、同会话、可见性和身份稳定性。"""
        reader = _Reader()
        window = context.window
        pid = wintypes.DWORD()
        if not reader.user32.IsWindow(window.handle):
            raise ChatAutomationError('窗口消息目标已消失')
        reader.user32.GetWindowThreadProcessId(window.handle,ctypes.byref(pid))
        process = psutil.Process(pid.value)
        session = wintypes.DWORD()
        if not reader.kernel32.ProcessIdToSessionId(pid.value,ctypes.byref(session)):
            raise ChatAutomationError('窗口会话读取失败')
        identity = (pid.value,process.create_time())
        if (session.value != self._message_session or not self._message_session
                or pid.value != window.control.ProcessId
                or process.name().lower() not in {'weixin.exe','wechat.exe'}):
            raise ChatAutomationError('窗口消息目标不是同会话的已核验微信')
        if window.handle in self._message_owners and self._message_owners[window.handle] != identity:
            raise ChatAutomationError('窗口消息句柄被其他进程复用')
        self._message_owners[window.handle] = identity
        if not reader.user32.IsWindowVisible(window.handle) or reader.user32.IsIconic(window.handle):
            raise ChatAutomationError('窗口消息目标不可见或已最小化')
        rect = wintypes.RECT()
        if not reader.user32.GetWindowRect(window.handle,ctypes.byref(rect)):
            raise ChatAutomationError('窗口消息目标范围读取失败')
        return rect.left,rect.top,rect.right,rect.bottom

    def activate(self, context, *, force=False):
        """仅向微信发送内部活动消息，Windows前台与锁屏保持原样。"""
        self.verify_window(context)
        # 仅向已核验微信进程表达控件焦点，不改变Windows前台或锁屏。
        u = ctypes.WinDLL('user32',use_last_error=True)
        u.PostMessageW.argtypes = [wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        for msg in (0x001C,0x0006,0x0007):
            if not u.PostMessageW(context.window.handle,msg,1 if msg in (0x001C,0x0006) else 0,0):
                raise ChatAutomationError('无法投递微信窗口焦点消息')
        time.sleep(.1)

    def check_point(self, context, x, y, *, phase='point_check'):
        """窗口消息只接受已核验窗口内部的坐标，不用全屏命中替代归属。"""
        left,top,right,bottom = self.verify_window(context)
        if not (left <= x < right and top <= y < bottom):
            raise ChatAutomationError('窗口消息坐标超出已核验微信窗口')

    def open_chat(self, adapter, name):
        """精确搜索并以标题回读验收；不调用全局输入或前台激活。"""
        diagnostic = adapter.diagnose()
        nodes = walk(adapter.main.control)
        titles = [n for n in nodes if n.AutomationId.endswith('current_chat_name_label')]
        if name is None or len(titles)==1 and titles[0].Name.strip()==name:
            title = unique(titles,'当前聊天标题')
            if not diagnostic['ready']:raise ChatAutomationError(diagnostic['detail'])
            return ChatContext(adapter.main,title.Name.strip())
        context = ChatContext(adapter.main,'',switching=True)
        edit = self.chat_search(adapter, context, nodes)
        pattern = edit.GetValuePattern()
        if pattern is None:raise ChatAutomationError('搜索框没有ValuePattern')
        # 投递的点击是异步的；焦点未到位时写值，微信不弹搜索列表，故重新点击并重输。
        for _ in range(3):
            adapter._post_click(context,edit)
            adapter._pause(.3)
            pattern.SetValue('');pattern.SetValue(name)
            for _ in range(15):
                lists = [n for n in walk(adapter.main.control) if n.AutomationId=='search_list']
                if len(lists)==1:break
                adapter._pause(.2)
            if lists:break
        listing = unique(lists,'搜索列表')
        match = unique(local_chat_matches(listing.GetChildren(),name),'精确会话')
        adapter._post_click(context,match,keep_parent_active=True)
        for _ in range(25):
            titles = [n for n in walk(adapter.main.control) if n.AutomationId.endswith('current_chat_name_label')]
            if len(titles)==1 and titles[0].Name.strip()==name:
                return ChatContext(adapter.main,name)
            adapter._pause(.2)
        raise ChatAutomationError('窗口消息未能打开精确会话')

    def chat_search(self, adapter, context, nodes):
        """窄布局先返回会话列表；仅接受聊天标题栏内的明确返回按钮。"""
        if any(getattr(n, 'AutomationId', '') == 'session_list' and n.Name == '折叠的聊天' for n in nodes):
            backs = [n for n in nodes if getattr(n, 'ControlTypeName', '') == 'ButtonControl'
                     and n.ClassName == 'mmui::ChatBackwardView' and n.Name == '返回']
            adapter._post_click(context, unique(backs, '折叠会话列表返回按钮'))
            for _ in range(25):
                nodes = adapter._nodes(context)
                if not any(getattr(n, 'AutomationId', '') == 'session_list' and n.Name == '折叠的聊天' for n in nodes):
                    break
                adapter._pause(.2)
            else:
                raise ChatAutomationError('未能返回正常会话列表')
        edits = adapter._search_edits(nodes)
        if edits:
            return unique(edits, '会话搜索框')
        backs = []
        if not any(n.ClassName == 'mmui::ChatMasterView' for n in nodes):
            for node in nodes:
                if node.ControlTypeName != 'ButtonControl' or node.Name not in {'返回', 'Back', '返回上頁'}:
                    continue
                parent = node.GetParentControl()
                for _ in range(8):
                    if parent is None:
                        break
                    if parent.ClassName == 'mmui::ChatTitleBarMasterView':
                        backs.append(node)
                        break
                    parent = parent.GetParentControl()
        adapter._post_click(context, unique(backs, '折叠会话标题栏返回按钮'))
        for _ in range(25):
            edits = adapter._search_edits(adapter._nodes(context))
            if edits:
                return unique(edits, '会话搜索框')
            adapter._pause(.2)
        raise ChatAutomationError('返回后会话搜索框仍不可读')

    def wheel(self, adapter, context, *, older, steps=1):
        """只向已核验列表所在窗口投递滚轮，不调用物理滚轮。"""
        self.activate(context)
        listing = adapter._list(context)
        bounds = rect_to_tuple(listing.BoundingRectangle)
        x, y = (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2
        self.check_point(context, x, y)
        if adapter.cancelled():
            raise ChatAutomationError('已取消')
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.PostMessageW.restype = wintypes.BOOL
        delta = 120 if older else -120
        for _ in range(steps):
            if not user.PostMessageW(context.window.handle, 0x020A, (delta & 0xffff) << 16,
                                     ((int(y) & 0xffff) << 16) | (int(x) & 0xffff)):
                raise ChatAutomationError('窗口消息滚动失败')
            adapter._pause(.05)
