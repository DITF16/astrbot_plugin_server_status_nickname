import psutil
import re
import os
import uuid
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger

DEFAULT_TEMPLATE = "理智使用{cpu_percent}% 脑容量使用{mem_percent}%"
ABSOLUTE_TEMPLATE = "理智使用{cpu_percent}% 脑容量使用{mem_used_gb}G/{mem_total_gb}G"
STATUS_SUFFIX_RE = re.compile(r"\s*\([^)]+\)$")
RECORDS_KEY = "status_records"
MAX_RECORD_DAYS = 10


@register(
    "astrbot_plugin_server_status_nickname",
    "DITF16",
    "通过群昵称动态显示服务器状态的插件，并提供状态历史图生成",
    "2.0",
    "https://github.com/DITF16/astrbot_plugin_server_status_nickname",
)
class ServerStatusPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.plugin_config = config or {}

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_server_status")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = self.data_dir / "tmp"
        self.tmp_dir.mkdir(exist_ok=True)

        self.font_dir = Path(__file__).parent / "font"
        self.font_dir.mkdir(exist_ok=True)
        self.font_path = self.font_dir / "font.ttf"
        self.font_prop = None

        # UMO 追踪的群组: {group_id: {client, self_id, umo}}
        self.tracked_groups = {}

        self._init_plugin()

    def _init_plugin(self):
        try:
            if self.font_path.exists():
                self.font_prop = fm.FontProperties(fname=self.font_path)
                logger.info(f"成功加载字体文件: {self.font_path}")
            else:
                logger.warning(
                    f"字体文件未找到: {self.font_path}，图表中的中文可能显示为方块。"
                )
                logger.warning(
                    "请在插件目录中创建 /font 文件夹，并放入 font.ttf 中文字体文件。"
                )

            interval = self._cfg("update_interval", 5)
            if not isinstance(interval, (int, float)) or interval < 1 or interval > 60:
                logger.warning(
                    f"更新间隔配置值 {interval} 无效，已回退为默认值 5 分钟。"
                )
                interval = 5
            self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
            self.scheduler.add_job(
                self._scheduled_record, "interval", minutes=5, id="job_record_status"
            )
            self.scheduler.add_job(
                self._scheduled_cleanup, "cron", hour=4, minute=0, id="job_cleanup_db"
            )
            self.scheduler.add_job(
                self._scheduled_nickname_update,
                "interval",
                minutes=interval,
                id="job_update_nickname",
            )
            self.scheduler.start()
            logger.info(f"定时任务调度器已启动，昵称更新间隔: {interval} 分钟。")
        except Exception as e:
            logger.error(f"插件初始化失败: {e}", exc_info=True)

    # ── Config helpers ──

    def _cfg(self, key, default=None):
        return self.plugin_config.get(key, default)

    def _save_plugin_config(self):
        if hasattr(self.plugin_config, "save_config"):
            self.plugin_config.save_config()

    def _is_group_allowed(self, group_id: str) -> bool:
        mode = self._cfg("mode", "whitelist")
        group_list = set(self._cfg("group_list", []))
        if mode == "whitelist":
            return group_id in group_list if group_list else True
        return group_id not in group_list

    def _format_status_text(self, stats: dict) -> str:
        template = self._cfg("nickname_template", DEFAULT_TEMPLATE)
        variables = {
            "cpu_percent": f"{stats['cpu']:.1f}",
            "mem_percent": f"{stats['mem_percent']:.1f}",
            "mem_used_gb": f"{stats['mem_used_mb'] / 1024:.1f}",
            "mem_total_gb": f"{stats['mem_total_mb'] / 1024:.1f}",
            "mem_used_mb": f"{stats['mem_used_mb']:.0f}",
            "mem_total_mb": f"{stats['mem_total_mb']:.0f}",
        }
        text = template
        for k, v in variables.items():
            text = text.replace(f"{{{k}}}", v)
        return f"({text})"

    # ── System stats ──

    async def _get_system_stats(self) -> dict:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        return {
            "cpu": cpu,
            "mem_percent": mem.percent,
            "mem_used_mb": mem.used / (1024 * 1024),
            "mem_total_mb": mem.total / (1024 * 1024),
        }

    # ── KV storage for records ──

    async def _append_record(self, stats: dict):
        records = await self.get_kv_data(RECORDS_KEY, [])
        records.append(
            {
                "ts": datetime.now().isoformat(),
                "cpu": stats["cpu"],
                "mem_pct": stats["mem_percent"],
                "mem_used_mb": stats["mem_used_mb"],
                "mem_total_mb": stats["mem_total_mb"],
            }
        )
        await self.put_kv_data(RECORDS_KEY, records)

    async def _query_records(
        self, start: datetime, end: datetime, field: str
    ) -> list[tuple[datetime, float]]:
        records = await self.get_kv_data(RECORDS_KEY, [])
        result = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r["ts"])
            except (ValueError, KeyError):
                continue
            if start <= ts <= end:
                val = r.get(field)
                if val is not None:
                    result.append((ts, float(val)))
        result.sort(key=lambda x: x[0])
        return result

    async def _cleanup_old_records(self):
        records = await self.get_kv_data(RECORDS_KEY, [])
        cutoff = datetime.now() - timedelta(days=MAX_RECORD_DAYS)
        kept = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r["ts"])
                if ts >= cutoff:
                    kept.append(r)
            except (ValueError, KeyError):
                continue
        await self.put_kv_data(RECORDS_KEY, kept)
        removed = len(records) - len(kept)
        if removed:
            logger.info(f"清理了 {removed} 条过期记录。")

    # ── Nickname management ──

    async def _update_nickname(self, client, group_id: str, self_id: int, stats: dict):
        status_text = self._format_status_text(stats)
        try:
            info = await client.api.call_action(
                "get_group_member_info", group_id=int(group_id), user_id=self_id
            )
            current_card = info.get("card", "")

            match = STATUS_SUFFIX_RE.search(current_card)
            base_name = (
                current_card[: match.start()].rstrip() if match else current_card
            )
            new_card = f"{base_name} {status_text}" if base_name else status_text

            if new_card == current_card:
                return

            for i in range(3):
                try:
                    await client.api.call_action(
                        "set_group_card",
                        group_id=int(group_id),
                        user_id=self_id,
                        card=new_card,
                    )
                    logger.info(f"更新群 {group_id} 昵称: {new_card}")
                    break
                except Exception as e:
                    if i == 2:
                        logger.error(f"更新群 {group_id} 昵称失败: {e}")
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"更新群 {group_id} 昵称出错: {e}")

    async def _cleanup_nickname(self, client, group_id: str, self_id: int):
        try:
            info = await client.api.call_action(
                "get_group_member_info", group_id=int(group_id), user_id=self_id
            )
            card = info.get("card", "")
            if not card:
                return
            match = STATUS_SUFFIX_RE.search(card)
            if match:
                new_card = card[: match.start()].rstrip()
                if new_card != card:
                    await client.api.call_action(
                        "set_group_card",
                        group_id=int(group_id),
                        user_id=self_id,
                        card=new_card,
                    )
                    logger.info(f"已清理群 {group_id} 昵称后缀。")
        except Exception as e:
            logger.error(f"清理群 {group_id} 昵称出错: {e}")

    # ── Scheduled tasks ──

    async def _scheduled_record(self):
        stats = await self._get_system_stats()
        await self._append_record(stats)

    async def _scheduled_cleanup(self):
        await self._cleanup_old_records()

    async def _scheduled_nickname_update(self):
        if not self.tracked_groups:
            return
        stats = await self._get_system_stats()
        for gid, info in list(self.tracked_groups.items()):
            if self._is_group_allowed(gid):
                await self._update_nickname(info["client"], gid, info["self_id"], stats)
            else:
                await self._cleanup_nickname(info["client"], gid, info["self_id"])

    # ── Event handlers ──

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """收到群消息时注册群组，后续由定时器自动更新。"""
        client = self._get_client_from_event(event)
        if not client:
            return
        try:
            self_id = int(event.message_obj.self_id)
        except (AttributeError, ValueError):
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        if group_id not in self.tracked_groups:
            umo = getattr(event, "unified_msg_origin", None)
            self.tracked_groups[group_id] = {
                "client": client,
                "self_id": self_id,
                "umo": umo,
            }
            logger.info(f"已注册群 {group_id} (UMO: {umo})，定时器将自动更新昵称。")
        else:
            self.tracked_groups[group_id]["client"] = client
            self.tracked_groups[group_id]["self_id"] = self_id

    # ── Commands ──

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("昵称显示开启")
    async def brain_on(self, event: AstrMessageEvent, group_id: str):
        if not group_id.isdigit():
            yield event.plain_result(f"错误：'{group_id}' 不是有效的群号。")
            return

        group_list = list(self._cfg("group_list", []))
        if group_id in group_list:
            yield event.plain_result(f"群 {group_id} 已在列表中。")
            return

        group_list.append(group_id)
        self.plugin_config["group_list"] = group_list
        self._save_plugin_config()

        client = self._get_client_from_event(event)
        if client:
            try:
                self_id = int(event.message_obj.self_id)
                self.tracked_groups[group_id] = {
                    "client": client,
                    "self_id": self_id,
                    "umo": None,
                }
                stats = await self._get_system_stats()
                await self._update_nickname(client, group_id, self_id, stats)
                yield event.plain_result(f"已将群 {group_id} 加入列表并立即更新昵称。")
            except (AttributeError, ValueError):
                yield event.plain_result(
                    f"已将群 {group_id} 加入列表，但无法立即更新昵称。"
                )
        else:
            yield event.plain_result(f"已将群 {group_id} 加入列表。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("昵称显示关闭")
    async def brain_off(self, event: AstrMessageEvent, group_id: str):
        if not group_id.isdigit():
            yield event.plain_result(f"错误：'{group_id}' 不是有效的群号。")
            return

        group_list = list(self._cfg("group_list", []))
        if group_id not in group_list:
            yield event.plain_result(f"群 {group_id} 不在列表中。")
            return

        group_list.remove(group_id)
        self.plugin_config["group_list"] = group_list
        self._save_plugin_config()

        client = self._get_client_from_event(event)
        if client:
            try:
                self_id = int(event.message_obj.self_id)
                await self._cleanup_nickname(client, group_id, self_id)
                yield event.plain_result(f"已将群 {group_id} 从列表移除并清理昵称。")
            except (AttributeError, ValueError):
                yield event.plain_result(f"已将群 {group_id} 从列表移除。")
        else:
            yield event.plain_result(f"已将群 {group_id} 从列表移除。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("刷新缓存")
    async def brain_refresh(self, event: AstrMessageEvent):
        client = self._get_client_from_event(event)
        group_id = event.get_group_id()
        reply = "已刷新配置。"

        if client and group_id:
            try:
                self_id = int(event.message_obj.self_id)
                stats = await self._get_system_stats()
                if self._is_group_allowed(group_id):
                    await self._update_nickname(client, group_id, self_id, stats)
                    reply += "\n并已刷新当前群聊的昵称状态。"
                else:
                    await self._cleanup_nickname(client, group_id, self_id)
                    reply += "\n当前群聊不在生效列表中，已清理昵称。"
            except (AttributeError, ValueError):
                pass

        yield event.plain_result(reply)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("服务器更新")
    async def brain_update(self, event: AstrMessageEvent):
        client = self._get_client_from_event(event)
        if not client:
            yield event.plain_result("错误：当前平台不支持或无法获取客户端。")
            return
        try:
            int(event.message_obj.self_id)
        except (AttributeError, ValueError):
            yield event.plain_result("错误：无法获取机器人自身ID。")
            return

        stats = await self._get_system_stats()
        await self._append_record(stats)
        await self._scheduled_nickname_update()
        yield event.plain_result("已更新所有受监控群聊的昵称状态。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("改变脑容量显示")
    async def toggle_display(self, event: AstrMessageEvent):
        current = self._cfg("nickname_template", DEFAULT_TEMPLATE)
        if current == DEFAULT_TEMPLATE:
            self.plugin_config["nickname_template"] = ABSOLUTE_TEMPLATE
            reply = "已切换为绝对值模式 (G/G)。"
        else:
            self.plugin_config["nickname_template"] = DEFAULT_TEMPLATE
            reply = "已切换为百分比模式 (%)。"
        self._save_plugin_config()

        client = self._get_client_from_event(event)
        group_id = event.get_group_id()
        if client and group_id:
            try:
                self_id = int(event.message_obj.self_id)
                stats = await self._get_system_stats()
                await self._update_nickname(client, group_id, self_id, stats)
            except (AttributeError, ValueError):
                pass

        yield event.plain_result(reply)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("理智记录")
    async def cpu_record(self, event: AstrMessageEvent, time_str: str = "1小时"):
        image_path = None
        try:
            delta = self._parse_time_arg(time_str)
            if delta is None:
                yield event.plain_result(
                    "格式错误！请使用 'n天' 或 'n小时'，例如: /理智记录 2天"
                )
                return

            end_time = datetime.now()
            start_time = end_time - delta
            data = await self._query_records(start_time, end_time, "cpu")

            if not data:
                yield event.plain_result(f"最近 {time_str} 内没有理智记录。")
                return

            timestamps, values = zip(*data)
            image_path = self._plot_graph(
                timestamps,
                values,
                f"最近 {time_str} 理智使用记录 (CPU %)",
                "CPU 占用 (%)",
            )
            yield event.image_result(str(image_path))

        except Exception as e:
            logger.error(f"理智记录指令失败: {e}", exc_info=True)
            yield event.plain_result("生成图表时发生内部错误。")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except OSError:
                    pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("脑容量记录")
    async def memory_record(self, event: AstrMessageEvent, time_str: str = "1小时"):
        image_path = None
        try:
            delta = self._parse_time_arg(time_str)
            if delta is None:
                yield event.plain_result(
                    "格式错误！请使用 'n天' 或 'n小时'，例如: /脑容量记录 12小时"
                )
                return

            end_time = datetime.now()
            start_time = end_time - delta
            data = await self._query_records(start_time, end_time, "mem_pct")

            if not data:
                yield event.plain_result(f"最近 {time_str} 内没有脑容量记录。")
                return

            timestamps, values = zip(*data)
            image_path = self._plot_graph(
                timestamps,
                values,
                f"最近 {time_str} 脑容量使用记录 (内存 %)",
                "内存占用 (%)",
            )
            yield event.image_result(str(image_path))

        except Exception as e:
            logger.error(f"脑容量记录指令失败: {e}", exc_info=True)
            yield event.plain_result("生成图表时发生内部错误。")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except OSError:
                    pass

    # ── Helpers ──

    def _get_client_from_event(self, event: AstrMessageEvent):
        if event.get_platform_name() == "aiocqhttp":
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    return event.bot
            except ImportError:
                logger.error("无法导入 AiocqhttpMessageEvent。")
        return None

    def _parse_time_arg(self, time_str: str) -> timedelta | None:
        m = re.match(r"(\d+)\s*(天|小时)", time_str)
        if not m:
            return None
        n, unit = int(m.group(1)), m.group(2)
        return timedelta(days=n) if unit == "天" else timedelta(hours=n)

    def _plot_graph(self, timestamps, values, title: str, ylabel: str) -> str:
        plt.style.use("seaborn-v0_8-darkgrid")
        fig, ax = plt.subplots(figsize=(12, 6), dpi=100)
        ax.plot(
            timestamps, values, marker="o", linestyle="-", markersize=3, label=ylabel
        )

        if self.font_prop:
            ax.set_title(title, fontsize=16, fontproperties=self.font_prop)
            ax.set_xlabel("时间", fontsize=12, fontproperties=self.font_prop)
            ax.set_ylabel(ylabel, fontsize=12, fontproperties=self.font_prop)
            ax.legend(prop=self.font_prop)
        else:
            ax.set_title(title, fontsize=16)
            ax.set_xlabel("Time", fontsize=12)
            ax.set_ylabel(ylabel, fontsize=12)
            ax.legend()

        ax.grid(True)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        fig.autofmt_xdate()
        plt.tight_layout()

        filepath = self.tmp_dir / f"{uuid.uuid4()}.png"
        plt.savefig(filepath, format="png")
        plt.close(fig)
        return str(filepath)

    async def terminate(self):
        logger.info("正在关闭 ServerStatus 插件...")
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown()
        logger.info("ServerStatus 插件已关闭。")
