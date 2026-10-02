"""ERP — 数据模型 v1
设计要点：
- partner 统一往来单位（客户/供应商双角色）
- material + material_alias：同一物料挂多方料号别名
- contract / contract_line：版本链 replaces、采购可挂参考销售 ref_contract_no
- stock_movement：draft → confirmed → voided；确认时快照加权成本；负库存允许
- invoice / payment 挂合同；bank_txn / expense / asset 为预留接口
成本口径：不含税移动加权平均；业务单据与凭证通过 operations.synchronize 保持一致。
"""
from datetime import datetime, date
from sqlalchemy import (String, Integer, Float, Boolean, DateTime, Date, Text,
                        ForeignKey, JSON, UniqueConstraint, Index, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Partner(Base):
    __tablename__ = "partner"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    short_name: Mapped[str] = mapped_column(String(64), default="")
    is_customer: Mapped[bool] = mapped_column(Boolean, default=False)
    is_supplier: Mapped[bool] = mapped_column(Boolean, default=False)
    tax_no: Mapped[str] = mapped_column(String(32), default="")
    address: Mapped[str] = mapped_column(String(256), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    contact: Mapped[str] = mapped_column(String(64), default="")
    contact_phone: Mapped[str] = mapped_column(String(64), default="")
    bank_name: Mapped[str] = mapped_column(String(128), default="")
    bank_account: Mapped[str] = mapped_column(String(64), default="")
    remark: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    is_related: Mapped[bool] = mapped_column(Boolean, default=False)   # 关联方（v0.35；如持股股东、同一控制下企业）


class Material(Base):
    __tablename__ = "material"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # OZ-组码(2位)+序号(4位)，如 OZ-010001；旧4位码已退役不复用
    name: Mapped[str] = mapped_column(String(128), index=True)
    spec: Mapped[str] = mapped_column(String(128), default="")
    unit: Mapped[str] = mapped_column(String(16), default="个")
    mat_group: Mapped[str] = mapped_column(String(4), default="", index=True)  # 物料组两位码，组名查 MATERIAL_GROUPS 注册表
    remark: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    invoice_name: Mapped[str] = mapped_column(String(100), default="")   # 开票项目名称（空=用 name），v0.32
    tax_code: Mapped[str] = mapped_column(String(19), default="")        # 商品和服务税收分类编码（空=本单位默认），v0.32
    # 制造属性（v0.35）：类型决定存货科目（finance.INV_ACCOUNT），老物料默认 goods
    material_type: Mapped[str] = mapped_column(String(12), default="goods")
    manufacturer: Mapped[str] = mapped_column(String(64), default="")
    mpn: Mapped[str] = mapped_column(String(64), default="", index=True)       # 制造商料号
    package: Mapped[str] = mapped_column(String(32), default="")               # 封装
    msl: Mapped[str] = mapped_column(String(8), default="")                    # 湿敏等级
    min_pack: Mapped[float] = mapped_column(Float, default=0)                  # 最小包装数量（每卷/盘/包）
    pack_unit: Mapped[str] = mapped_column(String(8), default="")              # 包装单位：卷/盘/包
    loss_rate: Mapped[float] = mapped_column(Float, default=0)                 # 默认损耗率（BOM 行没填时用）
    lot_control: Mapped[bool] = mapped_column(Boolean, default=False)          # 出入库要填批号
    key_part: Mapped[bool] = mapped_column(Boolean, default=False)             # 关键件（批次追溯到序列号）
    std_hours: Mapped[float] = mapped_column(Float, default=0)                 # 产品单件标准工时（小时，报价测算默认带出，v0.37）
    smt_points: Mapped[int] = mapped_column(Integer, default=0)                # 贴片点数（以后 SMT 按点数分摊用）


class MaterialAlias(Base):
    __tablename__ = "material_alias"
    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"), index=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partner.id"), nullable=True)  # 空=通用
    alias_name: Mapped[str] = mapped_column(String(128), index=True)
    alias_spec: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class MaterialBom(Base):
    """BOM（v0.4 架子）：父子边表，天然支持多阶展开；组套/拆套单据与成本分摊逻辑后续版本再上"""
    __tablename__ = "material_bom"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_material_id: Mapped[int] = mapped_column(ForeignKey("material.id"), index=True)
    child_material_id: Mapped[int] = mapped_column(ForeignKey("material.id"), index=True)
    qty_per: Mapped[float] = mapped_column(Float, default=1)  # 单套用量
    remark: Mapped[str] = mapped_column(String(256), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Contract(Base):
    __tablename__ = "contract"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_no: Mapped[str] = mapped_column(String(48), index=True)
    contract_type: Mapped[str] = mapped_column(String(16))  # sales / purchase
    partner_id: Mapped[int] = mapped_column(ForeignKey("partner.id"))
    sign_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_tax: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/void/replaced/draft
    replaces_contract_id: Mapped[int | None] = mapped_column(ForeignKey("contract.id"), nullable=True)
    ref_contract_no: Mapped[str] = mapped_column(String(48), default="")  # 采购参考销售合同
    project: Mapped[str] = mapped_column(String(128), default="")
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint("contract_no", "contract_type", name="uq_contract_no_type"),)

    partner: Mapped[Partner] = relationship()
    lines: Mapped[list["ContractLine"]] = relationship(back_populates="contract",
                                                       cascade="all, delete-orphan",
                                                       order_by="ContractLine.line_no")


class ContractLine(Base):
    __tablename__ = "contract_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contract.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer, default=1)
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"))
    qty: Mapped[float] = mapped_column(Float, default=0)
    price_tax: Mapped[float] = mapped_column(Float, default=0)
    amount_tax: Mapped[float] = mapped_column(Float, default=0)
    remark: Mapped[str] = mapped_column(String(256), default="")

    material: Mapped[Material] = relationship()
    contract: Mapped[Contract] = relationship(back_populates="lines")


class StockMovement(Base):
    __tablename__ = "stock_movement"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_no: Mapped[str] = mapped_column(String(48), index=True)
    move_type: Mapped[str] = mapped_column(String(16))       # in / out / adjust / opening
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("contract.id"), nullable=True, index=True)
    contract_line_id: Mapped[int | None] = mapped_column(ForeignKey("contract_line.id"), nullable=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partner.id"), nullable=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"), index=True)
    qty: Mapped[float] = mapped_column(Float)                # 恒正，方向由 move_type 决定
    unit_cost: Mapped[float | None] = mapped_column(Float, nullable=True)  # 不含税账面成本（in：录入含税价自动折算）；out：确认时快照不含税加权价
    tax_rate: Mapped[float] = mapped_column(Float, default=0.13)  # 价税分离用：收货暂估/折算依据，默认 13%
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)  # draft/confirmed/voided
    move_date: Mapped[date] = mapped_column(Date, index=True)
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("stock_location.id"), nullable=True, index=True)  # 空=默认地点
    # 制造（v0.35）：工单领料/退料/完工入库挂工单；wo_cost 是只调金额的成本分摊单（qty=0，金额在 amount）
    work_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    lot_no: Mapped[str] = mapped_column(String(48), default="")
    lot_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    material: Mapped[Material] = relationship()
    partner: Mapped[Partner | None] = relationship()
    contract: Mapped[Contract | None] = relationship()


class StockLocation(Base):
    """存放地点：只管「货在哪」的数量，不参与计价（成本仍是全公司移动加权平均）。

    kind: own 自有 / third_party 寄存在往来单位处（partner_id 指向该单位）。恰有一个 is_default。
    """
    __tablename__ = "stock_location"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(24), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="own")
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partner.id"), nullable=True)
    address: Mapped[str] = mapped_column(String(200), default="")
    contact: Mapped[str] = mapped_column(String(64), default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # 仓库用途（v0.37）：general 通用 / raw 原材料仓 / wip 线边仓 / finished 成品仓 / defect 不良品仓 / rd 研发
    # 工单领料默认从 raw 出、完工默认进 finished；没设这类地点就用默认地点
    purpose: Mapped[str] = mapped_column(String(12), default="general")


class StockTransfer(Base):
    """调拨单：同一物料从一个地点挪到另一个地点。只动地点数量，不改成本、不出凭证。"""
    __tablename__ = "stock_transfer"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_no: Mapped[str] = mapped_column(String(48), index=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"), index=True)
    qty: Mapped[float] = mapped_column(Float)
    from_location_id: Mapped[int] = mapped_column(ForeignKey("stock_location.id"))
    to_location_id: Mapped[int] = mapped_column(ForeignKey("stock_location.id"))
    move_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)  # draft/confirmed/voided
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    material: Mapped[Material] = relationship()


class UserViewPref(Base):
    """列表视图设置，跟账号走（v0.20）：显示哪些列、列顺序、排序。每人每页一行。"""
    __tablename__ = "user_view_pref"
    __table_args__ = (UniqueConstraint("user_id", "view_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_user.id"), index=True)
    view_key: Mapped[str] = mapped_column(String(48))
    prefs: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class TodoItem(Base):
    """看板手工待办：系统算不出来、要人记住的事（如账实差异、待补单、待要发票）。"""
    __tablename__ = "todo_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    detail: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(8), default="warn")          # red 紧急 / warn 关注 / info 备忘
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("contract.id"), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="open", index=True)  # open / done
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    contract: Mapped[Contract | None] = relationship()


class Invoice(Base):
    __tablename__ = "invoice"
    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(String(8), index=True)  # input 进项 / output 销项
    invoice_no: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partner.id"), nullable=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("contract.id"), nullable=True, index=True)
    amount_ex_tax: Mapped[float] = mapped_column(Float, default=0)
    tax_amount: Mapped[float] = mapped_column(Float, default=0)
    amount_tax: Mapped[float] = mapped_column(Float, default=0)
    file_path: Mapped[str] = mapped_column(String(512), default="")
    verify_status: Mapped[str] = mapped_column(String(16), default="未查验")
    remark: Mapped[str] = mapped_column(Text, default="")

    partner: Mapped[Partner | None] = relationship()
    contract: Mapped[Contract | None] = relationship()


class InvoiceDoc(Base):
    """发票档案（v0.26）：收到/开出的每一张票（贸易、费用、工程、资产都在这里），带归档文件、入账与抵扣标记。

    与业务的关系：贸易票 → invoice_id 指向 invoice 表（该表仍是贸易过账的来源）；
    非贸易票 → voucher_id 指向入账凭证。进项抵扣按 deduct_period（税款所属期）由 synchronize 出
    222104 待认证 → 222102 进项 的转出凭证，使账上进项 = 申报表进项。
    """
    __tablename__ = "invoice_doc"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_no: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    direction: Mapped[str] = mapped_column(String(8), index=True)        # in 进项 / out 销项
    kind: Mapped[str] = mapped_column(String(32), default="")             # 数电专票 / 数电普票 / …
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    seller_name: Mapped[str] = mapped_column(String(128), default="")
    seller_tax_no: Mapped[str] = mapped_column(String(32), default="")
    buyer_name: Mapped[str] = mapped_column(String(128), default="")
    buyer_tax_no: Mapped[str] = mapped_column(String(32), default="")
    amount_ex_tax: Mapped[float] = mapped_column(Float, default=0)
    tax_amount: Mapped[float] = mapped_column(Float, default=0)
    amount_tax: Mapped[float] = mapped_column(Float, default=0)
    tax_rates: Mapped[str] = mapped_column(String(64), default="")        # 如 "13%" 或 "13%,6%"
    items: Mapped[str] = mapped_column(Text, default="")                  # 品目摘要
    face_remark: Mapped[str] = mapped_column(Text, default="")            # 票面备注
    status: Mapped[str] = mapped_column(String(16), default="normal")     # normal / reversed 被红冲 / red 红字票
    red_of: Mapped[str] = mapped_column(String(48), default="")           # 红字票对应的蓝字票号
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partner.id"), nullable=True)
    category: Mapped[str] = mapped_column(String(16), default="")         # trade / expense / prepay / asset / none
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoice.id"), nullable=True)
    voucher_id: Mapped[int | None] = mapped_column(ForeignKey("voucher.id"), nullable=True)
    booked_month: Mapped[str] = mapped_column(String(7), default="")      # 入账月份 YYYY-MM
    deduct_status: Mapped[str] = mapped_column(String(16), default="")    # 进项：pending/deducted/nondeductible/transferred
    deduct_period: Mapped[str] = mapped_column(String(7), default="")     # 勾选所属期 YYYY-MM
    deduct_time: Mapped[str] = mapped_column(String(19), default="")
    deduct_tax: Mapped[float] = mapped_column(Float, default=0)           # 有效抵扣税额
    transfer_period: Mapped[str] = mapped_column(String(7), default="")   # 进项转出所属期
    transfer_account: Mapped[str] = mapped_column(String(16), default="") # 进项转出计入的费用科目
    note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="")           # file / tax_export
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    partner: Mapped[Partner | None] = relationship()
    files: Mapped[list["InvoiceFile"]] = relationship(back_populates="doc", cascade="all, delete-orphan")


class InvoiceFile(Base):
    __tablename__ = "invoice_file"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("invoice_doc.id"), index=True)
    file_name: Mapped[str] = mapped_column(String(256))                   # 原文件名
    stored_path: Mapped[str] = mapped_column(String(512))                 # 相对归档根目录
    file_type: Mapped[str] = mapped_column(String(8))                     # pdf / ofd / xml / png …
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    doc: Mapped[InvoiceDoc] = relationship(back_populates="files")


class Payment(Base):
    __tablename__ = "payment"
    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(String(8), index=True)  # pay 付款 / receive 收款
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partner.id"), nullable=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("contract.id"), nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Float, default=0)
    pay_date: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual / bank
    bank_txn_id: Mapped[int | None] = mapped_column(ForeignKey("bank_txn.id"), nullable=True)
    remark: Mapped[str] = mapped_column(Text, default="")

    partner: Mapped[Partner | None] = relationship()
    contract: Mapped[Contract | None] = relationship()


class DocumentState(Base):
    """业务纠错状态，兼容旧单据而不删除历史行。"""
    __tablename__ = "document_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    document_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")
    reason: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("kind", "document_id"),)


class AuditEvent(Base):
    """业务语义事件（锁期、红冲等，由 operations.audit 显式写入）；操作人取自当前请求"""
    __tablename__ = "audit_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)  # v0.11 起本地时间；此前为 UTC


class ChangeLog(Base):
    """数据变更日志：ORM flush 时自动记录（见 app/audit.py），与业务改动同事务。

    一次请求一个 request_id；自动过账等连带改动记在触发它的操作人名下。行只追加不修改。
    """
    __tablename__ = "change_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(32), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_name: Mapped[str] = mapped_column(String(64), default="")   # 快照，改名不影响历史
    method: Mapped[str] = mapped_column(String(8), default="")
    path: Mapped[str] = mapped_column(String(160), default="")
    table_name: Mapped[str] = mapped_column(String(32), index=True)
    row_key: Mapped[str] = mapped_column(String(64), index=True)
    op: Mapped[str] = mapped_column(String(8))                       # insert / update / delete
    changes: Mapped[dict] = mapped_column(JSON)                       # update: {字段:[旧,新]}；insert/delete: 行快照


class PeriodLock(Base):
    __tablename__ = "period_lock"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    reason: Mapped[str] = mapped_column(Text)


class Allocation(Base):
    __tablename__ = "allocation"
    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payment.id"))
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoice.id"))
    amount: Mapped[float] = mapped_column(Float)
    allocation_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="active")


class StockConversion(Base):
    __tablename__ = "stock_conversion"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    move_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    detail: Mapped[dict] = mapped_column(JSON)


class ConversionMovement(Base):
    __tablename__ = "conversion_movement"
    movement_id: Mapped[int] = mapped_column(ForeignKey("stock_movement.id"), primary_key=True)
    conversion_id: Mapped[int] = mapped_column(ForeignKey("stock_conversion.id"))


class MigrationRecord(Base):
    __tablename__ = "migration_record"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AuthUser(Base):
    """本机登录用户。空库首个账号由页面创建为管理员，其余账号只由管理员创建。

    role: admin 管理员 / editor 可读写 / viewer 只读（权限判定见 auth.py）。
    """
    __tablename__ = "auth_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="editor", server_default="editor")
    password_hash: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)  # 本地时间，与 last_login_at 同口径
    # 外网登录（v0.38）：外网必须过验证器动态码（TOTP）；内网不要。allow_external 关掉则该账号只能内网用
    totp_secret: Mapped[str] = mapped_column(String(64), default="")      # base32；开通前是待确认的密钥
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_step: Mapped[int] = mapped_column(Integer, default=0)       # 已用过的最后一个时间步，防同一个码重放
    allow_external: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_exempt_until: Mapped[str] = mapped_column(String(10), default="")  # 管理员临时放行：到这天（含）为止，没开通动态码也能外网凭密码登录


class AuthSession(Base):
    __tablename__ = "auth_session"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_user.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)  # 本地时间，与 last_login_at 同口径

    user: Mapped[AuthUser] = relationship()


class ApiToken(Base):
    """API 令牌（v0.31）：给 agent/脚本调用 ERP 接口用（Authorization: Bearer <令牌>）。

    令牌属于某个用户，权限、留痕与该用户网页登录完全一致；只存哈希，明文只在创建时显示一次；
    管理员在 系统→用户管理 签发/吊销。建议专设「AI 助手」可读写账号，操作日志里能分清人和 agent。
    """
    __tablename__ = "api_token"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_user.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(12), default="")          # 令牌前几位，便于辨认
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[AuthUser] = relationship()


class DocumentLink(Base):
    __tablename__ = "document_link"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    original_id: Mapped[int] = mapped_column(Integer)
    child_id: Mapped[int] = mapped_column(Integer)
    amount: Mapped[float] = mapped_column(Float)


# ---------- 预留接口（一期建表不实现业务） ----------
class BankTxn(Base):
    """银行流水（v0.24，中行 HISQRY CSV 导入）。amount 带符号：收入为正、支出为负。

    与业务的关系：贸易款 → payment.bank_txn_id 指回本行（一笔流水可对多笔收付款）；
    非贸易（投资款/费用/税费/工程款等）→ voucher_id 指向分类入账生成的凭证。
    """
    __tablename__ = "bank_txn"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_no: Mapped[str] = mapped_column(String(32), default="")
    txn_no: Mapped[str] = mapped_column(String(64), unique=True)       # 交易流水号，去重键
    txn_date: Mapped[date] = mapped_column(Date, index=True)
    txn_time: Mapped[str] = mapped_column(String(8), default="")
    amount: Mapped[float] = mapped_column(Float, default=0)
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)  # 交易后余额
    counterparty_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    counterparty_account: Mapped[str] = mapped_column(String(64), default="")
    counterparty_bank: Mapped[str] = mapped_column(String(128), default="")
    biz_type: Mapped[str] = mapped_column(String(32), default="")      # 业务类型
    summary: Mapped[str] = mapped_column(String(128), default="")      # 摘要
    purpose: Mapped[str] = mapped_column(String(128), default="")      # 用途
    memo: Mapped[str] = mapped_column(Text, default="")                # 交易附言
    status: Mapped[str] = mapped_column(String(16), default="unmatched", index=True)  # unmatched/matched/posted/ignored
    category: Mapped[str] = mapped_column(String(32), default="")      # 分类入账时的类别
    voucher_id: Mapped[int | None] = mapped_column(ForeignKey("voucher.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    source_file: Mapped[str] = mapped_column(String(256), default="")
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Expense(Base):
    __tablename__ = "expense"
    id: Mapped[int] = mapped_column(primary_key=True)
    expense_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    category: Mapped[str] = mapped_column(String(64), default="")
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partner.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0)
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Asset(Base):
    """固定资产卡片（v0.25）：原值、年限、残值率、起折月；按月直线法计提，凭证 source_type=depreciation。"""
    __tablename__ = "asset"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(24), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    account: Mapped[str] = mapped_column(String(16))                # 资产科目，如 160101
    expense_account: Mapped[str] = mapped_column(String(16), default="660209")   # 折旧费用科目
    acquired_date: Mapped[date] = mapped_column(Date)
    cost: Mapped[float] = mapped_column(Float, default=0)              # 原值（不含税）
    life_months: Mapped[int] = mapped_column(Integer, default=120)
    residual_rate: Mapped[float] = mapped_column(Float, default=0)
    start_month: Mapped[str] = mapped_column(String(7))                # 起折月 YYYY-MM（默认取得次月）
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partner.id"), nullable=True)
    voucher_id: Mapped[int | None] = mapped_column(ForeignKey("voucher.id"), nullable=True)   # 入账凭证
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / disposed
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DepreciationLine(Base):
    """某资产某月的折旧额（一个月一张凭证，行按资产展开）。"""
    __tablename__ = "depreciation_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    amount: Mapped[float] = mapped_column(Float)
    voucher_id: Mapped[int] = mapped_column(ForeignKey("voucher.id"))
    __table_args__ = (UniqueConstraint("asset_id", "period"),)


class Archive(Base):
    __tablename__ = "archive"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(32), default="")   # contract/invoice/payment/other
    rel_type: Mapped[str] = mapped_column(String(32), default="")
    rel_no: Mapped[str] = mapped_column(String(64), default="", index=True)  # 合同号/发票号等
    file_path: Mapped[str] = mapped_column(String(512))
    file_name: Mapped[str] = mapped_column(String(256), default="")
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ---------- 财务模块：SAP 风格凭证-行项目模型 ----------
class Account(Base):
    """会计科目（小企业准则子集；parent_code 成树，只允许叶子过账）"""
    __tablename__ = "account"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    acc_type: Mapped[str] = mapped_column(String(16))              # asset/liability/equity/revenue/expense
    direction: Mapped[str] = mapped_column(String(4))              # D 借 / C 贷
    parent_code: Mapped[str | None] = mapped_column(ForeignKey("account.code"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    memo: Mapped[str] = mapped_column(String(256), default="")


class CostCenter(Base):
    """成本中心（树形架构，费用/损益类凭证行的辅助核算维度）"""
    __tablename__ = "cost_center"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("cost_center.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    memo: Mapped[str] = mapped_column(String(256), default="")


class Voucher(Base):
    """会计凭证头：业务单据自动过账或手工录入，借贷必须平衡"""
    __tablename__ = "voucher"
    id: Mapped[int] = mapped_column(primary_key=True)
    voucher_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)   # 记-YYYYMM-NNNN
    voucher_date: Mapped[date] = mapped_column(Date, index=True)
    source_type: Mapped[str] = mapped_column(String(24), default="manual", index=True)
    # manual / invoice_out / invoice_in / goods_in / goods_out / payment_in / payment_out / reversal / closing
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    summary: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="posted", index=True)  # posted/reversed
    reversal_of: Mapped[int | None] = mapped_column(ForeignKey("voucher.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    lines: Mapped[list["VoucherLine"]] = relationship(back_populates="voucher",
                                                       cascade="all, delete-orphan",
                                                       order_by="VoucherLine.line_no")


class VoucherLine(Base):
    """凭证行项目：科目 + 借/贷 + 辅助核算（往来单位/成本中心）"""
    __tablename__ = "voucher_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    voucher_id: Mapped[int] = mapped_column(ForeignKey("voucher.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer, default=1)
    account_code: Mapped[str] = mapped_column(ForeignKey("account.code"), index=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partner.id"), nullable=True, index=True)  # 往来明细账
    cost_center_id: Mapped[int | None] = mapped_column(ForeignKey("cost_center.id"), nullable=True)
    debit: Mapped[float] = mapped_column(Float, default=0)
    credit: Mapped[float] = mapped_column(Float, default=0)
    summary: Mapped[str] = mapped_column(String(256), default="")

    voucher: Mapped[Voucher] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()
    partner: Mapped[Partner | None] = relationship()
    cost_center: Mapped[CostCenter | None] = relationship()


Index("ix_movement_mat_status", StockMovement.material_id, StockMovement.status)


# ---------------- 制造（v0.35，docs/PLAN_manufacturing.md） ----------------
class BomHeader(Base):
    """BOM 版本头：一个产品可有多个版本，同一时刻最多一个 active；启用时同步写 material_bom（组套/拆套继续用）。"""
    __tablename__ = "bom_header"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("material.id"), index=True)
    version: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12), default="draft")      # draft / active / obsolete
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    change_note: Mapped[str] = mapped_column(Text, default="")             # 工程变更说明（ECN）
    source: Mapped[str] = mapped_column(String(128), default="")           # 导入来源文件
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    lines: Mapped[list["BomLine"]] = relationship(order_by="BomLine.seq", cascade="all, delete-orphan")


class BomLine(Base):
    __tablename__ = "bom_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    bom_id: Mapped[int] = mapped_column(ForeignKey("bom_header.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1)
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"), index=True)
    qty: Mapped[float] = mapped_column(Float, default=1)                   # 单台用量
    loss_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # 空=用物料默认损耗率
    ref_des: Mapped[str] = mapped_column(Text, default="")                 # 位号
    side: Mapped[str] = mapped_column(String(8), default="")               # TOP / BOT / THT
    alt_group: Mapped[str] = mapped_column(String(16), default="")         # 替代组：同组只有 primary 参与领料
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str] = mapped_column(String(256), default="")
    material: Mapped[Material] = relationship()


class WorkOrder(Base):
    """工单：production 自制 / subcontract 委外 / rd 研发领料。领料、退料、完工入库都是挂 work_order_id 的出入库单。"""
    __tablename__ = "work_order"
    id: Mapped[int] = mapped_column(primary_key=True)
    wo_no: Mapped[str] = mapped_column(String(32), unique=True)
    kind: Mapped[str] = mapped_column(String(12), default="production")
    product_id: Mapped[int | None] = mapped_column(ForeignKey("material.id"), nullable=True)
    bom_id: Mapped[int | None] = mapped_column(ForeignKey("bom_header.id"), nullable=True)
    qty: Mapped[float] = mapped_column(Float, default=0)                   # 计划数量
    status: Mapped[str] = mapped_column(String(12), default="released")   # released / closed / cancelled
    plan_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    plan_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    cost_center_id: Mapped[int | None] = mapped_column(ForeignKey("cost_center.id"), nullable=True)   # 生产线 / 研发项目
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partner.id"), nullable=True)           # 委外厂
    location_id: Mapped[int | None] = mapped_column(ForeignKey("stock_location.id"), nullable=True)   # 委外厂寄存地点
    fee_price: Mapped[float] = mapped_column(Float, default=0)             # 委外加工费单价（不含税）
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    product: Mapped[Material | None] = relationship()
    bom: Mapped[BomHeader | None] = relationship()


class WorkReport(Base):
    """报工：产出、不良、工时（月末按工时分摊人工和制造费用）。"""
    __tablename__ = "work_report"
    id: Mapped[int] = mapped_column(primary_key=True)
    work_order_id: Mapped[int] = mapped_column(ForeignKey("work_order.id"), index=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    good_qty: Mapped[float] = mapped_column(Float, default=0)
    bad_qty: Mapped[float] = mapped_column(Float, default=0)
    hours: Mapped[float] = mapped_column(Float, default=0)
    cost_center_id: Mapped[int | None] = mapped_column(ForeignKey("cost_center.id"), nullable=True)
    remark: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(12), default="active")     # active / voided
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class MfgClose(Base):
    """月末成本结转记录：人工与制造费用分摊到当月完工工单，研发支出转研究费用。只能撤最后一个月。"""
    __tablename__ = "mfg_close"
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), unique=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    voucher_ids: Mapped[list] = mapped_column(JSON, default=list)
    movement_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SupplierPrice(Base):
    """供应商价目表（v0.37）：物料 × 供应商的不含税单价、最小起订量、交期。报价测算优先取首选价，再取最低价。"""
    __tablename__ = "supplier_price"
    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"), index=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partner.id"), index=True)
    price: Mapped[float] = mapped_column(Float, default=0)              # 不含税单价
    tax_rate: Mapped[float] = mapped_column(Float, default=0.13)
    moq: Mapped[float] = mapped_column(Float, default=0)                # 最小起订量
    lead_days: Mapped[int] = mapped_column(Integer, default=0)          # 交期（天）
    supplier_pn: Mapped[str] = mapped_column(String(64), default="")    # 供应商自己的料号
    preferred: Mapped[bool] = mapped_column(Boolean, default=False)     # 首选供应商（同一物料最多一个）
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    remark: Mapped[str] = mapped_column(String(256), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    material: Mapped[Material] = relationship()
    partner: Mapped[Partner] = relationship()


class AuthIpBlock(Base):
    """外网 IP 登录失败计数与封禁（v0.38）：15 分钟内失败 10 次封 1 小时；内网 IP 不计。"""
    __tablename__ = "auth_ip_block"
    ip: Mapped[str] = mapped_column(String(64), primary_key=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    first_fail_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_username: Mapped[str] = mapped_column(String(32), default="")
    total_fails: Mapped[int] = mapped_column(Integer, default=0)
