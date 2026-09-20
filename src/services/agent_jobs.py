# encoding: UTF-8
"""Agent 任务生命周期；同一后端只允许一个桌面任务。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Event, RLock, Thread, Timer
from uuid import uuid4


class JobConflict(RuntimeError):
    """桌面被占用，或幂等键对应不同请求。"""


class AgentJobs:
    """保存当前进程内的任务和取消事件，取消完成前不释放桌面。"""

    def __init__(self, runner):
        """注入 runner(kind, request, stop)，便于独立验证任务状态。"""
        self.runner = runner
        self.lock = RLock()
        self.jobs = {}
        self.keys = {}
        self.active = None
        self.closing = False

    def submit(self, kind, request, key=None):
        """创建任务；相同幂等键和参数返回原任务。"""
        with self.lock:
            if self.closing:
                raise JobConflict('服务正在关闭')
            if key and key in self.keys:
                job = self.jobs[self.keys[key]]
                if job['kind'] != kind or job['request'] != request:
                    raise JobConflict('幂等键已用于不同请求')
                return self.get(job['id'])
            if self.active:
                raise JobConflict(f'桌面任务正在运行：{self.active}')
            job_id = uuid4().hex
            job = dict(id=job_id, kind=kind, request=deepcopy(request), status='running',
                       created_at=datetime.now(timezone.utc).isoformat(), result=None,
                       error=None, stop=Event(), timed_out=False)
            self.jobs[job_id] = job
            if key:
                self.keys[key] = job_id
            self.active = job_id
            thread = Thread(target=self._run, args=(job_id,), daemon=True)
            job['thread'] = thread
            thread.start()
            return self.get(job_id)

    def get(self, job_id):
        """返回 JSON 可序列化快照，不暴露线程和事件。"""
        with self.lock:
            return deepcopy({k: v for k, v in self.jobs[job_id].items()
                             if k not in {'stop', 'thread'}})

    def cancel(self, job_id, *, timeout=False):
        """请求协作式停止；底层退出之前保持互斥。"""
        with self.lock:
            job = self.jobs[job_id]
            if job['status'] in {'running', 'stopping'}:
                job['timed_out'] = job['timed_out'] or timeout
                job['status'] = 'stopping'
                job['stop'].set()
            return self.get(job_id)

    def close(self):
        """停止接收请求并等待活动任务清理窗口和代理。"""
        with self.lock:
            self.closing = True
            job_id = self.active
            if not job_id:
                return
            self.cancel(job_id)
            thread = self.jobs[job_id]['thread']
        thread.join()

    def _run(self, job_id):
        """执行任务，超时只发取消信号，等待底层清理后结束。"""
        job = self.jobs[job_id]
        timer = Timer(job['request']['timeout_seconds'],
                      lambda: self.cancel(job_id, timeout=True))
        timer.daemon = True
        timer.start()
        result, error = None, None
        try:
            result = self.runner(job['kind'], job['request'], job['stop'])
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
        finally:
            timer.cancel()
            with self.lock:
                job['result'], job['error'] = result, error
                job['status'] = ('timed_out' if job['timed_out'] else
                                 'cancelled' if job['stop'].is_set() else
                                 'failed' if error else 'completed')
                job['finished_at'] = datetime.now(timezone.utc).isoformat()
                self.active = None


def navigate_chat(adapter, request, stop):
    """从当前可视位置向历史方向寻找指定日期，随后按预算滚动。"""
    from datetime import date
    from src.services.chat_attachment_service import older_prefix

    context = adapter.open_chat(request['chat'])
    adapter.prepare_dates(context)
    target = date.fromisoformat(request['date'])
    found = False
    reason = 'scan_limit'
    previous = None
    scrolls = 0
    page = None
    for index in range(request['max_pages']):
        if stop.is_set() or adapter.cancelled():
            return {'reason': 'cancelled'}
        page = adapter.read_page(context)
        if previous is not None:
            older_prefix(previous, page)
        days = [m.day or adapter.resolve_day(context, m) for m in page.messages]
        if target in days:
            found, reason = True, 'found'
            break
        if days and all(day is not None and day < target for day in days):
            reason = 'date_before_current_position'
            break
        if not page.messages or page.at_start:
            reason = 'history_start'
            break
        if index + 1 < request['max_pages']:
            previous = page
            adapter.scroll_older(context)
            scrolls += 1
    if found:
        for _ in range(request['scroll_pages']):
            if stop.is_set() or adapter.cancelled() or page.at_start:
                break
            adapter.scroll_older(context)
            current = adapter.read_page(context)
            older_prefix(page, current)
            page = current
            scrolls += 1
    return {'chat': adapter.chat_name(context), 'date': request['date'],
            'found': found, 'reason': reason, 'scrolls': scrolls,
            'visible_messages': [{'signature': m.signature, 'kind': m.kind,
                                  'date': m.day.isoformat() if m.day else None}
                                 for m in page.messages] if page else []}
