# 给 AI 助手 / 脚本的 ERP 接入说明（v0.36）

> 原则：**agent 的成果要落进 ERP**，不要留在各自的临时目录。固定、可重复的步骤（盖章、导入流水/发票）ERP 已经做成功能，
> agent 直接调接口；需要判断的步骤（从报价单/订单/客户合同提取字段、起草合同）由 agent 做，做完把结果写回 ERP。

## 认证

- 管理员在 系统→用户管理 → ⋯「API 令牌」给账号签发令牌（建议专设「AI 助手」可读写账号 `agent`，日志里能分清人和 AI）。
- 请求头：`Authorization: Bearer owe_xxxxxxxx`；权限与该账号一致（只读账号只能 GET），所有写操作进操作日志。
- 地址：内网 `http://<服务器>:8186`。令牌只能内网用，经外网域名调用会 403。令牌泄露就吊销重发。
- 全部接口与字段：`GET /openapi.json`（下表只列常用的）。金额一律两位小数；日期 `YYYY-MM-DD`。
- **不要直接读写 ERP 数据目录**：库、发票/合同原件都归 ERP 管，经接口查看和上传。

## 常用接口

| 做什么 | 接口 | 说明 |
|---|---|---|
| 查往来单位 / 物料 | `GET /api/partners`、`GET /api/materials` | 物料别名（对方料号→我方编码）在物料返回的 aliases 里 |
| 新建合同 | `POST /api/contracts` | `{contract_no, contract_type: purchase|sales, partner_id, sign_date, amount_tax, project, remark, lines:[{material_id, qty, price_tax, remark}]}`；合同号查重 |
| 上传合同原件 | `POST /api/contracts/{id}/files` | `{name, content_b64, kind: signed|attachment|other, note}`；草稿/未签版用 other，双方签署版用 signed |
| 盖我方电子章 | `POST /api/contract-files/{fid}/stamp-preview` → `POST /api/contract-files/{fid}/stamp` | `{seal: 合同章|公章, mode: anchor|mirror|fixed, page(0=末页), anchors, placement: right|center, gap_mm, offset_x_mm, offset_y_mm, x_mm, y_mm}`；预览返回 png_b64；盖章另存新文件，镜像（对方已盖章）自动记为双方签署版 |
| 导入发票 | `POST /api/invoice-docs/upload` | `{name, content_b64, direction: in|out}`；xlsx（税务导出）也走这里 |
| 贸易票挂合同 | `POST /api/invoice-docs/{id}/register` | `{contract_id}` |
| 导入银行流水 | `POST /api/bank/import` | `{filename, content}`（CSV 文本） |
| 月结检查 | `GET /api/close-check?month=YYYY-MM` | 月结前先看还差什么；每项 `refs` 是逐条线索（流水带 `suggestions`），处理完再查一次 |
| 银行流水 | `GET /api/bank/txns?status=unmatched`、`GET /api/bank/txns/{id}/candidates` | 候选＝可关联的收付款 |
| 登记收付款 | `POST /api/payments` | `{direction: pay|receive, partner_id, contract_id, amount, pay_date, remark}` |
| 拆分收付款 | `POST /api/payments/{id}/split` | `{parts:[{amount, contract_id, remark}]}`；原笔保留余额（预付抵扣、一笔款对多合同用它） |
| 流水关联收付款 | `POST /api/bank/txns/{id}/link` | `{payment_ids:[…]}`，合计须等于流水额 |
| 流水入账（非贸易） | `POST /api/bank/txns/{id}/post` | `{category, summary, lines:[{account, amount, partner_id, cost_center_id, summary}]}`；银行存款一侧自动；撤销 `…/unpost {note}`＝红冲 |
| 手工凭证 | `POST /api/vouchers` | `{voucher_date, summary, lines:[{account, debit, credit, partner_id, summary}]}`（工资计提等）；改错用 `POST /api/vouchers/{id}/reverse` 红冲再重做 |
| 科目余额 | `GET /api/finance/trial-balance`、`/api/finance/ledger`（明细账） | 见 openapi |
| 纳税申报 | `GET /api/tax-filings/report?period=YYYY-Qn`、`GET /api/tax-filings/export.xlsx?period=` | 申报口径数与税局导入文件；导回已申报 `POST /api/tax-filings/import {name, content_b64}` 由用户在页面做 |
| BOM | `GET /api/boms?product_id=`、`POST /api/boms`、`PUT /api/boms/{id}/lines`、`POST /api/boms/{id}/activate`、`POST /api/boms/import` | 导入先不带 apply 预览，确认后 apply（可按组新建缺失物料） |
| 工单 | `POST /api/work-orders`，`/{id}/issue`（`from_bom` 按 BOM 未领量）、`/return`、`/report`、`/receipt`、`/close` | 自制 / 委外 / 研发领料；领料库存不足会被拒 |
| 成本结转 | `GET /api/mfg/close?month=` 预览，`POST /api/mfg/close {month}`，`POST /api/mfg/close/undo` | 月结时做，先看预览 |
| 发票原件 | `GET /api/invoice-docs`、`GET /api/invoice-docs/files/{fid}` | 原件以这里为准 |

## 与 agent 技能的衔接

- **quote-order-to-contract**（报价单/订单/客户合同 → 采购合同）：提取、归一化、起草仍在 agent 侧做（需要判断，出审阅清单给用户）。
  用户确认后：① `POST /api/contracts` 建合同（物料用 ERP 物料 id，对不上的先问用户或按别名）；② docx 导出 PDF；
  ③ `POST /api/contracts/{id}/files` 上传 PDF（kind=other）；④ 在 ERP 里盖章（`stamp`，或用户在页面点「盖章」）；
  ⑤ 对方回签后上传为 kind=signed。
- **pdf-stamp-overlay**：已移植进 ERP（`app/seals.py`），日常盖章在 ERP 页面做；章图在数据目录 `seals/`，尺寸/透明度按印章档案（合同章 40.51×40.77mm、公章 40.00×40.34mm、0.52，生产校准过）。技能仅留作特殊版式的后备。
