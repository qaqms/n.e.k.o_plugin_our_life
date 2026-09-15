# 我们的生活 Design Brief

> 定位：第三方作者插件（独立 Git 仓库，不寄放宿主 `plugin/plugins/`）。
> 风格：**养成系 + 强注入**——数值随真实时间演化，并以"她自己的身体与心情感受"注入对话上下文。

## Identity Lock

- plugin_id: `our_life`
- folder: `D:\neko kaifa\n.e.k.o_plugin_our_life`（开发工作区，仓库名 `n.e.k.o_plugin_our_life`）
- 挂载态目录名: `our_life` —— 宿主按 entry 的包名匹配目录名，**必须是合法标识符**，
  所以开发工作区目录名不能直接挂载（见「已知陷阱」§11）
- name: `我们的生活`
- entry: `plugin.plugins.our_life:OurLifePlugin`
- main class: `OurLifePlugin`
- repository: `n.e.k.o_plugin_our_life`

## Purpose

为猫娘维护一组随时间**真实演化**的状态数值（饱食 / 精力 / 心情 / 健康 / 好感），
把它们以她自己的身心感受强注入对话上下文，使她的语气、主动性、话题选择随数值变化；
她还有作息与口袋：夜里会睡、每天自己吃掉背包里的口粮、主人用金币囤货。
主人通过日常互动（来找她、陪她、连续相处的天数）喂养数值，通过囤口粮维持日常生活，
长期冷落则按拟真惩罚衰减，掉到"生病 / 闹脾气 / 饿坏了 / 累垮了"档时她会主动开口。
**她自己也会判断"刚才这轮聊得怎么样"**（v0.3.0），把它回流成一个有上限的修正项——
补上"成长只按发言条数算"的失真。
面板可视化当前状态、分档、金币与背包、口粮顾问、连续天数、她自己的感受与近期注入记录。

## Package Type and Capabilities

- package type: `plugin`（独立功能，**完全独立**：不调用任何其它插件、无外网、不改宿主平台层；
  v0.5.0 起含一发宿主回环只读在位性查询 `GET /api/tools`——官方 tool-calling.md 钦定的
  工具注册 resilience 模式，属"文档承诺面"，不是私有端点）
- capabilities:
  - callable entries：状态查询 / 手动纠偏 / 重置 / 总开关 / 照顾她（用物品）/ 商店购买 /
    金币补贴 / **口粮顾问**（她每天吃多少、还够几天、缺口多少）
  - LLM tools：她能自主查询身心状态、主动索取陪伴（带冷却）
  - lifecycle：startup 装配、shutdown 落盘
  - timer：周期采样行为（只读总线）+ 节律折算 + 自动进食 + 经济结算 + 注入判定
  - message injection：`push_message(visibility=[], ai_behavior="read"|"respond", coalesce_key=...)`
  - UI：Hosted TSX panel（「顶部常驻状态带 + Tabs」，v0.8.0 起六页：
    总览(今日/五轴/节律) / 打卡·打工 / 过日子(顾问/商店/背包) / 她的世界(感受/事件) /
    **状态**(她的自述 + 身体/作息/我们/她的想法/她的一天) / 管理(纠偏/注入史/配置)）
  - store：`PluginStore` 持久化（必须 `[plugin.store].enabled = true`，否则静默不落盘）
  - i18n：zh-CN / en（其余语言后续补齐，见"路线图"）
- inferred architecture:

```
timer tick（默认 30s，无 watchdog：每拍自成一拍，不持有跨拍对象）
  ├─ 行为采样 services/sampler.py
  │    └─ bus.conversations（只读快照；**不支持 watch()**）
  │         逐条读 turn 记录：turn_type=user/assistant + lanlan_name
  │         → 去重（conversation_id）→ 聚合出 user_turns / last_touch / streak / 时段分布
  ├─ 作息 core/rhythm.py（睡眠窗、时段、相处天数、纪念日）
  ├─ 惰性衰减 core/model.apply_decay（按清醒/睡眠分段积分 → exp(-Δt/τ) 折算到此刻）
  │    └─ core/model.apply_coupling（饿 → 心情掉更快；累 → 健康恢复更慢）
  ├─ 成长/冷落结算 core/model.apply_impulse† / apply_neglect（纯函数，无副作用）
  ├─ 经济 services/state（金币/背包/吃饭账/商店锁存账）+ core/economy + core/shop
  │    ├─ 自动进食 meal_plan → apply_meal → bump_meal_day（她每天吃几餐的账）
  │    ├─ 日薪 + 零花钱 + 里程碑金币 + 收藏件产出（按缺席天数补发、封顶 7 天）→ shard.sodas
  │    └─ 货架 core/shop（解锁锁存/当日限定/每日特惠哈希）→ shop_entry 成交前必复算
  └─ 注入判定 services/injector.py（危机/饿/累 > 纪念日 > 跨档 > 首触 > 判断 > 漂移；三层频控 + 睡觉静默）
       └─ core/injection.build_text（第一人称感受 + 行为倾向 + 时段，{MASTER_NAME}/{LANLAN_NAME}）

她自己的判断（v0.3.0 反馈闭环，与上面那条链并行）
  ├─ LLM 工具 our_life_judge（她自主调用）→ core/judgment.judge 四道闸门
  │    ├─ ① 单次幅度 = growth.turn_*_gain × _TURN_GAIN_SCALE（常量引用，永不写死数字）
  │    ├─ ② 会话递减（复用 ShardState.session_turns，跨会话归零）
  │    ├─ ③ 每日正向/负向双预算（ShardState.judgment_used_add / _subtract）
  │    └─ ④ 真实互动作前提（last_touch_at > last_judgment_at）
  ├─ 记账：ShardState.note_judgment（只存标签/时刻/幅度）+ 进程内增量队列
  └─ 施加：下一拍 tick 在 apply_decay/apply_coupling **之后** apply_judgment
       └─ 然后才判 transitions → 让判断带来的跨档也可能触发一次 tier_change 注入

† v0.1.0 的 `apply_impulse` 在 v0.2.0 里就是 `apply_turn_gain` / `apply_day_greet` / `apply_meal`。
```

## 数值模型（拟真衰减 / 惩罚式）

五项数值均 `0..100`，**按角色卡分片**（store key `ourlife@<lanlan>`）：

| 数值 | 周期 | 衰减 | 基线/地板 | 分档（下界 0/20/40/60/80） |
|---|---|---|---|---|
| `satiety` 饱食 | 小时级 | 清醒 4.0/时、睡眠 1.5/时 ↘ | 地板 0（靠吃饭回来） | 饿坏了 / 有点饿 / 吃饱了 / 吃得很饱 / 撑得慌 |
| `energy` 精力 | 小时级 | 清醒 −2.2/时、睡眠 +6/时；23 点后 ×1.35 ↘ | 地板 0 | 累垮了 / 有点累 / 还行 / 精神不错 / 精力充沛 |
| `mood` 心情 | 小时级 | τ = 3h，向静息基线回落（homeostasis） | 基线 55 | 闹脾气 / 低落 / 平静 / 愉快 / 雀跃 |
| `health` 健康 | 天级 | τ = 36h，向静息基线回落 | 基线 70 | 生病 / 虚弱 / 一般 / 良好 / 精神饱满 |
| `affection` 好感 | 周级 | τ = 45d，只缓慢回落不自动回升 | 地板 0 | 陌生 / 熟悉 / 亲近 / 亲密 / 羁绊 |

- 衰减是**读数时惰性折算**（`exp(-Δt/τ)`），不依赖常驻循环，也不怕进程重启。
  饱和/精力按**清醒与睡眠分段积分**（睡眠窗来自 `[our_life.rhythm]`，支持跨零点）。
- **分档阈值固定在 `core/model.py` 的常量**（单一来源），不暴露成配置项——避免"配置漂移"类回归。
  节律常量（`SATIETY_PER_HOUR_*` / `ENERGY_*` / `LATE_NIGHT_ENERGY_PENALTY`）、
  耦合阈值（`COUPLING_THRESHOLDS` / `COUPLING_FACTORS`）与纪念日锚点（`rhythm.ANNIVERSARY_DAYS`）
  同样是模型常量：它们是"这个角色怎么过日子"的刻画，不是用户调参。
- 配置只露**行为旋钮**（衰减时间常数、睡眠窗与纪念日奖励、经济旋钮、成长步长、冷落惩罚、注入频控）。
- **跨轴耦合不是"额外扣分"，而是改变时间常数**（`apply_coupling`）：饱食 < 40 → 心情朝向基线
  掉落 ×1.6（< 20 再 ×1.4）；精力 < 40 → 健康朝向基线**恢复** ×0.55（< 20 再 ×0.7）。
  所以"饿一天"的效果是心情一路往下掉，"熬夜攒久了"会病——因果读得出来。
  **耦合只在 `apply_coupling` 里施加**（`apply_decay` 只管时间）：第一版两处都放了，
  低精力那侧健康被恢复两次，被自己的门抓住（见 CHANGELOG v0.2.0）。
- **分档有两套读法，不许混用**（`core/model.py` 的模块 docstring 是权威说明）：
  - `tier_of` / `tier_transitions` = 硬比较，**面板与"这数值算哪一档"用它**，忠实反映每个数值；
  - `crosses_tier_boundary` / `eventful_tier_transitions` = **带 0.05 分迟滞**，
    只给**注入判定**用。否则分界线上的亚分噪声会被当成跨档事件：默认好评 20.0 正好压着
    stranger/acquainted 的线，第一拍 30 秒心跳就把它折成 19.9998，于是凭空发一条
    `tier_change` 强注入（真机 store 里抓到过这条，见 CHANGELOG 对应轮次）。

## 经济层与日常循环（v0.2.0）

口径：**她有作息、有肚子、有口袋**，而"每天要回来一次"这个动力来自一条会持续产生压力的循环——
**她每天自己吃掉背包里的口粮**。

```
rhythm（睡眠窗/时段/纪念日）
  ├─► model.apply_decay（清醒/睡眠分段折算 + 深夜惩罚）
  │        └─► model.apply_coupling（饿 → 心情掉更快；累 → 健康恢复更慢）
  ├─► economy.meal_plan ──► model.apply_meal（她吃饭：消耗背包里的一份口粮）
  │        └─► economy.bump_meal_day（吃饭账：面板"实测每天几餐"）
  ├─► economy.daily_income + daily_allowance（日薪与零花钱 → shard.sodas）
  └─► economy.advise（口粮顾问：每天几餐 / 还够几天 / 缺口 / 建议囤多少）
```

- **物品表固定在 `core/economy.py`**（v0.7.0 起 10 件：`meat/fish/cake/pudding/royal/medicine/toy/gift/giftbox/charm`），
  而"谁能上架、今天卖什么价、收藏件每天产什么"在 **`core/shop.py`**（同为纯函数层）。
  `plugin.toml` 只露旋钮（`staple_item_id` / `daily_allowance` / `carry_max` /
  `shop_daily_limit` / `meal_threshold` / 顾问视野与告警 / 日薪四项 / `start_sodas`）。
  与"分档阈值不进配置"同一条纪律。
- **不含无头随机**：不做抽卡/暴击。数值养成的手感来自可预期的规划。
  v0.7.0 的"每日特惠"不违反这条：它是 `(日期, 角色卡名)` 的 **SHA-256 确定性哈希**
  抽 1~2 件 -30%——同一天同一张卡，面板轮询/入口复算/测试重放三方看到同一份今日价；
  用的是 hashlib 而不是 `hash()`（后者按进程 salt，重启一次价格就变，那才是无头随机）。
- **睡眠期照吃**（只有"饿到阈值"这一道闸）：否则默认睡眠窗（8 小时掉 12 分）会把饱食压到阈值以下，
  她一醒来就同时"饿着"且"今天还没吃过"，账面对不上。
- **冷落只扣心情/健康/好感**：饱食与精力由作息与进食驱动，再按天扣一次就是双重惩罚。
- **口粮顾问是纯计算**（`economy.advise`，不读时钟不看 store）：输入是"她的消耗画像 + 背包 + 金币"，
  所以能直接单测"三天没回来会饿几天"这类问题。它与真实衰减速**同源**
  （`model.satiety_per_day` 与 `_satiety_points` 共用同一组常量）。

## First Version Scope

v0.1.0（首版）：

1. 三项数值 + 拟真衰减 + 成长/冷落结算（纯函数，可单测）
2. 行为统计：用户发言次数、最近互动时间、连续互动天数、时段分布（全部本地，零外部依赖）
3. 事件驱动 + 频控的强注入（分档跨越即时注入；低档跨越升级为让她主动开口）
4. Hosted TSX 面板：三项数值 + 分档 + 连续天数 + 近期注入记录 + 开关与手动纠偏
5. LLM 工具两个：她查询自身状态、她主动索取陪伴（带冷却）
6. fail-closed 总开关 `[our_life].enabled = false`（默认关：未打开前不注入、不结算、不推送）
7. 中英 i18n、`tests/` 数值门 + i18n 契约门、`tools/release_gate.py` 五门

## v0.8.0 Scope（「她此刻的状态」页：自述 + 四段档案）

用户反馈：现有角色状态太简单。本轮加一个**专属页面**回答"她现在怎么样、为什么"，
与总览的"每一项现在多少"分工不同。**零新配置、零新入口、零新权限、schema 不变**（全为读侧派生）。

1. **新判据层 `core/state_note.py`**（纯函数、零 SDK）：选自述句、定句数、算跨轴拖累。
   判据不进 TSX，否则会出现"面板上说的与她注入时说的不是一回事"且无门可管。
2. **自述句数随等级变动**（用户口径）：危机 3 句 / 偏低 2 句 / 平稳 1 句。
   关键取舍：**好感不参与升级判据**（`_DAILY_AXES`）——`Stats.affection` 默认就是 20.0（档 1），
   把它当"偏低"会让**每个新角色永远**多说一句"我还不太敢跟你撒娇"。
3. **顺序三层**：危机档最先 → 离中性档最远 → **同距离按 `STAT_NAMES`**。第 3 层是正确性而不是审美：
   否则"差 0.1 分的好感"会插到"精力累垮"前面。
4. **与 `build_text` 是平行出口不是复用**：注入文案是给模型的第二人称行为指令
   （`_FOOTER` 禁数字禁机制），面板是给你的第三人称档案；复用会把两条语义搅在一起。
5. **整句键，绝不逐词拼**（`panel.stateVoice.<轴>.<档>` 25 格）：中英词序不同，
   拼词在 en 下必坏（forever_companion 第九轮的同一课）。
6. **新补的拼接族结构门**：`stateVoice`（双向含孤儿键）、`stateCoupling`、`judgment`、`panel.stat`。
   动态拼键不在引用面门的射程里——缺文案不会报错，只会喷一个空引号。
7. **耦合因果第一次给主人看**：`coupling_codes` 直接消费 `COUPLING_FACTORS` 算出的因子，
   **不在本层重算阈值**（否则面板说她饿与心情真掉得快就有两个来源，早晚漂移）。

### 本轮踩到并已修的坑

- **后端判据不能只在独立仓跑**：`voice_keys` 初版把"数值"排在 `STAT_NAMES` 之前，
  默认好感 20.0 就会插到精力前面——单看函数签名看不出来，是手写四组样本跑出来的。
- **未登记标签会变空白徽章**：`_mind_view` 初版只判"非空字符串"，模型给了没登记的词
  就会拼出 i18n 里不存在的键。现按 `JUDGMENT_LABELS` 白名单收窄（行为门钉住）。

## v0.7.0 Scope（金币系统完善：签到日历 / 双向打工 / 商店深化）

本轮四项**全部完工**（#4 商店深化随 v0.7.0 收尾一并落地）：

1. **签到日历**：手动签到（基础 8 + 连续加成 2/天、封顶 10 天）+ 幸运事（15% ×1.5~3，
   后端掷骰）+ 金币补签（只续链不给钱、7 天窗、每 ISO 周 1 次）。关键决定：
   **连续天数从日历集合现算**（`core/checkin.current_streak`），不存增量计数器——
   补签接链是读数性质而非特判，计数器与集合漂移的空间为零。
   月历自绘（UI kit 无 Calendar，与作息条同一条"只用布局原语+面板级 CSS"纪律）。
2. **猫娘打工（真实班次）**：三份工固定在 `core/jobs.py`（与商品表同一单一来源纪律）；
   班次写进分片、tick 按时间判据到点结算（无常驻计时器，重启不丢）；工钱按下班时刻
   精力/心情/健康打 0.8~1.1 折（**不看饱食/好感**：前者已被自然衰减惩罚，再进工资是
   双重扣；后者不进时薪）；表列损耗是**额外磨损**，班期自然衰减照常；睡眠窗全段冲突
   不可排班；早退与到点共用同一个 `settle_shift`（fraction<1 自动折价按比例扣），
   实现里不长第二套算法；下班叙事走独立注入通道（`TRIGGER_JOB`，永远 read）。
3. **玩家打工（服务器权威小游戏）**：面板 `api.call` 可伪造 → 客户端上报的分数不可信。
   真值（题答/牌序）存分片 challenge，公开视图由 `challenge_public_view` 切分，
   快照泄露面有行为门钉住；一次性（consumed 后重放拒付）；时限过期作废且**不发钱的局
   不占额度**；猜大小牌序签发时一次抽定（重发/拖延不换牌）。
4. **商店深化（稀有度/解锁/每日折扣）**：三档稀有度是**呈现层**（解锁与折扣判定都不读它，
   否则改个标签会静默改变折扣池语义）。新四件：皇家肉干/布丁是消耗品口粮（皇家可选 staple
   但不自动升级——贵的先吃会把"囤什么吃什么"的旋钮吃掉），限定礼盒/幸运符是**收藏件**
   （买断后每天产出心情+2/金币+3，`feed` 入口以 `item_keepsake` 硬门拒收）。
   解锁判据全读已有后端计数器（累计班次/最长连签/好感档/当日幸运签），唯一新状态是
   **锁存账 `shop_unlocked`**（schema 5→6，缺键=空账，写前重走 `normalize_unlocks` 消毒）：
   解锁前**彻底隐藏**、解锁后**永不回退**（好感掉了礼盒不收回）。幸运符永不进账——
   "仅幸运签当日出现、过期下架"是行为契约。成交面：可见性与单价都在 `shop_entry` 后端
   复算（面板参数可伪造，与小游戏同一威胁模型），未上架的货只能撞 `shop_locked`；
   日限额按**实付价**计。收藏件产出按缺席天数补发、封顶 7 天（用户拍板；与零花钱
   "跨天只发当天一份"刻意不同——"不上线也在攒"是它的卖点），买入当天不产。

schema 4→5（签到/打工/游戏三本台账同批入档）；错误码 +26；入口 +7；面板新 Tab
「打卡·打工」三张卡；经济总账：被动 ~35-50 + 签到 8→28 + 她的班次 50-110（要拿口粮/药回扣）
+ 你的小游戏 40-100，消费侧缺口由商店深化补齐。

v0.7.0 收尾追加（#4）：schema 5→6（商店锁存账，旧分片缺键回退空账，无迁移）；
错误码 +2（`shop_locked` / `item_keepsake`）并把未注册的旧码 `unknown_item` 收编成已登记的
`invalid_item`（商店/照料同码，旧版那个码在面板上翻不出文案——本轮顺手还的债）；
无新入口、无新配置旋钮（货架规则是模型不是旋钮）。

## v0.6.0 Scope（面板细化：三层总览 + 七文件拆分，第十五轮）

1. **总览页三层化**：今日事实带（五个 StatCard）/ 五轴卡（数值 + 档位徽章 + 字符走势柱
   + 距下一档差 N 分 + 今日变化）/ 24 格作息条（小时柱 + 睡眠窗底色 + 此刻描边）。
   旧走势卡与节律卡是**被吸收**：sparkline 进卡、activeHours 进柱、KeyValue 降为四行补充。
2. **`axes` context 字段（本轮唯一 Python 数据面）**：`core.model.axis_details` 从
   `TIER_BOUNDS` 现算 `to_next/next_tier`（判据线==档位线，与 v0.4.0 事件钉档位表同纪律）；
   `_axis_view` 从 inject_history 挑**今日最早**快照做 `delta_today` 锚点，缺锚点返回
   `None`（面板整行不渲染——拿 0 冒充"没变"是编数据）；脏 `at` 跳过不炸。TSX 侧
   硬编码 20/40/60/80 有专门门拦。
3. **背包物品卡**：只有"在她手上"的物品成卡，「给她」直达（砍"下拉选中→再按照顾"两步流）；
   商店批量购买表单保留。组件不碰调用门面，动作一律回调上提（`Bag.onGive`）。
4. **经历时间线**：每条挂"{stat} {value}（跨线 {width}）"——value/width 从调试数据变叙事。
5. **文件拆分**：`ui/panel.tsx`（骨架）+ `ui/shared.tsx`（类型/纯函数，全仓唯一一份）+
   `ui/components/{axis_cards,day_band,rhythm_bar,bag,timeline}.tsx`；面板门扫描面
   扩到整个 `ui/**`（hosted-tsx 是顺依赖发现的文件级扫描，门只看主文件=留盲区）。
6. **顺手拔定时炸弹**：判断族测试 `_enabled_config` 显式关 `quiet_during_sleep`——
   默认睡眠窗把非危机注入整层抑制，端到端门到半夜变假红（未改动的 HEAD 在 00:10
   实踩）；假红的门等于没有门，睡眠抑制另由 `test_injection.py` 专测。

刻意不做：轴卡点击详情 Modal（下一轮候选）；新入口/新配置键（三处同源面零变动）；
新动作；动 Tabs 四页骨架与顶部状态带（v0.4.1/v0.4.2 真机成果原样保留）。

## v0.5.0 Scope（工具注册心跳：她的判断通道不再静默缺席，第十四轮）

1. **`services/tool_watch.py`（新）**：挂在 `on_tick` 上的低频巡检器，每 300s 回环
   `GET /api/tools`（stdlib urllib + `to_thread`，超时 4s），比对 `list_llm_tools()`
   声明面与实际在位集合，缺席者逐个重发 `LLM_TOOL_REGISTER`（宿主 replace 语义、幂等）。
2. **三条纪律**：main_server 不可达时**不盲挂**（只推进时钟等下轮，不追打也不每拍轰）；
   补挂走 `_notify_llm_tool_registered` 重发而非公开 `register_llm_tool` 重调
   （后者撞 `EntryConflictError`；unregister→register 有本地已删远端未挂的窗口）；
   认不出的响应形状按"全场无工具"处理（宁可幂等重发，不可漏挂）。
3. **永不连坐**：巡检器自己的异常全部内部消化（`watch_failed` 状态），`on_tick` 里再套
   一层双保险——心跳坏掉不许把行为采样 / 衰减结算拖下水。
4. **首拍即查**（`_last_run_at=0`）：竞态窗口要的是早发现；`no_tools` 不推进时钟，
   工具收集齐后下一拍立刻核对。与总开关无关：注册韧性是宿主层面的在场性，不随业务冻结。
5. **门**：`tests/test_tool_watch.py` 12 条——形状差集（嵌套/平铺/认不出/空声明）、
   间隔自节流、no_tools 不推钟、不可达零补挂且推钟、点名只补缺席者、
   单名失败不连坐、protected 面缺席整体降级、fetch 异常吞掉。

刻意不做：把间隔进配置（心跳参数不是行为参数）；面板暴露心跳状态（它健康的表现就是
"没有状态"）；给 `@llm_tool` 装饰器层做自动重试（那是宿主的事，插件侧只保证自己
声明过的工具最终在场）。

## v0.4.5 Scope（禁用按钮光标：not-allowed，第十三轮）

1. 面板根部注入 `.neko-button:disabled { cursor: not-allowed; }`（已知陷阱 §14）。
2. 新增光标门：`<style>{PANEL_STYLE_OVERRIDES}</style>` 与 not-allowed 字面量必须在。
3. 纯样式轮：交互、刷新通道、Python 侧零改动。

## v0.4.4 Scope（动作后即时刷新：金币/背包"后台扣了前台不动"，第十二轮）

1. **根因**：kit 只在 `ActionButton`/`ActionForm` 里兑现 `refresh_context`（runtime.js 两处），
   本面板全走普通 `Button` + `props.api.call`——动作成功后没人重拉 context（已知陷阱 §13）。
2. **单飞 + 尾随合并的 `refreshContext()`**：同一时刻最多一个在途 + 一次尾随补拉
   （`refreshBusy`/`refreshRerun` refs），轮询与动作刷新共用，慢机器上不叠请求。
3. **`run()` 成功路径统一调 `refreshContext()`**；Err 走异常、不动 context；
   八个动作处理器零改动。`switchFocus` 的手动补拉移除。
4. **门**：全文件 `props.api.refresh()` 只允许出现在 `refreshContext` 内一处
   （数次数），谁手写第二处就是绕过合并闸门。

刻意不做：把普通 Button 换成 ActionButton（表单形态不合商店/背包的交互形状，且通道已进门）；
改 Python 侧 `refresh_context` 声明（它对 ActionButton 形态调用方仍有效，也是宿主将来
在 api.call 层兑现该标志时的自愈面）。

## v0.4.3 Scope（面板自动刷新，第十一轮）

1. **轮询**：`useEffect` + `setInterval(10s)` 调 `props.api.refresh()`，节奏与 `tick_seconds=30`
   的结算周期同量级；写法沿用 `forever_companion` 真机验证过的形态。
2. **三条性能闸门**：防重入（`refreshBusy` ref，未返回就跳拍，慢则自动降频）、
   后台暂停（`document.hidden` 不拉）、回可见补拉（`visibilitychange`）。失败静默、下拍重试。
3. **手动刷新按钮退役**（状态带 + 空态两处）；`actionOf`/`HostedAction` 随之移除。
   Python 侧 `status` 入口**不动**（Agent / 命令面板还在用，note 同步门的键还在）。
   动作后的即时刷新走宿主 `refresh_context=True`。
4. **纯面板轮**：Python 零改动；新增刷新门断言轮询三形态存在、`<ActionButton`/`actionOf` 不回潮。

刻意不做：把轮询间隔做成配置项（它是 UI 实现细节，不是行为参数）；
WebSocket/推送式面板同步（宿主 context 模型是拉式，不自造通道）。

## v0.4.2 Scope（真机首跑适配：面板焦点 / deadlock / 流式布局，第十轮）

1. **归属解析链升级**：`_ctx（本次注入，权威）> 面板焦点 > 全局唯一分片`。焦点由新入口
   `focus`（@ui.action）设置，持久化在独立 store 键 `ourlife.focus`（刻意不带 `ourlife@` 前缀，
   否则分片扫描会把它当成一张卡）。焦点指向已消失的分片时当场自清（startup 与每次判定都验）。
2. **无分片死锁修好**：空态分支也渲染开关卡（Switch + 刷新 + 引导），新装机第一次开面板就能启用。
3. **`shards>1` 时状态带出现 `SegmentedControl` 切换器**（含「自动」段 = 清除焦点）。
4. **流式布局**：五轴带 / 商店目录用 `Columns`（fluid minWidth）；纠偏 `Slider`；
   顾问顶部 `StatCard` 三读数；`Progress` label 去重百分号（kit 自带）并取整。
5. **组件能力按真机核对**：Steam 安装包内嵌 kit 导出清单逐项确认
   （本轮用件不依赖比真机更新的宿主能力）。

刻意不做：跨进程广播焦点（store 键即单一事实源）；把焦点做成配置项（它是视图态，不是行为参数）。

## v0.4.1 Scope（面板分区重构：状态带 + 标签页，第九轮）

1. **顶部常驻状态带**：五轴 Progress + 今日事实徽章（第几天 / 连续天数 / 时段 / 睡眠 / 距边界 /
   危机轴）+ 总开关与刷新。告警（冻结 / 错误码 / 口粮告急）仍在带上方，任何位置都看得到她的状态。
2. **Tabs 四页**（kit 的 `Tabs`，激活态由宿主 `useLocalState("tabs:<id>")` 持久化，刷新不丢位置）：
   总览（走势 + 相处节律）/ 过日子（口粮顾问 + 商店×背包双栏）/ 她的世界（感受 + 经历）/
   管理（纠偏 + 近期注入 + 配置）。独立「总开关」卡并入状态带，不再单列。
3. **纯布局轮**：Python 侧零改动（context / action / 码 / 台账都不动），只新增面板文案键
   （`panel.band.*` / `panel.tab.*`，双语纯插入保 CRLF）与一条文本级布局门。

## v0.4.0 Scope（「阶段性事件与面板叙事」主线，第八轮）

1. `core/events.py`（新）：两个事件（`sick_recovery` / `cheered_up`）、判定顺序、
   冷却窗口、台账读取助手。**全是纯函数**——不读时钟、不接 `ShardState`、不产生文案。
2. **判据线引用档位表**：病愈 = 健康跨过 `TIER_BOUNDS[2]`（40，脱离 sick+frail 两档，
   因为那两档面板都读作"病着"）；哄好 = 心情跨过 `TIER_BOUNDS[1]`（20，脱离 sulking）。
   import 时自检，改档位表就会炸而不是悄悄错位。
3. `services/injector.plan_for_event`（新通道）：与 `plan_for_tick` **并列**而不是插进它的链，
   避免事件与危机互相挤掉；同轴抑制；睡眠抑制按 `wake_ok` 分。
4. `core/injection`：新增 `TRIGGER_STAGED_EVENT` 与 `EVENT_NARRATION_ZH`（第一人称叙事模板，
   按既有注入契约：不给数字、不给档名、带 `{MASTER_NAME}` / `{LANLAN_NAME}`）。
5. tick 接线（`__init__.py` 步骤 6.6）：**先无条件记账，再决定发不发**——
   睡过去的事件也要留在经历里，而"记账必须无条件"正是冷却判定的前提。
6. 面板「她经历过什么」块（时刻 / 事件 / 轴）+ 说明文案。
7. schema 3 → 4：新增有界 `event_history`（12 条，只存事件名/轴/时刻/数值快照）。

刻意不做（见 CHANGELOG 与下节）：**生日**（需要新配置面与用户真实输入，单独立轮）、
**事件给数值奖励**（会让"康复"变成刷数值路径）、**为事件开第三条频控**（复用总闸门）。

## v0.3.0 Scope（「反馈闭环」主线）

1. `core/judgment.py`（新）：五个标签的白名单枚举、输入收敛（非法 → `neutral`）、
   **四道闸门**（单次幅度引用 `turn_*_gain`、会话递减、每日正向/负向双预算、真实互动作前提）、
   修正量计算。全是纯函数，不接 `ShardState`、不读时钟。
2. `core/model.apply_judgment`：把已算好的增量加到数值上（加法 + 夹取，未知轴忽略）。
   **与 `apply_turn_gain` 并列但不替代它**——判断是修正项，成长基线原样保留。
3. `our_life_judge` LLM 工具：她自主回传"刚才那轮聊得怎么样"。工具**只判定 + 记账 + 排队**，
   数值由 tick 在折算之后施加；**永不抛异常、返回值不含数值**。
4. 注入第 8 档触发 `TRIGGER_JUDGMENT`：她的判断作为"她自己的感受"进上下文（静默、不打断），
   排在 `daily_greet` 之后、`interval` 之前。
5. 面板新增「她自己的感受」块：今日次数、剩余额度、最近几条判断。
6. schema 2 → 3：判断台账（**只存标签 / 时刻 / 幅度，不存任何对话正文**）。

刻意不做（见 CHANGELOG 与本档「Out of Scope」）：代餐成长、情绪感知、阶段性叙事、随机性。

## v0.2.0 Scope（「过日子」主线）

1. 五轴：新增 `satiety`（饱食）与 `energy`（精力），并把衰减改成**节律感知**
   （清醒/睡眠分段积分、深夜熬夜惩罚）
2. 作息：`core/rhythm.py` —— 睡眠窗（支持跨零点）、时段、相处天数与**纪念日**（含一次性礼物）
3. 经济：金币（日薪 + 每日零花钱 + 里程碑）、物品表、背包、携带上限与当日消费上限、
   **商店入口**、**照顾她入口**、**口粮顾问入口**
4. 日常循环：**她每天自己吃掉背包里的口粮**；囤的吃完而主人没回来就会饿（饿坏了会主动开口）
5. 跨轴耦合：饿 → 心情掉更快；累 → 健康恢复更慢（改时间常数，不是额外扣分）
6. 注入层加固：`respond` 独立小时上限、`coalesce_key` 折叠、**睡觉静默**（非危机不打扰）
7. 面板重写：五条 + 走势字符图 + 口粮顾问 + 商店卡片 + 背包 + 节律/纪念日
8. schema 1 → 2（向后兼容：旧分片逐字段回退默认值，不需要迁移脚本）

## Out of Scope

- ❌ 调用任何其它插件（用户明确要求完全独立；情绪感知后续以独立插件联动方式另开）
- ❌ 联网、读宿主 `core_config.json`、直连模型端点（不做任何数据出域）
- ❌ 复用宿主 `/api/emotion/analysis` 等内部端点（无版本承诺面，留待后续轮次评估）
- ❌ 修改宿主平台层任何代码
- ❌ 真图表组件（Hosted UI Kit 无图表组件）：v0.2.0 的走势用**字符画折线**（`▁▂▃▄▅▆▇█`）由
  `props.state.trend` 里的注入快照渲染，不做 canvas/SVG 图表
- ❌ **作息推断**：睡眠窗是配置项，不根据互动时段反推（推断错了会明显暴露成"她半夜不困"）
- ❌ 抽卡 / 随机奖励 / 暴击：数值养成的手感来自可预期的规划，随机化也写不出确定性测试门
- ❌ **让判断替代成长基线**（v0.3.0 明确了这条）：`apply_turn_gain` 按发言条数给的成长**原样保留**，
  她的判断只作为**叠加的、有上限的修正项**。两者是"基线 + 微调"，不是"新算法替换旧算法"——
  这样 v0.2.0 的全部测试门一条都不用改，回归面为零
- ❌ **给反馈闭环开负向大口子**：负向日预算刻意只有正向一半，且同样需要真实互动作前提
  （让工具能扣分等于给模型一条惩罚通道）
- ❌ **判断工具接受自由文本 / 数值绝对值**：签名里只有一个枚举 + 一个 0..1 的强度，
  从根上杜绝"模型把对话原文写进持久化状态"与"直接设数值"

## Inferred Technical Needs

- plugin.toml sections: `[plugin]`、`[plugin.author]`、`[plugin.sdk]`、`[plugin.i18n]`、`[plugin.store]`、`[plugin.ui]` + `[[plugin.ui.panel]]`、`[plugin_runtime]`、自定义 `[our_life]` 族
- SDK surfaces: `NekoPluginBase`、`@neko_plugin`、`@plugin_entry`、`@lifecycle`、`@timer_interval`、`@ui.context`、`@ui.action`、`@llm_tool`、`push_message`、`Ok/Err/SdkError`、`self.store`、`self.config`、`self.bus.conversations`
- UI surfaces: hosted-tsx panel `ui/panel.tsx`（v0.6.0 起：`ui/shared.tsx` + `ui/components/**`），
  权限 `state:read` + `config:read` + `action:call`；context 额外字段 `axes`（五轴卡明细，
  判据全部在 Python 侧现算）
- state/config: `self.store` 存数值与时间戳；`self.config` 读 `[our_life]`；开关写入走 entry → `self.config.set`
- lifecycle/background work: startup 装配采样状态；tick 内完成采样+结算+注入，**不持有跨拍对象**（timer 每拍新 event loop）
- external integrations: 无（仅宿主本地 bus 只读）
- v0.2.0 新增 SDK 面：`push_message` 的 `coalesce_key` / `priority`（折叠与加急）、
  `self.store.set/get/keys/delete`（分片与背包）、`@plugin_entry` 的 `input_schema.enum`（商店与物品选择）
- v0.3.0 新增 SDK 面：`@llm_tool` 的 `parameters.enum`（判断标签的**白名单**——把"非法输入"
  挡在模型这一侧的同时，插件侧仍按不可信输入再收敛一次）；`@llm_tool` 返回值刻意**不含数值**

## Read Context Plan

- 已读：`.agent/rules/neko-guide.md`、`.agent/skills/neko-plugin/**`（含 execution-boundary / core-plugin-contract /
  plugin-creation-workflow / checks-and-tests / cli-and-debugging / surface-map）、`plugin/PLUGIN_DEVELOPMENT_GUIDE.md`、
  `docs/zh-CN/plugins/**`、`plugin/config/schema.py`、`plugin/neko_plugin_cli/**`（init/build/validate 源码）、
  `plugin/sdk/plugin/**`、`plugin/sdk/shared/core/bus_context.py`、`plugin/core/host.py`、`plugin/sdk/hosted-ui/index.d.ts`、
  宿主 `main_routers/system_router/emotion.py` + `main_routers/proactive_router.py`（确认本地端点边界）、
  三份台账（`未完成的问题/*.md`）与三个既有插件仓库（catgirl_seiyuu / forever_companion / your_memory）
- 参考落盘：`../neko-plugin-sdk-api-reference.md`、`../hosted-ui-reference.zh-CN.md`

## Write Workspace

`D:\neko kaifa\n.e.k.o_plugin_our_life\`（独立 Git 仓库；不触碰 N.E.K.O 源码树）。
宿主仓在同级的 `D:\neko kaifa\N.E.K.O\` —— `tools/release_gate.py` 的默认宿主探测
（`PLUGIN_ROOT.parent / "N.E.K.O"`）正好命中这个布局，无需 `--host-root`。

## 已知陷阱（实现时必须绕开，均源码核实）

1. **`ctx._current_lanlan` 是脏值**：`plugin/plugins/jukebox_controller` 明确写了 "Deliberately no
   `ctx._current_lanlan` fallback"——该属性由"某次调用"的 `_ctx["lanlan_name"]` 写入，本次调用不带时
   里面留着**上一次**的角色名。归属只能取本次注入的 `_ctx`，或总线记录自带的 `lanlan_name`。
2. **`bus.conversations` 不支持 `watch()`**：只有 `messages` / `events` / `lifecycle` 可 watch
   （`migration-v0.9`）。行为采样只能定时轮询只读快照。
3. **timer 每拍 `asyncio.run(fn())`（新 event loop）且无 watchdog**：tick 内不得使用在其它拍/`startup`
   里创建的 loop 绑定对象；异常只记日志不停表，需自己兜。
   - **已核实 = 真·独立线程**（v0.4.x 复核，`plugin/core/host.py:1217-1250`）：每个
     `auto_start=True` 的 `@timer_interval` 由 `threading.Thread(daemon=True)` 起**专属线程**，
     循环体是 `asyncio.run(fn())` + `stop_event.wait(interval)`（`:1218-1230`）；
     而入口 / 工具 / 消息走**另一条** `asyncio.run(_async_command_loop())`（`:1788`，主线程）。
   - ⇒ `on_tick`（timer 线程）与 `our_life_judge` 等入口/工具（command loop 线程）是**真并发**，
     不是同一事件循环里的交替。两者共享同一份 `ShardState` 实例
     （`services/state.py:512-520` 的 `load` 返回缓存对象、`:522-524` 的 `save` 原地更新缓存），
     因此在 `await` 点上的读-改-写**理论上有交错窗口**。
   - 当前结论（不夸大）：`judgment_*` 轴的唯一写入方就是 `our_life_judge` 自己
     （`__init__.py:1127-1147`，入口只在 `judge` 里调），所以没有第二个写者去丢它的更新；
     `daily_allowance_granted` / 里程碑同样只有 `_settle` 写。**未观察到真实损失，暂不加锁**，
     但这条窗口是**没有测试门覆盖**的——新增任何"由入口/工具写、由 tick 消费"的双写字段时，
     必须先回头评估这里（或改为单一队列/加 `threading.Lock`）。
4. **`[plugin.store].enabled = false` 时 store 静默失效**：`set` 返回 `Ok` 却不落盘、`get` 返回默认值。
5. **`push_message` 的 `submitted=True` 不等于宿主已消费**：启动钩子期推送会落在订阅窗口之前被静默丢弃。
   注入只在 tick / 入口 / 工具里发，不在 `startup` 里发。
6. **面向用户的文本必须用 `{MASTER_NAME}` / `{LANLAN_NAME}` 占位符**，不得硬编码"主人/用户"。
7. **面板可达入口的 `Err(SdkError(...))` 与 `note` 只用稳定 ASCII 码**（`^[a-z][a-z0-9_]*$`），
   动态细节进日志；前端按 `panel.errors.<camelCase(码)>` 翻译。
8. **`data/` 与 `cache/` 才是可写位置**：`self.plugin_dir`（= `config_dir` 别名）是只读安装目录。
9. **隐私**：注入正文含用户互动信息，只进总线不进日志正文；涉及原文一律不上 `logger`。
10. **`@llm_tool` 名称**必须匹配 `^[A-Za-z0-9_.\-]{1,64}$`，且工具注册表在 `main_server` 内存里，
    宿主重启即丢、无自动重注册——v0.5.0 起由 `services/tool_watch.py` 心跳兜住
    （回环 `GET /api/tools` 核实 + 缺席重发，官方 tool-calling.md 钦定模式）。
11. **挂载态目录名必须等于 entry 的包名**（宿主源码核实）。两段链：
    - `plugin/server/application/plugins/lifecycle_service.py:1078-1091` 先调
      `normalize_plugin_entry_point(...)`，再调 `describe_plugin_entry_directory_mismatch(...)`，
      不一致即 `code="PLUGIN_ENTRY_DIRECTORY_MISMATCH"` / `status_code=400` / 插件停在 failed。
    - 判定本体在 `plugin/core/entry_points.py:47-74`：取 entry 模块路径的**第 2 段**
      （`plugins.our_life` → `our_life`）与 `config_path.parent.name` 硬比对。
    - **归一化会把 entry 改写**（`entry_points.py:15-44`）：只能 entry 对应**在宿主仓内**的插件，
      canonical 写法 `plugin.plugins.our_life:OurLifePlugin` 原样保留；**用户安装态**会被重写成
      `plugins.our_life:OurLifePlugin`。所以两种形态下目录名都必须真叫 `our_life`。
    - `n.e.k.o_plugin_our_life`（合法 Git 仓名，不是合法 Python 包名）因此**永远挂不上**。
      **软链接 / junction 也救不回来**（`resolve()` 会把链接解开、目录名变回真实名）。
      唯一挂载方式就是**复制**：`tools/release_gate.py` 的 release / hosted-tsx 两门就是
      真复制进 `<宿主>/plugin/plugins/our_life/`（探针副本，用后即删）。
    - 另注（易混）：目录名与 `[plugin].id` 不一致**只是 warning**
      （`plugin/config/plugin_toml_semantics.py:64-76`，`PLUGIN_DIRECTORY_ID_MISMATCH`），
      与上面这条 entry 包名不匹配的**硬 400** 是两回事，别把两者当成同一条规则。
12. **Hosted TSX 检查要求被检路径在宿主仓内**（`frontend/plugin-manager/scripts/check-hosted-tsx.mjs`
    的 `assertPathInsideRepo` 用 `realpathSync` 比对），所以它**只能**对宿主仓内的副本跑，
    对仓外工作区路径会直接报 "Plugin search target outside repo root"。
    这也是 release_gate 必须复制副本的原因之一。
13. **`refresh_context=True` 只有 kit 的 `ActionButton`/`ActionForm` 会兑现**（真机踩坑，v0.4.4 修）：
    宿主 `ui-kit/runtime.js` 的两处 `if (action.refresh_context !== false) await api.refresh()`
    都长在**组件**里，普通 `Button` + `props.api.call(...)` 成功后**不会**重拉 context——
    面板会停在"后台已扣、前台不动"（如商店购买后的金币/背包）。
    本面板因此自带单飞通道 `refreshContext()`（`run()` 成功路径与轮询共用），
    Python 侧的 `refresh_context=True` 声明保留（对 ActionButton 形态的调用方仍有效）。
14. **kit 的禁用按钮画成“忙”**：`.neko-button:disabled { cursor: wait }`
    （`ui-kit/styles.css:123`）——鼠标悬停出现转圈的等待光标，读起来像“处理中”，
    而插件侧的禁用多半是“现在按不了”。平台 CSS 只读，面板侧解法是根部注入
    `<style>` 覆盖为 `not-allowed`（同特异度后置获胜，不用 `!important`）；
    kit 的 Button 没有内部 spinner，“转圈”只可能是这条光标。
15. **新写判据必须跑一组「全默认值」样本**（v0.8.0 踩到）：`core/state_note.voice_keys` 初版
    把“同距离时比数值”排在 `STAT_NAMES` 之前，函数签名怎么读都对——是手跑 `Stats()` 才看出来：
    **`Stats.affection` 的默认值就是 20.0**，坐在档 1（`acquainted`），于是每个新角色的第一句
    永远是“我还不太敢跟你撑娇”，把长期结果插到“精力累垮”前面。
    ⇒ **默认值就是大多数用户的初始态**：任何“看起来合理”的样本都测不出这类错，
    判据层的新写必须至少跑一组 `Stats()`（含句数、顺序、耦合三类输出）。
    同一理由还养出了 `_DAILY_AXES`：**好感不参与“偏低 → 多说一句”的升级判据**。
16. **入口路径碰后端掷骰的门必须钉种子**（v0.8.0 踩到，与 §3 的“必须钉窗”同一条纪律）：
    `_GAME_RNG` / `_CHECKIN_RNG` 是**模块级共享且未播种**的 `random.Random()`
    （`__init__.py:132,135`）。入口级测试不重播种子，拿到的序列就取决于“同一次 pytest
    进程里前面跑了几个走 `game_start` 的门”；而猜大小牌序是 `randint(1, 13)` **有放回**抽的
    （`core/games.py:138`），抽到同点是常态，同点按输——于是 `test_hielo_flow_bets_to_settlement`
    的“两战全胜”在**约 15% 的跑次里凭空红**（`release_gate` 的 pytest 门实测隔一次红一次）。
    ⇒ 修法：进门之前 `our_life._GAME_RNG.seed(...)`，**并断言抽出的序列不含同点**
    （钉了种子不等于安全：谁改了抽牌方式，要响亮地报出来，而不是留一句 `won is False` 让人猜）。

## Risk Follow-ups

- **惩罚式衰减的用户体验**：拟真衰减会让长期不互动明显掉档。默认 `enabled = false` 是唯一知情同意闸门；
  README 必须写清"关掉即冻结、不结算不衰减"，且面板要显示"当前是否在结算"。
- **私有依赖面**：本版**不依赖**任何宿主内部端点，只依赖 `plugin.sdk.plugin` 公共门面与 `bus` 只读快照；
  v0.5.0 新增的唯一例外是回环只读 `GET /api/tools`（在位性查询，官方 `docs/zh-CN/plugins/tool-calling.md`
  明文钦定的 resilience 模式——**文档承诺面**，随宿主文档版本演进）；补挂动作走 SDK 基类的
  `_notify_llm_tool_registered`（protected 面，`getattr` 守卫，缺席时降级为"心跳失效"）。
  若后续要用 `/api/emotion/analysis`，仍需按台账惯例单列"无版本承诺面"债务。
- **prompt 膨胀**：注入受 `max_chars`(320) + `min_interval_sec`(1200) + `max_per_hour`(3) 三重约束；
  注入文案不含数值表以外的长文本，且不做每轮注入。
- **跨环境双态验证**：常驻门必须区分独立仓态与宿主挂载态（CI 是 `cp -R` 进 `plugin/plugins/our_life`
  后按**裸 id** 跑 `check -r`），`tools/release_gate.py` 复刻该形态。
- **Windows 码面**：`release_gate.py` 的子进程管道与 stdout 必须钉 UTF-8（本机已两次踩 GBK 坑）。

## 路线图（后续轮次）

- **阶段性事件（已完成，v0.4.0 第八轮）**：病愈 / 哄好两个事件 + 面板「她经历过什么」。
  下一轮可以在此之上加**叙事深度**：把同一件事在不同处境下的说法区分开
  （比如"病愈时主人就在旁边"与"病愈时他刚回来"该是两种语气），或加"她记得自己病过多久"。
- **生日**：需要"角色的生日"这个新配置面与用户的真实输入，与下面的情绪感知一样属于
  **需要单独设计的一轮**——v0.4.0 刻意没把它塞进事件层（那会让"事件"混进两种时间语义：
  档位跨越 vs 日历日期）。
- 其余 6 语言（zh-TW / ja / ko / ru / es / pt）补齐，并加"键集一致门"
  （注意：**键集一致门已有**，见 `tests/test_i18n_contract.py`；缺的是那 6 份译文本身）。
- 情绪感知：以**独立插件联动**方式接入（用户明确要求后做），届时按跨插件契约单列设计。
  注意它与 v0.3.0 的反馈闭环**不是同一件事**：后者是**她自己**回传感受，
  前者是插件去读对话正文推情绪（会碰隐私边界，故单独立项）。
- 作息推断精细化（当前是固定睡眠窗 + 互动时段分布）
- ~~`@llm_tool` 重注册心跳~~（**已完成，v0.5.0**）：`services/tool_watch.py` 每 5 分钟回环
  `GET /api/tools` 核实在位性、缺席点名重发 IPC；"宿主起得比插件晚"的那次启动
  最迟 5 分钟内自愈，不再需要重载插件。
