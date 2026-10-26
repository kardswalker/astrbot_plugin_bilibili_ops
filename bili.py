"""bilibili-api-python 的薄封装：统一凭据、错误转中文、不泄露 Cookie。"""

from __future__ import annotations

try:
    from .core import redact
except ImportError:  # 测试里按顶层模块加载
    from core import redact


class BiliError(Exception):
    """面向用户的错误，message 已脱敏。"""


# 常见错误码；其余走通用提示。需在真实账号上手工验证。
_CODE_MSG = {
    -101: "Cookie 无效或未登录，请重新获取 SESSDATA / bili_jct。",
    -111: "csrf 校验失败，请检查 bili_jct 是否正确。",
    -352: "触发 B 站风控校验，请稍后再试或在浏览器中正常登录一次。",
    -412: "请求过于频繁，被 B 站限流，请稍后再试。",
    12002: "该视频评论区已关闭。",
    12015: "B 站要求验证码，暂时无法自动发送。",
}


class BiliClient:
    def __init__(self, sessdata="", bili_jct="", buvid3="", dedeuserid=""):
        self._secrets = [sessdata, bili_jct, buvid3, dedeuserid]
        self._kw = dict(
            sessdata=sessdata or None,
            bili_jct=bili_jct or None,
            buvid3=buvid3 or None,
            dedeuserid=dedeuserid or None,
        )
        self._cred = None

    @property
    def credential(self):
        if self._cred is None:
            from bilibili_api import Credential

            self._cred = Credential(**self._kw)
        return self._cred

    @property
    def has_login(self) -> bool:
        return bool(self._kw["sessdata"] and self._kw["bili_jct"])

    def _wrap(self, exc: Exception) -> BiliError:
        code = getattr(exc, "code", None)
        if isinstance(code, int) and code in _CODE_MSG:
            return BiliError(_CODE_MSG[code])
        name = type(exc).__name__
        if name.startswith("CredentialNo"):
            return BiliError("Cookie 配置不完整，请在插件配置中补全。")
        if code is not None:
            return BiliError(f"B 站返回错误 {code}：{redact(str(getattr(exc, 'msg', '')), self._secrets)}")
        return BiliError(f"请求 B 站失败（{name}）。")

    async def _run(self, coro):
        try:
            return await coro
        except BiliError:
            raise
        except Exception as e:  # noqa: BLE001 - 统一脱敏后再抛
            raise self._wrap(e) from None

    async def check_valid(self) -> bool:
        try:
            return bool(await self.credential.check_valid())
        except Exception:  # noqa: BLE001
            return False

    async def video_info(self, bvid: str) -> dict:
        from bilibili_api import video

        v = video.Video(bvid=bvid, credential=self.credential)
        return await self._run(v.get_info())

    def _aid(self, bvid: str) -> int:
        from bilibili_api import video

        return video.Video(bvid=bvid).get_aid()  # 本地 bvid→aid 换算，不发请求

    async def comments(self, bvid: str, page: int = 1) -> dict:
        from bilibili_api import comment

        aid = self._aid(bvid)
        return await self._run(
            comment.get_comments(
                aid, comment.CommentResourceType.VIDEO, page_index=page, credential=self.credential
            )
        )

    async def dynamic_info(self, dynamic_id: int) -> dict:
        from bilibili_api import dynamic

        d = dynamic.Dynamic(dynamic_id, credential=self.credential)
        return await self._run(d.get_info())

    async def post_comment(self, bvid: str, text: str) -> dict:
        from bilibili_api import comment

        aid = self._aid(bvid)
        return await self._run(
            comment.send_comment(
                text, aid, comment.CommentResourceType.VIDEO, credential=self.credential
            )
        )

    async def post_dynamic(self, text: str) -> dict:
        from bilibili_api import dynamic

        info = dynamic.BuildDynamic.empty().add_text(text)
        return await self._run(dynamic.send_dynamic(info, self.credential))
