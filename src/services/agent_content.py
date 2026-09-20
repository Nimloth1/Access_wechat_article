# encoding: UTF-8
"""只读归档正文，不接受客户端提供的文件路径。"""
from html.parser import HTMLParser
from pathlib import Path

from src.storage.sqlite.connection import sqlite_connection


class BodyText(HTMLParser):
    """提取 js_content 中可读文本，忽略脚本和样式，保留段落分隔。"""
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        """初始化正文栈与输出。"""
        super().__init__(convert_charrefs=True)
        self.stack, self.parts = [], []
        self.found = False

    def handle_starttag(self, tag, attrs):
        """仅进入指定正文容器及其后代。"""
        if not self.stack and dict(attrs).get('id') != 'js_content':
            return
        self.found = True
        if tag in {'p', 'div', 'section', 'br', 'li'}:
            self.parts.append('\n')
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        """处理自闭合标签，不改变后续文本的归属。"""
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        """匹配栈内闭合标签。"""
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]
            if tag in {'p', 'div', 'section', 'li'}:
                self.parts.append('\n')

    def handle_data(self, data):
        """忽略不可见执行内容。"""
        if self.stack and not {'script', 'style'} & set(self.stack):
            self.parts.append(data)


def article_content(backend, article_id):
    """按数据库 ID 返回归档正文；路径逃逸、缺失、损坏均明确失败。"""
    from fastapi import HTTPException
    from src.modules.request.article_parser import _is_text_share_article, _extract_title

    with sqlite_connection(backend.db_path, write=False) as connection:
        row = connection.execute(
            'SELECT article_title, article_link, archive_dir FROM awa_public_articles WHERE id = ?',
            (article_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '文章不存在')
    root = Path(backend.runtime.config.storage.article_storage_root).resolve()
    path = (root / str(row['archive_dir'] or '') / 'origin' / 'main.html').resolve()
    if not row['archive_dir'] or not path.is_relative_to(root):
        raise HTTPException(404, '归档路径无效')
    try:
        with path.open('rb') as stream:
            raw = stream.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise HTTPException(413, '正文源文件超过 10 MiB 上限')
        source = raw.decode('utf-8-sig')
    except FileNotFoundError:
        raise HTTPException(404, '正文尚未归档') from None
    except (OSError, UnicodeError):
        raise HTTPException(422, '正文文件不可读取') from None
    parser = BodyText()
    parser.feed(source)
    text = '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())
    if not parser.found:
        if _is_text_share_article(source):
            text = _extract_title(source)
        else:
            raise HTTPException(422, '归档中未识别到文章正文')
    return dict(id=article_id, title=row['article_title'], url=row['article_link'],
                format='text', content=text, text_empty=not bool(text))
