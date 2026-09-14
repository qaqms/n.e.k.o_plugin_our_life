# 我们的生活 Design Brief

> 定位：第三方作者插件（独立 Git 仓库，不寄放宿主 `plugin/plugins/`）。
> 风格：**养成系 + 强注入**——数值随真实时间演化，并以"她自己的身体与心情感受"注入对话上下文。

## Identity Lock

- plugin_id: `our_life`
- folder: `D:\neko kaifa\n.e.k.o_plugin_our_life`
- name: `我们的生活`
- entry: `plugin.plugins.our_life:OurLifePlugin`
- main class: `OurLifePlugin`
- repository: `n.e.k.o_plugin_our_life`

## Purpose

为猫娘维护一组随时间**真实演化**的状态数值（好感度 / 心情 / 健康），把它们以她自己的身心感受
强注入对话上下文，使她的语气、主动性、话题选择随数值变化；主人通过日常互动（来找她、陪她、
连续相处的天数）喂养数值，长期冷落则按拟真惩罚衰减，掉到"生病 / 闹脾气"档时她会主动开口。
面板可视化当前状态、分档、连续天数与近期注入记录。

## Package Type and Capabilities

- package type: `plugin`（独立功能，**完全独立**：不调用任何其它插件、不联网、不改宿主平台层）
- capabilities:
  - callable entries：状态查询 / 手动纠偏 / 重置 / 总开关
  - LLM tools：她能自主查询身心状态、主动索取陪伴（带冷却）
  - lifecycle：startup 装配、shutdown 落盘
  - timer：周期采样行为（只读总线）+ 惰性衰减结算 + 注入判定
  - message injection：`push_message(visibility=[], ai_behavior="read"|"respond")`
  - UI：Hosted TSX panel（数值仪表盘 + 分档 + 连续天数 + 近期注入 + 开关/纠偏）
  - store：`PluginStore` 持久化（必须 `[plugin.store].enabled = true`，否则静默不落盘）
  - i18n：zh-CN / en（其余语言后续补齐，见"路线图"）
- inferred architecture:

```
timer tick（默认 30s，无 watchdog：每拍自成一拍，不持有跨拍对象）
  ├─ 行为采样 services/sampler.py
  │    └─ bus.conversations（只读快照；**不支持 watch()**）
  │         逐条读 turn 记录：turn_type=user/assistant + lanlan_name
  │         → 去重（conversation_id）→ 聚合出 user_turns / last_touch / streak / 时段分布
  ├─ 归属判定 services/shard.py
  │    └─ 优先本次调用注入的 _ctx["lanlan_name"]；后台无 _ctx 时用采样记录自带的 lanlan_name
  │       **绝不**用 ctx._current_lanlan（那是上次调用残留的脏值，见"已知陷阱"§1）
  ├─ 惰性衰减 core/model.apply_decay（读取时按 exp(-Δt/τ) 折算到当前时刻，只在写入时落盘）
  ├─ 成长/冷落结算 core/model.apply_impulse / apply_neglect（纯函数，无副作用）
  └─ 注入判定 services/injector.py（分档跨越 → 即时；否则受 min_interval / max_per_hour 频控）
       └─ core/injection.build_text（第一人称感受 + 行为倾向，{MASTER_NAME}/{LANLAN_NAME} 占位符）
```

## 数值模型（拟真衰减 / 惩罚式）

三项数值均 `0..100`，**按角色卡分片**（store key `ourlife@<lanlan>`）：

| 数值 | 周期 | 衰减 | 基线/地板 | 分档（下界 0/20/40/60/80） |
|---|---|---|---|---|
| `mood` 心情 | 小时级 | τ = 3h，向静息基线回落（homeostasis） | 基线 55 | 闹脾气 / 低落 / 平静 / 愉快 / 雀跃 |
| `health` 健康 | 天级 | τ = 36h，向静息基线回落 | 基线 70 | 生病 / 虚弱 / 一般 / 良好 / 精神饱满 |
| `affection` 好感 | 周级 | τ = 45d，只缓慢回落不自动回升 | 地板 0 | 陌生 / 熟悉 / 亲近 / 亲密 / 羁绊 |

- 衰减是**读数时惰性折算**（`exp(-Δt/τ)`），不依赖常驻循环，也不怕进程重启。
- **分档阈值固定在 `core/model.py` 的常量**（单一来源），不暴露成配置项——避免"配置漂移"类回归。
- 配置只露**行为旋钮**（衰减时间常数、成长步长、冷落惩罚、注入频控）。
- **分档有两套读法，不许混用**（`core/model.py` 的模块 docstring 是权威说明）：
  - `tier_of` / `tier_transitions` = 硬比较，**面板与"这数值算哪一档"用它**，忠实反映每个数值；
  - `crosses_tier_boundary` / `eventful_tier_transitions` = **带 0.05 分迟滞**，
    只给**注入判定**用。否则分界线上的亚分噪声会被当成跨档事件：默认好评 20.0 正好压着
    stranger/acquainted 的线，第一拍 30 秒心跳就把它折成 19.9998，于是凭空发一条
    `tier_change` 强注入（真机 store 里抓到过这条，见 CHANGELOG 对应轮次）。

## First Version Scope

1. 三项数值 + 拟真衰减 + 成长/冷落结算（纯函数，可单测）
2. 行为统计：用户发言次数、最近互动时间、连续互动天数、时段分布（全部本地，零外部依赖）
3. 事件驱动 + 频控的强注入（分档跨越即时注入；低档跨越升级为让她主动开口）
4. Hosted TSX 面板：三项数值 + 分档 + 连续天数 + 近期注入记录 + 开关与手动纠偏
5. LLM 工具两个：她查询自身状态、她主动索取陪伴（带冷却）
6. fail-closed 总开关 `[our_life].enabled = false`（默认关：未打开前不注入、不结算、不推送）
7. 中英 i18n、`tests/` 数值门 + i18n 契约门、`tools/release_gate.py` 五门

## Out of Scope

- ❌ 调用任何其它插件（用户明确要求完全独立；情绪感知后续以独立插件联动方式另开）
- ❌ 联网、读宿主 `core_config.json`、直连模型端点（不做任何数据出域）
- ❌ 复用宿主 `/api/emotion/analysis` 等内部端点（无版本承诺面，留待后续轮次评估）
- ❌ 修改宿主平台层任何代码
- ❌ 趋势图表（Hosted UI Kit 无图表组件；走势只能用 `StatCard` / `Progress` / 自绘横条）
- ❌ 睡眠/作息推断的精细化（首版只用互动时段分布做粗略加成，不做作息结论）

## Inferred Technical Needs

- plugin.toml sections: `[plugin]`、`[plugin.author]`、`[plugin.sdk]`、`[plugin.i18n]`、`[plugin.store]`、`[plugin.ui]` + `[[plugin.ui.panel]]`、`[plugin_runtime]`、自定义 `[our_life]` 族
- SDK surfaces: `NekoPluginBase`、`@neko_plugin`、`@plugin_entry`、`@lifecycle`、`@timer_interval`、`@ui.context`、`@ui.action`、`@llm_tool`、`push_message`、`Ok/Err/SdkError`、`self.store`、`self.config`、`self.bus.conversations`
- UI surfaces: hosted-tsx panel `ui/panel.tsx`，权限 `state:read` + `config:read` + `action:call`
- state/config: `self.store` 存数值与时间戳；`self.config` 读 `[our_life]`；开关写入走 entry → `self.config.set`
- lifecycle/background work: startup 装配采样状态；tick 内完成采样+结算+注入，**不持有跨拍对象**（timer 每拍新 event loop）
- external integrations: 无（仅宿主本地 bus 只读）

## Read Context Plan

- 已读：`.agent/rules/neko-guide.md`、`.agent/skills/neko-plugin/**`（含 execution-boundary / core-plugin-contract /
  plugin-creation-workflow / checks-and-tests / cli-and-debugging / surface-map）、`plugin/PLUGIN_DEVELOPMENT_GUIDE.md`、
  `docs/zh-CN/plugins/**`、`plugin/config/schema.py`、`plugin/neko_plugin_cli/**`（init/build/validate 源码）、
  `plugin/sdk/plugin/**`、`plugin/sdk/shared/core/bus_context.py`、`plugin/core/host.py`、`plugin/sdk/hosted-ui/index.d.ts`、
  宿主 `main_routers/system_router/emotion.py` + `main_routers/proactive_router.py`（确认本地端点边界）、
  三份台账（`未完成的问题/*.md`）与三个既有插件仓库（catgirl_seiyuu / forever_companion / your_memory）
- 参考落盘：`../neko-plugin-sdk-api-reference.md`、`../hosted-ui-reference.zh-CN.md`

## Write Workspace

`D:\neko kaifa\n.e.k.o_plugin_our_life\`（独立 Git 仓库；不触碰 N.E.K.O 源码树）

## 已知陷阱（实现时必须绕开，均源码核实）

1. **`ctx._current_lanlan` 是脏值**：`plugin/plugins/jukebox_controller` 明确写了 "Deliberately no
   `ctx._current_lanlan` fallback"——该属性由"某次调用"的 `_ctx["lanlan_name"]` 写入，本次调用不带时
   里面留着**上一次**的角色名。归属只能取本次注入的 `_ctx`，或总线记录自带的 `lanlan_name`。
2. **`bus.conversations` 不支持 `watch()`**：只有 `messages` / `events` / `lifecycle` 可 watch
   （`migration-v0.9`）。行为采样只能定时轮询只读快照。
3. **timer 每拍 `asyncio.run(fn())`（新 event loop）且无 watchdog**：tick 内不得使用在其它拍/`startup`
   里创建的 loop 绑定对象；异常只记日志不停表，需自己兜。
4. **`[plugin.store].enabled = false` 时 store 静默失效**：`set` 返回 `Ok` 却不落盘、`get` 返回默认值。
5. **`push_message` 的 `submitted=True` 不等于宿主已消费**：启动钩子期推送会落在订阅窗口之前被静默丢弃。
   注入只在 tick / 入口 / 工具里发，不在 `startup` 里发。
6. **面向用户的文本必须用 `{MASTER_NAME}` / `{LANLAN_NAME}` 占位符**，不得硬编码"主人/用户"。
7. **面板可达入口的 `Err(SdkError(...))` 与 `note` 只用稳定 ASCII 码**（`^[a-z][a-z0-9_]*$`），
   动态细节进日志；前端按 `panel.errors.<camelCase(码)>` 翻译。
8. **`data/` 与 `cache/` 才是可写位置**：`self.plugin_dir`（= `config_dir` 别名）是只读安装目录。
9. **隐私**：注入正文含用户互动信息，只进总线不进日志正文；涉及原文一律不上 `logger`。
10. **`@llm_tool` 名称**必须匹配 `^[A-Za-z0-9_.\-]{1,64}$`，且工具注册表在 `main_server` 内存里，
    宿主重启即丢、无自动重注册——首版不额外做重注册心跳（记为待办），并在 README 说明。

## Risk Follow-ups

- **惩罚式衰减的用户体验**：拟真衰减会让长期不互动明显掉档。默认 `enabled = false` 是唯一知情同意闸门；
  README 必须写清"关掉即冻结、不结算不衰减"，且面板要显示"当前是否在结算"。
- **私有依赖面**：本版**不依赖**任何宿主内部端点，只依赖 `plugin.sdk.plugin` 公共门面与 `bus` 只读快照；
  若后续要用 `/api/emotion/analysis`，需按台账惯例单列"无版本承诺面"债务。
- **prompt 膨胀**：注入受 `max_chars`(320) + `min_interval_sec`(1200) + `max_per_hour`(3) 三重约束；
  注入文案不含数值表以外的长文本，且不做每轮注入。
- **跨环境双态验证**：常驻门必须区分独立仓态与宿主挂载态（CI 是 `cp -R` 进 `plugin/plugins/our_life`
  后按**裸 id** 跑 `check -r`），`tools/release_gate.py` 复刻该形态。
- **Windows 码面**：`release_gate.py` 的子进程管道与 stdout 必须钉 UTF-8（本机已两次踩 GBK 坑）。

## 路线图（后续轮次）

- 其余 6 语言（zh-TW / ja / ko / ru / es / pt）补齐，并加"键集一致门"
- 情绪感知：以**独立插件联动**方式接入（用户明确要求后做），届时按跨插件契约单列设计
- 睡眠/作息推断精细化、病愈/哄好等阶段性事件
