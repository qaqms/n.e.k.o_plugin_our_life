# Changelog

本插件采用"轮次叙事"记录：每一轮写清**病因 / 做法 / 验证 / 测试数**，而不是只列增删。
历史条目只追加、不改写。

## [0.1.0] - 2026-09-14（第四轮 · 接手复核：三个真问题）

接手时的基线是"五门全绿、132 passed"。复核后确认三处真问题——**其中两处让"全绿"这件事本身
不可信**（门比被测物先死），第三处是真机 store 里躺着的实际错误行为。

### 1. 发版链在"打印结果"这一步崩掉（报告先死，门是好的）

**病因**：Windows 的 `sys.stdout.encoding` 取的是**活动代码页**（本机 GBK），
**输出被重定向时也一样**（后台任务 / 管道 / CI 捕获都会走进去）。于是
`print("\n链路中断 ❌")` 抛 `UnicodeEncodeError: 'gbk' codec can't encode character '\u274c'`，
整条链在打印报告时崩掉。真机实测的现场就是这样：`pytest` 门 `[OK]`、`check` 门 `[OK]`，
然后在 `print` 上崩——**看起来像插件有问题，其实是报告自己死了**。
README 的"坑位存档"第 4 条明明写着"子进程管道与 stdout 必须钉 UTF-8"，但当时只钉了管道那一侧。

**修法**：模块 import 时按"是否 tty"分两条路兜住 stdout——重定向（管道 / 捕获）重配
UTF-8 + `errors=replace`；真控制台保持编码不动、只降到 `errors=replace`（否则中文字节在 cp936
控制台会变乱码）。子进程另用 `PYTHONIOENCODING=utf-8` 钉死，下游 gate 的日志不再变成
`����ʱ 0.7s` 这种读不出耗时的乱码。

### 2. ruff 门把整条链拖死在一个网络依赖上

**病因**：门里固定用 `uvx ruff==0.12.4`。`uvx` 每次都会去 PyPI 解析一次——断网 / 代理不通时
它重试三次然后失败，于是 **ruff 门一红，后面三门（check / release / hosted-tsx）根本没机会跑**，
报告却指向"插件有问题"。

**修法**：工具**钉死版本 + 离线优先**。先在 uv 缓存里按同一个钉住版本跑（`--offline`，断网也照跑、
版本与 CI 逐字一致），缓存没命中才联网解析一次。两条路都不通就明确报错并打印预热命令——
**绝不**顺手用 PATH 上那个版本未知的 `ruff` 顶上来（那等于在没人知道的情况下换了门）；
真要用它必须先核对版本一致。

**中途自己踩到并修掉的**：第一版写成"离线跑一次，失败就当缓存未命中、再联网跑一次"——
可 ruff 因**代码里有 lint 错误**而退出码 1 也是"失败"，于是正常的 lint 失败被误报成缓存未命中，
多跑一次联网还打出一句误导人的"缓存未命中"。报告里的假信号比门本身红更坏（会把人带去查网络）。
改成先用 `--version` 探缓存是否命中、只在诊断输出确实指向"离线取不到工具"时才回退联网。
留了常驻门 `test_offline_probe_hit_does_not_misreport_a_lint_failure`（用真 lint 违规逼门红，
断言报告里不出现"缓存未命中"）。

### 3. 分界线上的亚分噪声白送一次强注入（真机 store 里抓到的）

**病因**：分档线是 `value >= lower` 的硬比较，而**默认好评 20.0 正好压在**
stranger/acquainted 的分界线上。只要落一个 30 秒心跳，`apply_decay` 就把它折成
`19.9998456796`，硬比较立刻判"跨档"，于是发一条 `tier_change` 强注入——
面板上数值还是 20.0、档名没变，用户看不到任何变化，注入文案却写着"关系档位刚刚变了"。

真机证据（部署态 `store.db` 的 `ourlife@MS` 分片，修复前）：
`inject_history` 里唯一一条记录就是 `trigger=tier_change`、
`stats.affection=19.999845577439277`；`stats.affection` 已是 `19.9978…`。

**修法**：新增 `crosses_tier_boundary` / `eventful_tier_transitions`（`core/model.py`），
判据带 **0.05 分迟滞**：两点都紧贴它们之间那条分界线（`|v - 边界| < margin`）时，
这次跨越只算噪声；否则算真事件。0.05 分用户读不出来（面板只显示一位小数），
心情走完它约需 24 秒、好感约需数月——语义无损。
**硬比较的 `tier_of` / `tier_transitions` 原样保留**（面板与"数值算哪一档"必须忠实反映），
只有 `__init__.py` 的 tick 注入判定换成带迟滞的版本。

**验证**：新增 17 条门（模型层 7 条 + 集成层 2 条 + 发版链卫生 8 条），并且**逐条做了反向对照**：
撤掉 stdout 兜底 / 去掉 `--offline` / 把 lint 失败误报成缓存未命中 / tick 改回硬比较 /
迟滞余量清零 —— 对应的门全部变红，还原后全绿。`132 → 148 passed`。

### 复核结论（本轮**没有**改的东西，都是有意的）

- 真机部署态未被触碰（`%LOCALAPPDATA%\N.E.K.O\plugins\our_life\` 的 config/data 原样保留）。
- `README` 里"其余 6 语言 i18n / 情绪感知 / 工具重注册心跳 / 趋势图表"四项仍是已知限制，
  按 README「已知限制」与「路线图」处理，本轮不动。
- 真机 store 里那条已经写歪的 `tier_change` 历史记录**不清理**：它只是历史展示，
  下一次注入会自然把 `inject_history` 往前推；擅改用户数据比留着更危险。

## [0.1.0] - 2026-09-14（第三轮 · 真机测试前加固）

### 冷启动空转风险（真机才会暴露的那类）

**病因**：后台心跳是定时器，**没有 `_ctx`**，它决定"结算哪个角色卡"只能靠
"已知分片 ∪ 总线记录里的角色名"。冷装机上两者都可能为空（若宿主的对话总线记录不带
`lanlan_name`），插件会安安静静地空转——面板看着正常，数值永远不动。

**修法**（两条，都不改注入语义）：

1. **碰过就 bootstrap**：面板与入口解析出角色卡后，落一个初始分片（每个角色卡每进程只落一次，
   避免面板刷新写 store）。"用户打开过面板"本身就是"这个角色卡在用"的信号。
2. **空转诊断**：认不出角色且有记录时，打一条 warning，并打印**记录键名**（只打键名、
   不打任何值，避免对话正文进日志）——真机上定位问题时一眼能看到宿主的真实记录形状。

**验证**：新增两条门（bootstrap 落盘、每进程只落一次且不覆盖已有值）；`130 → 132 passed`，
五门全绿。

### 发布产物卫生：`__pycache__` 不入包（上游问题的调用侧绕法）

**病因**：上游打包器的元数据探测会**以子进程 import 插件本体**，而 import 的目标是"已经过滤完的
暂存树"，CPython 于是把 `__pycache__/*.pyc` 写进 payload 并归档。本插件首版产物因此多出
12 个 `.pyc`（36 条目 → 128 KB）。同一现象已在
`dist/upstream-issue-packager-pycache.md` 里被记录（forever_companion v1.2.1 多 21 个、体积 +73%），
属于低severity的上游缺陷。

**修法**：探测子进程**继承环境变量**，所以在调用侧设 `PYTHONDONTWRITEBYTECODE=1` 即可——
`tools/release_gate.py` 的所有子进程统一带上它（这也是那份报告给出的首选修法）。
产物变为 **24 条目 / 0 个 .pyc / 128 KB**。

⚠️ **不能事后删包里的 `.pyc`**：那会让 `metadata.toml` 的 `payload.hash` 与实际内容不符，
宿主导入时的完整性校验会不过。

## [0.1.0] - 2026-09-14（第二轮 · 五门打通 + 双态修正）

### 打通了五门

`pytest` / `ruff` / `check` / `release` / `hosted-tsx` 全绿。其中 **release 门是在真挂载态通过的**：
插件被真的复制进宿主 `<host>/plugin/plugins/our_life/`，再按**裸 id** 跑 `check -r`
（校验 → 插件本地测试 → 打包 → 包内 payload hash 复核），随后无条件清理宿主仓副本。

### 两个真实缺陷（都是"独立仓全绿、挂载态/CI 全红"这一类）

1. **挂载态下测不了**：能导入真 SDK 时，真 `NekoPluginBase` 会自己建一套真通道
   （真 `PluginStore` / `PluginConfig`），`bus` 更是**只读 property**，测试既接管不了、
   一碰就是 `TransportError: ctx.get_own_config is not available`。
   根因有两层：①`make_plugin` 夹具绕过了通道接管逻辑；②即使接管，`bus` 也无法赋值。
   修法：测试进程里**无条件**把 `plugin.sdk.plugin` 换成桩门面，让两种形态行为一致
   （挂载态真正要验的裸 id 目录 / entry 解析 / 清单校验 / 打包，由 `check -r` 自己覆盖）。
2. **hosted-tsx 检查器是文本级规则，连注释都不跳过**：面板里一句解释性注释写了
   "`api.call()` 返回的是信封…"，就被判成"用了全局对象"直接拒收。
   同时确认：从 props 解构出与全局对象同名的短名字也会被拒（catgirl_seiyuu 台账同款坑）。
   修法：注释与代码都避开该标识符的裸形态，一律写完整的成员访问形式。

### 反向对照（确认门不是摆设）

- 删掉一个 `panel.errors.*` 键 → i18n 契约门 **2 failed**；
- 只改 `plugin.toml` 的 `mood_tau_hours`（3.0 → 4.0）→ 文档↔配置同源门 **2 failed**；
- 把 `[plugin.store].enabled` 改成 `false` → 静默失效门 **1 failed**。
还原后 `130 passed`。

### 已修掉的（本轮）

- pytest 的 `<Package <目录名>>` 节点会以模块名 `"__init__"` 导入仓根 `__init__.py`，
  触发无父包相对导入崩溃 → conftest 预注册 `sys.modules["__init__"]` + 目录强制按 `Dir` 收集。
- CI 的 ruff 门带 `--ignore-noqa`：裸 `import plugin.sdk.plugin  # noqa: F401` 会被判成未使用导入，
  且 ruff 的修复动作是**删掉整行**（桩探测就此失效）→ 改用 `importlib.import_module` 探测。
- 两处 `import pytest` 与自家包之间的空行不符合 isort 对第三方包的归组 → 已对齐。

## [0.1.0] - 2026-09-14（第一轮 · 首版）

### 落地的东西

- **数值模型**（`core/model.py`，纯函数零 SDK 依赖）：好感度 / 心情 / 健康三项，惰性衰减
  （`exp(-Δt/τ)` 读数时折算）、心情与健康向静息基线回落、好感只缓慢回落不自动回升、
  分档阈值固定为单一来源（不进配置）。
- **行为统计**（`core/behavior.py`）：只读宿主 `bus.conversations` 快照，按 `lanlan_name`
  归属 + `conversation_id` 去重，聚合出用户发言数 / 最近互动 / 连续相处天数 / 时段分布。
- **强注入**（`core/injection.py` + `services/injector.py`）：事件驱动（危机 / 跨档 / 跨天首触 /
  显著漂移）+ 三重频控（间隔、每小时上限、字符预算）；注入正文不给模型看原始数字与档名。
- **入口与面板**：`status` / `tune` / `reset` / `switch` 四个入口 + Hosted TSX 仪表盘；
  fail-closed 总开关默认关闭。
- **她可自主调用的 LLM 工具**：`our_life_feel`（查状态）、`our_life_company`（索取陪伴，带冷却）。
- **中英双语 i18n** + 常驻契约门（码形 / 码↔键双向同步 / 键集一致 / 引用面 / tier 与 trigger 结构门）。
- **文档 ↔ 配置同源门**：`plugin.toml`、`config.example.toml` 与 `core/configuration.py` 的默认值
  必须逐键相等（防"默认值漂移"这类历史债）。
- **五门发版校验链** `tools/release_gate.py`：pytest / ruff / check / release / hosted-tsx，
  其中 release 门真的挂载到宿主 `plugin/plugins/our_life/` 后按裸 id 跑 `check -r`。

### 开发期抓到的真实缺陷（都留下了回归门）

1. **`"{MASTER_NAME} ...".format()` 会 KeyError**：占位符被当成 format 字段，冷落分支一走到就炸。
   由 ruff 的 `F524` 抓到，`test_injection.py` 留了 `test_gap_branches_are_all_reachable`。
2. **唯一分片兜底只在内存缓存里找**：面板早于第一次 tick 打开时会误报 `invalid_lanlan`。
   改为缓存空时回查一次持久化分片（`_single_known_lanlan`）。
3. **总开关关着时「索取陪伴」回 `company_cooldown`**：语义撒谎（不是冷却，是没开）。
   改为回 `not_enabled`。
4. **禁用期的冻结语义自相矛盾**：只把折算基准点推在内存里，进程重启后会一次性补算整段衰减，
   等于惩罚"关掉插件"的用户。改为启动时归一化一次并落盘；禁用期的每拍 tick 不写盘（避免写放大）。

### 测试基建（踩坑记录）

- pytest 9 的 importlib 模式会以模块名 `"__init__"` 导入仓根 `__init__.py`，
  触发"无父包的相对导入"崩溃；`tests/conftest.py` 预注册 `sys.modules["__init__"]`
  并强制目录按 `Dir` 收集。
- 挂载态（市场 CI 把仓库挂进宿主包树）下外层父包会被一起导入，轻量 venv 里没有那些依赖；
  预注册父包链的轻量桩挡住。
- 新增**隐私门**：会话记录里的 `content`（对话正文）在整个持久化状态里必须找不到。

**验证**：`129 → 130 passed`；`ruff`（CI 原样参数）全绿；五门全链通过。

### 已知但有意缓做

- 其余 6 语言 i18n 与注入模板多语言。
- 情绪感知（读对话判心情）——按需求留待后续以独立插件联动方式接入。
- LLM 工具在宿主重启后的自动重注册（宿主不自动重注册；本版靠重载插件恢复）。
