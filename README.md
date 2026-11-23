# AstrBot LFE 搜打撤文字游戏

LFE 是运行在 AstrBot 中的文字搜打撤插件，支持普通、机密和绝密模式。装备、事件和地图从 `data` 目录读取，玩家账户数据使用 SQLite 持久化。

## 安装

将项目目录放入 `data/plugins/astrbot_plugin_lfe/`，然后在 AstrBot WebUI 中加载或重载插件。插件仅使用 Python 标准库，不需要额外依赖。

## 玩家指令

命令前缀以 AstrBot 配置为准，默认使用 `/lfe`。

```text
/lfe                                      查看帮助
/lfe start <普通|机密|绝密>                开始游戏
/lfe status                                查看当前状态
/lfe shop [武器|护甲|头盔]                 查看商店
/lfe buy <装备名称>                        购买装备
/lfe ammo <弹药等级> <数量>                购买子弹
/lfe profile                               查看余额、装备和 KD
/lfe money                                 查看余额
/lfe go <区域名|next|撤离点>               移动或撤离
/lfe search                                搜索当前区域
/lfe flee                                  逃往其他区域
/lfe end                                   主动结束本局
```

购买子弹前需要先拥有枪械。更换枪械会清空原枪械子弹，需要重新购买。

## 游戏规则

普通模式不需要装备，开始后会自动搜索所有区域并直接结算收益，用于积累资金。

机密和绝密模式需要达到带入价值门槛：机密 220,000，绝密 600,000。玩家从地图 JSON 中排除撤离点后随机出生。使用 `/lfe go <区域名>` 前往指定区域，或使用 `/lfe go next` 随机前往未访问区域。到达未访问区域后会自动触发清场事件并搜索物资；使用 `/lfe search` 可搜索当前尚未搜索的区域。

前往撤离点会自动结算。撤离成功会保留装备并结算收益；死亡或主动结束会按规则结算，并可能清空装备和子弹。

## 配置文件

```text
data/weapons.json       武器、口径、火力和价格
data/armorVests.json    护甲等级、防护系数和价格
data/helmets.json       头盔等级、防护系数和价格
data/ammo.json          弹药等级、系数和价格
data/event.json         事件分类、权重、条件和文本
data/map.json           区域和撤离点配置
```

事件由 `event.json` 的分类权重和事件权重动态抽取。

## 管理员指令

只有 AstrBot 管理员可以使用：

```text
/lfe_admin addmoney <QQ> <金额>              增加或扣除余额
/lfe_admin balance <QQ> <金额>               直接设置余额
/lfe_admin kd <QQ> <机密|绝密> <击杀> <死亡>  设置 KD
/lfe_admin give <QQ> <装备名称>              发放装备
/lfe_admin setnext <QQ> <事件ID> [次数]      指定后续移动事件
/lfe_admin clearnext <QQ>                    清除指定事件
/lfe_admin info <QQ>                         查看档案和当前对局
/lfe_admin reset <QQ>                        清理未结算对局
/lfe_admin events                            列出事件 ID
```

## 数据持久化

玩家余额、装备、子弹、局数和 KD 保存在 `data/lfe.db`，插件首次运行时自动创建。请勿手动删除数据库，以免造成玩家数据丢失。

## 项目结构

```text
astrbot_plugin_lfe/
├── data/
│   ├── ammo.json
│   ├── armorVests.json
│   ├── event.json
│   ├── helmets.json
│   ├── lfe.db              # 运行时自动生成
│   ├── map.json
│   └── weapons.json
├── main.py
├── metadata.yaml
└── README.md
```
