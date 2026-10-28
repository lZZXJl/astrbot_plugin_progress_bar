"""AstrBot 插件：进度条。

办事的时候把进度按节点上报，插件渲染成一行进度条写进日志，
在 WebUI 的日志窗口里能看着它一格一格走。

日志一行一条，不原地刷新——控制台那套 \\r 在面板里不管用。
"""

from __future__ import annotations

import json
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# 彩虹铺色用的方块，按这个顺序从左到右铺开
RAINBOW = ("🟥", "🟧", "🟨", "🟩", "🟦", "🟪")


@register(
    "astrbot_plugin_progress_bar",
    "lZZXJl",
    "把办事进度渲染成进度条写进日志",
    "0.1.0",
)
class ProgressBarPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self._started: dict[str, float] = {}
        self._last: dict[str, tuple[float, int]] = {}
        self._sent: dict[str, object] = {}
        self._chat_at: dict[str, float] = {}

    # ---------- 配置 ----------

    def _cfg(self, key: str, default):
        if self.config is None:
            return default
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    # ---------- 渲染 ----------

    @staticmethod
    def _clock(seconds: float) -> str:
        total = int(max(0, seconds))
        return f"{total // 60:02d}:{total % 60:02d}"

    def _render(
        self, session: str, percent: int, note: str, status: str
    ) -> tuple[str, bool]:
        percent = max(0, min(100, int(percent)))
        width = max(4, min(24, int(self._cfg("bar_width", 8))))
        filled = int(round(width * percent / 100))

        mode = str(self._cfg("color_mode", "rainbow") or "none").strip().lower()
        if mode == "rainbow":
            off = str(self._cfg("color_empty", "⬜") or "⬜")
            n = min(len(RAINBOW), max(1, width // 2))
            base, extra = divmod(width, n)
            palette = []
            for k in range(n):
                palette += [RAINBOW[k]] * (base + (1 if k < extra else 0))
            bar = "".join(
                palette[i] if i < filled else off for i in range(width)
            )
        elif mode == "emoji":
            on = str(self._cfg("color_filled", "🟦") or "🟦")
            off = str(self._cfg("color_empty", "⬜") or "⬜")
            bar = on * filled + off * (width - filled)
        else:
            bar = "█" * filled + "░" * (width - filled)
            code = str(self._cfg("color_code", "38;5;39") or "").strip()
            if mode == "ansi" and code:
                bar = f"\x1b[{code}m{bar}\x1b[0m"

        now = time.monotonic()
        if status == "done" or percent >= 100:
            started = self._started.pop(session, None)
            self._last.pop(session, None)
            cost = self._clock(now - started) if started else "--:--"
            line = f"{self._cfg('prefix', '[进度]')} {bar} 100%"
            if note:
                line += f"  {note}"
            line += f"  | 用时 {cost}"
            return line, True

        started = self._started.get(session)
        if started is None:
            started = now
            self._started[session] = now
        cost = self._clock(now - started)
        line = f"{self._cfg('prefix', '[进度]')} {bar} {percent:3d}%  {cost}"
        if note:
            line += f"  {note}"
        return line, False

    def _report(
        self, event: AstrMessageEvent, percent: int, note: str, status: str
    ) -> str:
        session = event.unified_msg_origin or "default"
        now = time.monotonic()
        gap = max(0, int(self._cfg("min_interval", 1)))
        last = self._last.get(session)
        if last is not None and status != "done" and percent < 100:
            if now - last[0] < gap and int(percent) == last[1]:
                return ""
        line, done = self._render(session, percent, note, status)
        self._last[session] = (now, int(percent))
        logger.info(line)
        return line

    # ---------- 往会话里发 ----------

    def _chat_allowed(self, event: AstrMessageEvent) -> bool:
        if not bool(self._cfg("chat_enabled", True)):
            return False
        scope = str(self._cfg("chat_scope", "private") or "private").strip().lower()
        if scope == "all":
            return True
        return event.is_private_chat()

    async def _push_chat(self, event: AstrMessageEvent, line: str) -> bool:
        """在会话里维持一条会变的进度。上一条撤掉，再发新的。"""
        if not line or not self._chat_allowed(event):
            return False
        bot = getattr(event, "bot", None)
        if bot is None:
            return False
        session = event.unified_msg_origin or "default"
        now = time.monotonic()
        floor = max(0, int(self._cfg("chat_interval", 3)))
        last_at = self._chat_at.get(session)
        if last_at is not None and now - last_at < floor:
            return False

        old = self._sent.pop(session, None)
        if old is not None:
            try:
                await bot.call_action("delete_msg", message_id=old)
            except Exception:
                pass
        try:
            group_id = event.get_group_id()
            if group_id:
                ret = await bot.call_action(
                    "send_group_msg", group_id=int(group_id), message=line
                )
            else:
                ret = await bot.call_action(
                    "send_private_msg",
                    user_id=int(event.get_sender_id()),
                    message=line,
                )
        except Exception as exc:
            logger.debug(f"[进度] 会话发送失败：{exc}")
            return False
        if isinstance(ret, dict):
            mid = ret.get("message_id") or (ret.get("data") or {}).get("message_id")
            if mid is not None:
                self._sent[session] = mid
        self._chat_at[session] = now
        return True

    # ---------- 给模型用的工具 ----------

    @filter.llm_tool(name="report_progress")
    async def report_progress(
        self,
        event: AstrMessageEvent,
        percent: int,
        note: str = "",
        status: str = "running",
    ) -> str:
        """办事时上报进度，插件会把它写成一行进度条塞进日志，主人在面板里能看到。

        什么时候用：下载大文件、批量改文件、跑长命令这类要等一阵的活。
        下载卡住在等就别硬报，等真动了再报。

        Args:
            percent(number): 0 到 100 的整数，100 表示办完。
            note(string): 一句话说明在干什么，短，别超过十个字。
            status(string): running（默认）或 done。
        """
        if not bool(self._cfg("enable_llm_tool", True)):
            return json.dumps(
                {"ok": False, "reason": "LLM 工具已在配置里关掉"}, ensure_ascii=False
            )
        line = self._report(event, percent, note, status)
        if not line:
            return json.dumps({"ok": True, "skipped": "太密，没重复打"}, ensure_ascii=False)
        await self._push_chat(event, line)
        return json.dumps({"ok": True, "line": line}, ensure_ascii=False)

    # ---------- 手动调试 ----------

    @filter.command("进度")
    async def cmd_progress(self, event: AstrMessageEvent):
        """手动试一下进度条：/进度 45 下载中"""
        raw = (event.message_str or "").strip()
        for word in ("进度", "/进度"):
            if raw.startswith(word):
                raw = raw[len(word) :].strip()
                break
        if not raw:
            yield event.plain_result("给我个数：/进度 45 下载中")
            return
        parts = raw.split(None, 1)
        try:
            percent = int(parts[0].rstrip("%"))
        except ValueError:
            yield event.plain_result("头一个得是数字。")
            return
        note = parts[1] if len(parts) > 1 else ""
        line = self._report(event, percent, note, "done" if percent >= 100 else "running")
        yield event.plain_result(line or "跟上次一样，没打。")

    async def terminate(self):
        self._started.clear()
        self._last.clear()
        self._sent.clear()
        self._chat_at.clear()
