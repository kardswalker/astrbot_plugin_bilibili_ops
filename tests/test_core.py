import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import (  # noqa: E402
    PendingStore, RateLimiter, TakeStatus, check_publish_allowed, clean_text,
    normalize_ids, redact, valid_bvid,
)

BV = "BV1xx411c7mD"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def limiter(clock, per_hour=5, interval=30):
    return RateLimiter(per_hour, interval, clock=clock)


def allowed(**kw):
    clock = kw.pop("clock", Clock())
    base = dict(sender="1", allowed_ids=["1"], read_only=False, text="hi", max_len=200,
                limiter=limiter(clock))
    base.update(kw)
    return check_publish_allowed(**base)


# ---- 校验 ----
def test_bvid():
    assert valid_bvid(BV)
    assert not valid_bvid("BV1xx411c7m")
    assert not valid_bvid("BV1xx411c7mD; drop")
    assert not valid_bvid("https://b23.tv/x")


def test_clean_and_redact():
    assert clean_text(" a\x00b\x07 ") == "ab"
    assert redact("sess=SECRET123 ok", ["SECRET123", ""]) == "sess=*** ok"


def test_whitelist():
    assert allowed() is None
    assert "无权限" in allowed(sender="2")
    assert "无权限" in allowed(allowed_ids=[])           # 默认空 = 禁止
    assert allowed(sender="123", allowed_ids=[123]) is None  # WebUI 填数字


def test_read_only_and_text():
    assert "只读" in allowed(read_only=True)
    assert "过长" in allowed(text="x" * 201)
    assert "不能为空" in allowed(text="  \x00 ")


# ---- 限频 ----
def test_rate_limit():
    c = Clock()
    lim = limiter(c, per_hour=2, interval=30)
    assert lim.check() is None
    lim.record()
    assert "频繁" in lim.check()
    c.t += 31
    lim.record()
    c.t += 31
    assert "上限" in lim.check()
    c.t += 3600
    assert lim.check() is None


def test_rate_limit_persists(tmp_path):
    c = Clock()
    p = tmp_path / "t.json"
    RateLimiter(5, 30, path=p, clock=c).record()
    assert RateLimiter(5, 30, path=p, clock=c).check() is not None


# ---- 待确认 ----
def test_confirm_flow_and_expiry():
    c = Clock()
    s = PendingStore(120, clock=c)
    p = s.create("comment", {}, "1", "o")
    st, got = s.take("o", "1", p.code)
    assert st is TakeStatus.OK and got is p
    assert s.take("o", "1", p.code)[0] is TakeStatus.NONE  # 不能重复确认
    p = s.create("comment", {}, "1", "o")
    c.t += 121
    assert s.take("o", "1", p.code)[0] is TakeStatus.EXPIRED


def test_other_user_rejected():
    s = PendingStore(120)
    p = s.create("comment", {}, "1", "o")
    assert s.take("o", "2", p.code)[0] is TakeStatus.NOT_OWNER
    assert s.peek("o", "1") is p  # 原请求不受影响


def test_bad_code_keeps_pending():
    s = PendingStore(120)
    p = s.create("comment", {}, "1", "o")
    assert s.take("o", "1", "zzzz")[0] is TakeStatus.BAD_CODE
    assert s.take("o", "1", p.code)[0] is TakeStatus.OK


def test_others_cannot_overwrite_mine():
    s = PendingStore(120)
    mine = s.create("comment", {"a": 1}, "1", "o")
    s.create("comment", {"a": 2}, "2", "o")
    assert s.peek("o", "1") is mine


# ---- main.py 集成（桩掉 astrbot 与 B 站调用） ----
def _load_main(monkeypatch):
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    class Star:
        def __init__(self, context):
            pass

    class _Filter:
        def llm_tool(self, **kw):
            return lambda f: f

        def command_group(self, name):
            def deco(f):
                f.command = lambda n: (lambda g: g)
                return f
            return deco

    class _Logger:
        infos = []

        def info(self, m): self.infos.append(m)
        def warning(self, m): self.infos.append(m)

    mod("astrbot")
    mod("astrbot.api", AstrBotConfig=dict, logger=_Logger())
    mod("astrbot.api.event", AstrMessageEvent=object, filter=_Filter())
    mod("astrbot.api.star", Context=object, Star=Star, StarTools=None)
    pkg = types.ModuleType("pkg")
    pkg.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "pkg", pkg)
    for n in ("pkg.main", "pkg.core", "pkg.bili"):
        sys.modules.pop(n, None)
    return importlib.import_module("pkg.main")


class Ev:
    def __init__(self, sender, origin="o"):
        self._s, self.unified_msg_origin = sender, origin

    def get_sender_id(self):
        return self._s

    def plain_result(self, t):
        return t


def _plugin(monkeypatch, **cfg):
    m = _load_main(monkeypatch)
    base = {"sessdata": "SECRETSESS", "bili_jct": "SECRETJCT", "allowed_qq_ids": ["1"],
            "min_interval_seconds": 0}
    base.update(cfg)
    p = m.BilibiliOps(None, base)
    calls = []

    async def post_comment(bvid, text):
        calls.append(("comment", bvid, text))

    async def post_dynamic(text):
        calls.append(("dynamic", text))

    p.client.post_comment, p.client.post_dynamic = post_comment, post_dynamic
    return p, calls


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def collect(agen):
    return [x async for x in agen]


def test_no_write_before_confirm(monkeypatch):
    p, calls = _plugin(monkeypatch)
    out = run(p.bili_post_comment(Ev("1"), BV, "你好"))
    assert "待确认" in out and calls == []
    code = p.pending.peek("o", "1").code
    assert run(collect(p.bili_confirm(Ev("1"), code))) == [f"评论已发送到 {BV}。"]
    assert calls == [("comment", BV, "你好")]


def test_non_whitelist_cannot_trigger(monkeypatch):
    p, calls = _plugin(monkeypatch)
    assert "无权限" in run(p.bili_post_comment(Ev("9"), BV, "x"))
    assert "无权限" in run(p.bili_post_dynamic(Ev("9"), "x"))
    assert p.pending.peek("o", "9") is None and calls == []


def test_other_user_confirm_rejected(monkeypatch):
    p, calls = _plugin(monkeypatch, allowed_qq_ids=["1", "2"])
    run(p.bili_post_comment(Ev("1"), BV, "x"))
    code = p.pending.peek("o", "1").code
    assert "不是由你发起" in run(collect(p.bili_confirm(Ev("2"), code)))[0]
    assert calls == []


def test_whitelist_removed_before_confirm(monkeypatch):
    p, calls = _plugin(monkeypatch)
    run(p.bili_post_dynamic(Ev("1"), "x"))
    code = p.pending.peek("o", "1").code
    p.cfg["allowed_qq_ids"] = []
    assert "无权限" in run(collect(p.bili_confirm(Ev("1"), code)))[0]
    assert calls == []


def test_read_only_and_rate_limit(monkeypatch):
    p, calls = _plugin(monkeypatch, read_only_mode=True)
    assert "只读" in run(p.bili_post_comment(Ev("1"), BV, "x"))
    p, calls = _plugin(monkeypatch, max_posts_per_hour=1)
    run(p.bili_post_dynamic(Ev("1"), "a"))
    run(collect(p.bili_confirm(Ev("1"), p.pending.peek("o", "1").code)))
    assert "上限" in run(p.bili_post_dynamic(Ev("1"), "b"))


def test_failure_message_has_no_cookie(monkeypatch):
    p, _ = _plugin(monkeypatch)
    from pkg.bili import BiliError

    async def boom(text):
        raise BiliError("bad SECRETSESS value")

    p.client.post_dynamic = boom
    run(p.bili_post_dynamic(Ev("1"), "x"))
    out = run(collect(p.bili_confirm(Ev("1"), p.pending.peek("o", "1").code)))[0]
    assert "SECRETSESS" not in out and "发布失败" in out
