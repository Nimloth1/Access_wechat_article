# encoding: UTF-8
# version: 1.0 @20260906: 增加聊天附件下载、范围控制与诊断入口;
# version: 1.1 @20260907: 增加兼容控件读取、文件菜单和翻页焦点核验;
# version: 1.2 @20260908: 实机适配滚轮、图片气泡、转发遍历与文件接收;
# version: 1.3 @20260908: 补全点击日期、跳过无未下载标记文件、增加连续图片路线;
"""独立运行聊天附件任务；不启动公众号代理服务。"""
from __future__ import annotations

__version__ = 'v1.3'
__platform__ = 'nt'
__pc__ = ['PW', 'AW', 'BW', 'UU', 'PM']
__py__ = 'py3.13+'

import argparse
from contextlib import contextmanager
from datetime import date, datetime
import json
import os
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Event, Thread

if os.name == 'nt':
    sys.path.append('D:/Sync/Dropbox/Scripts/')
else:
    sys.path.append('/home/maeglin/Dropbox/Scripts/')

from src.modules.window.wechat_chat_adapter import WechatChatAdapter
from src.services.chat_attachment_service import AttachmentOptions, ChatAttachmentService

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / 'data' / 'chat_attachments'


def event_message(event: dict) -> str:
    """把底层事件转换为用户可读的进度说明，JSON 仅写入记录文件。"""
    statuses = {'candidate': '候选', 'confirmed': '界面确认下载完成', 'available': '界面显示已可用',
                'unconfirmed': '待核验', 'expired': '已过期', 'cancelled': '已停止', 'failed': '失败',
                'skipped_file': '无未下载标记，已跳过（未核验缓存）'}
    reasons = {'message_limit': '达到消息条数上限', 'forward_message_limit': '达到转发内部条数上限',
               'date_boundary': '已到截止日期之前', 'history_start': '已到记录起点', 'empty': '列表为空',
               'cancelled': '已停止', 'unknown_date': '日期不明确，跳过', 'depth_limit': '达到转发层数上限',
               'previous_limit': '达到上一张点击次数上限', 'previous_unavailable': '上一张按钮不可用，停止',
               'viewer_candidate': '已找到连续图片起点；仅扫描模式不打开预览'}
    prefix = f"[{event['chat']}] " if event.get('chat') else ''
    if event.get('path'):
        prefix += '消息 ' + ' / '.join(map(str, event['path'])) + '：'
    kind = event['event']
    if kind == 'viewer_progress':
        return prefix + ('已打开起始图片预览' if event['status'] == 'preview_opened' else
                         f"已点击上一张 {event['count']} 次（未核验原图下载完成）")
    if kind == 'forward_started':
        return prefix + '已进入合并转发，开始处理内部记录'
    if kind == 'date_resolving':
        return prefix + '正在补全日期，必要时查找前一个时间标签并返回'
    if kind == 'date_resolved':
        return prefix + '日期已补全为 ' + event['day']
    if kind in ('attachment', 'candidate'):
        media = {'image': '图片', 'file': '文件', 'forward': '合并转发'}.get(event.get('kind'), '')
        return prefix + media + ' · ' + statuses.get(event.get('status', 'candidate'), '待核验')
    if kind in ('chat_stopped', 'forward_stopped', 'skipped'):
        return prefix + reasons.get(event.get('reason'), str(event.get('reason', '')))
    if kind in ('error', 'fatal_error'):
        return prefix + '任务停止：' + event['detail']
    if kind == 'scan_progress':
        return prefix + f"已扫描 {event['scanned']} 条" + ('内部消息' if event.get('path') else '消息')
    if kind == 'finished':
        counts = event['counts']
        return (f"任务结束：扫描外层 {counts.get('messages', 0)} 条、转发内部 {counts.get('forward_messages', 0)} 条；"
                f"确认完成 {counts.get('confirmed', 0)}，已可用 {counts.get('available', 0)}，"
                f"待核验 {counts.get('unconfirmed', 0)}，日期不明跳过 {counts.get('unknown_date', 0)}，"
                f"无未下载标记文件跳过 {counts.get('skipped_file', 0)}，上一张点击 {counts.get('previous_clicked', 0)}，"
                f"错误 {counts.get('errors', 0)}。")
    return prefix + {'chat_started': '开始扫描', 'cancelled': '已停止'}.get(kind, kind)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行；不带参数时显示操作窗口。"""
    parser = argparse.ArgumentParser(description='微信聊天附件下载（F9 随时停止）')
    parser.add_argument('--diagnose', action='store_true', help='只检查微信控件是否可读')
    parser.add_argument('--compatibility', action='store_true', help='临时启用已核验微信版本的控件读取，结束时恢复')
    parser.add_argument('--config', type=Path, help='任务 JSON 配置文件')
    parser.add_argument('--chat', action='append', help='精确对话名称，可重复；不填使用当前对话')
    parser.add_argument('--since', help='向前扫描截止日期 YYYY-MM-DD，包含当天')
    parser.add_argument('--max-messages', type=int, default=100, help='每个对话最多扫描消息数，默认 100')
    parser.add_argument('--no-images', action='store_true')
    parser.add_argument('--no-files', action='store_true')
    parser.add_argument('--no-forwards', action='store_true')
    parser.add_argument('--max-depth', type=int, default=3)
    parser.add_argument('--max-forward-messages', type=int, default=1000)
    parser.add_argument('--download-timeout', type=float, default=30)
    parser.add_argument('--image-route', choices=['messages', 'viewer'], default='messages', help='逐条消息或连续图片路线')
    parser.add_argument('--previous-clicks', type=int, default=100, help='连续图片路线最多点击上一张次数，起始图片另计')
    parser.add_argument('--image-interval', type=float, default=1, help='每张图片停留秒数，默认 1')
    parser.add_argument('--dry-run', action='store_true', help='只扫描候选，不打开附件或转发窗口')
    parser.add_argument('--run', action='store_true', help='直接执行任务，不显示操作窗口')
    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    if args.config:
        task_flags = {'--chat', '--since', '--max-messages', '--no-images', '--no-files',
                      '--no-forwards', '--max-depth', '--max-forward-messages', '--download-timeout', '--dry-run', '--compatibility',
                      '--image-route', '--previous-clicks', '--image-interval'}
        if any(arg.split('=', 1)[0] in task_flags for arg in argv):
            parser.error('--config 不能与单独的任务参数混用，请修改 JSON 配置')
    return args


def options_from_dict(values: dict) -> AttachmentOptions:
    """加载配置并严格校验类型，防止字符串 false 被当作真值。"""
    allowed = set(AttachmentOptions.__dataclass_fields__)
    unknown = set(values) - allowed
    if unknown:
        raise ValueError('未知配置项：' + ', '.join(sorted(unknown)))
    values = dict(values)
    for key in ('images', 'files', 'forwards', 'dry_run', 'compatibility'):
        if key in values and type(values[key]) is not bool:
            raise ValueError(f'{key} 必须为 true 或 false')
    for key in ('max_messages', 'max_depth', 'max_forward_messages', 'previous_clicks'):
        if key in values and type(values[key]) is not int:
            raise ValueError(f'{key} 必须为整数')
    if 'download_timeout' in values and type(values['download_timeout']) not in (int, float):
        raise ValueError('download_timeout 必须为数字')
    if 'image_interval' in values and type(values['image_interval']) not in (int, float):
        raise ValueError('image_interval 必须为数字')
    chats = values.get('chats', [])
    if not isinstance(chats, (list, tuple)) or any(not isinstance(x, str) for x in chats):
        raise ValueError('chats 必须为对话名称数组')
    values['chats'] = tuple(x.strip() for x in chats)
    if values.get('since'):
        values['since'] = date.fromisoformat(values['since'])
    else:
        values['since'] = None
    return AttachmentOptions(**values)


@contextmanager
def task_lock():
    """同一项目只允许运行一个附件任务，异常退出后系统自动释放锁。"""
    try:
        import msvcrt
    except ImportError as exc:
        raise RuntimeError('聊天附件操作仅支持 Windows') from exc
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / 'task.lock').open('a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('另一个附件任务正在运行，请先停止它') from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def execute(options: AttachmentOptions, stop: Event, notify=print) -> dict:
    """运行任务并逐条刷新本地日志；不向外部发送通知或聊天内容。"""
    with task_lock():
        notify('当前为【仅扫描候选】：不会打开图片、进入转发或下载文件。' if options.dry_run else '当前为【执行采集】：将操作微信附件。')
        if options.image_route == 'viewer':
            notify(f'连续图片路线：最多查找 {options.max_messages} 条消息定位起始图片，再点击上一张 {options.previous_clicks} 次，每张停留 {options.image_interval:g} 秒；不筛日期、不处理文件或转发。')
        log_path = LOG_DIR / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.jsonl')
        notify(f'处理记录：{log_path}')
        with log_path.open('w', encoding='utf-8', newline='\n') as log:
            def emit(event: dict) -> None:
                """持久化当前事件，再通知调用端。"""
                line = json.dumps(event, ensure_ascii=False)
                log.write(line + '\n')
                log.flush()
                notify(event_message(event))

            try:
                adapter = WechatChatAdapter(stop)
                with adapter.compatibility(options.compatibility):
                    return ChatAttachmentService(adapter, emit).run(options)
            except Exception as exc:
                emit({'event': 'fatal_error', 'detail': str(exc)})
                raise


def show_gui() -> None:
    """显示简洁任务窗口；工作线程通过队列更新界面。"""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError as exc:
        raise RuntimeError('当前 Python 没有 tkinter，可使用 --run 命令行模式') from exc
    root = tk.Tk()
    root.title('微信聊天附件下载 · 试验版')
    root.geometry('800x760')
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='先在微信打开对话；任务从当前消息位置向前扫描。').pack(anchor='w')
    ttk.Label(frame, text='下载到微信自身缓存，文档可能自动打开。请勿与公众号采集同时运行；F9 可停止。').pack(anchor='w', pady=(0, 12))
    ttk.Label(frame, text='指定对话（每行一个完整名称；留空使用当前对话）').pack(anchor='w')
    chats = tk.Text(frame, height=3, width=70)
    chats.pack(fill='x', pady=5)
    settings = ttk.Frame(frame)
    settings.pack(fill='x', pady=6)
    since = tk.StringVar()
    maximum = tk.StringVar(value='100')
    ttk.Label(settings, text='截止日期').grid(row=0, column=0, sticky='w')
    since_entry = ttk.Entry(settings, textvariable=since, width=15)
    since_entry.grid(row=0, column=1, padx=8)
    maximum_label = ttk.Label(settings, text='每个对话最多扫描条数')
    maximum_label.grid(row=0, column=2)
    ttk.Entry(settings, textvariable=maximum, width=10).grid(row=0, column=3, padx=8)
    ttk.Label(frame, text='日期格式 2026-08-01，包含当天；日期和数量先达到哪个就停止。').pack(anchor='w')
    routes = ttk.Frame(frame)
    routes.pack(fill='x', pady=6)
    route = tk.StringVar(value='逐条聊天记录')
    previous_clicks = tk.StringVar(value='100')
    image_interval = tk.StringVar(value='1')
    route_box = ttk.Combobox(routes, textvariable=route, state='readonly',
                             values=('逐条聊天记录', '连续图片上一张'), width=18)
    route_box.pack(side='left')
    ttk.Label(routes, text='上一张最多点击').pack(side='left', padx=(12, 0))
    previous_entry = ttk.Entry(routes, textvariable=previous_clicks, width=7)
    previous_entry.pack(side='left', padx=4)
    ttk.Label(routes, text='次，每张停留').pack(side='left')
    interval_entry = ttk.Entry(routes, textvariable=image_interval, width=6)
    interval_entry.pack(side='left', padx=4)
    ttk.Label(routes, text='秒').pack(side='left')
    ttk.Label(frame, text='连续图片：起始图片 + N 次上一张；不按日期筛选，不处理文件和合并转发。').pack(anchor='w')
    choices = ttk.Frame(frame)
    choices.pack(fill='x', pady=8)
    images, files, forwards, dry = [tk.BooleanVar(value=True) for _ in range(4)]
    compatibility = tk.BooleanVar(value=True)
    media_choices = []
    for text, var in [('图片', images), ('仅未下载文件', files), ('展开合并转发', forwards), ('仅扫描候选（不下载）', dry)]:
        choice = ttk.Checkbutton(choices, text=text, variable=var)
        choice.pack(side='left', padx=(0, 10))
        if var is not dry:
            media_choices.append(choice)

    def update_route(event=None) -> None:
        """让当前路线不适用的设置不可编辑，保留切换前的填写值。"""
        viewer = route.get() == '连续图片上一张'
        since_entry.configure(state='disabled' if viewer else 'normal')
        maximum_label.configure(text='查找起始图片最多消息数' if viewer else '每个对话最多扫描条数')
        for choice in media_choices:
            choice.configure(state='disabled' if viewer else 'normal')
        for entry in (previous_entry, interval_entry):
            entry.configure(state='normal' if viewer else 'disabled')

    route_box.bind('<<ComboboxSelected>>', update_route)
    update_route()
    ttk.Checkbutton(frame, text='启用兼容读取（仅已核验微信版本；任务结束恢复运行时标志）', variable=compatibility).pack(anchor='w')
    ttk.Label(frame, text='转发最多 3 层，每个对话内部合计最多 1,000 条；仅扫描时不进入转发。').pack(anchor='w')
    controls = ttk.Frame(frame)
    controls.pack(fill='x', pady=10)
    output = tk.Text(frame, height=17, wrap='word', state='disabled')
    output.pack(fill='both', expand=True)
    queue = Queue()
    stop = Event()
    state = {'running': False, 'closing': False}

    def append(text: str) -> None:
        """在主线程追加运行信息。"""
        output.configure(state='normal')
        output.insert('end', text + '\n')
        output.see('end')
        output.configure(state='disabled')

    def start(diagnose: bool = False) -> None:
        """冻结本次设置，启动一个工作线程。"""
        if state['running']:
            return
        try:
            viewer = route.get() == '连续图片上一张'
            options = options_from_dict({'chats': chats.get('1.0', 'end').strip().splitlines(),
                'since': None if viewer else since.get().strip(), 'max_messages': int(maximum.get()),
                'images': True if viewer else images.get(), 'files': False if viewer else files.get(),
                'forwards': False if viewer else forwards.get(), 'dry_run': dry.get(),
                'image_route': 'viewer' if viewer else 'messages', 'previous_clicks': int(previous_clicks.get()),
                'image_interval': float(image_interval.get()), 'compatibility': compatibility.get()})
        except (ValueError, TypeError) as exc:
            messagebox.showerror('设置有误', str(exc))
            return
        state['running'] = True
        stop.clear()
        run_button.configure(state='disabled')
        diagnose_button.configure(state='disabled')

        def worker() -> None:
            """工作线程独立初始化 UIA，结束时释放 COM。"""
            try:
                import uiautomation as auto
                with auto.UIAutomationInitializerInThread():
                    if diagnose:
                        with task_lock():
                            adapter = WechatChatAdapter(stop)
                            with adapter.compatibility(options.compatibility):
                                queue.put(adapter.diagnose()['detail'])
                    else:
                        execute(options, stop, queue.put)
            except Exception as exc:
                queue.put('任务停止：' + str(exc))
            finally:
                queue.put(None)

        Thread(target=worker, daemon=True).start()

    def pump() -> None:
        """读取消息队列，关闭窗口前等待工作线程退出。"""
        try:
            while True:
                item = queue.get_nowait()
                if item is None:
                    state['running'] = False
                    run_button.configure(state='normal')
                    diagnose_button.configure(state='normal')
                    append('任务已结束。')
                else:
                    append(item)
        except Empty:
            pass
        if state['closing'] and not state['running']:
            root.destroy()
            return
        root.after(100, pump)

    def close() -> None:
        """关闭时先取消任务，保留子窗口清理和日志写入机会。"""
        state['closing'] = True
        stop.set()

    diagnose_button = ttk.Button(controls, text='检查微信', command=lambda: start(True))
    diagnose_button.pack(side='left', padx=(0, 8))
    run_button = ttk.Button(controls, text='开始', command=start)
    run_button.pack(side='left', padx=8)
    ttk.Button(controls, text='停止', command=stop.set).pack(side='left', padx=8)
    ttk.Button(controls, text='打开处理记录', command=lambda: (LOG_DIR.mkdir(parents=True, exist_ok=True), os.startfile(LOG_DIR))).pack(side='right')
    root.protocol('WM_DELETE_WINDOW', close)
    root.after(100, pump)
    root.mainloop()


def main() -> int:
    """程序入口；诊断不可运行或任务出错时返回非零状态。"""
    sys.stdout.reconfigure(line_buffering=True)
    print('\nChatAttachments %s' % __version__)
    args = parse_args()
    try:
        if os.name != 'nt':
            raise RuntimeError('聊天附件操作仅支持 Windows')
        if args.diagnose:
            with task_lock():
                adapter = WechatChatAdapter()
                with adapter.compatibility(args.compatibility):
                    result = adapter.diagnose()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['ready'] else 2
        if not args.run and not args.config:
            show_gui()
            return 0
        if args.config:
            if not args.config.is_file():
                raise ValueError(f'配置文件不存在：{args.config}')
            values = json.loads(args.config.read_text(encoding='utf-8-sig'))
            if not isinstance(values, dict):
                raise ValueError('配置文件必须是 JSON 对象')
        else:
            values = {'chats': args.chat or [], 'since': args.since, 'max_messages': args.max_messages,
                'images': not args.no_images, 'files': not args.no_files, 'forwards': not args.no_forwards,
                'max_depth': args.max_depth, 'max_forward_messages': args.max_forward_messages,
                'image_route': args.image_route, 'previous_clicks': args.previous_clicks, 'image_interval': args.image_interval,
                'download_timeout': args.download_timeout, 'dry_run': args.dry_run, 'compatibility': args.compatibility}
        result = execute(options_from_dict(values), Event())
        return 2 if result.get('errors') or result.get('cancelled') else 0
    except (RuntimeError, ValueError, OSError, TypeError) as exc:
        print('无法运行：' + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
