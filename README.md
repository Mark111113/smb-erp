# 小微企业进销存 + 财务 ERP

给**一般纳税人小型贸易 / 小批量制造企业**用的一套自建 ERP：进销存、合同四流、自动凭证、增值税台账、小企业会计准则报表、纳税申报导入导出、简单的生产成本。单机部署（一个 Docker 容器 + 一个数据目录），几个人同时用。

它来自一家真实公司的日常使用，从替代 Excel 台账开始，一条条按实际业务磨出来的。这里是**脱敏后的公开版**，和原项目持续同步（单向：原项目改完导出到这里，这边的 PR 需要人工搬回原项目）。

## 先说清楚它是什么、不是什么

- **是**：中国大陆小企业（执行《小企业会计准则》，一般纳税人）的内部管理系统；业务单据驱动凭证，凭证只红冲不改；月底可以交给 AI agent 经 API 按清单结账。
- **不是**：通用 ERP / SaaS。没有多公司、多币种、多仓库计价（存放地点只管数量，成本是全公司移动加权平均）、审批流、移动端。
- **会碰到要自己改的地方**：
  - 银行流水只认中国银行网银导出的 CSV（`app/bank.py`），别的银行要加解析
  - 纳税申报导入模板、数电批量开票模板（`app/templates/`）是某地某时的版本，税局会改版
  - 物料组（`app/main.py` `MATERIAL_GROUPS`）是电子元器件 / 控制板行业的分法
  - 科目表（`app/finance.py` `CHART`）按企业会计准则编码、小企业会计准则出表
- **不构成财税建议**：口径是一家公司和它的会计一起定的，用之前请让你的会计过一遍。

## 功能

| 模块 | 内容 |
|---|---|
| 业务 | 合同（版本链、原件归档与电子盖章、开票 Excel 导出）、出入库（草稿 → 过账、存放地点、调拨、批号）、发票、收付款（拆分、核销）、三单匹配、执行看板（签约 → 收发货 → 开票 → 收付款四流 + 待办） |
| 财务 | 凭证引擎（自动过账、红冲不改行、期间锁定）、银行流水导入与对账、发票档案与增值税台账、固定资产与折旧、明细账 / 余额表 / 往来账、小企业会计准则报表、纳税申报（税局导入文件导出、已申报导回）、月结检查、关联交易 |
| 生产 | BOM 版本（导入、改版）、工单（自制 / 委外 / 研发领料）、月末成本结转、报价成本测算、研发支出辅助账、批次库存 |
| 系统 | 三级账号权限、外网登录动态码（TOTP）与 IP 封禁、API 令牌（给 agent 用，限内网）、操作留痕（字段级）、本单位设置 |

技术：FastAPI + SQLAlchemy 2 + SQLite（WAL）；前端 Vue 3 无构建直载（`web/js/` 多文件按序加载）；pytest。

## 跑起来

```bash
docker build -t owe-erp .
docker run -d --name owe-erp --restart unless-stopped -p 8186:8000 \
  -v /srv/owe-erp:/data -e TZ=Asia/Shanghai owe-erp
```

浏览器打开 `http://<服务器>:8186`，空库第一次打开会让你**创建管理员**（没有内置默认密码）。然后到 系统 → 本单位设置 填公司名称、税号、开户账户。

想先看看效果，用演示数据（一家虚构公司、一个月的采购销售，只往空库里灌）：

```bash
python scripts/demo_seed.py ./demo-data        # 会问管理员 demo 的密码
OWE_DATA_DIR=./demo-data python -m uvicorn app.main:app --port 8123
```

### 数据目录

全部状态都在一个目录里（容器内 `/data`）：`owe.db`（及 `-wal`/`-shm`）、`invoice-archive/`、`contract-archive/`、`seals/`、`tax-filing/`、`backup/`。库里只存相对路径，**换服务器＝停容器、拷整个目录、起新容器**。

备份：`scripts/nas_backup.py` 在宿主机 cron 里每天跑一次（sqlite 在线备份 + 完整性校验 + 原件增量镜像，留 30 份），`OWE_DATA_DIR` 指向数据目录。

### 外网访问

建议只在内网用；要从外面访问，前面放一个反向代理，并设：

- `OWE_TRUSTED_PROXIES`：反代的地址（逗号分隔，默认只信 Docker 网桥 `172.17.0.1`）。程序从这些地址转发来的 `X-Forwarded-For` 取真实来源
- 内网（局域网段、本机，以及 系统 → 用户管理 →「办公室公网地址」里列的地址）只要密码；外网要密码 + 手机验证器动态码（本人在内网「改密码 / 动态码」开通）；外网同一 IP 15 分钟失败 10 次封 1 小时；API 令牌只能内网用

### 账号

账号只能由管理员建（系统 → 用户管理）。三级：只读 / 可读写 / 管理员。命令行兜底：

```bash
python scripts/manage_users.py /srv/owe-erp/owe.db list|create|reset-password|unlock|set-role|reset-totp
```

## 开发

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt pytest httpx
.venv/Scripts/python -m pytest tests/ -q                     # 改代码后必须全绿
.venv/Scripts/python -m uvicorn app.main:app --port 8123      # 默认库 data/owe.db；OWE_DB / OWE_DATA_DIR 可指定
```

改代码前先读 `AGENTS.md`（代码约定、已知的坑、核心业务规则，给人也给 AI agent 看）。agent 接 API 见 `docs/AGENT_API.md`。

## 许可证

[AGPL-3.0](LICENSE)：可以自由使用、修改；如果你把改过的版本提供给别人用（包括以网络服务的形式），需要同样开放源码。
