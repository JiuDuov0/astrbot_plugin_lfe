# AstrBot LFE 文字搜打撤

这是一个 AstrBot 群聊文字游戏插件。玩家可以购买枪械、护甲、头盔和子弹，选择普通、机密或绝密模式进入战局；每局创建后会等待 2 分钟，期间其他玩家可以加入，发起人也可以立即开始。玩家资金初始为 0，新玩家会获得一套最低等级的临时装备；战局中的事件收益用于积累资金。

## 项目结构

```text
astrbot_plugin_lfe/
├── data/
│   ├── ammo.json
│   ├── armorVests.json
│   ├── event.json
│   ├── helmets.json
│   ├── map.json
│   └── weapons.json
├── main.py
├── data/lfe.db        # 首次运行后自动生成，保存玩家数据
├── metadata.yaml
└── README.md
```

## 安装和运行

将整个项目目录放到 AstrBot 的 `data/plugins/astrbot_plugin_lfe`，在 AstrBot WebUI 的插件页面加载或重载插件。无需额外第三方依赖；插件由 AstrBot 运行，不要直接执行 `main.py`。

命令前缀以 AstrBot 的设置为准。发送 `/lfe 帮助` 查看完整指令。

## 常用流程

```text
/lfe 商店 枪械
/lfe 购买 枪械 M4A1
/lfe 购买 护甲 1级 尼龙防弹衣
/lfe 购买 头盔 1级 户外棒球帽
/lfe 购买 子弹 5.56x45mm FMJ 10
/lfe 开始 普通
/lfe 加入
/lfe 立即开始
/lfe 前往 码头
/lfe 攻击 <玩家ID>
/lfe 撤离
/lfe 统计
```

普通模式是不生成伪玩家的快速结算模式，候场结束后直接为参与者发放一笔资金并结束游戏。机密和绝密模式中，真人与伪玩家合计最多 6 名；剩余名额由伪玩家填充。玩家会从 `map.json` 中排除撤离点后随机出生，使用 `/lfe 前往 <区域>` 前往新区域，到达后会自动按 `event.json` 的事件分类和权重搜索；前往撤离点会自动结算撤离。伪玩家的装备等级来自对应 JSON 数据，击杀伪玩家同样计入该模式击杀数。

## 数据文件

装备、护甲、子弹、头盔、事件和地图均从 `data` 下的 JSON 文件读取。玩家的装备、各模式资金、击杀数和死亡数会写入自动生成的 SQLite 数据库 `data/lfe.db`。
