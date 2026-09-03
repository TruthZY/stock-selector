# 股票推送系统设计方案

> 状态：设计稿（待评审）
> 目标：在现有「实盘系统 + 回测系统」之上，新增一个**独立**的股票推送系统，部署到阿里云 Ubuntu 服务器持续运行，只把命中买入信号的股票推送到钉钉群机器人。当前只实现**日 K**级别，30 分钟 K 及其他周期预留。

---

## 0. 核心约束（来自需求方）

1. **独立架构**：推送系统作为一个新的独立模块（`push/` 包 + 独立进程），**只通过 `import` 复用现有代码**（买入战法 `BUY_REGISTRY`、公共函数 `BarContext`/`bars`/`DataSource`、股票池 `config`），**不改动** `main.py`、`app/scanner.py`、`app/backtest/*`、`app/store.py`、`config.py` 等任何既有文件。
2. **只推送命中买入信号的股票**，按 K 线级别合并成一条消息（先做日 K）。
3. **区分周期**：日 K / 30 分钟 K 用配置维度隔离，当前只开启日 K，其余预留开关。
4. **盘中 14:00 扫描**：当日日线尚未收盘，用实时快照合成"当日日 K"再判定，方便盘中操作。
5. **服务器只有推送系统常驻**；实盘 Web、回测按需手动启动，不常驻。
6. **性能与安全**优先。

**运行模型（本轮定稿）**：常驻的只有一个"睡到点就唤醒作业"的轻量调度器，footprint 极小；实际干活的两个作业都是**用完即弃**——到点实例化、干完销毁、内存回落基线，状态只落磁盘不落内存：

- **作业 A · 14:00 盘中扫描推送**：推送系统**自建一个短命的 `PushScanner`**（照实盘 Scanner 的架构新写，**不复用也不改动** `app/scanner.py` 的常驻 Scanner），14:00 唤醒 → 有界扫全池（扫完或超时即止）→ 合成当日日 K → 跑买入战法 → 合并推送 → 销毁。它让推送系统**完全不依赖实盘 Scanner 是否运行**。
- **作业 B · 盘后定期更新**：收盘后把当日**已收盘**权威日线增量写回 `kline_cache`，覆盖 14:00 那根盘中合成值；同样是短命作业。它保证次日 14:00 历史已完整（作业 A 因此几乎只需拉快照）。

对既有仓库的唯一 additive 改动：新增 `.gitignore` 条目（`.env`、`push.env`、`push/state/`）——不改任何现有代码逻辑。

---

## 1. 复用点盘点（只读 import，零改动）

| 复用对象 | 来源 | 用途 |
|---|---|---|
| `BUY_REGISTRY` / `_ensure_user_rules()` | `app/backtest/rules.py` | 拿到 `accumulation_detect` 等买入战法类，含 `user_rules/` 自定义 |
| `BarContext` / `Signal` | `app/backtest/strategy.py` | 构造规则求值上下文，接收 `Signal(action="buy", reason=...)` |
| `is_closed` / `last_closed_index` | `app/bars.py` | 已收盘判定（30m 周期预留时用） |
| `DataSource`（`snapshots` / `fetch_many_kline`） | `app/datasource.py` | 实时批量快照（腾讯）+ 日 K 历史（东财→腾讯→新浪→BaoStock 四级降级） |
| `Scanner._live_daily_bar` / `is_trading_time` | `app/scanner.py` | 用实时快照合成当日日 K 的**静态方法**（只 import 调用，不实例化 Scanner、不启动其循环） |
| `KlineCache` | `app/backtest/cache.py` | 日线历史缓存读写（`kline_cache` 表，长历史，满足 accumulation ≥200 根需求） |
| `load_kline_merged` | `app/backtest/loader.py` | 增量补齐日线历史（只补缺口） |
| `BUILTIN_POOL` / `DB_PATH` / `REALTIME_PERIODS` | `config.py` | 股票池、库路径 |

**信号判定契约**（照搬现有约定，顺序不可乱）：
```
rule = BUY_REGISTRY["accumulation_detect"]()
rule.reset()                       # 1. 先 reset（会把 params 重置为 default）
rule.params = {**rule.default_params, **overrides}   # 2. 再注入参数
rule.prepare(bars)                 # 3. 用整段历史 prepare（指标按绝对下标缓存）
sig = rule.on_bar(BarContext(code, name, bars, i, None, rule.params))  # position=None
# sig 为 None 或 Signal(action="buy", reason="建仓信号：低位23%，量比1.4")
```
每股一个独立 rule 实例（跨股共用会串指标 / IndexError）；`bars` 升序且 ≥200 根。

---

## 2. 独立模块结构

新增目录，全部是新文件，不触碰既有代码：

```
push/
  __init__.py
  __main__.py          # 入口：python -m push [--daemon|--once|--update|--test-push]
  settings.py          # 推送系统独立配置（周期/扫描时刻/超时预算/规则/去重），密钥从环境变量读
  calendar.py          # 交易日历：周中 + 节假日哨兵（指数当日有量才算交易日）
  scheduler.py         # 常驻：asyncio 调度器，算下一个触发时刻、错过补跑、幂等（唯一常驻件）
  jobs.py              # 作业编排：job_scan_push(作业A) / job_postclose_update(作业B)，各自建-用-弃
  scanner.py           # PushScanner：短命有界扫描器（自建，非 app/scanner.py），拉全池数据+超时降级
  datafeed.py          # 数据装配：日线历史增量补齐(复用 loader) + 批量快照合成当日日K(复用 _live_daily_bar)
  detector.py          # 信号检测：复用 BUY_REGISTRY，对全池跑买入战法，产出命中清单
  formatter.py         # 按周期合并成钉钉 markdown 消息
  state.py             # 去重/幂等/运行状态（独立 SQLite push/state/push.db，不碰 kline.db 的业务表）
  logsetup.py          # 结构化日志 + 轮转 + 密钥脱敏
  pushers/
    __init__.py        # Pusher 抽象基类 + 工厂（按 settings 装配）
    dingtalk.py        # 钉钉群机器人：HMAC-SHA256 加签 + 重试退避
    webhook.py         # 通用 HTTP webhook（预留，占位实现）
deploy/
  Dockerfile
  docker-compose.yml
  push.service         # systemd unit（含安全加固指令）
  push.env.example     # 环境变量样例（占位密钥）
  DEPLOY.md            # 阿里云 Ubuntu 部署手册
docs/
  push-system-design.md  # 本文档
```

**零新增第三方依赖**：钉钉加签用标准库 `hmac/hashlib/base64/urllib`，HTTP 用已有的 `httpx`，调度用 `asyncio`（不引入 APScheduler）。

---

## 3. 关键流程

一个常驻调度器（`scheduler.py`）睡到触发点，唤醒对应的短命作业（`jobs.py`）；作业内部现建 `PushScanner`、干完销毁。原「13:40 独立 warm-up」已取消——历史补齐并入下面两个作业。

### 3.1 作业 A · 14:00 盘中扫描 → 合并推送

```
14:00（交易日，调度器唤醒 job_scan_push）
  ├─ 0. 实例化 PushScanner(pool, budget=scan_budget_sec)；deadline = now + budget
  ├─ 1. 交易日历校验：拉上证指数 sh000001 快照，当日无量/日期不符 → 判休市，跳过并销毁
  ├─ 2. 有界扫描（并发 gather_concurrency=10，到 deadline 停止发起新请求）：
  │      a) 增量补齐日线历史到「上一交易日收盘」（复用 load_kline_merged；正常日历史已齐→近乎零外呼，
  │         仅当作业B曾失败才真拉，作安全网）
  │      b) 批量实时快照 DataSource.snapshots(pool)  # 腾讯 50/批 → 3 批，~1-2s
  ├─ 3. 逐股装配 bars：kline_cache 取历史(≥200) + Scanner._live_daily_bar(snap) 合成当日盘中日K，
  │      追加/替换末根（仅内存视图，绝不写回 kline_cache）
  ├─ 4. 逐股跑 accumulation_detect（每股独立实例：reset→params→prepare→on_bar，i=末根=今日进行中）
  │      → 中轨角度过滤：算 BOLL 中轨(MA20)角度 ATAN((mid/REF(mid)-1)*100)*180/π，
  │        < min_mid_angle(默认0°) 剔除（去掉中轨走平/向下的票）
  ├─ 5. 覆盖率兜底：数据齐全占比 < min_coverage(0.6) → 判数据异常，改推「降级告警」而非误报「无信号」
  ├─ 6. 收集命中 → 按 (date, period) 去重（当日只推一次）→ 按质量分排序
  │      quality = rank_angle_w*中轨角度 + rank_vol_w*(量比-1)；主键命中条件数 desc → quality desc → 代码 asc
  │      默认 min_conditions=4、min_mid_angle=0、只推前 10
  ├─ 7. formatter 合并成一条钉钉 markdown（标题标注「日K · 盘中预估 14:00 · 未收盘」）
  ├─ 8. DingTalkPusher.send()：加签 + POST，失败重试 3 次退避；结果写 state
  └─ 9. 销毁 PushScanner、bars、rule 实例、快照 dict（出作用域 + 可选 gc.collect()），内存回落基线
```

命中消息字段（每股一行）：代码、名称、现价、涨跌幅、触发原因（`sig.reason`，如"建仓信号：低位23%，量比1.4"）、周期标签。

**超时是安全阀不是失败**：到 deadline 就用已扫到的部分算+推，剩余记日志计数；配合覆盖率兜底避免"没扫到"被当成"没信号"。

### 3.2 作业 B · 盘后定期更新（数据维护，不推送）

```
收盘后（调度器唤醒 job_postclose_update）
  ├─ 1. 首跑 15:40：增量拉全池当日「已收盘」权威日线（复用 load_kline_merged / downloader，四级降级）
  ├─ 2. 写回 kline_cache.put（INSERT OR REPLACE，幂等；覆盖 14:00 那根盘中合成的临时值）
  ├─ 3. 校验「今日日线是否落库」：未落（数据源尚未发布）→ 按 16:40 / 18:00 / 20:00 阶梯重试，直到成功或记日志放弃
  ├─ 4. 预留周期一并补 30m/60m（当 periods[*].enabled 开启时）
  └─ 5. 销毁，内存回落基线
```

价值：昨晚作业 B 已把历史补到「昨日收盘」，所以**次日 14:00 的作业 A 几乎无需补历史、只拉快照**，关键路径更稳更快。

可选维护项（本期不做）：分红除权会让更早历史的前复权基准漂移，建议加**每周/每月一次全量 rebase**（整段重拉日线），保证 accumulation 200 根窗口一致性。

### 3.3 30 分钟 K（预留，不在本期实现）

配置里 `periods["30m"].enabled=False`。开启后调度器自动在每根 30m 收盘时刻（10:00 / 10:30 / 11:30 / 14:00 / 14:30 / 15:00）唤醒一次作业 A，但：
- 用 `last_closed_index` 取**已收盘**末根（`confirm_on_close` 语义，避免半根假信号）；
- 历史从 `kline_cache` 的 `period="30m"` 读；作业 B 相应补 30m；
- 去重键含 `(date, period, bar_ts)`，每根只推一次；
- 消息按周期分组合并（日 K 一条、30m 每根一条或按级别合并）。

「周期」是一等维度，30m 上线只需改配置 + 补数据，不动核心代码。


### 3.4 作业 C · 21:00 盘后扫描推送（只用收盘数据）+ 命中滑动记录

盘后（默认 21:00，须排在作业 B 末次 20:00 之后）调度器唤醒 `job_scan_postclose`：

- **只用盘后数据、不碰实时快照**：`PushScanner(source="cache", mode="close")` 直接读
  `kline_cache` 里作业 B 已落库的**当日官方收盘 bar**（`build_bars_close` 截断到 <= 今日），
  不合成盘中快照 bar。因此结果就是当日收盘定论，不含尾盘预估成分。纯本地读，无联网，~1s。
- **数据就绪门禁代替日历哨兵**：以「今日 bar 在位的股票占比」(coverage) 判定——低于
  `min_coverage` 即视为非交易日/收盘数据未就绪，**静默跳过**（不推、不告警，避免周末节假日刷屏）；
  作业 B 若当天成功落库，21:00 时覆盖率自然达标。
- **精简推送**：`build_postclose_message` 只列 排名 + 名称 + 代码 + 命中次数，不含现价/涨跌幅/
  中轨角度/建仓信号明细（需求："不推送详细数据"），同样只取前 10（`top_n`）。
- 与作业 A 同战法、同参数（mc=4 + 中轨角度>=0 + 质量分排序），仅数据源与展示详略不同。

**命中滑动记录（盘前/盘后各一条）**：`state.hits` 表按 `(session, date, period, code)` 逐日留痕，
session=`pre`(14:00 作业A) / `post`(21:00 作业C)。每次扫描把**完整命中清单**写入（同日同 session
先删后插 → 幂等，手动重跑不翻倍）。推送时按 code 统计「近 `hit_window_days`(默认10) 个**扫描日**内
盘前/盘后各命中几次」注入每只票（`cnt_pre`/`cnt_post`），两个推送都展示（如 `近10日 盘前3·盘后2`）。
窗口按"最近 N 个有记录的交易日"计（自然跳过周末节假日），比 N 个自然日更贴合盘感；`prune_hits`
按 `hit_keep_dates`(默认40) 滑动修剪防表膨胀。次数是"持续度"信号——反复上榜的票更值得留意。


---

## 4. 性能设计

- **关键路径极轻**：正常日 14:00 只有 3 次批量快照 HTTP + 124 次本地规则求值 + 1 次推送，端到端 < 5s（历史已由前一晚作业 B 补齐，作业 A 近乎不拉历史）。
- **重活挪到盘后**：日线历史增量放在作业 B（15:40 起、非关键路径），且只补缺口（`load_kline_merged`），非全量重拉；避开 14:00。
- **有界扫描**：作业 A 带 `scan_budget_sec` 硬截止，到点即用已获取数据算+推，绝不因个别慢请求/被 WAF 而挂住。
- **本地缓存优先**：历史读 `kline_cache`（SQLite，WAL），只有当日末根来自快照合成，最大化本地命中、最小化外呼（呼应此前腾讯 ifzq WAF 501 教训）。
- **无 O(n²)**：规则 `prepare()` 每股一次 O(n)，`on_bar` O(1)；不在扫描里做重复指标计算。
- **用完即弃、内存不留**：常驻态只有调度器（几 KB）；实盘那种持久内存态（snapshots dict、bars 400根/股、带 prepare 缓存的 rule 实例、去重集合）**一概不常驻**——每次作业现建现用、干完丢弃，峰值内存仅作业运行那一两分钟里的 124×~250 根日线（几 MB）+ 快照，结束即回落基线。去重/运行状态写磁盘 `push/state/push.db`，不占内存。
- **并发**：数据抓取用信号量并发 10 + 0.5s throttle；规则求值 CPU-bound 且极快，顺序执行即可（必要时线程池，或改流式处理把峰值压到并发窗口）。

---

## 5. 安全设计

**密钥管理**
- 钉钉 `webhook` 与 `加签 secret` **只从环境变量读**（`push/settings.py`），仓库里只放 `deploy/push.env.example` 占位样例。
- `.env` / `push.env` 加入 `.gitignore`，服务器上手写、权限 `chmod 600`，永不入 git、永不入镜像。
- 日志对 webhook URL、secret、sign 做脱敏（只留 host + 掩码）。

**钉钉机器人**
- 采用**加签**方式（`timestamp + "\n" + secret` → HMAC-SHA256 → base64 → urlencode，拼 `&timestamp=&sign=`）；timestamp 与钉钉服务器时差需 < 1h（服务器务必设对时区/NTP）。
- 也可叠加"自定义关键词"或"IP 白名单"（把服务器出口 IP 加白）做双保险。
- 遵守机器人 20 条/分钟限流（本期每天 1 条，远低于限）；失败重试带上限与退避，避免风暴。

**服务器加固（阿里云 Ubuntu）**
- 专用**非 root** 用户运行（如 `stockpush`）。
- **无入站端口**：推送系统只发出站请求（腾讯行情 + `oapi.dingtalk.com`）。防火墙 `ufw` 只放行 SSH(22)。若加健康检查 HTTP，只绑 `127.0.0.1`。
- systemd 加固：`NoNewPrivileges=true`、`ProtectSystem=strict`、`ProtectHome=true`、`PrivateTmp=true`、`ReadWritePaths=` 仅项目 state 目录。或 Docker：非 root、只读根文件系统 + 卷挂 `.env`/`state`/`kline.db`。
- **时区 `TZ=Asia/Shanghai`**（阿里云 Ubuntu 常默认 UTC，会导致 14:00 错位——部署必检项）。
- **既有实盘 Web（FastAPI）无鉴权**：如需在服务器启动，只绑 `127.0.0.1`，通过 SSH 隧道访问，或置于带鉴权的反代之后，**严禁公网裸暴露**。本期它不常驻。
- 依赖最小化、锁版本；私有仓库用 deploy key / 只读 PAT 拉取，凭据不落地到镜像。

---

## 6. 可靠性设计

- **交易日历**：现有 `is_trading_time` 只判周一~周五、无节假日。推送侧独立加"哨兵"：取上证指数当日快照，无量或日期不符即判休市跳过；可选再挂一份节假日文件精化。不改原函数。
- **覆盖率兜底（作业A）**：数据齐全的股票占比 < `min_coverage`(0.6)（如数据源被 WAF、大面积超时）→ 判为数据异常，改推一条「降级告警」而非误报「今日无信号」，避免把"没扫到"当成"没信号"。
- **超时降级（作业A）**：到 `scan_budget_sec` 硬截止就用已扫到的部分算+推，未完成的记日志计数，不挂住、不空跑。
- **落库校验 + 阶梯重试（作业B）**：盘后更新后校验「今日日线是否已落 `kline_cache`」；数据源尚未发布则按 16:40 / 18:00 / 20:00 阶梯重试，直到成功或记日志放弃。写入用 `INSERT OR REPLACE`，重跑幂等。
- **幂等/去重**：`state` 记录每个 `(date, period, job)` 的执行状态；进程重启后若同一槽位已成功执行则跳过，避免重复推送/重复更新。
- **错过补跑**：启动时对两个作业分别检查今日槽位——作业A 若已过 14:00 且未推且在补跑窗口（如 14:00~15:00）内则补跑一次；作业B 若当晚未成功则续跑重试阶梯；均超窗则跳过并记日志（可配 missed-run 策略）。
- **失败自愈**：作业异常 → 结构化日志 + 可选发一条错误告警到钉钉（带关键词）；进程崩溃由 systemd `Restart=always` / Docker `restart: always` 拉起；作业用完即弃，单次失败不留脏内存态。
- **数据不足降级**：某股历史 < 200 根或快照缺失 → 跳过该股并计数，不打断整场（照搬 `_eval_on_day` 的"单股异常视为未命中"容错）。
- **可观测**：`state` 暴露每个作业的 last_run / last_result / next_run / 命中数 / 覆盖率 / 错误数；日志轮转。

---

## 7. 部署方案（阿里云 Ubuntu）

推荐 **Docker Compose**（隔离、迁移省心），也提供 **systemd + venv** 裸机方案。

**首次部署步骤**
1. 建钉钉群 → 添加自定义机器人 → 安全设置选「加签」→ 得到 webhook + secret。
2. 服务器装 Docker（或 python3.11 + venv）；`git clone` 私有仓库。
3. 写 `deploy/push.env`（真实密钥，`chmod 600`）。
4. **种子数据**：下载 GitHub Release 的 `kline_cache_20260903.db.gz` → `python tools/import_cache.py kline_cache_20260903.db.gz` 生成 `kline.db`（含 124 只、2020~今 日线/30m/60m 历史），首跑无需长时间补数据。
5. 设 `TZ=Asia/Shanghai`、开 NTP、`ufw` 只放行 22。
6. `docker compose up -d push`（或 `systemctl enable --now push`）。
7. 验收：`python -m push --test-push`（发测试消息）→ `python -m push --update`（跑一次盘后更新，校验今日日线落库）→ `python -m push --once --period daily`（手动跑一次盘中扫描推送全流程）→ 等次日 14:00 观察自动推送。

**三系统共存**：同一镜像/代码库，`push` 为唯一常驻服务；实盘 Web (`uvicorn main:app`) 与回测 (`python backtest.py ...`) 用 `docker compose run --rm` 或手动命令按需启动，不常驻。

---

## 8. 分阶段落地计划

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 准备 | 钉钉机器人（加签）、阿里云 Ubuntu、密钥、时区/NTP/防火墙 | 手发 webhook 测试消息成功 |
| P1 骨架 | `push/` 包、`settings`（env 密钥）、`logsetup`（脱敏）、`Pusher` 抽象 + `dingtalk.py` | `--test-push` 本地发出加签消息 |
| P2 检测 | `scanner.py`（短命有界 PushScanner）、`datafeed`（增量补历史 + 快照合成当日日K）、`detector`（复用 BUY_REGISTRY） | `--once --period daily` 本地跑出命中清单（对照 `/api/scan-rules` 结果一致） |
| P3 调度+作业 | `scheduler`（常驻）、`jobs`（作业A扫描推送 + 作业B盘后更新）、`calendar`（节假日哨兵）、`state`（去重/幂等/补跑）、超时降级 + 覆盖率兜底 + 盘后阶梯重试 | 改系统时间/模拟休市/模拟数据源失败，验证触发、跳过、补跑、降级告警、不重复、盘后落库 |
| P4 消息 | `formatter` 按周期合并 markdown + 排序 + 周期标签 | 钉钉收到格式正确的合并榜单 |
| P5 部署 | Dockerfile/compose 或 systemd unit + 加固 + 导入种子快照 | 服务器常驻，`--once`/`--update` 在服务器跑通 |
| P6 联调 | 连续 3~5 个交易日观察 14:00 自动推送、盘后更新、假日跳过、重启幂等、断网重试 | 稳定无人工干预 |
| P7 预留 | 开启 30m（多时刻 + 收盘确认 + 每根去重）、其他渠道（webhook/邮件）、多规则、收盘确认二次推送、全量 rebase 维护 | 仅改配置即可上线 30m |

**配置项（`push/settings.py`）**：`scan_budget_sec=120`、`gather_concurrency=10`、`min_coverage=0.6`、`rank_angle_w=1.0`、`rank_vol_w=10.0`、`postclose_first="15:40"`、`postclose_retries=["16:40","18:00","20:00"]`、`periods={"daily":{"enabled":True,"rule":"accumulation_detect","mode":"live","times":["14:00"],"params":{"min_conditions":4},"top_n":10,"min_mid_angle":0},"30m":{"enabled":False,...}}`。env 覆盖：`PUSH_DAILY_PARAMS="min_conditions=4"`、`PUSH_DAILY_TOPN=10`、`PUSH_DAILY_MIN_MID_ANGLE=0`、`PUSH_RANK_ANGLE_W`、`PUSH_RANK_VOL_W`、`PUSH_DAILY_TIMES`、`PUSH_ENABLE_30M` 等。密钥从环境变量读：`DINGTALK_WEBHOOK` + 二选一 `DINGTALK_SECRET`（加签）或 `DINGTALK_KEYWORD`（关键词模式）。

---

## 9. 已知权衡与风险

- **14:00 未收盘 → 假信号**：盘中判定，尾盘回落可能使信号消失。缓解：消息显式标注"盘中预估·未收盘"；可选加 14:50 复核或 15:05 收盘确认二次推送（后续开关）。需求方要盘中操作，接受此权衡。
- **`accumulation_detect` 局部低点的 lookahead**：`prepare()` 用 `j+low_window` 判定局部低点，序列末端右窗被截断，"最新一根"的判定与回测中段不完全等价。这是战法既有特性（回测亦如此），推送侧标注即可，属已知偏差，不在本期修改战法（遵守"不改原架构"）。
- **当日成交量为盘中部分值**：14:00 时约完成全日 80%+ 量能，量价条件（条件4）为近似；越接近收盘越准。
- **数据源限流/WAF**：作业A 关键路径仅 3 次批量快照，作业B 每日盘后一次增量且走缓存+四级降级，外呼量低；再叠加超时降级 + 覆盖率兜底，风险可控。
- **前复权基准漂移**：作业B 增量只覆盖近端若干根，遇分红除权时更早历史的前复权基准会与新增段不一致，轻微影响 accumulation 的 200 根窗口。缓解：预留每周/每月一次全量 rebase（本期不做）。
- **表选择**：推送独立维护 `kline_cache`（回测表，长历史），不写实时 `klines`，避免与（未常驻的）实盘 Scanner 写者冲突。

---

## 10. 对既有仓库的改动清单

- **新增**：`push/`（整包）、`deploy/`（部署文件）、`docs/push-system-design.md`（本文档）。
- **additive 修改**：`.gitignore` 追加 `.env`、`push.env`、`push/state/`（仅新增忽略项）。
- **不改动**：`main.py`、`app/scanner.py`、`app/backtest/*`、`app/store.py`、`app/datasource.py`、`config.py` 等全部现有代码逻辑（只 import 复用）。
