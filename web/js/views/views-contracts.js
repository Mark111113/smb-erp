/* owe-erp 前端（由 app.js 机械拆分，经典脚本按序加载，顺序见 index.html；依赖：base 先于 forms 先于 views 先于 app） */

/* ---------------- 合同列表 ---------------- */
const ContractsView = defineComponent({
  components: { ContractForm, DataTable },
  template: `
  <div>
    <div class="filter-bar">
      <select v-model="f.type"><option value="">全部类型</option><option value="purchase">采购</option><option value="sales">销售</option></select>
      <select v-model="f.status"><option value="">全部状态</option><option value="active">有效</option><option value="replaced">被替代</option><option value="void">作废</option></select>
      <select v-model="f.stage"><option value="">四流：全部</option><option value="open">未闭合</option><option value="closed">已闭合</option></select>
      <input class="search" style="margin-left:auto;width:200px" placeholder="搜合同号 / 单位…" v-model="f.q">
      <button class="btn btn-ink" @click="showNew=true">＋ 新建合同</button>
    </div>
    <div class="card">
      <data-table view="contracts" date-key="sign_date" :columns="cols" :rows="rows" export-name="合同" empty="无合同"
                  :row-click="c => go('contract/'+c.id)" :row-class="c => c.status!=='active' ? 'dim' : ''">
        <template #cell-partner_short="{ row }"><div class="dt-ellipsis" :title="row.partner_name">{{row.partner_short||row.partner_name}}<span v-if="row.is_dual_role" class="tag-dual">供+客</span></div></template>
        <template #cell-contract_type="{ row }"><span class="pill" :class="row.contract_type==='sales'?'p-ok':'p-info'">{{row.contract_type==='sales'?'销售':'采购'}}</span></template>
        <template #cell-status="{ row }"><span class="pill" :class="row.status==='active'?'p-ok':'p-gray'">{{statusName[row.status]||row.status}}</span></template>
        <template #cell-flows="{ row }"><div class="flows flows-2" v-if="row.status==='active'" :title="['签约','收发货','开票','收付款'].map((n,i)=>(row.flows[i]?'✓':'·')+n).join('  ')">
          <div class="flow" v-for="(f,i) in row.flows" :key="i" :class="f ? 'on'+(i+1) : ''"></div></div><span v-else class="sub-line">—</span></template>
        <template #cell-stage="{ row }"><span v-if="row.status==='active'" class="pill" :class="row.stage_cls">{{row.stage}}</span><span v-else class="sub-line">—</span></template>
      </data-table>
    </div>
    <contract-form v-if="showNew" @close="showNew=false" @saved="load"></contract-form>
  </div>`,
  data: () => ({ list: [], showNew: false, f: { type: '', status: '', q: '', stage: '' },
                 statusName: { active: '有效', replaced: '被替代', void: '作废', draft: '草稿' } }),
  computed: {
    rows() {
      if (!this.f.stage) return this.list;
      return this.list.filter(c => c.status === 'active' && ((c.stage === '已闭合') === (this.f.stage === 'closed')));
    },
    cols() {
      return [
        { key: 'contract_no', label: '合同号', type: 'id', width: 128 },
        { key: 'contract_type', label: '类型', type: 'status', width: 64, value: c => c.contract_type === 'sales' ? '销售' : '采购' },
        { key: 'partner_short', label: '对方', type: 'partner', width: 110, value: c => c.partner_short || c.partner_name },
        { key: 'partner_name', label: '对方全称', type: 'text', hidden: true },
        { key: 'sign_date', label: '签订日', type: 'date' },
        { key: 'amount_tax', label: '金额·含税', type: 'money' },
        { key: 'invoice_sum', label: '已开票', type: 'money' },
        { key: 'payment_sum', label: '已收付', type: 'money' },
        { key: 'unpaid', label: '未收付', type: 'money', hidden: true, value: c => Math.round((c.amount_tax - c.payment_sum) * 100) / 100 },
        { key: 'delivery', label: '交付', type: 'short', width: 72, hidden: true, value: c => c.total_lines ? `${c.moved_lines}/${c.total_lines} 行` : '—' },
        { key: 'flows', label: '四流', type: 'short', width: 70, sortable: false, value: c => c.flows.filter(x => x).length + '/4' },
        { key: 'stage', label: '执行状态', type: 'status', width: 88, value: c => c.status === 'active' ? c.stage : '' },
        { key: 'project', label: '项目', type: 'text', hidden: true },
        { key: 'ref_contract_no', label: '参考销售', type: 'id', width: 120, hidden: true },
        { key: 'signed_files', label: '签署版', type: 'short', width: 60, value: c => c.signed_files ? '✓' : (c.status === 'active' ? '缺' : '—') },
        { key: 'remark', label: '备注', type: 'text' },
        { key: 'created_by', label: '录入人', type: 'person', hidden: true },
        { key: 'status', label: '合同状态', type: 'status', width: 76, value: c => this.statusName[c.status] || c.status },
      ];
    },
  },
  methods: {
    fmt, async load() { this.list = await api.get(`/api/contracts?q=${encodeURIComponent(this.f.q)}&ctype=${this.f.type}&status=${this.f.status}`); },
    go(k) { location.hash = '#/' + k; },
  },
  watch: { f: { deep: true, handler() { this.load(); } } },
  mounted() { this.load(); },
});

/* 合同新建/编辑弹窗 */

/* ---------------- 按合同明细收货/发货 ---------------- */
const FulfillForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:820px">
      <div class="modal-h"><b>按明细{{verb}} · {{c.contract_no}}</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="form-grid">
        <div class="fg"><label>{{verb}}日期</label><input type="date" v-model="f.move_date"></div>
        <div class="fg" v-if="purchase"><label>税率（含税单价折不含税成本）</label><input class="mono" type="number" step="0.01" min="0" max="1" v-model.number="f.tax_rate"></div>
        <div class="fg"><label>{{purchase ? '收到哪里' : '从哪里发出'}}</label><select v-model.number="f.location_id">
          <option v-for="l in locs" :key="l.id" :value="l.id">{{l.name}}{{l.is_default?'（默认）':''}}</option></select></div>
        <div class="fg" :class="purchase ? 'span2' : ''"><label>备注</label><input v-model="f.remark" placeholder="如：物流单号、签收人"></div>
        <div class="fg span2"><label style="display:flex;gap:6px;align-items:center;color:var(--ink);font-size:13px">
          <input type="checkbox" v-model="f.confirm" style="width:auto">直接确认过账（不勾则生成草稿，稍后确认）</label></div>
      </div>
      <div style="padding:0 18px 4px">
        <table>
          <thead><tr><th style="width:28px"><input type="checkbox" :checked="allOn" @change="toggleAll($event.target.checked)" style="width:auto"></th>
            <th>#</th><th>物料</th><th class="num">订购</th><th class="num">已{{verb}}</th><th class="num">草稿中</th><th class="num">本次{{verb}}</th></tr></thead>
          <tbody>
            <tr v-for="r in rows" :key="r.line_id" :style="r.open<=0 ? 'opacity:.45' : ''">
              <td><input type="checkbox" v-model="r.on" :disabled="r.open<=0" style="width:auto"></td>
              <td class="mono">{{r.line_no}}</td>
              <td>{{r.material_name}}<div class="sub-line mono">{{r.material_code}} {{r.spec}}</div></td>
              <td class="num">{{r.ordered}}</td><td class="num">{{r.delivered}}</td><td class="num">{{r.draft||''}}</td>
              <td class="num"><input class="mono" type="number" min="0" :max="r.open" step="any" v-model.number="r.qty" :disabled="!r.on"
                   style="width:80px;text-align:right"></td>
            </tr>
          </tbody>
        </table>
        <div class="sub-line" style="padding:8px 0">本次最多可{{verb}}「订购 − 已{{verb}} − 草稿中」；草稿请先确认或作废。{{purchase ? '' : '出库成本按确认日的不含税加权均价结转。'}}</div>
      </div>
      <div v-if="err" class="auth-error" style="padding:0 18px">{{err}}</div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button>
        <button class="btn btn-ink" :disabled="busy || !picked.length" @click="save">{{f.confirm ? '生成并过账' : '生成草稿'}}（{{picked.length}} 行）</button></div>
    </div>
  </div>`,
  props: { c: Object },
  emits: ['close', 'saved'],
  data: () => ({ rows: [], locs: [], f: { move_date: today(), tax_rate: 0.13, remark: '', confirm: true, location_id: null }, err: '', busy: false }),
  computed: {
    purchase() { return this.c.contract_type === 'purchase'; },
    verb() { return this.purchase ? '收货' : '发货'; },
    picked() { return this.rows.filter(r => r.on && r.qty > 0); },
    allOn() { const open = this.rows.filter(r => r.open > 0); return open.length > 0 && open.every(r => r.on); },
  },
  methods: {
    toggleAll(v) { for (const r of this.rows) if (r.open > 0) r.on = v; },
    async save() {
      this.err = '';
      const bad = this.picked.find(r => r.qty - r.open > 1e-6);
      if (bad) { this.err = `第 ${bad.line_no} 行超过可${this.verb}数量 ${bad.open}`; return; }
      this.busy = true;
      try {
        const r = await api.post(`/api/contracts/${this.c.id}/fulfill`, { ...this.f,
          lines: this.picked.map(x => ({ line_id: x.line_id, qty: x.qty })) });
        alert(`已生成 ${r.created.length} 张${this.verb}单` + stockWarnings(r, id => (this.rows.find(x => x.material_id === id) || {}).material_name || ''));
        this.$emit('saved'); this.$emit('close');
      } catch (e) { this.err = String(e); }
      finally { this.busy = false; }
    },
  },
  async mounted() {
    this.locs = (await api.get('/api/locations')).filter(l => l.active);
    this.f.location_id = (this.locs.find(l => l.is_default) || {}).id || null;
    const prog = Object.fromEntries((await api.get(`/api/contracts/${this.c.id}/progress`)).map(p => [p.line_id, p]));
    this.rows = this.c.lines.map(l => {
      const p = prog[l.id] || { ordered: l.qty, delivered: 0, draft: 0, open: l.qty };
      return { line_id: l.id, line_no: l.line_no, material_id: l.material_id, material_name: l.material_name,
               material_code: l.material_code, spec: l.spec, ...p, on: p.open > 0, qty: Math.max(p.open, 0) };
    });
  },
});

/* 批量确认本合同的草稿（迁移暂估的草稿日期常需改成实际日期） */
const DraftConfirmForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:680px">
      <div class="modal-h"><b>确认草稿 · {{c.contract_no}}</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="form-grid" style="grid-template-columns:1fr 2fr">
        <div class="fg"><label>统一改为日期（留空=保留各自日期）</label><input type="date" v-model="moveDate"></div>
        <div class="fg sub-line" style="align-self:end">确认后计入库存并自动过账；出库成本按确认日期的不含税加权均价结转。</div>
      </div>
      <div style="padding:0 18px">
        <table>
          <thead><tr><th style="width:28px"><input type="checkbox" :checked="ids.length===drafts.length" @change="ids=$event.target.checked?drafts.map(d=>d.id):[]" style="width:auto"></th>
            <th>单号</th><th>物料</th><th class="num">数量</th><th>草稿日期</th><th>备注</th></tr></thead>
          <tbody><tr v-for="d in drafts" :key="d.id"><td><input type="checkbox" :value="d.id" v-model="ids" style="width:auto"></td>
            <td class="mono">{{d.doc_no}}</td><td>{{d.material_name}}</td><td class="num">{{d.qty}}</td>
            <td class="mono">{{d.move_date}}</td><td class="sub-line">{{d.remark}}</td></tr></tbody>
        </table>
      </div>
      <div v-if="err" class="auth-error" style="padding:8px 18px 0">{{err}}</div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button>
        <button class="btn btn-ink" :disabled="busy || !ids.length" @click="save">确认 {{ids.length}} 张</button></div>
    </div>
  </div>`,
  props: { c: Object },
  emits: ['close', 'saved'],
  data: () => ({ ids: [], moveDate: '', err: '', busy: false }),
  computed: { drafts() { return this.c.movements.filter(m => m.status === 'draft'); } },
  methods: {
    async save() {
      this.busy = true; this.err = '';
      try {
        const r = await api.post('/api/movements/batch-confirm', { ids: this.ids, move_date: this.moveDate || null });
        alert(`已确认 ${r.confirmed} 张` + stockWarnings(r, id => (this.c.movements.find(x => x.material_id === id) || {}).material_name || ''));
        this.$emit('saved'); this.$emit('close');
      } catch (e) { this.err = String(e); }
      finally { this.busy = false; }
    },
  },
  mounted() { this.ids = this.drafts.map(d => d.id); },
});

/* ---------------- 合同详情 ---------------- */
const ContractDetailView = defineComponent({
  components: { ArchiveForm, ContractForm, FulfillForm, DraftConfirmForm, RelinkForm, ConfirmMoveForm, StampForm, InvoiceExportForm },
  template: `
  <div v-if="c">
    <confirm-move-form v-if="confirming" :m="confirming" @close="confirming=null" @saved="load"></confirm-move-form>
    <div class="filter-bar">
      <button class="btn btn-ghost" @click="go('contracts')">‹ 返回列表</button>
      <span style="margin-left:8px;font-size:15px;font-weight:600" class="mono">{{c.contract_no}}</span>
      <span class="pill p-info">{{c.contract_type==='sales'?'销售合同':'采购合同'}}</span>
      <span class="pill" :class="c.status==='active'?'p-ok':'p-gray'">{{{active:'有效',replaced:'被替代',void:'作废'}[c.status]||c.status}}</span>
      <button class="btn btn-ghost" @click="showEdit=true">编辑</button>
      <button class="btn btn-ghost" v-if="['active','void'].includes(c.status) && !c.replaced_by_no" @click="showRevise=true" title="签了新版本合同：生成新版并替代本合同">合同改版</button>
      <span style="margin-left:auto" class="mono">{{fmt(c.amount_tax)}} 元</span>
      <button class="btn btn-ghost" v-if="c.status==='active' && c.contract_type==='sales' && c.lines.length" @click="showInvExport=true" title="生成税局批量开票导入模板">导出开票 Excel</button>
      <button class="btn btn-red" v-if="c.status==='active' && c.lines.length" @click="showFulfill=true">按明细{{c.contract_type==='purchase'?'收货':'发货'}}</button>
    </div>
    <contract-form v-if="showEdit" :edit-id="Number(id)" @close="showEdit=false" @saved="load"></contract-form>
    <contract-form v-if="showRevise" :revise-from="Number(id)" @close="showRevise=false" @saved="onRevised"></contract-form>
    <fulfill-form v-if="showFulfill" :c="c" @close="showFulfill=false" @saved="load"></fulfill-form>
    <draft-confirm-form v-if="showDrafts" :c="c" @close="showDrafts=false" @saved="load"></draft-confirm-form>
    <relink-form v-if="relink" :kind="relink.kind" :doc="relink.doc" @close="relink=null" @saved="load"></relink-form><div class="card"><div class="card-h"><b>按行履约</b></div><table><thead><tr><th>物料</th><th>订购</th><th>已交付</th><th>欠交</th></tr></thead><tbody><tr v-for="l in c.fulfillment.lines"><td>{{l.material_name}}</td><td>{{l.ordered}}</td><td>{{l.delivered}}</td><td>{{l.remaining}}</td></tr></tbody></table><p v-if="c.fulfillment.unlinked_movement_ids.length" style="padding:12px">未关联明细的库存单：{{c.fulfillment.unlinked_movement_ids.join(', ')}}。请在出入库页补关联。</p></div>
    <div class="detail-grid" style="display:grid;grid-template-columns:1fr 320px;gap:16px;align-items:start">
      <div>
        <div class="card">
          <div style="padding:8px 0">
            <div class="info-row"><span class="k">{{c.contract_type==='sales'?'客户':'供应商'}}</span><span>{{c.partner_name}}<span v-if="c.is_dual_role" class="tag-dual">供+客</span></span></div>
            <div class="info-row"><span class="k">签订日期</span><span class="mono">{{c.sign_date||'—'}}</span></div>
            <div class="info-row" v-if="c.ref_contract_no"><span class="k">参考销售</span><span class="mono">{{c.ref_contract_no}}</span></div>
            <div class="info-row" v-if="c.remark"><span class="k">备注</span><span>{{c.remark}}</span></div>
            <div class="info-row" v-if="c.replaces_no"><span class="k">替代</span><span class="mono">{{c.replaces_no}}</span></div>
            <div class="info-row" v-if="c.replaced_by_no"><span class="k">被替代为</span><span class="mono">{{c.replaced_by_no}}</span></div>
          </div>
          <table>
            <thead><tr><th>#</th><th>物料</th><th>规格</th><th class="num">数量</th><th class="num">单价</th><th class="num">金额</th><th>备注</th></tr></thead>
            <tbody>
              <tr v-for="l in c.lines"><td class="mono">{{l.line_no}}</td><td>{{l.material_name}}</td><td class="mono">{{l.spec}}</td>
                <td class="num">{{l.qty}}</td><td class="num">{{l.price_tax}}</td><td class="num">{{fmt(l.amount_tax)}}</td><td class="sub-line">{{l.remark}}</td></tr>
              <tr><td colspan="5" style="text-align:right;color:var(--ink-3)">合计（含税）</td><td class="num" style="font-weight:600">{{fmt(c.amount_tax)}}</td><td></td></tr>
            </tbody>
          </table>
        </div>
        <div class="card">
          <div class="card-h"><b>出入库单据</b><span class="sub">{{c.movements.length}} 条</span>
            <button v-if="draftCount" class="btn btn-ink btn-sm" style="margin-left:auto" @click="showDrafts=true">确认草稿（{{draftCount}}）</button></div>
          <table>
            <thead><tr><th>单号</th><th>类型</th><th>物料</th><th class="num">数量</th><th>日期</th><th>状态</th><th></th></tr></thead>
            <tbody>
              <tr v-for="m in c.movements">
                <td class="mono">{{m.doc_no}}</td>
                <td>{{{in:'入库',out:'出库',opening:'期初',adjust:'调整'}[m.move_type]}}</td>
                <td>{{m.material_name}}</td>
                <td class="num">{{m.qty}}</td>
                <td class="mono">{{m.move_date}}</td>
                <td><span class="pill" :class="m.status==='confirmed'?'p-ok':m.status==='draft'?'p-draft':'p-gray'">{{{draft:'草稿',confirmed:'已确认',voided:'已作废'}[m.status]}}</span></td>
                <td style="text-align:right">
                  <button v-if="m.status==='draft'" class="btn btn-ink btn-sm" @click="confirmMove(m)">确认</button>
                  <button v-if="m.status==='confirmed'" class="btn btn-ghost btn-sm" @click="redate(m)">改日期</button>
                  <button v-if="m.status!=='voided'" class="btn btn-ghost btn-sm" @click="voidMove(m)">作废</button>
                </td>
              </tr>
              <tr v-if="!c.movements.length"><td colspan="7" class="empty">暂无出入库——点右上「按明细{{c.contract_type==='purchase'?'收货':'发货'}}」</td></tr>
            </tbody>
          </table>
        </div>
        <div class="card">
          <div class="card-h"><b>发票</b><span class="sub">进项/销项合计 {{fmt(c.invoice_sum)}}</span></div>
          <table>
            <thead><tr><th>发票号</th><th>方向</th><th>日期</th><th class="num">价税合计</th><th>文件</th><th></th></tr></thead>
            <tbody>
              <tr v-for="i in c.invoices"><td class="mono">{{i.invoice_no}}</td><td>{{i.direction==='input'?'进项':'销项'}}</td>
                <td class="mono">{{i.invoice_date}}</td><td class="num">{{fmt(i.amount_tax)}}</td><td class="sub-line">{{i.file_path}}</td>
                <td style="text-align:right"><button class="btn btn-ghost btn-sm" @click="openRelink('invoice', i)">改挂</button></td></tr>
              <tr v-if="!c.invoices.length"><td colspan="6" class="empty">未挂发票</td></tr>
            </tbody>
          </table>
        </div>
        <div class="card">
          <div class="card-h"><b>收付款</b><span class="sub">合计 {{fmt(c.payment_sum)}}</span></div>
          <table>
            <thead><tr><th>日期</th><th>方向</th><th class="num">金额</th><th>备注</th><th></th></tr></thead>
            <tbody>
              <tr v-for="p in c.payments"><td class="mono">{{p.pay_date}}</td><td>{{p.direction==='pay'?'付款':'收款'}}</td>
                <td class="num">{{fmt(p.amount)}}</td><td class="sub-line">{{p.remark}}</td>
                <td style="text-align:right"><button class="btn btn-ghost btn-sm" @click="openRelink('payment', p)">改挂</button></td></tr>
              <tr v-if="!c.payments.length"><td colspan="5" class="empty">无收付款记录</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <div>
        <div class="card">
          <div class="card-h"><b>合同原件</b>
            <select v-model="upKind" style="margin-left:auto;width:118px"><option value="signed">双方签署版</option><option value="attachment">附件</option><option value="other">其他</option></select>
            <label class="btn btn-ghost btn-sm" style="margin-left:6px">＋ 上传<input type="file" multiple accept=".pdf,.png,.jpg,.jpeg,.docx,.xlsx" style="display:none" @change="uploadFiles"></label></div>
          <div v-for="f in files" :key="f.id" style="display:flex;gap:8px;padding:9px 16px;border-bottom:1px solid var(--line);font-size:12.5px;align-items:center">
            <span class="pill" :class="f.kind==='signed'?'p-ok':'p-gray'" style="font-size:10px">{{f.kind_label}}</span>
            <div style="flex:1;min-width:0"><a :href="'/api/contract-files/'+f.id" target="_blank" style="word-break:break-all">{{f.file_name}}</a>
              <div class="sub-line" v-if="f.note">{{f.note}}</div></div>
            <button class="btn btn-ghost btn-sm" v-if="f.file_type==='pdf'" @click="stamping=f" title="加盖我方电子章（另存新文件）">盖章</button>
            <button class="btn btn-ghost btn-sm" v-if="f.kind!=='signed'" @click="setKind(f,'signed')" title="设为双方签署版">设为签署版</button>
            <button class="btn btn-ghost btn-sm" @click="delFile(f)" title="删除登记（归档文件保留）">✕</button>
          </div>
          <div v-if="!files.length" class="empty">还没有合同原件：请上传双方签字盖章的版本</div>
          <div v-else-if="!files.some(f=>f.kind==='signed')" class="empty neg">缺双方签署版</div>
        </div>
        <div class="card"><div class="card-h"><b>执行进度</b></div>
          <div style="padding:14px 18px;font-size:12.5px;display:grid;gap:8px">
            <div>签约 <b>✓</b>　{{c.sign_date}}</div>
            <div>收发货 <b>{{c.moved_lines?'✓ '+c.moved_lines+'/'+c.total_lines+' 行':'—'}}</b></div>
            <div>开票 <b>{{fmt(c.invoice_sum)}}</b> / {{fmt(c.amount_tax)}}</div>
            <div>收付 <b>{{fmt(c.payment_sum)}}</b> / {{fmt(c.amount_tax)}}</div>
          </div>
        </div>
      </div>
    </div>
    <invoice-export-form v-if="showInvExport" :c="c" @close="showInvExport=false"></invoice-export-form>
    <stamp-form v-if="stamping" :file="stamping" :contract-type="c.contract_type" @close="stamping=null" @saved="loadFiles"></stamp-form>
    <archive-form v-if="showArchive" :rel_no="c.contract_no" @close="showArchive=false" @saved="load"></archive-form>
  </div>`,
  props: { id: String },
  data: () => ({ files: [], upKind: 'signed', stamping: null, showInvExport: false, confirming: null, c: null, showEdit:false, showArchive: false, showRevise: false, showFulfill: false, showDrafts: false, relink: null }),
  computed: { draftCount() { return this.c ? this.c.movements.filter(m => m.status === 'draft').length : 0; } },
  methods: {
    fmt,
    go(k) { location.hash = '#/' + k; },
    async load() { this.c = await api.get('/api/contracts/' + this.id); this.loadFiles(); },
    openRelink(kind, d) {
      this.relink = { kind, doc: { ...d, partner_id: this.c.partner_id, contract_id: this.c.id, contract_no: this.c.contract_no } };
    },
    onRevised(newId) { if (newId) this.go('contract/' + newId); else this.load(); },
    confirmMove(m) { this.confirming = m; },
    async voidMove(m) {
      const reason = prompt('作废 ' + m.doc_no + ' 的原因（必填，记入备注；已确认单据作废会影响库存，凭证红冲）');
      if (!reason || !reason.trim()) return;
      try { await api.post(`/api/movements/${m.id}/void`, { reason }); this.load(); } catch (e) { alert(e); }
    },
    async redate(m) {
      const d = prompt('把 ' + m.doc_no + ' 的日期从 ' + m.move_date + ' 改为（YYYY-MM-DD）', m.move_date);
      if (!d || d === m.move_date) return;
      const reason = prompt('改日期原因（必填，记入备注）');
      if (!reason || !reason.trim()) return;
      try { await api.post(`/api/movements/${m.id}/redate`, { move_date: d, reason }); this.load(); } catch (e) { alert(e); }
    },
    async loadFiles() { this.files = await api.get('/api/contracts/' + this.id + '/files'); },
    async uploadFiles(ev) {
      const list = [...ev.target.files]; ev.target.value = '';
      for (const f of list) {
        const buf = new Uint8Array(await f.arrayBuffer());
        let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
        try { await api.post('/api/contracts/' + this.id + '/files', { name: f.name, content_b64: btoa(bin), kind: this.upKind }); }
        catch (e) { alert(f.name + '：' + e); }
      }
      this.loadFiles();
    },
    async setKind(f, kind) { await api.put('/api/contract-files/' + f.id, { kind }); this.loadFiles(); },
    async delFile(f) { if (confirm('删除「' + f.file_name + '」的登记？（归档目录里的文件保留）')) { await api.del('/api/contract-files/' + f.id); this.loadFiles(); } },
  },
  mounted() { this.load(); },
});
