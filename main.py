import asyncio
import json
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


MODES = {"普通": "normal", "机密": "confidential", "绝密": "topSecret"}
MODE_LABELS = {value: key for key, value in MODES.items()}
LOBBY_SECONDS = 120
MODE_FAKE_PLAYERS = {"normal": 5, "confidential": 7, "topSecret": 9}
MODE_EQUIPMENT_LEVEL = {"normal": 2, "confidential": 4, "topSecret": 5}
MAX_COMBATANTS = 6
NORMAL_REWARD_MIN = 100_000
NORMAL_REWARD_MAX = 300_000


class LfePlugin(Star):
    """LFE（文字搜打撤）插件。"""

    def __init__(self, context: Context):
        super().__init__(context)
        self.root = Path(__file__).resolve().parent
        self.data_dir = self.root / "data"
        self.database_file = self.data_dir / "lfe.sqlite3"
        self.catalog = self._load_catalog()
        self._init_database()
        self.players: Dict[str, Dict[str, Any]] = self._load_players()
        self.lobbies: Dict[str, Dict[str, Any]] = {}
        self.games: Dict[str, Dict[str, Any]] = {}
        self.lobby_tasks: Dict[str, asyncio.Task] = {}

    def _load_json(self, path: Path, default: Any) -> Any:
        try:
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _load_catalog(self) -> Dict[str, Any]:
        return {
            "weapons": self._load_json(self.data_dir / "weapons.json", {}).get("weapons", []),
            "armor": self._load_json(self.data_dir / "armorVests.json", {}).get("armorVests", []),
            "helmets": self._load_json(self.data_dir / "helmets.json", {}).get("helmets", []),
            "ammo": self._load_json(self.data_dir / "ammo.json", {}).get("ammo", {}),
            "events": self._load_json(self.data_dir / "event.json", {}),
            "map": self._load_json(self.data_dir / "map.json", {}).get("zones", []),
        }

    def _init_database(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_file) as database:
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    user_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    loadout_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS mode_stats (
                    user_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    balance INTEGER NOT NULL,
                    kills INTEGER NOT NULL,
                    deaths INTEGER NOT NULL,
                    PRIMARY KEY (user_id, mode),
                    FOREIGN KEY (user_id) REFERENCES players(user_id) ON DELETE CASCADE
                )
                """
            )

    def _load_players(self) -> Dict[str, Dict[str, Any]]:
        players: Dict[str, Dict[str, Any]] = {}
        with sqlite3.connect(self.database_file) as database:
            database.execute("PRAGMA foreign_keys = ON")
            player_rows = database.execute(
                "SELECT user_id, name, loadout_json FROM players"
            ).fetchall()
            for user_id, name, loadout_json in player_rows:
                try:
                    loadout = json.loads(loadout_json)
                except json.JSONDecodeError:
                    loadout = {}
                players[user_id] = {"name": name, "modes": {}, "loadout": loadout}
            stat_rows = database.execute(
                "SELECT user_id, mode, balance, kills, deaths FROM mode_stats"
            ).fetchall()
            for user_id, mode, balance, kills, deaths in stat_rows:
                player = players.setdefault(user_id, {"name": user_id, "modes": {}})
                player["modes"][mode] = {
                    "balance": balance,
                    "kills": kills,
                    "deaths": deaths,
                }

        return players

    def _save_players(self) -> None:
        with sqlite3.connect(self.database_file) as database:
            database.execute("PRAGMA foreign_keys = ON")
            for user_id, player in self.players.items():
                database.execute(
                    """
                    INSERT INTO players (user_id, name, loadout_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        name = excluded.name,
                        loadout_json = excluded.loadout_json
                    """,
                    (
                        user_id,
                        str(player.get("name", user_id)),
                        json.dumps(player.get("loadout", {}), ensure_ascii=False),
                    ),
                )
                for mode, stats in player.get("modes", {}).items():
                    database.execute(
                        """
                        INSERT INTO mode_stats (user_id, mode, balance, kills, deaths)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(user_id, mode) DO UPDATE SET
                            balance = excluded.balance,
                            kills = excluded.kills,
                            deaths = excluded.deaths
                        """,
                        (
                            user_id,
                            mode,
                            int(stats.get("balance", 0)),
                            int(stats.get("kills", 0)),
                            int(stats.get("deaths", 0)),
                        ),
                    )

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    @staticmethod
    def _scope_id(event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        return str(group_id or "private:" + str(event.get_sender_id()))

    def _player(self, user_id: str) -> Dict[str, Any]:
        player = self.players.setdefault(
            user_id,
            {"name": user_id, "modes": {}, "loadout": self._starter_loadout()},
        )
        player.setdefault("modes", {})
        if not player.get("loadout"):
            player["loadout"] = self._starter_loadout()
        return player

    def _starter_loadout(self) -> Dict[str, Any]:
        weapon = min(self.catalog["weapons"], key=lambda item: int(item.get("price", 0)))
        armor = min(self.catalog["armor"], key=lambda item: int(item.get("price", 0)))
        helmet = min(self.catalog["helmets"], key=lambda item: int(item.get("price", 0)))
        ammo = next(iter(self.catalog["ammo"].get(weapon.get("caliber", ""), {}).values()), [{}])[0]
        return {"weapons": [weapon], "armor": armor, "helmet": helmet, "ammo": [{"item": ammo, "quantity": 30}]}

    def _mode_stats(self, user_id: str, mode: str) -> Dict[str, Any]:
        player = self._player(user_id)
        stats = player["modes"].setdefault(
            mode, {"balance": 0, "kills": 0, "deaths": 0}
        )
        return stats

    @staticmethod
    def _money(value: int) -> str:
        return f"{value:,}"

    @staticmethod
    def _find(items: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
        exact = next((item for item in items if item.get("name") == name), None)
        if exact:
            return exact
        return next((item for item in items if name.lower() in item.get("name", "").lower()), None)

    def _help(self) -> str:
        return (
            "LFE 文字游戏指令：\n"
            "/lfe 商店 [枪械|护甲|头盔|子弹] —— 查看商品\n"
            "/lfe 购买 <枪械|护甲|头盔|子弹> <名称> [数量]\n"
            "/lfe 开始 <普通|机密|绝密> —— 创建 2 分钟候场\n"
            "/lfe 加入 —— 加入当前候场（需先购买装备）\n"
            "/lfe 立即开始 —— 候场发起人提前开始\n"
            "/lfe 搜索 —— 游戏中搜索并触发战斗\n"
            "/lfe 前往 <区域> —— 前往新区域并自动搜索\n"
            "/lfe 攻击 <玩家ID> —— 攻击同局玩家\n"
            "/lfe 撤离 —— 带着战利品撤离并结算\n"
            "/lfe 统计 —— 查看自己的资金、击杀和死亡"
        )

    def _store_text(self, category: str) -> str:
        aliases = {"枪械": "weapons", "武器": "weapons", "护甲": "armor", "防弹衣": "armor", "头盔": "helmets", "子弹": "ammo"}
        key = aliases.get(category, "")
        if key == "ammo":
            rows = []
            for caliber, levels in self.catalog["ammo"].items():
                for level, items in levels.items():
                    for item in items:
                        rows.append(f"{item['name']}（{caliber}，{item['price']:,}）")
            return "子弹商店：\n" + "\n".join(rows)
        if key not in ("weapons", "armor", "helmets"):
            return "请指定商店分类：枪械、护甲、头盔或子弹。"
        label = {"weapons": "枪械", "armor": "护甲", "helmets": "头盔"}[key]
        return label + "商店：\n" + "\n".join(
            f"{item['name']}（{item['price']:,}）" for item in self.catalog[key]
        )

    def _purchase(self, user_id: str, category: str, name: str, quantity: int) -> str:
        aliases = {"枪械": "weapons", "武器": "weapons", "护甲": "armor", "防弹衣": "armor", "头盔": "helmets", "子弹": "ammo"}
        key = aliases.get(category)
        item = None
        if key == "ammo":
            for levels in self.catalog["ammo"].values():
                for items in levels.values():
                    item = self._find(items, name)
                    if item:
                        break
                if item:
                    break
        elif key:
            item = self._find(self.catalog[key], name)
        if not item:
            return "找不到该商品，请先用 /lfe 商店 查看名称。"
        quantity = max(1, min(quantity, 999))
        stats = self._mode_stats(user_id, "normal")
        price = int(item["price"]) * quantity
        if stats["balance"] < price:
            return f"资金不足，需要 {self._money(price)}，当前只有 {self._money(stats['balance'])}。"
        stats["balance"] -= price
        loadout = self._player(user_id).setdefault("loadout", {"weapons": [], "armor": None, "helmet": None, "ammo": []})
        if key == "weapons":
            loadout["weapons"] = [item]
        elif key == "armor":
            loadout["armor"] = item
        elif key == "helmets":
            loadout["helmet"] = item
        else:
            loadout.setdefault("ammo", []).append({"item": item, "quantity": quantity})
        self._save_players()
        return f"购买成功：{item['name']} ×{quantity}，扣除 {self._money(price)}。普通模式资金余额：{self._money(stats['balance'])}。"

    def _equipment_ready(self, user_id: str) -> bool:
        loadout = self._player(user_id).get("loadout", {})
        return bool(loadout.get("weapons") and loadout.get("armor") and loadout.get("helmet") and loadout.get("ammo"))

    def _mode_items(self, mode: str, category: str) -> List[Dict[str, Any]]:
        level = MODE_EQUIPMENT_LEVEL[mode]
        items = self.catalog[category]
        if category == "weapons":
            return items[max(0, len(items) // 3) :] if mode != "normal" else items[len(items) // 2 :]
        return [item for item in items if int(item.get("level", 0)) <= level] or items

    def _fake_loadout(self, mode: str) -> Dict[str, Any]:
        weapon = random.choice(self._mode_items(mode, "weapons"))
        armor = max(self._mode_items(mode, "armor"), key=lambda item: int(item.get("level", 0)))
        helmet = max(self._mode_items(mode, "helmets"), key=lambda item: int(item.get("level", 0)))
        ammo_options = []
        for item in self.catalog["ammo"].get(weapon.get("caliber", ""), {}).values():
            ammo_options.extend(item)
        ammo = max(ammo_options, key=lambda item: float(item.get("firepowerCoefficient", 0))) if ammo_options else {}
        return {"weapons": [weapon], "armor": armor, "helmet": helmet, "ammo": [{"item": ammo, "quantity": 30}]}

    def _fake_player(self, mode: str, index: int) -> Dict[str, Any]:
        loadout = self._fake_loadout(mode)
        weapon = loadout["weapons"][0]
        armor = loadout["armor"]
        helmet = loadout["helmet"]
        score = float(weapon.get("firepower", 0))
        score *= 1 + float(armor.get("reductionCoefficient", 0)) + float(helmet.get("reductionCoefficient", 0))
        return {
            "name": f"AI-{index}",
            "is_fake": True,
            "loadout": loadout,
            "zone": self._spawn_zone().get("name", "未知区域"),
            "loot": random.randint(50_000, 300_000) * (1 if mode == "normal" else 2 if mode == "confidential" else 3),
            "hp": 100,
            "combat_score": score,
            "alive": True,
        }

    def _event_pool(self) -> List[Tuple[float, Dict[str, Any]]]:
        pool = []
        for category in self.catalog["events"].get("eventCategories", {}).values():
            category_weight = float(category.get("weight", 0))
            for event in category.get("events", {}).values():
                if isinstance(event, dict):
                    pool.append((category_weight * float(event.get("weight", 1)), event))
        return pool

    def _roll_event(self) -> Optional[Dict[str, Any]]:
        pool = self._event_pool()
        if not pool:
            return None
        total = sum(weight for weight, _ in pool)
        roll = random.uniform(0, total)
        for weight, event in pool:
            roll -= weight
            if roll <= 0:
                return event
        return pool[-1][1]

    def _map_zone(self) -> str:
        if not self.catalog["map"]:
            return "未知区域"
        return random.choice(self.catalog["map"]).get("name", "未知区域")

    def _spawn_zone(self) -> Dict[str, Any]:
        zones = [zone for zone in self.catalog["map"] if zone.get("type") != "Extraction"]
        return random.choice(zones) if zones else {"id": "unknown", "name": "未知区域", "type": "POI"}

    def _find_zone(self, name: str) -> Optional[Dict[str, Any]]:
        return next(
            (
                zone
                for zone in self.catalog["map"]
                if zone.get("name") == name or zone.get("id") == name or name in zone.get("name", "")
            ),
            None,
        )

    def _combat_with_fake(self, scope: str, user_id: str, fake_id: str) -> str:
        game = self.games[scope]
        player = game["players"][user_id]
        fake = game["players"][fake_id]
        player_score = self._combat_score(player.get("loadout", {})) * random.uniform(0.8, 1.2)
        fake_score = float(fake.get("combat_score", 1)) * random.uniform(0.8, 1.2)
        if player_score >= fake_score:
            game["players"].pop(fake_id)
            stats = self._mode_stats(user_id, game["mode"])
            stats["kills"] += 1
            player["loot"] += int(fake.get("loot", 0))
            self._save_players()
            return f"你击杀了伪玩家 {fake['name']}，本模式击杀数 +1，缴获 {self._money(int(fake.get('loot', 0)))}。"
        player["hp"] -= random.randint(35, 75)
        if player["hp"] <= 0:
            return self._finish_player(scope, user_id, False, fake["name"])
        return f"你与伪玩家 {fake['name']} 交战失利，当前生命 {player['hp']}。"

    @staticmethod
    def _combat_score(loadout: Dict[str, Any]) -> float:
        weapons = loadout.get("weapons", [])
        weapon = weapons[0] if weapons else {}
        armor = loadout.get("armor") or {}
        helmet = loadout.get("helmet") or {}
        return float(weapon.get("firepower", 20)) * (
            1 + float(armor.get("reductionCoefficient", 0)) + float(helmet.get("reductionCoefficient", 0))
        )

    def _resolve_search(self, scope: str, user_id: str) -> str:
        game = self.games[scope]
        entry = game["players"][user_id]
        zone = entry.get("zone", "未知区域")
        rolled_event = self._roll_event()
        if not rolled_event:
            return f"{zone}：没有发现任何异常。"
        text = rolled_event.get("text", "你在区域内搜索。")
        event_type = rolled_event.get("type", "normal")
        if event_type == "profit":
            value = rolled_event.get("value")
            if isinstance(value, dict):
                reward = random.randint(int(value.get("min", 50_000)), int(value.get("max", 500_000)))
            elif isinstance(rolled_event.get("values"), list):
                reward = int(random.choice(rolled_event["values"]))
            else:
                reward = random.randint(50_000, 500_000)
            multiplier = {"normal": 1, "confidential": 2, "topSecret": 3}[game["mode"]]
            reward *= multiplier
            entry["loot"] += reward
            return f"{zone}：{text}\n获得战利品 {self._money(reward)}，本局共 {self._money(entry['loot'])}。"
        if event_type == "combat" or rolled_event.get("combat"):
            fake_ids = [key for key, value in game["players"].items() if value.get("is_fake")]
            if fake_ids:
                return f"{zone}：{text}\n{self._combat_with_fake(scope, user_id, random.choice(fake_ids))}"
            return f"{zone}：{text}\n附近已经没有伪玩家。"
        if event_type == "consume":
            damage = random.randint(10, 35)
            entry["hp"] -= damage
            result = f"{zone}：{text}\n受到 {damage} 点伤害，当前生命 {max(0, entry['hp'])}。"
            if entry["hp"] <= 0:
                return result + "\n" + self._finish_player(scope, user_id, False)
            return result
        return f"{zone}：{text}"

    def _create_game(self, scope: str, mode: str) -> Dict[str, Any]:
        game = {"mode": mode, "players": {}, "started": True}
        self.games[scope] = game
        return game

    def _finish_player(self, scope: str, user_id: str, escaped: bool, killed_by: str = "") -> str:
        game = self.games[scope]
        entry = game["players"].pop(user_id)
        stats = self._mode_stats(user_id, game["mode"])
        if escaped:
            reward = int(entry["loot"])
            stats["balance"] += reward
            message = f"撤离成功，带出 {self._money(reward)}。"
        else:
            stats["deaths"] += 1
            message = f"你已死亡{('，击杀者：' + killed_by) if killed_by else ''}，本局带入装备全部损失。"
        self._save_players()
        if not game["players"]:
            del self.games[scope]
        return message

    async def _open_lobby(self, scope: str) -> None:
        try:
            await asyncio.sleep(LOBBY_SECONDS)
            if scope in self.lobbies:
                lobby = self.lobbies.pop(scope)
                self.lobby_tasks.pop(scope, None)
                await self._start_lobby(scope, lobby)
        except asyncio.CancelledError:
            return

    async def _start_lobby(self, scope: str, lobby: Dict[str, Any]) -> str:
        if lobby["mode"] == "normal":
            rewards = []
            for user_id in lobby["players"]:
                stats = self._mode_stats(user_id, "normal")
                reward = random.randint(NORMAL_REWARD_MIN, NORMAL_REWARD_MAX)
                stats["balance"] += reward
                rewards.append(f"{user_id} +{self._money(reward)}")
            self._save_players()
            return "普通模式为快速结算模式，不生成伪玩家，游戏结束。\n" + "\n".join(rewards)

        game = self._create_game(scope, lobby["mode"])
        for user_id in lobby["players"]:
            self._mode_stats(user_id, lobby["mode"])
            game["players"][user_id] = {
                "loot": 0,
                "hp": 100,
                "alive": True,
                "is_fake": False,
                "loadout": self._player(user_id).get("loadout", {}),
                "zone": self._spawn_zone().get("name", "未知区域"),
            }
        fake_count = min(MODE_FAKE_PLAYERS[lobby["mode"]], max(0, MAX_COMBATANTS - len(lobby["players"])))
        for index in range(1, fake_count + 1):
            game["players"][f"bot:{scope}:{index}"] = self._fake_player(lobby["mode"], index)
        self._save_players()
        return f"{MODE_LABELS[lobby['mode']]}局已开始，真人 {len(lobby['players'])} 人，伪玩家 {fake_count} 人。使用 /lfe 搜索、/lfe 攻击 <玩家ID> 或 /lfe 撤离。"

    @filter.command("lfe")
    async def lfe(self, event: AstrMessageEvent):
        """文字搜打撤主命令。"""
        args = event.message_str.strip().split()
        if args and args[0].lower() in ("/lfe", "lfe"):
            args = args[1:]
        user_id, scope = self._sender_id(event), self._scope_id(event)
        if not args or args[0] in ("帮助", "help"):
            yield event.plain_result(self._help())
            return
        command = args[0]
        if command == "商店":
            yield event.plain_result(self._store_text(args[1] if len(args) > 1 else ""))
            return
        if command == "购买" and len(args) >= 3:
            try:
                quantity = int(args[-1]) if len(args) > 3 else 1
            except ValueError:
                quantity = 1
            name_end = -1 if len(args) > 3 else len(args)
            yield event.plain_result(self._purchase(user_id, args[1], " ".join(args[2:name_end]), quantity))
            return
        if command == "统计":
            player = self._player(user_id)
            lines = [f"玩家 {user_id} 统计："]
            for mode, stats in player["modes"].items():
                lines.append(f"{MODE_LABELS.get(mode, mode)}：资金 {self._money(stats['balance'])}，击杀 {stats['kills']}，死亡 {stats['deaths']}")
            yield event.plain_result("\n".join(lines) if len(lines) > 1 else "暂无统计。玩家资金初始为 0。")
            return
        if command == "开始" and len(args) >= 2:
            mode = MODES.get(args[1])
            if not mode:
                yield event.plain_result("模式只能是：普通、机密、绝密。")
                return
            if scope in self.lobbies or scope in self.games:
                yield event.plain_result("本群已有一局游戏或候场。")
                return
            if not self._equipment_ready(user_id):
                yield event.plain_result("请先购买枪械、护甲、头盔和子弹，再开始游戏。")
                return
            self.lobbies[scope] = {"mode": mode, "owner": user_id, "players": [user_id], "created": time.time()}
            self.lobby_tasks[scope] = asyncio.create_task(self._open_lobby(scope))
            yield event.plain_result(f"已创建{args[1]}局，候场 2 分钟。其他玩家发送 /lfe 加入；发起人可发送 /lfe 立即开始。")
            return
        if command == "加入":
            lobby = self.lobbies.get(scope)
            if not lobby:
                yield event.plain_result("当前没有候场中的游戏。")
                return
            if user_id in lobby["players"]:
                yield event.plain_result("你已经在本局候场中。")
                return
            if len(lobby["players"]) >= MAX_COMBATANTS:
                yield event.plain_result(f"本局最多 {MAX_COMBATANTS} 名玩家或伪玩家，候场已满。")
                return
            if not self._equipment_ready(user_id):
                yield event.plain_result("请先购买枪械、护甲、头盔和子弹，再加入游戏。")
                return
            lobby["players"].append(user_id)
            yield event.plain_result(f"加入成功，当前 {len(lobby['players'])} 人。")
            return
        if command == "立即开始":
            lobby = self.lobbies.get(scope)
            if not lobby or lobby["owner"] != user_id:
                yield event.plain_result("只有候场发起人可以立即开始。")
                return
            self.lobbies.pop(scope)
            task = self.lobby_tasks.pop(scope, None)
            if task:
                task.cancel()
            yield event.plain_result(await self._start_lobby(scope, lobby))
            return
        game = self.games.get(scope)
        if not game or user_id not in game["players"]:
            yield event.plain_result("你当前不在游戏中。先购买装备并发送 /lfe 开始 <模式>。")
            return
        entry = game["players"][user_id]
        if command == "搜索":
            yield event.plain_result(self._resolve_search(scope, user_id))
            return
        if command == "前往" and len(args) >= 2:
            target_zone = self._find_zone(" ".join(args[1:]))
            if not target_zone:
                available = "、".join(zone.get("name", "未知区域") for zone in self.catalog["map"])
                yield event.plain_result(f"找不到该区域，可前往：{available}")
                return
            target_name = target_zone.get("name", "未知区域")
            if target_name == entry.get("zone"):
                yield event.plain_result(f"你已经在{target_name}，请选择其他区域。")
                return
            entry["zone"] = target_name
            if target_zone.get("type") == "Extraction":
                yield event.plain_result(f"你到达撤离点 {target_name}。\n{self._finish_player(scope, user_id, True)}")
                return
            yield event.plain_result(f"你已到达 {target_name}。\n{self._resolve_search(scope, user_id)}")
            return
        if command == "攻击" and len(args) >= 2:
            target = args[1].lstrip("@").strip()
            target_id = next((item for item in game["players"] if item == target or item.endswith(target)), None)
            if not target_id or target_id == user_id:
                yield event.plain_result("找不到可攻击的同局玩家。")
                return
            if game["players"][target_id].get("is_fake"):
                yield event.plain_result(self._combat_with_fake(scope, user_id, target_id))
                return
            if random.random() < 0.6:
                game["players"].pop(target_id)
                self._mode_stats(user_id, game["mode"])["kills"] += 1
                self._mode_stats(target_id, game["mode"])["deaths"] += 1
                self._save_players()
                yield event.plain_result(f"你击杀了玩家 {target_id}，本模式击杀数 +1。")
            else:
                entry["hp"] -= random.randint(20, 45)
                yield event.plain_result(f"攻击未命中，遭到反击，当前生命 {max(0, entry['hp'])}。")
                if entry["hp"] <= 0:
                    yield event.plain_result(self._finish_player(scope, user_id, False))
            return
        if command == "撤离":
            yield event.plain_result(self._finish_player(scope, user_id, True))
            return
        yield event.plain_result("未知指令，发送 /lfe 帮助查看用法。")
