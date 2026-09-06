from types import SimpleNamespace
from threading import Event

import pytest

from src.services.main_flow.article_dispatcher import ArticleDispatcher
from src.services.main_flow.home_scan_service import HomeScanService
from src.services.main_flow.main_flow_coordinator import MainFlowCoordinator
from src.services.main_flow.main_flow_models import MainFlowCommand, SingleArticleOptions
from src.services.main_flow.main_flow_state import MainFlowState
from test_uia_window_test_reader import snapshot


@pytest.mark.parametrize('mode', ['before', 'range'])
@pytest.mark.parametrize('limit', [0, 1])
@pytest.mark.parametrize('paginated', [False, True])
def test_date_boundary_drains_all_cards_with_fresh_coordinates(mode, limit, paginated):
    """不限量须处理边界当天全部文章；返回主页后下一篇坐标必须更新。"""
    from dataclasses import replace
    from src.modules.window.article_date_filter import ArticleDateFilter
    from src.services.main_flow.home_scan_service import HomeScanSession

    original, _ = snapshot(True)
    cards = tuple(c for c in original.visible_cards if not c.is_more_trigger)
    dates = sorted({c.published_date for c in cards})
    command = MainFlowCommand.from_payload({
        'recordLimit': limit, 'dateFilterMode': mode,
        'startDate': dates[0], 'endDate': dates[0] if mode == 'before' else dates[-1],
    })
    reads = []
    dispatched = []
    page = [0]

    def read(_window):
        """每次返回主页模拟布局向下移动，旧快照点击会失败。"""
        offset = len(dispatched) * 20
        reads.append(offset)
        visible = (cards[:1] if page[0] == 0 else cards[1:]) if paginated else cards
        shifted = tuple(replace(c, visible_rect=(10, c.visible_rect[1] + offset, 390, c.visible_rect[3] + offset),
                                click_point=(200, c.click_point[1] + offset)) for c in visible)
        older = replace(cards[-1], published_date='2020-01-01', raw_title='范围外文章')
        return replace(original, visible_cards=shifted if paginated and page[0] == 0 else (*shifted, older))

    def scroll(_session):
        """模拟下一屏，日期边界出现后不应再次滚动。"""
        assert paginated and page[0] == 0
        page[0] += 1
        return True

    scanner = HomeScanService(window_factory=None, config=SimpleNamespace())
    session = HomeScanSession(window=SimpleNamespace(handle=1), account_name='测试号', cursor=None,
                              scroller=None, command=command,
                              date_filter=ArticleDateFilter.create(mode=mode, start_date=dates[0],
                                                                  end_date=command.end_date),
                              snapshot_reader=SimpleNamespace(read=read))
    scanner.initialize = lambda _: session
    scanner.scroll_next = scroll

    def runner(target, **_kwargs):
        """按最新布局校验实际分发坐标，并返回正常完成回执。"""
        card = next(c for c in cards if c.raw_title == target.title_raw)
        assert target.click_point[1] == card.click_point[1] + len(dispatched) * 20
        dispatched.append(target)
        return {'phase': 'finished', 'status': 'success', 'foreground_done': True,
                'tab_closed': True, 'progress_counted': True, 'article_saved': True}

    dispatcher = ArticleDispatcher(runner=runner)
    state = MainFlowState(task_id='test', target_count=limit)
    context = SimpleNamespace(state=state, cancel_token=Event())
    MainFlowCoordinator(scanner=scanner, dispatcher=dispatcher).run(context, command)
    assert state.snapshot()['status'] == 'completed'
    assert len(dispatched) == (limit or 3)
    assert len({t.fingerprint for t in dispatched}) == len(dispatched)
    assert [t.sequence for t in dispatched] == list(range(1, len(dispatched) + 1))


def test_terminal_failure_wakes_foreground_wait_without_claiming_safe_home():
    """前台打开失败应立即上报原错误，不能等到回执超时或谎报已回主页。"""
    dispatcher = ArticleDispatcher(runner=lambda **_: {
        'phase': 'finished', 'status': 'failed', 'error_detail': '未检测到已打开的文章标签',
    })
    target = SimpleNamespace(fingerprint='test', sequence=1, title_raw='测试', title_display='测试', article_date='2026-09-06')
    handle = dispatcher.submit(target, SingleArticleOptions(), SimpleNamespace())
    receipt = dispatcher.wait_foreground_done(handle, timeout_seconds=1)
    assert receipt.status == 'failed'
    assert not receipt.foreground_done
    assert receipt.error_detail == '未检测到已打开的文章标签'
