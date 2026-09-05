from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

from src.modules.window import uia_window_test_reader as reader
from src.modules.window.window_models import WindowInfo


class Control:
    """仅模拟读取器依赖的 UIA 父子、兄弟关系和矩形。"""

    def __init__(self, kind="GroupControl", name="", rect=(0, 0, 400, 700), children=()):
        self.ControlTypeName = kind
        self.Name = name
        self.BoundingRectangle = rect
        self.children = list(children)
        self.parent = None
        for child in self.children:
            child.parent = self

    def GetChildren(self):
        """返回直接子节点。"""
        return self.children

    def GetFirstChildControl(self):
        """返回首个子节点，空节点返回 None。"""
        return next(iter(self.children), None)

    def GetLastChildControl(self):
        """返回最后一个子节点。"""
        return self.children[-1] if self.children else None

    def _sibling(self, offset):
        """按偏移返回同级节点。"""
        if self.parent is None:
            return None
        siblings = self.parent.children
        index = siblings.index(self) + offset
        return siblings[index] if 0 <= index < len(siblings) else None

    def GetNextSiblingControl(self):
        """返回下一个兄弟节点。"""
        return self._sibling(1)

    def GetPreviousSiblingControl(self):
        """返回上一个兄弟节点。"""
        return self._sibling(-1)

    def GetRuntimeId(self):
        """返回对象在测试内的稳定标识。"""
        return (id(self),)


def row(text, top, bottom):
    """生成包裹单个文本节点的日期行或文章卡片。"""
    rect = (10, top, 390, bottom)
    return Control(rect=rect, children=[Control("TextControl", text, rect)])


def snapshot(flat, *, viewport_top=0):
    """通过真实读取入口验证新旧两种主页结构。"""
    today = [row("今天", 10, 40), row("甲文", 40, 200)]
    yesterday = [row("昨天", 210, 240), row("乙文", 240, 400), row("丙文", 400, 550)]
    more = row("余下 2 篇", 550, 590)
    if flat:
        article_list = Control(children=[*today, *yesterday, more])
    else:
        article_list = Control(children=[
            Control(rect=(10, 10, 390, 200), children=today),
            Control(rect=(10, 210, 390, 590), children=[*yesterday, more]),
        ])
    # 外层包裹必须存在，才能重现把整张平铺列表误认成一个日期组的问题。
    document = Control("DocumentControl", children=[Control(children=[article_list])])
    window = WindowInfo(1, "微信", "Chrome_WidgetWin_0", "WeChatAppEx.exe", (0, 0, 400, 700), control=document)
    window = replace(window, rect=(0, viewport_top, 400, 700))
    instance = reader.UiaWindowTestReader()
    with patch.object(reader, "find_wechat_document_control", return_value=document):
        return instance.read(window), instance.read_date_groups(window)


def test_flat_date_rows_are_not_clicked_as_articles():
    """平铺日期行不能生成 xxx 伪文章，后续文章应归属各自日期。"""
    result, dates = snapshot(True)
    assert [(c.date_text, c.title) for c in result.visible_cards] == [
        ("今天", "甲文"), ("昨天", "乙文"), ("昨天", "丙文"), ("昨天", "余下 2 篇"),
    ]
    assert [g.date_text for g in dates.groups] == ["今天", "昨天"]
    assert result.visible_cards[-1].is_more_trigger
    assert result.visible_cards[-1].remaining_count == 2


def test_nested_date_groups_remain_supported():
    """旧版嵌套日期组与新版平铺结构应返回相同文章和日期。"""
    flat, _ = snapshot(True)
    nested, _ = snapshot(False)
    assert [(c.date_text, c.title) for c in nested.all_cards] == [
        (c.date_text, c.title) for c in flat.all_cards
    ]


def test_scrolled_flat_group_keeps_date_when_header_is_offscreen():
    """日期行滚出视口后，组范围仍覆盖尚在视口内的文章。"""
    result, dates = snapshot(True, viewport_top=300)
    assert [c.title for c in result.visible_cards] == ["乙文", "丙文", "余下 2 篇"]
    assert [g.date_text for g in dates.visible_groups] == ["昨天"]
    assert all(c.date_text == "昨天" for c in result.visible_cards)
