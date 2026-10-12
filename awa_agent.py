# encoding: UTF-8
# version: 1.0 @20260920: 增加本机 Agent 服务和 JSON 命令行客户端;
# version: 2.0 @20260920: 增加公众号自动导航、可见消息操作和归档正文读取;
# version: 2.1 @20261012: 固化 MTA 主线程初始化，增加微信就绪诊断和兼容读屏选项;
"""PW 交互桌面运行 serve；SSH 会话运行其余客户端命令。"""
from __future__ import annotations

__version__ = 'v2.1'
__platform__ = 'both'
__pc__ = ['PW', 'AW', 'BW', 'UU', 'PM']
__py__ = 'py3.13+'

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def parse_args(argv=None):
    """解析服务端和客户端参数；所有客户端结果输出 JSON。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('serve', help='仅在已登录、未锁屏的 Windows 桌面启动')
    sub.add_parser('health')
    diagnostic = sub.add_parser('diagnose', help='检查微信搜索控件；不切换对话')
    diagnostic.add_argument('--accessibility', choices=['auto', 'off', 'on'], default='auto')
    chat = sub.add_parser('chat', aliases=['chat-v2'])
    chat.add_argument('--chat', required=True)
    chat.add_argument('--date', required=True)
    chat.add_argument('--max-pages', type=int, default=100)
    chat.add_argument('--scroll-pages', type=int, default=0)
    chat.add_argument('--compatibility', action='store_true')
    article = sub.add_parser('articles', aliases=['articles-v2'])
    article.add_argument('--account', required=True)
    article.add_argument('--limit', type=int, default=10)
    article.add_argument('--collect-comments', action='store_true')
    article.add_argument('--archive-offline', action='store_true')
    article.add_argument('--include-collected', action='store_true')
    article.add_argument('--already-open', action='store_true', help='V2 跳过自动打开公众号')
    article.add_argument('--accessibility', choices=['auto', 'off', 'on'], default='auto', help='V2 兼容读屏模式')
    action = sub.add_parser('chat-action')
    action.add_argument('--chat', required=True)
    action.add_argument('--action', choices=['read', 'scroll', 'click'], default='read')
    action.add_argument('--page-token')
    action.add_argument('--message-id')
    action.add_argument('--scroll-pages', type=int, default=1)
    action.add_argument('--compatibility', action='store_true')
    content = sub.add_parser('content')
    content.add_argument('article_id', type=int)
    for child in (chat, article, action):
        child.add_argument('--timeout-seconds', type=int, default=600)
        child.add_argument('--idempotency-key')
        child.add_argument('--wait', action='store_true')
    for name in ('status', 'cancel', 'wait'):
        child = sub.add_parser(name)
        child.add_argument('job_id')
    sub.add_parser('accounts')
    saved = sub.add_parser('saved-articles')
    saved.add_argument('account_id', type=int)
    saved.add_argument('--page', type=int, default=1)
    return parser.parse_args(argv)


def call_api(path, token, *, payload=None, key=None, version=1):
    """调用固定 localhost 端口，避免凭据意外发往其他主机。"""
    headers = {'Authorization': 'Bearer ' + token}
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if key:
        headers['Idempotency-Key'] = key
    request = Request(f'http://127.0.0.1:8766/agent/v{version}/' + path, data=data, headers=headers)
    # 公众号采集会接管系统代理；本机控制请求必须直接连接服务。
    with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
        return json.load(response)


def wait_job(job_id, token):
    """持续查询至终态；中断客户端不会取消服务器任务。"""
    while True:
        result = call_api('jobs/' + job_id, token)
        if result['status'] not in {'running', 'stopping'}:
            return result
        time.sleep(0.5)


def serve(token):
    """先独占默认端口，再初始化数据库和队列，避免与 GUI 后端争用。"""
    if os.name != 'nt':
        raise RuntimeError('服务端仅支持 Windows；客户端可跨平台调用')
    # UIA 客户端为进程单例，必须在存活的 MTA 主线程创建，不能由短命请求线程首次创建。
    sys.coinit_flags = 0
    try:
        import uiautomation as auto
        import uvicorn
        from dev_server import create_dev_backend
        from src.services.agent_api import create_agent_app
    except ImportError as exc:
        raise RuntimeError('请使用项目 .venv 运行服务端') from exc
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', 8766))
        listener.listen(128)
        auto.GetRootControl()
        backend = create_dev_backend(project_root=Path(__file__).resolve().parent)
        app = create_agent_app(backend, token=token)
        print('awa_agent ' + __version__ + ' http://127.0.0.1:8766', file=sys.stderr)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8766))
        server.run(sockets=[listener])


def main(argv=None):
    """运行命令，成功返回 0，失败返回 1，所有错误使用 JSON。"""
    args = parse_args(argv)
    token = os.environ.get('AWA_AGENT_TOKEN', '').strip()
    try:
        if not token:
            raise ValueError('请先配置 AWA_AGENT_TOKEN 环境变量')
        if args.command == 'serve':
            serve(token)
            return 0
        if args.command in {'chat', 'articles', 'chat-v2', 'articles-v2', 'chat-action'}:
            payload = vars(args).copy()
            for key in ('command', 'wait', 'idempotency_key'):
                payload.pop(key)
            if args.command in {'articles', 'articles-v2'}:
                payload['skip_collected'] = not payload.pop('include_collected')
                already_open = payload.pop('already_open')
                if args.command == 'articles-v2':
                    payload['open_account'] = not already_open
                else:
                    payload.pop('accessibility')
            path = 'chat-actions' if args.command == 'chat-action' else (
                'chat-jobs' if args.command.startswith('chat') else 'article-jobs')
            options = {'version': 2} if args.command.endswith('-v2') or args.command == 'chat-action' else {}
            result = call_api(path, token, payload=payload, key=args.idempotency_key, **options)
            if args.wait:
                print(json.dumps({'job_id': result['id']}, ensure_ascii=False), file=sys.stderr)
                result = wait_job(result['id'], token)
        elif args.command == 'wait':
            result = wait_job(args.job_id, token)
        elif args.command in {'status', 'cancel'}:
            path = 'jobs/' + args.job_id
            result = call_api(path + '/cancel', token, payload={}) if args.command == 'cancel' else call_api(path, token)
        elif args.command == 'saved-articles':
            result = call_api(f'accounts/{args.account_id}/articles?page={args.page}', token)
        elif args.command == 'content':
            result = call_api(f'articles/{args.article_id}/content', token, version=2)
        elif args.command == 'diagnose':
            result = call_api('diagnose?accessibility=' + args.accessibility, token, version=2)
        else:
            result = call_api(args.command, token)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if (result.get('status') in {'failed', 'cancelled', 'timed_out'} or
                     args.command == 'diagnose' and not result.get('wechat_ready')) else 0
    except HTTPError as exc:
        print(json.dumps({'error': 'http_error', 'status': exc.code,
                          'detail': exc.read().decode('utf-8', errors='replace')}, ensure_ascii=False))
    except (OSError, URLError, ValueError, RuntimeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
    except KeyboardInterrupt:
        print(json.dumps({'error': '客户端已中断；已提交的任务仍在运行，请使用 cancel 停止'}, ensure_ascii=False))
    return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
