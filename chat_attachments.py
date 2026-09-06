# encoding: UTF-8
# version: 1.0 @20260906: 增加聊天附件下载、范围控制与诊断入口;
"""独立运行聊天附件任务；不启动公众号代理服务。"""
from __future__ import annotations

__version__ = 'v1.0'
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
                'unconfirmed': '待核验', 'expired': '已过期', 'cancelled': '已停止', 'failed': '失败'}
    reasons = {'message_limit': '达到消息条数上限', 'forward_message_limit': '达到转发内部条数上限',
               'date_boundary': '已到截止日期之前', 'history_start': '已到记录起点', 'empty': '列表为空',
               'cancelled': '已停止', 'unknown_date': '日期不明确，跳过', 'depth_limit': '达到转发层数上限'}
    prefix = f"[{event['chat']}] " if event.get('chat') else ''
    if event.get('path'):
        prefix += '消息 ' + ' / '.join(map(str, event['path'])) + '：'
    kind = event['event']
    if kind in ('attachment', 'candidate'):
        media = {'image': '图片', 'file': '文件', 'forward': '合并转发'}.get(event.get('kind'), '')
        return prefix + media + ' · ' + statuses.get(event.get('status', 'candidate'), '待核验')
    if kind in ('chat_stopped', 'forward_stopped', 'skipped'):
        return prefix + reasons.get(event.get('reason'), str(event.get('reason', '')))
    if kind in ('error', 'fatal_error'):
        return prefix + '任务停止：' + event['detail']
    if kind == 'finished':
        counts = event['counts']
        return (f"任务结束：扫描外层 {counts.get('messages', 0)} 条、转发内部 {counts.get('forward_messages', 0)} 条；"
                f"确认完成 {counts.get('confirmed', 0)}，已可用 {counts.get('available', 0)}，"
                f"待核验 {counts.get('unconfirmed', 0)}，错误 {counts.get('errors', 0)}。")
    return prefix + {'chat_started': '开始扫描', 'cancelled': '已停止'}.get(kind, kind)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行；不带参数时显示操作窗口。"""
    parser = argparse.ArgumentParser(description='微信聊天附件下载（F9 随时停止）')
    parser.add_argument('--diagnose', action='store_true', help='只检查微信控件是否可读')
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
    parser.add_argument('--dry-run', action='store_true', help='只扫描候选，不打开附件或转发窗口')
    parser.add_argument('--run', action='store_true', help='直接执行任务，不显示操作窗口')
    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    if args.config:
        task_flags = {'--chat', '--since', '--max-messages', '--no-images', '--no-files',
                      '--no-forwards', '--max-depth', '--max-forward-messages', '--download-timeout', '--dry-run'}
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
    for key in ('images', 'files', 'forwards', 'dry_run'):
        if key in values and type(values[key]) is not bool:
            raise ValueError(f'{key} 必须为 true 或 false')
    for key in ('max_messages', 'max_depth', 'max_forward_messages'):
        if key in values and type(values[key]) is not int:
            raise ValueError(f'{key} 必须为整数')
    if 'download_timeout' in values and type(values['download_timeout']) not in (int, float):
        raise ValueError('download_timeout 必须为数字')
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
    root.geometry('760x650')
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='先在微信打开对话；任务从当前消息位置向前扫描。').pack(anchor='w')
    ttk.Label(frame, text='下载到微信自身缓存。请勿与公众号采集同时运行；F9 可停止。').pack(anchor='w', pady=(0, 12))
    ttk.Label(frame, text='指定对话（每行一个完整名称；留空使用当前对话）').pack(anchor='w')
    chats = tk.Text(frame, height=3, width=70)
    chats.pack(fill='x', pady=5)
    settings = ttk.Frame(frame)
    settings.pack(fill='x', pady=6)
    since = tk.StringVar()
    maximum = tk.StringVar(value='100')
    ttk.Label(settings, text='截止日期').grid(row=0, column=0, sticky='w')
    ttk.Entry(settings, textvariable=since, width=15).grid(row=0, column=1, padx=8)
    ttk.Label(settings, text='每个对话最多扫描条数').grid(row=0, column=2)
    ttk.Entry(settings, textvariable=maximum, width=10).grid(row=0, column=3, padx=8)
    ttk.Label(frame, text='日期格式 2026-08-01，包含当天；日期和数量先达到哪个就停止。').pack(anchor='w')
    choices = ttk.Frame(frame)
    choices.pack(fill='x', pady=8)
    images, files, forwards, dry = [tk.BooleanVar(value=True) for _ in range(4)]
    for text, var in [('图片原图', images), ('文件', files), ('展开合并转发', forwards), ('仅扫描候选', dry)]:
        ttk.Checkbutton(choices, text=text, variable=var).pack(side='left', padx=(0, 10))
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
            options = options_from_dict({'chats': chats.get('1.0', 'end').strip().splitlines(),
                'since': since.get().strip(), 'max_messages': int(maximum.get()),
                'images': images.get(), 'files': files.get(), 'forwards': forwards.get(), 'dry_run': dry.get()})
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
                        queue.put(WechatChatAdapter(stop).diagnose()['detail'])
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
    print('\nChatAttachments %s' % __version__)
    args = parse_args()
    try:
        if os.name != 'nt':
            raise RuntimeError('聊天附件操作仅支持 Windows')
        if args.diagnose:
            result = WechatChatAdapter().diagnose()
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
                'download_timeout': args.download_timeout, 'dry_run': args.dry_run}
        result = execute(options_from_dict(values), Event())
        return 2 if result.get('errors') or result.get('cancelled') else 0
    except (RuntimeError, ValueError, OSError, TypeError) as exc:
        print('无法运行：' + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
