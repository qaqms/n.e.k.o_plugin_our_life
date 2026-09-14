# 我们的生活

养成系陪伴插件：为猫娘维护**好感度 / 心情 / 健康**三项随真实时间演化的状态，并以她自己的
身心感受**强注入**对话上下文，让语气、长短与主动性随状态变化。

| | |
|---|---|
| plugin_id | `our_life` |
| 包类型 | `plugin`（完全独立：不调用其它插件、不联网、不改宿主） |
| 存储 | `PluginStore`，**按角色卡分片**（键 `ourlife@<角色名>`） |
| UI | Hosted TSX 面板 `ui/panel.tsx` |
| 默认状态 | **fail-closed：安装后总开关为关** |

## 它做什么

三件事：**数值会随时间真实变化**、**她的状态会进入对话**、**你可以从面板看清发生了什么**。

- 数值只在你有互动时成长；长期不来会按真实时间衰减（拟真、惩罚式，但有上限，不做不可逆清零）。
- 状态跨越档位、掉进"生病 / 闹脾气"、或隔了一天你再回来时，插件会把当前状态注入她的上下文；
  掉到危机档时她会主动开口。
- 面板展示三项数值、档位、连续相处天数、互动时段分布、近期注入记录与当前配置。

## 数值机制

| 数值 | 周期 | 衰减 | 静息/地板 | 分档（下界 0/20/40/60/80） |
|---|---|---|---|---|
| `mood` 心情 | 小时级 | τ = 3h，向静息基线回落 | 基线 55 | 闹脾气 / 低落 / 平静 / 愉快 / 雀跃 |
| `health` 健康 | 天级 | τ = 36h，向静息基线回落 | 基线 70 | 生病 / 虚弱 / 一般 / 良好 / 精神饱满 |
| `affection` 好感 | 周级 | τ = 45d，只缓慢回落、不自动回升 | 地板 0 | 陌生 / 熟悉 / 亲近 / 亲密 / 羁绊 |

- **惰性衰减**：不跑常驻循环，读取时按 `exp(-Δt/τ)` 把存值折算到当前时刻，只在写入时落盘。
  进程重启、宿主休眠、电脑关机都不会算错。
- **心情与健康是 homeostasis**：高于基线回落、低于基线回升——不会被一次性扣死。
- **成长**：每次你发言给心情 +2.5 / 健康 +0.8 / 好感 +0.35，同一会话内递减（防连发刷数值）；
  跨天第一次来找她额外给一次"问候礼"；连续相处 3 / 7 / 14 / 30 天各有一次性奖励。
- **冷落**：超过 `grace_hours`（默认 24h）没互动后按天扣，三项各自有上限（心情最多 −25、
  健康 −20、好感 −12）。
- 分档阈值固定在 `core/model.py` 的 `TIER_BOUNDS`，**不作为配置项暴露**（单一来源，避免配置漂移）。

## 强注入

触发优先级（事件驱动）：

1. **危机**：心情/健康掉进危机档 → 立即可注入，并（默认）升级为让她**主动开口**
2. **跨档**：任一轴档位变化 → 立即可注入
3. **跨天首触**：今天第一次来找她 → 注入一次
4. **显著漂移**：有新互动、距上次注入已过间隔、且任一轴相对**上次注入时**变化 ≥ 5 分

三重频控（`[our_life.inject]`）：`min_interval_sec`（默认 1200s）、`max_per_hour`（默认 3）、
`max_chars`（默认 320）。跨档与危机会绕过间隔限制，但一样受每小时上限约束。

注入契约：

- `ai_behavior="read"` = 进上下文但不打断；`"respond"` = 让她主动开口。`visibility=[]`，
  用户不会看到这段状态说明。
- 正文里**不给模型看原始数字，也不提档位名称**——只有第一人称感受与行为倾向，
  并明确要求她不要复述。给数字会被念出来，给标签会被当台词。
- 正文带 `{MASTER_NAME}` / `{LANLAN_NAME}` 占位符，由宿主按会话展开（插件无从得知该用哪个称呼）。
- 注入模板语言目前固定中文（面向模型的指令性文本）；面板等**用户可见**文案走 `i18n/`。

她还持有两个可自主调用的 LLM 工具：`our_life_feel`（查自己的身体状态）、
`our_life_company`（想让你陪她，带 2 小时冷却）。

## 按角色卡分片

每个角色卡一套独立数值，键为 `ourlife@<角色名>`。归属判定只认**本次调用注入的
`_ctx["lanlan_name"]`**；后台采样时用总线记录自带的 `lanlan_name`。

> 刻意**不使用** `ctx._current_lanlan` 兜底：那是"上一次调用"留下的脏值，用它会导致
> A 角色的互动记到 B 角色头上（宿主的 `jukebox_controller` 源码里对此有明确警示）。

## 启用与操作

1. 在插件中心导入并启动插件（`auto_start = true`，随宿主启动）。
2. 打开面板 → **总开关**。默认关闭时数值完全冻结：不注入、不结算、不推送，数值原样保留。
3. 打开后开始"过日子"。面板可以查看三项数值、相处节律、近期注入，也能手动纠偏或重置。

## 配置

配置分两层：`plugin.toml` 是默认值，用户在
`<用户数据根>/plugins/our_life/config/plugin.toml` 的覆盖层优先。`config.example.toml` 是同一份形状的镜像。

| 键 | 默认 | 含义 |
|---|---|---|
| `[our_life].enabled` | `false` | fail-closed 总开关；关 = 冻结 |
| `[our_life].tick_seconds` | `30` | 后台心跳（采样 + 结算 + 注入判定） |
| `decay.mood_tau_hours` / `mood_rest_baseline` | `3.0` / `55.0` | 心情的时间常数与静息基线 |
| `decay.health_tau_hours` / `health_rest_baseline` | `36.0` / `70.0` | 健康同上 |
| `decay.affection_tau_days` | `45.0` | 好感衰减周期（好感不会自动回升） |
| `growth.*` | 见 `plugin.toml` | 每次发言/跨天/里程碑的成长步长与会话递减 |
| `neglect.*` | 见 `plugin.toml` | 冷落宽限、每日扣分与各项上限 |
| `inject.min_interval_sec` / `max_per_hour` / `max_chars` | `1200` / `3` / `320` | 注入频控与字符预算 |
| `inject.respond_on_crisis` | `true` | 危机档是否让她主动开口 |
| `inject.crisis_mood_tier` / `crisis_health_tier` | `sulking` / `sick` | 危机档位判据（"不高于"该档即算危机） |
| `inject.company_cooldown_sec` | `7200` | 「索取陪伴」冷却 |

配置被手改坏（类型漂移、负值、字符串数字）时一律回退默认值，不会让 tick 崩掉——这条有常驻测试门。

## 隐私与安全

- **不联网**：插件运行时不发起任何外部请求，不读宿主 `core_config.json`，不做数据出域。
- **不调用其它插件**：完全独立。
- 行为数据只来自宿主只读总线 `bus.conversations`，且只消费 **4 个非正文字段**
  （`conversation_id` / `timestamp` / `lanlan_name` / `turn_type`）。记录里虽然有 `content`
  （对话正文），本插件**不读取、不落盘、不记录**——这条有专门的隐私门测试守着。
- 需要说明的宿主属性：该总线在宿主侧**不按插件隔离**，任何已启用插件都能读到同样的记录。
  本插件的处理方式是"只取非正文字段 + 不落盘"。
- 注入正文含互动节律信息（多久没来、连续多少天），只进总线，不写日志正文。

## 已知限制

- 面板文案有中英两语；其余 6 语言（zh-TW / ja / ko / ru / es / pt）待补，注入模板也暂只有中文。
- 情绪感知（读对话判断心情）**未做**——按需求留待后续以独立插件联动方式接入。
- 「索取陪伴」等 LLM 工具的注册表在宿主 `main_server` 内存里，宿主重启即丢且宿主不自动重注册；
  本版不做重注册心跳，重载插件即可恢复。
- 面板没有趋势图表（Hosted UI Kit 无图表组件），历史只能看档位摘要与时段分布。

## 坑位存档（开发期实际踩到的，供后续维护者省时间）

1. **pytest 会把仓根 `__init__.py` 当模块导入**：它以模块名 `"__init__"` 导入
   `ROOT/__init__.py`，那里有顶层相对导入 → `attempted relative import with no known parent
   package`。解法是 `tests/conftest.py` 预注册 `sys.modules["__init__"]` + 强制所有目录按
   `Dir` 收集（见该文件 docstring）。
2. **`"{MASTER_NAME} ...".format(...)` 会 KeyError**：占位符被当成 format 字段。
   ruff 的 F524 抓到了它，`tests/test_injection.py` 留了回归用例。
3. **ruff 门必须钉 CI 原样参数**：`--ignore-noqa` 意味着本地用 `# noqa` 压掉的问题在市场 CI
   照样算失败（本仓已两次踩坑，故 `release_gate.py` 直接复用 CI 参数）。
4. **子进程管道必须钉 UTF-8**：Windows 默认 GBK 码面，`text=True` 不指定编码会崩 reader 线程
   并丢掉整门日志。
5. **`[plugin.store].enabled = false` 会静默失效**：`set` 返回 `Ok` 却不落盘、`get` 返回默认值。
   有常驻门盯着它必须为 `true`。
6. **`check-hosted-tsx` 要求被检文件在宿主仓内**：所以 hosted-tsx 门走"点前缀探针副本"，
   跑完无条件清理；`.vscode` 必须一起复制（`check -r` 要求仓库支撑文件在位）。
7. **`Grid` 的列数是 `cols`、`Select/NumberInput` 没有 `label`、`StatusBadge` 用 `label`、
   `Tone` 里没有 `neutral`**：照 `plugin/sdk/hosted-ui/index.d.ts` 的精确签名写面板，
   错误 props 会被运行时静默丢弃、布局默默坏。
8. **hosted-tsx 检查器是文本级规则，不跳过注释**：面板注释里写一句 "`api.call()` 返回的是
   信封…" 就会被判成"用了全局 api 对象"而拒收；从 props 解构出与全局同名的短名字同理。
   代码与注释都避开那个标识符的裸形态，一律写完整成员访问。
9. **挂载态（市场 CI）下测试不能依赖真 SDK 的通道**：真 `NekoPluginBase` 会自建真通道，
   而 `bus` 是只读 property；`tests/conftest.py` 因此**无条件**桩掉 `plugin.sdk.plugin`，
   让独立仓态与挂载态行为一致——否则"本地全绿、CI 全红"。
   这正是 `release` 门存在的意义（它真的 `cp` 进宿主仓按裸 id 跑 `check -r`）。

## 发版校验（五门）

```bash
# 一条命令跑完：pytest / ruff / check / release / hosted-tsx
uv run python tools/release_gate.py

# 子集 / 保留宿主仓副本便于反复迭代 / 指定宿主仓
uv run python tools/release_gate.py --only pytest,ruff --keep
uv run python tools/release_gate.py --host-root "D:/other/N.E.K.O"
```

`release` 门会**真的**把插件挂载到 `<宿主>/plugin/plugins/our_life/` 再按**裸 id** 跑
`check -r`，复刻市场 verify workflow 的形态（独立仓本地全绿 ≠ CI 绿）。

## Development

This directory is both the editable plugin source and its Git repository.

当前目录既是可编辑的插件源码，也是插件自己的 Git 仓库。

このディレクトリは、編集するプラグインソースであり、プラグイン自身の Git リポジトリでもあります。

When publishing to the plugin market, use this GitHub repository name:

发布到插件市场时，请使用以下 GitHub 仓库名：

プラグインマーケットへ公開する際は、次の GitHub リポジトリ名を使用してください：

```text
n.e.k.o_plugin_our_life
```

From this plugin repository root / 在当前插件仓库根目录中 / このプラグインリポジトリのルートで：

```bash
uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml .
uv run python -m pytest tests -q
uv run --with pip --project "../N.E.K.O" neko-plugin sync . --clean
uv run --project "../N.E.K.O" neko-plugin check .
uv run --project "../N.E.K.O" neko-plugin check -r .
```

Python runtime dependencies are declared in `pyproject.toml` and synced into
`vendor/` for packaging. The generated `vendor/` directory is not committed;
local builds and CI recreate it before release checks.

Python 运行时依赖声明在 `pyproject.toml` 中，并在打包时同步到 `vendor/`。
生成的 `vendor/` 不提交；本地构建和 CI 会在发布检查前重新生成它。

Python ランタイム依存関係は `pyproject.toml` に宣言し、パッケージ化時に
`vendor/` へ同期します。生成された `vendor/` はコミットせず、ローカルビルドと
CI が公開前チェックで再生成します。

> 本插件运行时**零第三方依赖**（`dependencies = []`）。

## Market release / Market 发布 / Market 公開

Publish the version declared in `plugin.toml`. By default this pushes the Git
tag, waits for the standard GitHub Release, and notifies the plugin market.

发布 `plugin.toml` 中声明的版本。默认会推送 Git tag、等待标准 GitHub
Release，然后通知插件市场。

`plugin.toml` で宣言されたバージョンを公開します。既定では Git tag を
push し、標準 GitHub Release を待ってからプラグインマーケットへ通知します。

```bash
uv run --project "../N.E.K.O" neko-plugin publish .
```

To run only one half explicitly / 如需仅执行一部分 / 一方のみを実行する場合:

```bash
uv run --project "../N.E.K.O" neko-plugin publish github .
uv run --project "../N.E.K.O" neko-plugin publish market https://github.com/owner/repo/releases/tag/v0.1.0
```

The generated `.github/workflows/release.yml` builds and uploads
`our_life.neko-plugin`. The market independently verifies that Release
before publishing it.

生成的 `.github/workflows/release.yml` 会构建并上传插件包；Market 会独立验证
该 Release 后再发布。

生成された `.github/workflows/release.yml` がプラグインパッケージをビルドして
アップロードし、Market はその Release を独立検証してから公開します。

`v*` tag 由宿主仓 reusable workflow 驱动 release/market，**不要手动** `neko-plugin publish`。

## Entry

```toml
entry = "plugin.plugins.our_life:OurLifePlugin"
```

## 路线图

- 其余 6 语言 i18n 与"键集一致门"扩展
- 情绪感知：以独立插件联动方式接入（需先确认后做）
- 睡眠/作息推断精细化（首版只用互动时段分布做粗略画像）
- 病愈 / 哄好等阶段性事件与面板叙事
