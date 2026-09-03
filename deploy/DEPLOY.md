# 股票推送系统 · 阿里云 Ubuntu 裸机部署手册（systemd，非 Docker）

目标：把 `push/` 常驻调度器装成 systemd 服务，7×24 自动运行——每个交易日 14:00
扫描并推送建仓信号到钉钉，盘后自动补当日收盘数据。本服务**只发出站请求**（行情源 +
`oapi.dingtalk.com`），不监听任何端口。

约定：安装目录 `/opt/stock-selector`，运行用户 `stockpush`（非 root）。若你的路径/用户名
不同，请同步改 `deploy/push.service` 里的 `WorkingDirectory` / `ExecStart` /
`EnvironmentFile` / `ReadWritePaths`。

---

## 0. 前置

- 一台阿里云 Ubuntu 20.04/22.04/24.04，有 sudo 权限。
- 钉钉群自定义机器人已建好，拿到 **webhook** 与安全设置（加签 secret 或自定义关键词）。
- 仓库地址 `https://github.com/TruthZY/stock-selector.git`（私有库需准备只读 PAT 或部署密钥）。

---

## 1. 系统时区与时间同步（**必做，否则 14:00 会错位**）

```bash
sudo timedatectl set-timezone Asia/Shanghai
timedatectl            # 确认 Time zone: Asia/Shanghai (CST, +0800)
sudo systemctl enable --now systemd-timesyncd   # NTP 对时（钉钉加签要求时差<1h）
```

## 2. 安装基础软件

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl
python3 --version      # 需 >= 3.10（推荐 3.11）；不足见下方说明
```

> 若系统自带 Python < 3.10（如 Ubuntu 20.04 的 3.8），用 deadsnakes PPA 装 3.11：
> `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install -y python3.11 python3.11-venv`
> 后续把命令里的 `python3` 换成 `python3.11`。

## 3. 创建非 root 运行用户

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin stockpush
```

## 4. 拉代码到 /opt/stock-selector

```bash
sudo mkdir -p /opt/stock-selector
sudo git clone https://github.com/TruthZY/stock-selector.git /opt/stock-selector
# 私有库：用只读 PAT —— sudo git clone https://<USER>:<PAT>@github.com/TruthZY/stock-selector.git /opt/stock-selector
sudo chown -R stockpush:stockpush /opt/stock-selector
```

## 5. 建虚拟环境 + 装依赖

```bash
cd /opt/stock-selector
sudo -u stockpush python3 -m venv venv
sudo -u stockpush venv/bin/pip install --upgrade pip
sudo -u stockpush venv/bin/pip install -r requirements.txt
```

## 6. 灌种子数据（用 GitHub Release 快照，秒级拥有多年历史）

新服务器上 `kline.db` 是空的。直接导入已发布的缓存快照，免去现场下载几年历史：

```bash
cd /opt/stock-selector
# 公开库可直接下；私有库加 -H "Authorization: Bearer <PAT>"
curl -L -o kline_cache_20260903.db.gz \
  https://github.com/TruthZY/stock-selector/releases/download/data-20260903/kline_cache_20260903.db.gz
sudo -u stockpush venv/bin/python tools/import_cache.py kline_cache_20260903.db.gz
# 看到 "OHLC 体检: 通过" 且行数/股票数正常即成功；随后可删掉 .db.gz
```

> 说明：快照只含 `kline_cache`（历史行情），不含 `stocks` 表，因此股票池会自动回落到
> `config.py` 里的 `BUILTIN_POOL`（约 124 只）——这是设计内的兜底，无需额外操作。
> 想改池子：编辑 `config.py` 的 `BUILTIN_POOL`，或在项目根放 `user_pool.json`。

## 7. 写密钥文件 deploy/push.env（**永不入 git**）

```bash
cd /opt/stock-selector
sudo -u stockpush cp deploy/push.env.example deploy/push.env
sudo -u stockpush nano deploy/push.env     # 填入真实值
sudo chmod 600 deploy/push.env
sudo chown stockpush:stockpush deploy/push.env
```

最少要填（**关键词模式**，对应当前机器人）：

```
PUSH_CHANNEL=dingtalk
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=你的token
DINGTALK_KEYWORD=推送
```

若机器人改用**加签**：填 `DINGTALK_SECRET=SECxxxx`（可不填 KEYWORD）。二者有一即可。

## 8. 防火墙：只放行 SSH（本服务无入站端口）

```bash
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status verbose     # 应只看到 22/tcp ALLOW
```

## 9. 安装并启动 systemd 服务

```bash
sudo cp /opt/stock-selector/deploy/push.service /etc/systemd/system/push.service
sudo systemctl daemon-reload
sudo systemctl enable --now push.service
systemctl status push --no-pager      # Active: active (running)
```

## 10. 验收

```bash
# a) 凭据/链路：发一条测试消息到钉钉群
cd /opt/stock-selector
sudo -u stockpush env $(grep -vE '^\s*#|^\s*$' deploy/push.env | xargs) \
  venv/bin/python -m push --test-push

# b) 手动跑一次盘中扫描（不推送，只看命中清单）
sudo -u stockpush env $(grep -vE '^\s*#|^\s*$' deploy/push.env | xargs) \
  venv/bin/python -m push --once --period daily --no-push

# c) 手动跑一次盘后更新（把当日收盘线补进 kline.db）
sudo -u stockpush env $(grep -vE '^\s*#|^\s*$' deploy/push.env | xargs) \
  venv/bin/python -m push --update

# d) 看常驻服务日志
journalctl -u push -f
```

之后**等下一个交易日 14:00**，钉钉群应自动收到"📈 建仓信号推送 · 日K"榜单；当天盘后
（15:40 起）日志应出现"作业B[daily] ... status ok"。

---

## 日常运维

```bash
# 更新代码
cd /opt/stock-selector && sudo git pull && sudo chown -R stockpush:stockpush /opt/stock-selector
sudo -u stockpush venv/bin/pip install -r requirements.txt   # 依赖有变化时
sudo systemctl restart push

# 启停/重启
sudo systemctl {start,stop,restart} push

# 实时日志 / 最近错误
journalctl -u push -f
journalctl -u push -p err -n 50

# 查看推送运行状态（去重/命中/覆盖率记录，只读）
sudo -u stockpush venv/bin/python -c "from push.state import State; from push.settings import settings; \
[print(r) for r in State(settings.state_dir).recent(10)]"
```

调阈值无需改代码，编辑 `deploy/push.env` 后 `systemctl restart push`：

```
PUSH_DAILY_PARAMS=min_conditions=4      # 选股紧度（3=宽/~70%命中，4=严/~26%）
PUSH_DAILY_TOPN=10                       # 只推前 N 只
PUSH_DAILY_TIMES=14:00                   # 触发时刻（逗号可多个）
PUSH_SCAN_BUDGET_SEC=120                 # 有界扫描超时
PUSH_MIN_COVERAGE=0.6                    # 覆盖率兜底阈值
```

---

## 排障

- **14:00 没推送**：`timedatectl` 确认时区是 Asia/Shanghai；`journalctl -u push` 看是否
  判为"非交易日跳过"（周末/节假日正常跳过）或覆盖率降级告警（数据源问题）。
- **推送失败 errcode=310000**：关键词不匹配或签名不符——检查 `DINGTALK_KEYWORD` 是否与
  机器人设置一致（关键词模式），或 `DINGTALK_SECRET` 是否正确、服务器时间是否准（加签模式）。
- **命中过多/过少**：调 `PUSH_DAILY_PARAMS=min_conditions=` 与 `PUSH_DAILY_TOPN`。
- **作业B 一直 fail**：多为收盘后数据源尚未发布当日日线，阶梯重试（16:40/18:00/20:00）
  通常会补上；持续失败检查网络与 `curl` 行情源连通性。
- **权限/写失败**：确认 `/opt/stock-selector` 属 `stockpush`，且 unit 里 `ReadWritePaths`
  指向该目录；`kline.db`、`push/state`、`push/logs` 需可写。
- **baostock 在加固下异常**（少见，仅末级降级用到）：可临时在 unit 里设 `ProtectHome=false`
  后 `daemon-reload && restart push`。
