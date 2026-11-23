# -*- coding: utf-8 -*-
"""
LFE 搜打撤文字游戏 —— AstrBot 插件

经济循环：
    初始 0 资金、0 装备 → 只能打普通模式（一键搜刮）攒钱
    → 在商店购买护甲/头盔/枪械/子弹 → 达到门槛后进机密 22 万 / 绝密 60 万
    → 撤离成功：装备保留、子弹只扣实际消耗
    → 撤离失败：枪械/护甲/头盔/子弹全部清零

指令：
    /lfe start <普通|机密|绝密>
    /lfe status | go <区域|撤离区> | search | flee | end
    /lfe shop [武器|护甲|头盔]      查看商店
    /lfe buy <名称>                 购买装备
    /lfe ammo <等级> <数量>         购买子弹（需先有枪）
    /lfe profile                    余额、装备、KD

管理员：
    /lfe_admin addmoney <QQ> <金额>
    /lfe_admin balance <QQ> <金额>
    /lfe_admin kd <QQ> <机密|绝密> <k> <d>
    /lfe_admin give <QQ> <名称>      直接发放装备
    /lfe_admin setnext <QQ> <事件ID> [次数]
    /lfe_admin clearnext <QQ>
    /lfe_admin info <QQ> | reset <QQ> | events
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("lfe")

PLUGIN_DIR = Path(__file__).parent
DATA_DIR = PLUGIN_DIR / "data"
DB_PATH = DATA_DIR / "lfe.db"

ALWAYS_EVENTS = {"searchLoot", "clearAI"}

MODE_ALIAS = {
    "普通": "normal", "normal": "normal", "n": "normal",
    "机密": "confidential", "confidential": "confidential", "c": "confidential",
    "绝密": "topSecret", "topsecret": "topSecret", "t": "topSecret",
}
MODE_LABEL = {"normal": "普通", "confidential": "机密", "topSecret": "绝密"}

# 入场门槛（带入装备价值）
MODE_THRESHOLD = {"confidential": 220000, "topSecret": 600000}

DEFAULT_ENEMY_GEN = {
    "normal": {"armorLevel": [0, 2], "gunChance": 0.3, "gunPool": "bottom11", "ammoLevel": {2: 1.0}},
    "confidential": {
        "armorLevel": [3, 4], "rareArmor": {5: 0.05, 6: 0.01},
        "gunChance": 1.0, "gunPool": "bottom11", "ammoLevel": {4: 0.9, 5: 0.1},
    },
    "topSecret": {
        "armorLevel": [5, 5], "rareArmor": {6: 0.10},
        "gunChance": 1.0, "gunPool": "top10", "ammoLevel": {5: 0.9, 4: 0.05, 6: 0.05},
    },
}

DEFAULT_REGIONS = ["发射区", "码头", "水平试车场", "浮力室", "离心机室",
                   "总裁室", "蓝室", "黑室", "宿舍区", "中控区", "工业区"]

NORMAL_HIGH_VALUE_CHANCE = 0.025
MAP_BASE_VALUE = 3000000


def _load(name: str) -> Optional[dict]:
    path = DATA_DIR / name
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[lfe] 读取 {name} 失败: {e}")
        return None


def _pick(weight_map: dict) -> Optional[str]:
    total = sum(w for w in weight_map.values() if w > 0)
    if total <= 0:
        return None
    r = random.uniform(0, total)
    acc = 0.0
    for k, w in weight_map.items():
        if w <= 0:
            continue
        acc += w
        if r <= acc:
            return k
    return list(weight_map.keys())[-1]


# ============================================================
# 持久化：余额 / 装备 / 子弹 / KD
# ============================================================

class PlayerDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    uid          TEXT PRIMARY KEY,
                    money        INTEGER NOT NULL DEFAULT 0,
                    gun          TEXT,
                    helmet       TEXT,
                    armor        TEXT,
                    ammo_level   INTEGER NOT NULL DEFAULT 0,
                    ammo_count   INTEGER NOT NULL DEFAULT 0,
                    conf_kills   INTEGER NOT NULL DEFAULT 0,
                    conf_deaths  INTEGER NOT NULL DEFAULT 0,
                    top_kills    INTEGER NOT NULL DEFAULT 0,
                    top_deaths   INTEGER NOT NULL DEFAULT 0,
                    runs_normal  INTEGER NOT NULL DEFAULT 0,
                    runs_conf    INTEGER NOT NULL DEFAULT 0,
                    runs_top     INTEGER NOT NULL DEFAULT 0,
                    updated_at   REAL
                )
                """
            )
            # 兼容旧库：补列
            cols = {r["name"] for r in c.execute("PRAGMA table_info(players)")}
            for col, ddl in [
                ("gun", "TEXT"), ("helmet", "TEXT"), ("armor", "TEXT"),
                ("ammo_level", "INTEGER NOT NULL DEFAULT 0"),
                ("ammo_count", "INTEGER NOT NULL DEFAULT 0"),
            ]:
                if col not in cols:
                    try:
                        c.execute(f"ALTER TABLE players ADD COLUMN {col} {ddl}")
                    except Exception:
                        pass

    def _ensure(self, c, uid: str) -> None:
        c.execute("INSERT OR IGNORE INTO players (uid) VALUES (?)", (str(uid),))

    def get(self, uid: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT * FROM players WHERE uid = ?", (str(uid),)).fetchone()
        if not row:
            return {
                "uid": str(uid), "money": 0, "gun": None, "helmet": None, "armor": None,
                "ammo_level": 0, "ammo_count": 0,
                "conf_kills": 0, "conf_deaths": 0, "top_kills": 0, "top_deaths": 0,
                "runs_normal": 0, "runs_conf": 0, "runs_top": 0,
            }
        return dict(row)

    def add_money(self, uid: str, amount: int) -> int:
        with self._conn() as c:
            self._ensure(c, uid)
            c.execute("UPDATE players SET money = money + ?, updated_at = ? WHERE uid = ?",
                      (int(amount), time.time(), str(uid)))
            row = c.execute("SELECT money FROM players WHERE uid = ?", (str(uid),)).fetchone()
        return int(row["money"]) if row else 0

    def set_money(self, uid: str, amount: int) -> int:
        with self._conn() as c:
            self._ensure(c, uid)
            c.execute("UPDATE players SET money = ?, updated_at = ? WHERE uid = ?",
                      (int(amount), time.time(), str(uid)))
        return int(amount)

    def spend_money(self, uid: str, amount: int) -> tuple[bool, int]:
        """扣钱，余额不足返回 False"""
        with self._conn() as c:
            self._ensure(c, uid)
            row = c.execute("SELECT money FROM players WHERE uid = ?", (str(uid),)).fetchone()
            money = int(row["money"]) if row else 0
            if money < amount:
                return False, money
            c.execute("UPDATE players SET money = money - ?, updated_at = ? WHERE uid = ?",
                      (int(amount), time.time(), str(uid)))
            return True, money - amount

    def set_item(self, uid: str, slot: str, name: Optional[str]) -> None:
        """slot: gun / helmet / armor"""
        assert slot in ("gun", "helmet", "armor")
        with self._conn() as c:
            self._ensure(c, uid)
            if slot == "gun":
                # 换枪时重置弹药口径
                c.execute("UPDATE players SET gun = ?, ammo_level = 0, ammo_count = 0, updated_at = ? WHERE uid = ?",
                          (name, time.time(), str(uid)))
            else:
                c.execute(f"UPDATE players SET {slot} = ?, updated_at = ? WHERE uid = ?",
                          (name, time.time(), str(uid)))

    def set_ammo(self, uid: str, level: int, count: int) -> None:
        with self._conn() as c:
            self._ensure(c, uid)
            c.execute("UPDATE players SET ammo_level = ?, ammo_count = ?, updated_at = ? WHERE uid = ?",
                      (int(level), int(count), time.time(), str(uid)))

    def spend_ammo(self, uid: str, n: int) -> int:
        """扣子弹，返回剩余"""
        with self._conn() as c:
            self._ensure(c, uid)
            row = c.execute("SELECT ammo_count FROM players WHERE uid = ?", (str(uid),)).fetchone()
            cur = int(row["ammo_count"]) if row else 0
            left = max(0, cur - int(n))
            c.execute("UPDATE players SET ammo_count = ?, updated_at = ? WHERE uid = ?",
                      (left, time.time(), str(uid)))
            return left

    def wipe_loadout(self, uid: str) -> None:
        """死亡：枪、甲、头、子弹全部清零"""
        with self._conn() as c:
            self._ensure(c, uid)
            c.execute(
                "UPDATE players SET gun = NULL, helmet = NULL, armor = NULL, "
                "ammo_level = 0, ammo_count = 0, updated_at = ? WHERE uid = ?",
                (time.time(), str(uid)),
            )

    def record_run(self, uid: str, mode: str, kills: int, deaths: int, loot: int) -> dict:
        runs_col = {"normal": "runs_normal", "confidential": "runs_conf", "topSecret": "runs_top"}[mode]
        with self._conn() as c:
            self._ensure(c, uid)
            c.execute(f"UPDATE players SET money = money + ?, {runs_col} = {runs_col} + 1, updated_at = ? WHERE uid = ?",
                      (int(loot), time.time(), str(uid)))
            if mode == "confidential":
                c.execute("UPDATE players SET conf_kills = conf_kills + ?, conf_deaths = conf_deaths + ? WHERE uid = ?",
                          (int(kills), int(deaths), str(uid)))
            elif mode == "topSecret":
                c.execute("UPDATE players SET top_kills = top_kills + ?, top_deaths = top_deaths + ? WHERE uid = ?",
                          (int(kills), int(deaths), str(uid)))
            row = c.execute("SELECT * FROM players WHERE uid = ?", (str(uid),)).fetchone()
        return dict(row) if row else {}

    def set_kd(self, uid: str, mode: str, kills: int, deaths: int) -> None:
        with self._conn() as c:
            self._ensure(c, uid)
            col = "conf" if mode == "confidential" else "top"
            c.execute(f"UPDATE players SET {col}_kills = ?, {col}_deaths = ? WHERE uid = ?",
                      (int(kills), int(deaths), str(uid)))


def _kd_text(kills: int, deaths: int) -> str:
    kd = kills / deaths if deaths else float(kills)
    return f"{kills}/{deaths}（{kd:.2f}）"


# ============================================================
# 局内状态
# ============================================================

@dataclass
class GameState:
    user_id: str
    mode: str
    gun: str = ""
    ammo_level: int = 0
    ammo_count: int = 0
    helmet: str = ""
    armor: str = ""
    loot: int = 0
    carry_value: int = 0
    steps: int = 0
    region: str = ""
    visited: list = field(default_factory=list)
    searched: list = field(default_factory=list)
    is_rat: bool = False
    has_gun: bool = True
    ammo_used: int = 0
    alive_teams: int = 6
    evac_triggered: bool = False
    evac_left: int = 0
    dead: bool = False
    finished: bool = False
    settled: bool = False
    next_event: Optional[str] = None
    next_event_times: int = 0
    kills: int = 0
    deaths: int = 0
    started_at: float = field(default_factory=time.time)


# ============================================================
# 游戏逻辑
# ============================================================

class LfeGame:
    def __init__(self) -> None:
        weapons = _load("weapons.json") or {}
        helmets = _load("helmets.json") or {}
        armors = _load("armorVests.json") or {}
        ammo = _load("ammo.json") or {}
        events = _load("events.json") or {}
        maps = _load("map.json") or _load("maps.json") or {}

        self.guns: dict[str, dict] = {w["name"]: w for w in weapons.get("weapons", [])}
        self.helmets: dict[str, dict] = {h["name"]: h for h in helmets.get("helmets", [])}
        self.armors: dict[str, dict] = {a["name"]: a for a in armors.get("armorVests", [])}
        self.ammo: dict[str, dict] = ammo.get("ammo", {})
        self.events_cfg = events
        self.categories = events.get("eventCategories", {}) or {}
        self.conditional = events.get("conditionalEvents", {}) or {}

        zones = maps.get("zones") or []
        if zones:
            self.regions = [z["name"] for z in zones if str(z.get("type", "")).lower() != "extraction"]
            self.evac_region = next(
                (z["name"] for z in zones if str(z.get("type", "")).lower() == "extraction"), "撤离区")
        else:
            self.regions = maps.get("regions") or DEFAULT_REGIONS
            self.evac_region = "撤离区"

        self.enemy_gen = dict(DEFAULT_ENEMY_GEN)
        if maps.get("enemyGen"):
            self.enemy_gen.update(maps["enemyGen"])

        self.gun_pool = {
            "top10": maps.get("gunPoolTop10") or self._sorted_guns()[:10],
            "bottom11": maps.get("gunPoolBottom11") or self._sorted_guns()[11:],
        }

        g = events.get("global", {}) or {}
        self.rand_min = float(g.get("randomCoefficient", {}).get("min", 0.8))
        self.rand_max = float(g.get("randomCoefficient", {}).get("max", 1.2))
        self.rat_threshold = g.get("ratThreshold", {}) or {}
        self.map_multiplier = g.get("mapMultiplier", {}) or {}
        self.total_teams = int(g.get("totalTeams") or 6)

        self.db = PlayerDB(DB_PATH)
        self.games: dict[str, GameState] = {}

    # ---------- 基础查询 ----------

    def _sorted_guns(self) -> list[str]:
        return [n for n, _ in sorted(self.guns.items(), key=lambda kv: kv[1].get("firepower", 0), reverse=True)]

    def gun_firepower(self, name: str) -> float:
        return float(self.guns.get(name, {}).get("firepower", 0))

    def gun_price(self, name: str) -> int:
        return int(self.guns.get(name, {}).get("price", 0))

    def helmet_price(self, name: str) -> int:
        return int(self.helmets.get(name, {}).get("price", 0))

    def armor_price(self, name: str) -> int:
        return int(self.armors.get(name, {}).get("price", 0))

    def gear_value(self, gun: str = "", helmet: str = "", armor: str = "") -> int:
        return (self.gun_price(gun) if gun else 0) \
            + (self.helmet_price(helmet) if helmet else 0) \
            + (self.armor_price(armor) if armor else 0)

    def ammo_coef(self, caliber: str, level: int) -> float:
        for item in self.ammo.get(caliber, {}).get(str(level), []):
            return float(item.get("firepowerCoefficient", 0))
        return 0.0

    def ammo_price(self, caliber: str, level: int) -> int:
        for item in self.ammo.get(caliber, {}).get(str(level), []):
            return int(item.get("price", 0))
        return 0

    def protection(self, armor_name: str, helmet_name: str) -> float:
        a = self.armors.get(armor_name, {}).get("reductionCoefficient", 0) if armor_name else 0
        h = self.helmets.get(helmet_name, {}).get("reductionCoefficient", 0) if helmet_name else 0
        return float(a) + float(h)

    def armor_level(self, name: str) -> int:
        return int(self.armors.get(name, {}).get("level", 0)) if name else 0

    def helmet_level(self, name: str) -> int:
        return int(self.helmets.get(name, {}).get("level", 0)) if name else 0

    # ---------- 商店 ----------

    def shop_list(self, kind: str = "") -> str:
        kind = kind.strip()
        lines = [f"余额：{0:,}".replace("0", "—")]  # 占位，实际由调用方补余额
        lines = []
        if kind in ("", "武器", "枪械", "gun"):
            rows = sorted(self.guns.values(), key=lambda x: -x.get("firepower", 0))
            lines.append(f"【武器 · {len(rows)} 把】")
            for w in rows:
                lines.append(f"  {w['name']:<12} 火力 {w.get('firepower', 0):<5} {w.get('price', 0):>10,}")
        if kind in ("", "护甲", "armor"):
            rows = sorted(self.armors.values(), key=lambda x: x.get("level", 0))
            lines.append(f"【护甲 · {len(rows)} 件】")
            for a in rows:
                lines.append(f"  {a['name']:<20} {a.get('level', 0)} 级 {a.get('price', 0):>10,}")
        if kind in ("", "头盔", "helmet"):
            rows = sorted(self.helmets.values(), key=lambda x: x.get("level", 0))
            lines.append(f"【头盔 · {len(rows)} 件】")
            for h in rows:
                lines.append(f"  {h['name']:<20} {h.get('level', 0)} 级 {h.get('price', 0):>10,}")
        return "\n".join(lines)

    def _match(self, table: dict, name: str) -> tuple[Optional[str], str]:
        """精确匹配失败时做包含匹配；返回 (唯一命中的全名, 错误说明)"""
        if name in table:
            return name, ""
        key = name.replace(" ", "").lower()
        cands = [k for k in table if key in k.replace(" ", "").lower()]
        if len(cands) == 1:
            return cands[0], ""
        if len(cands) > 1:
            show = "、".join(cands[:6])
            more = "" if len(cands) <= 6 else f" 等 {len(cands)} 项"
            return None, f"「{name}」匹配到多个装备：{show}{more}，请写得更具体。"
        return None, ""

    def buy_gear(self, uid: str, name: str) -> tuple[bool, str]:
        slot, price = None, 0
        full, err = self._match(self.guns, name)
        if full:
            slot, price, name = "gun", self.gun_price(full), full
        else:
            full, err2 = self._match(self.armors, name)
            if full:
                slot, price, name = "armor", self.armor_price(full), full
            else:
                full, err3 = self._match(self.helmets, name)
                if full:
                    slot, price, name = "helmet", self.helmet_price(full), full
                else:
                    msg = err or err2 or err3
                    return False, msg or f"商店里没有「{name}」，用 /lfe shop 查看可购买的物品。"


        ok, left = self.db.spend_money(uid, price)
        if not ok:
            return False, f"余额不足：需要 {price:,}，当前 {left:,}。"

        old = self.db.get(uid).get(slot)
        self.db.set_item(uid, slot, name)
        note = ""
        if slot == "gun" and old and old != name:
            note = "（换枪后原有子弹已清空，需重新购买）"
        return True, f"购买成功：{name}（-{price:,}），余额 {left:,}{note}"

    def buy_ammo(self, uid: str, level: int, count: int) -> tuple[bool, str]:
        inv = self.db.get(uid)
        gun = inv.get("gun")
        if not gun:
            return False, "你还没有枪械，无法购买子弹。先用 /lfe buy <枪名> 买一把。"
        caliber = self.guns.get(gun, {}).get("caliber", "")
        if not caliber:
            return False, f"「{gun}」没有可用口径数据。"

        if str(level) not in self.ammo.get(caliber, {}):
            avail = "、".join(sorted(self.ammo.get(caliber, {}).keys(), key=int))
            return False, f"{gun}（{caliber}）没有 {level} 级弹，可选等级：{avail}"

        unit = self.ammo_price(caliber, level)
        if unit <= 0:
            return False, "该弹药暂无价格数据。"
        total = unit * count
        ok, left = self.db.spend_money(uid, total)
        if not ok:
            return False, f"余额不足：需要 {total:,}（{count} × {unit:,}），当前 {left:,}。"

        same_level = int(inv.get("ammo_level") or 0) == int(level)
        exist = int(inv.get("ammo_count") or 0) if same_level else 0
        self.db.set_ammo(uid, level, exist + count)
        return True, f"购买 {level} 级弹 {count} 发（-{total:,}），现有 {exist + count} 发，余额 {left:,}"

    # ---------- 普通模式 ----------

    def run_normal(self, uid: str) -> str:
        count = max(1, len(self.regions))
        mult = float(self.map_multiplier.get("normal", 1.0))
        per = MAP_BASE_VALUE * mult / count
        base_total = 0
        extra_total = 0
        detail: list[str] = []

        for name in self.regions:
            gain = int(per * random.uniform(0.8, 1.2))
            base_total += gain
            if random.random() < NORMAL_HIGH_VALUE_CHANCE:
                value = random.randint(3000000, 9000000)
                tag = "高价值物资"
                if random.random() < 0.01:
                    value = 13140000
                    tag = "★ 大奖"
                extra_total += value
                detail.append(f"  · {name}：{tag} +{value:,}")

        total = base_total + extra_total
        row = self.db.record_run(uid, "normal", 0, 0, total)
        lines = [f"【LFE · 普通】快速搜刮结算（{count} 个区域）", f"基础搜刮合计：{base_total:,}"]
        if detail:
            lines.append("额外发现：")
            lines.extend(detail)
            lines.append(f"额外收益合计：{extra_total:,}")
        else:
            lines.append("本次没有碰到高价值物资。")
        lines += [f"—— 本局带出：{total:,} 哈夫币", f"—— 账户余额：{row.get('money', 0):,}"]
        return "\n".join(lines)

    # ---------- 开局 ----------

    def start(self, uid: str, mode: str) -> tuple[bool, str]:
        mode = MODE_ALIAS.get(mode.lower(), mode)
        if mode not in MODE_LABEL:
            return False, "模式只能是 普通 / 机密 / 绝密"

        if mode == "normal":
            return True, self.run_normal(uid)

        if uid in self.games and not self.games[uid].finished:
            return False, "你还有一局没结束，先撤离或 /lfe end。"

        inv = self.db.get(uid)
        gun = inv.get("gun") or ""
        helmet = inv.get("helmet") or ""
        armor = inv.get("armor") or ""
        ammo_level = int(inv.get("ammo_level") or 0)
        ammo_count = int(inv.get("ammo_count") or 0)

        value = self.gear_value(gun, helmet, armor)
        need = MODE_THRESHOLD[mode]
        if value < need:
            return False, (
                f"带入价值不足，无法进入{MODE_LABEL[mode]}。\n"
                f"当前：{value:,} ｜ 需要：{need:,} ｜ 还差 {need - value:,}\n"
                f"提示：先刷普通模式攒钱，再用 /lfe shop 购买装备。"
            )

        # 有枪但没子弹：仍允许进，只是打完就空枪
        state = GameState(user_id=uid, mode=mode)
        state.gun = gun
        state.has_gun = bool(gun)
        state.ammo_level = ammo_level
        state.ammo_count = ammo_count
        state.helmet = helmet
        state.armor = armor
        state.loot = 0
        state.carry_value = value
        state.is_rat = value < int(self.rat_threshold.get(mode, 0))
        state.alive_teams = self.total_teams
        state.region = random.choice(self.regions)
        state.visited.append(state.region)
        self.games[uid] = state

        caliber = self.guns.get(gun, {}).get("caliber", "") if gun else ""
        lines = [f"【LFE · {MODE_LABEL[mode]}】"]
        if gun:
            lines.append(
                f"武器：{gun}（火力 {self.gun_firepower(gun):.1f}）+ "
                f"{ammo_level or '—'} 级弹 ×{ammo_count}（系数 "
                f"{self.ammo_coef(caliber, ammo_level):.2f}）"
            )
        else:
            lines.append("武器：无（不消耗子弹，遇战只能逃）")
        lines += [
            f"护甲：{armor or '无'}｜头盔：{helmet or '无'}｜保护 {self.protection(armor, helmet):.2f}",
            f"带入价值：{value:,} 哈夫币" + ("（鼠鼠）" if state.is_rat else ""),
            f"你被投放到：{state.region}",
            "",
            f"行动：/lfe go <区域> ｜ /lfe search ｜ /lfe status（撤离区：{self.evac_region}）",
        ]
        return True, "\n".join(lines)

    # ---------- 敌人 ----------

    def gen_enemy(self, mode: str) -> dict:
        cfg = self.enemy_gen.get(mode, DEFAULT_ENEMY_GEN["normal"])
        rare = cfg.get("rareArmor", {}) or {}
        if rare and random.random() < sum(rare.values()):
            level = int(_pick({int(k): v for k, v in rare.items()}))
        else:
            lo, hi = cfg.get("armorLevel", [0, 2])
            level = random.randint(int(lo), int(hi))

        gun_fp = 0.0
        if random.random() < float(cfg.get("gunChance", 0)):
            pool = [g for g in self.gun_pool.get(cfg.get("gunPool", "bottom11"), []) if g in self.guns]
            if pool:
                gun_fp = self.gun_firepower(random.choice(pool))

        weights = {int(k): v for k, v in (cfg.get("ammoLevel") or {2: 1.0}).items()}
        ammo_level = int(_pick(weights) or 2)
        coef = {2: 0.3, 3: 0.5, 4: 0.8, 5: 1.0, 6: 1.2}.get(ammo_level, 0.5)
        return {
            "armorLevel": level, "helmetLevel": level,
            "gunFirepower": gun_fp, "ammoCoef": coef,
            "protection": self._level_protection(level) * 2,
        }

    def _level_protection(self, level: int) -> float:
        return {0: 0.0, 1: 0.06, 2: 0.12, 3: 0.18, 4: 0.25, 5: 0.32, 6: 0.40}.get(level, 0.0)

    # ---------- 战斗 ----------

    def combat(self, state: GameState, self_mult: float = 1.0) -> dict:
        enemy = self.gen_enemy(state.mode)
        caliber = self.guns.get(state.gun, {}).get("caliber", "")
        self_fp = self.gun_firepower(state.gun)
        self_coef = self.ammo_coef(caliber, state.ammo_level) or 0.5
        self_prot = self.protection(state.armor, state.helmet)

        # 弹药消耗记录（有枪且还有子弹时）
        if state.has_gun:
            spent = int(6 + 1.2 * max(0, enemy["armorLevel"] * 2 - 2)
                        + 0.3 * max(0, enemy["armorLevel"] * 2 - 2) ** 2)
            if state.ammo_count >= spent:
                state.ammo_count -= spent
                state.ammo_used += spent
            elif state.ammo_count > 0:
                state.ammo_used += state.ammo_count
                state.ammo_count = 0
                self_coef *= 0.4  # 弹药见底，火力打折
            else:
                self_coef = 0.15  # 空枪

        r_self = random.uniform(self.rand_min, self.rand_max)
        r_enemy = random.uniform(self.rand_min, self.rand_max)
        self_score = self_fp * self_coef * (1 - enemy["protection"]) * self_mult * r_self
        enemy_score = enemy["gunFirepower"] * enemy["ammoCoef"] * (1 - self_prot) * r_enemy
        ratio = 99.0 if enemy_score <= 0.01 else self_score / enemy_score

        if ratio >= 2.0:
            result, desc = "crushing", "碾压"
        elif ratio >= 1.3:
            result, desc = "clearWin", "稳胜"
        elif ratio >= 1.0:
            result, desc = "narrowWin", "险胜"
        elif ratio >= 0.77:
            result, desc = "defeat", "败退"
        else:
            result, desc = "rout", "完败"

        if result in ("crushing", "clearWin", "narrowWin"):
            state.kills += 1
        elif result == "rout":
            state.deaths += 1

        return {
            "ratio": round(ratio, 2), "result": result, "desc": desc,
            "selfScore": round(self_score, 2), "enemyScore": round(enemy_score, 2),
            "enemy": enemy,
        }

    # ---------- 事件 ----------

    def event_ids(self) -> list[str]:
        ids: list[str] = []
        for cat, val in self.categories.items():
            if cat.startswith("_") or not isinstance(val, dict):
                continue
            for eid in (val.get("events") or {}):
                if not eid.startswith("_"):
                    ids.append(eid)
        return ids

    def get_event_cfg(self, event_id: str) -> Optional[dict]:
        for cat, val in self.categories.items():
            if not isinstance(val, dict):
                continue
            evs = val.get("events") or {}
            if event_id in evs:
                return evs[event_id]
        return self.conditional.get(event_id)

    def check_require(self, event_id: str, state: GameState) -> tuple[bool, str]:
        cfg = self.get_event_cfg(event_id)
        if cfg is None:
            return False, f"事件 `{event_id}` 不存在"

        cond = cfg.get("condition")
        if cond == "carryValueBelowRatThreshold" and not state.is_rat:
            return False, "玩家不是鼠鼠（带入价值未低于阈值）"
        if cond == "noArmorRepair":
            return True, ""  # 目前没有甲修系统，碎甲可触发

        req = cfg.get("require")
        if not req:
            return True, ""
        if req == "meetRat":
            return (True, "") if state.is_rat else (False, "玩家不是鼠鼠（带入价值未低于阈值）")
        if isinstance(req, dict):
            if req.get("isRat") and not state.is_rat:
                return False, "玩家不是鼠鼠"
            if "headingToEvac" in req and not state.evac_triggered:
                return False, "玩家当前不在撤离倒计时状态"
            if "nonPlayerTeams" in req:
                alive_other = max(0, state.alive_teams - 1)
                if alive_other < req["nonPlayerTeams"]:
                    return False, f"存活非玩家队伍不足：当前 {alive_other} 队，需要 {req['nonPlayerTeams']} 队"
            if "searchLootMin" in req and state.loot < req["searchLootMin"]:
                return False, f"局内收益不足：当前 {state.loot:,}，需要 {req['searchLootMin']:,}"
        return True, ""

    def roll_event(self, state: GameState) -> Optional[str]:
        if state.next_event and state.next_event_times > 0:
            eid = state.next_event
            state.next_event_times -= 1
            if state.next_event_times <= 0:
                state.next_event = None
            return eid

        cat_weights = {
            cat: float(val.get("weight", 0))
            for cat, val in self.categories.items()
            if not cat.startswith("_") and isinstance(val, dict)
        }
        if not cat_weights:
            return None
        cat = _pick(cat_weights)
        if not cat:
            return None

        evs = self.categories[cat].get("events") or {}
        usable: dict[str, float] = {}
        for eid, cfg in evs.items():
            if eid in ALWAYS_EVENTS:
                continue
            if eid == "evacCountdown" and state.evac_triggered:
                continue
            if not self.check_require(eid, state)[0]:
                continue
            try:
                if float(cfg.get("triggerChance", 0)) >= 1.0:
                    continue
            except (TypeError, ValueError):
                pass
            usable[eid] = float(cfg.get("weight", 0))
        return _pick(usable) if usable else None

    def resolve_event(self, eid: str, state: GameState) -> str:
        cfg = self.get_event_cfg(eid) or {}
        text = cfg.get("text", "")
        lines = [text] if text else []
        combat_evs = self.categories.get("combat", {}).get("events", {})

        if eid == "searchLoot":
            count = max(1, len(self.regions))
            base = int(self.map_multiplier.get(state.mode, 1.0) * MAP_BASE_VALUE / count)
            gain = int(base * random.uniform(0.8, 1.2))
            state.loot += gain
            state.searched.append(state.region)
            lines.append(f"搜到物资，价值 {gain:,} 哈夫币（累计 {state.loot:,}）")

        elif eid == "highValueLoot":
            lo = int((cfg.get("value") or {}).get("min", 3000000))
            hi = int((cfg.get("value") or {}).get("max", 9000000))
            gain = random.randint(lo, hi)
            jackpot = cfg.get("jackpot") or {}
            if jackpot and random.random() < float(jackpot.get("chance", 0)):
                gain = int(jackpot.get("value", gain))
                lines.append("★ 大奖命中！")
            state.loot += gain
            lines.append(f"高价值物资：+{gain:,}（累计 {state.loot:,}）")

        elif eid == "surpriseRaven":
            stake = int(state.loot)
            if stake <= 0:
                lines.append("你身上没有值得押的东西，渡鸦悻悻飞走了。")
            elif random.random() < 0.30:
                state.loot = 0
                lines.append(f"渡鸦叼走了 {stake:,}，什么也没留下。")
            else:
                mult = random.uniform(0, 3)
                state.loot = int(state.loot * mult)
                lines.append(f"倍率 ×{mult:.2f}，当前收益 {state.loot:,}")

        elif eid == "mandelBrick":
            values = cfg.get("values", [2000000, 3000000, 4000000])
            idx = {"normal": 0, "confidential": 1, "topSecret": 2}.get(state.mode, 0)
            value = int(values[min(idx, len(values) - 1)])
            state.loot += value
            lines.append(f"曼德尔砖到手：+{value:,}（累计 {state.loot:,}）")

        elif eid == "clearAI":
            enemy = self.gen_enemy(state.mode)
            score = enemy["armorLevel"] + enemy["helmetLevel"]
            need = int(6 + 1.2 * (score - 2) + 0.3 * (score - 2) ** 2)
            if state.has_gun:
                if state.ammo_count >= need:
                    state.ammo_count -= need
                    state.ammo_used += need
                    lines.append(f"清场消耗：弹药 {need} 发（剩余 {state.ammo_count}）")
                else:
                    state.ammo_used += state.ammo_count
                    state.ammo_count = 0
                    lines.append("清场时弹药见底，勉强脱身。")
            else:
                lines.append("你没有枪，绕开了这些人机。")

        elif eid == "evacCountdown":
            state.evac_triggered = True
            state.evac_left = int(cfg.get("deadlineActions", 5))
            lines.append(f"撤离倒计时开始，剩余 {state.evac_left} 次行动。")

        elif eid == "meetRat":
            lines.append("你确认了面前这只鼠鼠的身份。")
            if random.random() < 0.5:
                lines.append("他趁你不备动了手……（背刺）")
                lines.append(self._combat_text(self.combat(state, self_mult=0.7)))
            else:
                lines.append("他缩了缩脖子，没有轻举妄动。")

        elif eid in combat_evs:
            if not state.has_gun:
                lines.append("你没有枪，只能逃。")
            else:
                mult = float(cfg.get("combatScoreMultiplier", 1.0))
                lines.append(self._combat_text(self.combat(state, self_mult=mult)))

        elif eid == "operatorBite":
            if random.random() < float(cfg.get("triggerChance", 0.8)):
                state.dead = True
                state.deaths += 1
                lines.append("撤离失败，你倒在了撤离点前。")
            else:
                lines.append("比特耸了耸肩，放你过去了。")
        else:
            lines.append("（该事件暂无结算逻辑）")

        return "\n".join([x for x in lines if x])

    def _combat_text(self, result: dict) -> str:
        return (f"战斗判定 ×{result['ratio']} → {result['desc']}"
                f"（我方 {result['selfScore']} / 敌方 {result['enemyScore']}）")

    # ---------- 行动 ----------

    def do_move(self, state: GameState, target: str) -> str:
        if state.dead:
            return self.settle(state, "撤离失败", dead=True)
        if state.finished:
            return "本局已结束。"
        if state.evac_triggered and state.evac_left <= 0:
            state.dead = True
            state.deaths += 1
            return self.settle(state, "撤离窗口关闭，你没能走出去", dead=True)

        if target in ("撤离", "撤离区", "evac", self.evac_region):
            return self.settle(state, "成功撤离")

        if target not in self.regions:
            return (f"没有这个区域：{target}\n可搜刮区域：{'、'.join(self.regions)}\n撤离区：{self.evac_region}")

        state.region = target
        state.steps += 1
        if state.evac_triggered:
            state.evac_left -= 1

        lines = [f"你移动到「{target}」。（行动 +1）"]
        if target not in state.visited:
            state.visited.append(target)
            lines.append(self.resolve_event("clearAI", state))
        return "\n".join(lines)

    def do_flee(self, state: GameState) -> str:
        if state.dead:
            return self.settle(state, "撤离失败", dead=True)
        if state.finished:
            return "本局已结束。"
        pool = [r for r in self.regions if r != state.region]
        if not pool:
            return "无处可逃。"
        state.region = random.choice(pool)
        state.steps += 1
        if state.evac_triggered:
            state.evac_left -= 1
        lines = [f"你强行脱离战斗，慌不择路跑到「{state.region}」。（行动 +1）"]
        if state.region not in state.visited:
            state.visited.append(state.region)
            lines.append(self.resolve_event("clearAI", state))
        return "\n".join(lines)

    def do_search(self, state: GameState) -> str:
        if state.dead:
            return self.settle(state, "撤离失败", dead=True)
        if state.finished:
            return "本局已结束。"
        if state.region in state.searched:
            return "这个区域已经被你翻遍了。"
        return self.resolve_event("searchLoot", state)

    def settle(self, state: GameState, reason: str, dead: bool = False) -> str:
        if state.settled:
            return "本局已结算。"
        state.settled = True
        state.finished = True

        if dead or state.dead:
            self.db.wipe_loadout(state.user_id)
            loot_gain = 0
            row = self.db.record_run(state.user_id, state.mode, state.kills, state.deaths, 0)
            lines = [
                f"【{MODE_LABEL.get(state.mode, state.mode)}】{reason}",
                f"本局收益 {state.loot:,} 全部丢失，装备与子弹已清零。",
                f"本局战斗：击杀 {state.kills} / 阵亡 {state.deaths}",
                f"账户余额：{row.get('money', 0):,}",
                "提示：用 /lfe shop 重新采购装备后再进图。",
            ]
        else:
            left = self.db.spend_ammo(state.user_id, state.ammo_used)
            row = self.db.record_run(state.user_id, state.mode, state.kills, state.deaths, state.loot)
            lines = [
                f"【{MODE_LABEL.get(state.mode, state.mode)}】{reason}",
                f"带出物资：{state.loot:,} 哈夫币",
                f"弹药消耗：{state.ammo_used} 发（剩余 {left} 发，可留到下一局）",
                f"本局战斗：击杀 {state.kills} / 阵亡 {state.deaths}",
                f"账户余额：{row.get('money', 0):,}",
            ]
        if state.mode == "confidential":
            lines.append(f"机密 KD：{_kd_text(row.get('conf_kills', 0), row.get('conf_deaths', 0))}")
        elif state.mode == "topSecret":
            lines.append(f"绝密 KD：{_kd_text(row.get('top_kills', 0), row.get('top_deaths', 0))}")

        self.games.pop(state.user_id, None)
        return "\n".join(lines)

    def status_text(self, state: GameState) -> str:
        caliber = self.guns.get(state.gun, {}).get("caliber", "") if state.gun else ""
        lines = [
            (f"【{MODE_LABEL.get(state.mode, state.mode)}】{state.gun} + {state.ammo_level or '—'}级弹 ×{state.ammo_count}"
             if state.has_gun else f"【{MODE_LABEL.get(state.mode, state.mode)}】无枪"),
            f"护甲 {state.armor or '无'}｜头盔 {state.helmet or '无'}｜保护 {self.protection(state.armor, state.helmet):.2f}",
            f"当前区域：{state.region}｜行动：{state.steps}｜收益：{state.loot:,}",
            f"战斗：击杀 {state.kills} / 阵亡 {state.deaths}｜带入价值：{state.carry_value:,}"
            + ("（鼠鼠）" if state.is_rat else ""),
        ]
        if state.evac_triggered:
            lines.append(f"⚠ 撤离倒计时：剩余 {state.evac_left} 次行动")
        if state.dead:
            lines.append("状态：已阵亡")
        elif state.finished:
            lines.append("状态：已撤离")
        return "\n".join(lines)

    def profile_text(self, uid: str) -> str:
        row = self.db.get(uid)
        gun = row.get("gun") or "无"
        helmet = row.get("helmet") or "无"
        armor = row.get("armor") or "无"
        ammo = f"{row.get('ammo_level') or '—'} 级 × {row.get('ammo_count') or 0}"
        value = self.gear_value(
            row.get("gun") or "", row.get("helmet") or "", row.get("armor") or "")
        return "\n".join([
            f"【档案 {uid}】",
            f"余额：{row['money']:,} 哈夫币",
            f"装备：枪 {gun}｜甲 {armor}｜头 {helmet}",
            f"子弹：{ammo}｜带入价值 {value:,}",
            f"机密 KD：{_kd_text(row['conf_kills'], row['conf_deaths'])}",
            f"绝密 KD：{_kd_text(row['top_kills'], row['top_deaths'])}",
            f"局数：普通 {row['runs_normal']} / 机密 {row['runs_conf']} / 绝密 {row['runs_top']}",
        ])


# ============================================================
# AstrBot 插件层
# ============================================================

def _strip_cmd(raw: str, *names: str) -> list[str]:
    parts = (raw or "").split()
    if parts and parts[0].lstrip("/").lower() in {n.lower() for n in names}:
        return parts[1:]
    return parts


def _is_admin(event: AstrMessageEvent) -> bool:
    try:
        fn = getattr(event, "is_admin", None)
        if callable(fn) and fn():
            return True
    except Exception:
        pass
    return getattr(event, "role", None) in ("admin", "owner", "superadmin")


def _uid(event: AstrMessageEvent) -> str:
    for attr in ("get_sender_id", "get_user_id"):
        fn = getattr(event, attr, None)
        if callable(fn):
            try:
                return str(fn())
            except Exception:
                continue
    for attr in ("sender_id", "user_id"):
        val = getattr(event, attr, None)
        if val:
            return str(val)
    return "unknown"


@register("lfe", "小朵", "LFE 搜打撤文字游戏", "3.0.0")
class LfePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.game = LfeGame()
        logger.info(
            f"[lfe] 载入：{len(self.game.guns)} 枪 / {len(self.game.armors)} 甲 / "
            f"{len(self.game.helmets)} 头 / {len(self.game.event_ids())} 事件 / "
            f"{len(self.game.regions)} 区域（撤离区 {self.game.evac_region}）"
        )

    @filter.command("lfe")
    async def lfe(self, event: AstrMessageEvent):
        args = _strip_cmd(event.message_str, "lfe")
        uid = _uid(event)

        if not args:
            yield event.plain_result(
                "【LFE 搜打撤】\n"
                "/lfe start <普通|机密|绝密>  开局（普通=一键搜刮）\n"
                "/lfe status / go / search / flee / end\n"
                "/lfe shop [武器|护甲|头盔]  商店\n"
                "/lfe buy <名称>             买装备\n"
                "/lfe ammo <等级> <数量>      买子弹（需先有枪）\n"
                "/lfe profile                档案｜/lfe money 余额\n"
                f"门槛：机密 22 万 / 绝密 60 万带入价值"
            )
            return

        sub = args[0].lower()

        if sub == "start":
            mode = args[1] if len(args) > 1 else "普通"
            ok, msg = self.game.start(uid, mode)
            yield event.plain_result(msg)
            return

        if sub == "shop":
            kind = args[1] if len(args) > 1 else ""
            row = self.game.db.get(uid)
            yield event.plain_result(f"余额：{row['money']:,}\n" + self.game.shop_list(kind))
            return

        if sub == "buy":
            if len(args) < 2:
                yield event.plain_result("用法：/lfe buy <名称>（名称里的空格可以省略）")
                return
            name = " ".join(args[1:])
            ok, msg = self.game.buy_gear(uid, name)
            yield event.plain_result(msg)
            return

        if sub in ("ammo", "子弹"):
            if len(args) < 3:
                yield event.plain_result("用法：/lfe ammo <等级> <数量>")
                return
            try:
                level, count = int(args[1]), int(args[2])
            except ValueError:
                yield event.plain_result("等级和数量必须是整数。")
                return
            if count <= 0:
                yield event.plain_result("数量必须大于 0。")
                return
            ok, msg = self.game.buy_ammo(uid, level, count)
            yield event.plain_result(msg)
            return

        if sub in ("profile", "me"):
            yield event.plain_result(self.game.profile_text(uid))
            return

        if sub in ("money", "余额"):
            yield event.plain_result(f"余额：{self.game.db.get(uid)['money']:,} 哈夫币")
            return

        state = self.game.games.get(uid)
        if not state:
            yield event.plain_result("你还没有开局，用 /lfe start <模式>。")
            return

        if sub == "status":
            yield event.plain_result(self.game.status_text(state))
        elif sub == "go":
            if len(args) < 2:
                yield event.plain_result(
                    f"用法：/lfe go <区域名>；撤离用 /lfe go {self.game.evac_region}\n"
                    f"可搜刮区域：{'、'.join(self.game.regions)}"
                )
                return
            yield event.plain_result(self.game.do_move(state, args[1]))
        elif sub == "search":
            yield event.plain_result(self.game.do_search(state))
        elif sub == "flee":
            yield event.plain_result(self.game.do_flee(state))
        elif sub == "end":
            yield event.plain_result(self.game.settle(state, "主动结束"))
        else:
            yield event.plain_result("未知子命令，发送 /lfe 查看帮助。")

    @filter.command("lfe_admin")
    async def lfe_admin(self, event: AstrMessageEvent):
        if not _is_admin(event):
            yield event.plain_result("只有管理员能用这个指令。")
            return

        args = _strip_cmd(event.message_str, "lfe_admin")
        if not args:
            yield event.plain_result(
                "【LFE 管理】\n"
                "/lfe_admin addmoney <QQ> <金额>\n"
                "/lfe_admin balance <QQ> <金额>\n"
                "/lfe_admin kd <QQ> <机密|绝密> <k> <d>\n"
                "/lfe_admin give <QQ> <装备名>\n"
                "/lfe_admin setnext <QQ> <事件ID> [次数]\n"
                "/lfe_admin clearnext <QQ>\n"
                "/lfe_admin info <QQ>｜reset <QQ>｜events"
            )
            return

        sub = args[0].lower()

        if sub == "events":
            yield event.plain_result("可用事件 ID：\n" + "、".join(self.game.event_ids()))
            return

        if len(args) < 2:
            yield event.plain_result("需要指定目标 QQ。")
            return

        target = args[1]
        state = self.game.games.get(target)

        if sub == "info":
            out = [self.game.profile_text(target)]
            if state:
                out.append("—— 当前对局 ——")
                out.append(self.game.status_text(state))
            yield event.plain_result("\n".join(out))
            return

        if sub == "reset":
            if not state:
                yield event.plain_result(f"{target} 没有进行中的对局。")
                return
            self.game.games.pop(target, None)
            yield event.plain_result(f"已清理 {target} 的对局（未结算）。")
            return

        if sub == "addmoney":
            if len(args) < 3:
                yield event.plain_result("用法：/lfe_admin addmoney <QQ> <金额>")
                return
            try:
                amount = int(args[2])
            except ValueError:
                yield event.plain_result("金额必须是整数。")
                return
            money = self.game.db.add_money(target, amount)
            yield event.plain_result(f"已为 {target} {'+' if amount>=0 else ''}{amount:,}，余额 {money:,}。")
            return

        if sub == "balance":
            if len(args) < 3:
                yield event.plain_result("用法：/lfe_admin balance <QQ> <金额>")
                return
            try:
                amount = int(args[2])
            except ValueError:
                yield event.plain_result("金额必须是整数。")
                return
            self.game.db.set_money(target, amount)
            yield event.plain_result(f"已将 {target} 余额设为 {amount:,}。")
            return

        if sub == "kd":
            if len(args) < 5:
                yield event.plain_result("用法：/lfe_admin kd <QQ> <机密|绝密> <kills> <deaths>")
                return
            mode = "confidential" if args[2] in ("机密", "confidential", "c") else \
                   "topSecret" if args[2] in ("绝密", "topSecret", "t") else None
            if not mode:
                yield event.plain_result("模式只能是 机密 或 绝密。")
                return
            try:
                k, d = int(args[3]), int(args[4])
            except ValueError:
                yield event.plain_result("kills 和 deaths 必须是整数。")
                return
            self.game.db.set_kd(target, mode, k, d)
            yield event.plain_result(f"已设定 {target} 的{MODE_LABEL[mode]} KD 为 {_kd_text(k, d)}。")
            return

        if sub == "give":
            if len(args) < 3:
                yield event.plain_result("用法：/lfe_admin give <QQ> <装备名>")
                return
            name = " ".join(args[2:])
            if name in self.game.guns:
                self.game.db.set_item(target, "gun", name)
                yield event.plain_result(f"已给 {target} 发放武器 {name}。")
            elif name in self.game.armors:
                self.game.db.set_item(target, "armor", name)
                yield event.plain_result(f"已给 {target} 发放护甲 {name}。")
            elif name in self.game.helmets:
                self.game.db.set_item(target, "helmet", name)
                yield event.plain_result(f"已给 {target} 发放头盔 {name}。")
            else:
                yield event.plain_result(f"找不到装备「{name}」。")
            return

        if not state:
            yield event.plain_result(f"{target} 没有进行中的对局（加钱请用 addmoney）。")
            return

        if sub == "setnext":
            if len(args) < 3:
                yield event.plain_result("用法：/lfe_admin setnext <QQ> <事件ID> [次数]")
                return
            eid = args[2]
            times = 1
            if len(args) >= 4:
                try:
                    times = max(1, int(args[3]))
                except ValueError:
                    yield event.plain_result("次数必须是整数。")
                    return
            if eid in ALWAYS_EVENTS:
                yield event.plain_result(f"`{eid}` 是必触发事件，无需手动指定。")
                return
            ok, reason = self.game.check_require(eid, state)
            if not ok:
                yield event.plain_result(f"无法指定事件 `{eid}`：{reason}")
                return
            state.next_event = eid
            state.next_event_times = times
            yield event.plain_result(f"已设定：{target} 接下来 {times} 次移动将触发 `{eid}`。")
            return

        if sub == "clearnext":
            state.next_event = None
            state.next_event_times = 0
            yield event.plain_result(f"已清除 {target} 的指定事件。")
            return

        yield event.plain_result("未知子命令，发送 /lfe_admin 查看帮助。")

    async def terminate(self):
        self.game.games.clear()
