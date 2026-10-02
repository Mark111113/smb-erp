/* owe-erp 前端（由 app.js 机械拆分，经典脚本按序加载，顺序见 index.html；依赖：base 先于 forms 先于 views 先于 app） */

/* ---------------- 库存 ---------------- */
const StockView = defineComponent({
  components: { DataTable },
  template: `
  <div>
    <div class="kpis">
      <div class="kpi c-info"><div class="kpi-l">物料数</div><div class="kpi-v">{{s.items.length}}</div></div>
      <div class="kpi c-ok"><div class="kpi-l">库存货值·不含税</div><div class="kpi-v">{{fmt(s.total_value)}}<small>元</small></div></div>
      <div class="kpi c-red"><div class="kpi-l">负库存</div><div class="kpi-v">{{s.neg_count}}<small>项</small></div></div>
      <div class="kpi c-warn"><div class="kpi-l">待确认草稿</div><div class="kpi-v">{{s.draft_count}}<small>张</small></div></div>
    </div>
    <div class="hint" v-if="s.neg_count>0">⚠ 负库存行以<span class="neg" style="font-weight:600">红字</span>标示——多由「暂估出库先于入库确认」导致，补录期初或确认入库后自动转正。</div>
    <div class="card">
      <div class="card-h"><b>库存余额表</b><span class="sub">移动加权平均 · 不含税成本 · 实时；地点只分数量，成本全公司统一</span>
        <select v-model.number="loc" style="margin-left:auto"><option :value="0">全部地点</option>
          <option v-for="l in locs" :key="l.id" :value="l.id">{{l.name}}</option></select></div>
      <div v-if="locRemark" class="hint" style="margin:12px 16px 0">{{locRemark}}</div>
      <data-table view="stock" :columns="cols" :rows="rows" export-name="库存余额" :empty="loc ? '该地点无库存' : '暂无库存数据'"
                  row-key="material_id">
        <template #toolbar><label class="sub" style="display:flex;gap:5px;align-items:center;cursor:pointer">
          <input type="checkbox" v-model="showZero" style="width:auto">含零库存</label></template>
        <template #cell-qty="{ row }"><div class="num dt-nowrap" :class="{neg: qtyOf(row) < 0}">{{qtyOf(row)}}</div></template>
        <template #cell-value="{ row }"><div class="num dt-nowrap" :class="{neg: valueOf(row) < 0}">{{fmt(valueOf(row))}}</div></template>
        <template #cell-neg="{ row }"><span v-if="qtyOf(row) < 0" class="pill p-red">负库存</span></template>
      </data-table>
    </div>
  </div>`,
  data: () => ({ s: { items: [], total_value: 0, neg_count: 0, draft_count: 0 }, locs: [], loc: 0, showZero: false }),
  computed: {
    rows() {
      if (!this.loc) return this.showZero ? this.s.items : this.s.items.filter(m => Math.abs(m.qty) > 1e-9);
      return this.s.items.map(m => ({ ...m, here: (m.locations.find(l => l.id === this.loc) || {}).qty || 0 }))
                         .filter(m => Math.abs(m.here) > 1e-9);
    },
    cols() {
      return [
        { key: 'code', label: '编码', type: 'code' },
        { key: 'name', label: '物料', type: 'text', min: 120 },
        { key: 'spec', label: '规格', type: 'text', min: 160 },
        { key: 'unit', label: '单位', type: 'short', width: 52 },
        { key: 'qty', label: this.loc ? '该地点数量' : '现存量', type: 'qty', width: 88, value: m => this.qtyOf(m) },
        { key: 'avg_cost', label: '均价·不含税', type: 'money' },
        { key: 'value', label: '货值·不含税', type: 'money', value: m => this.valueOf(m) },
        { key: 'locations', label: '地点分布', type: 'text', min: 150,
          value: m => m.locations.map(l => `${l.name} ${l.qty}`).join('；') },
        { key: 'last_date', label: '最近动向', type: 'date' },
        { key: 'neg', label: '提示', type: 'status', width: 76, sortable: false, value: m => this.qtyOf(m) < 0 ? '负库存' : '' },
      ];
    },
    locRemark() { const l = this.locs.find(x => x.id === this.loc); return l && l.remark ? l.name + '：' + l.remark : ''; },
  },
  methods: {
    fmt,
    isDefault(id) { return (this.locs.find(l => l.id === id) || {}).is_default; },
    qtyOf(m) { return this.loc ? m.here : m.qty; },
    valueOf(m) { return this.loc ? Math.round(m.here * m.avg_cost * 100) / 100 : m.value; },
    async load() { [this.s, this.locs] = await Promise.all([api.get('/api/stock'), api.get('/api/locations')]); },
  },
  mounted() { this.load(); },
});

/* ---------------- 改出入库单的存放地点（只影响地点数量，不影响成本凭证） ---------------- */
const RelocateForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:440px">
      <div class="modal-h"><b>改存放地点 · {{m.doc_no}}</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="form-grid" style="grid-template-columns:1fr">
        <div class="fg"><label>{{m.material_name}} × {{m.qty}}（{{m.move_type==='out'?'从哪里发出':'收到哪里'}}）</label>
          <select v-model.number="to"><option v-for="l in locs.filter(x=>x.active)" :key="l.id" :value="l.id">{{l.name}}{{l.is_default?'（默认）':''}}</option></select></div>
        <div class="fg sub-line">登记错了地点用这里改；货后来从一处挪到另一处，请用「库存调拨」。</div>
        <div v-if="err" class="fg auth-error">{{err}}</div>
      </div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button>
        <button class="btn btn-ink" :disabled="busy" @click="save">保存</button></div>
    </div>
  </div>`,
  props: { m: Object },
  emits: ['close', 'saved'],
  data: () => ({ locs: [], to: null, err: '', busy: false }),
  methods: {
    async save() {
      this.busy = true; this.err = '';
      try { await api.post(`/api/movements/${this.m.id}/location`, { location_id: this.to }); this.$emit('saved'); this.$emit('close'); }
      catch (e) { this.err = String(e); } finally { this.busy = false; }
    },
  },
  async mounted() {
    this.locs = await api.get('/api/locations');
    this.to = this.m.location_id || (this.locs.find(l => l.is_default) || {}).id;
  },
});

/* ---------------- 出入库 ---------------- */
const MovementsView = defineComponent({
  components: { MovementForm, ConfirmMoveForm, RelocateForm, DataTable },
  template: `
  <div>
    <confirm-move-form v-if="confirming" :m="confirming" @close="confirming=null" @saved="load"></confirm-move-form>
    <relocate-form v-if="relocating" :m="relocating" @close="relocating=null" @saved="load"></relocate-form>
    <div class="filter-bar">
      <select v-model="f.status"><option value="">全部状态</option><option value="draft">草稿</option><option value="confirmed">已确认</option><option value="voided">已作废</option></select>
      <select v-model="f.type"><option value="">全部类型</option><option value="in">入库</option><option value="out">出库</option><option value="opening">期初</option><option value="adjust">调整</option></select>
      <button class="btn btn-ink" style="margin-left:auto" @click="showNew=true">＋ 新建单据</button>
    </div>
    <div class="hint hint-blue" v-if="drafts">💡 有 {{drafts}} 张草稿待确认——草稿不影响库存，确认后才按移动加权平均过账。</div>
    <div class="card">
      <data-table view="movements" date-key="move_date" :columns="cols" :rows="list" export-name="出入库" empty="无单据"
                  :row-class="m => m.status==='voided' ? 'dim' : ''" :menu="menuItems">
        <template #cell-move_type="{ row }"><span class="pill" :class="typePill[row.move_type]" :title="typeName[row.move_type]">{{typeName[row.move_type]}}</span></template>
        <template #cell-contract_no="{ row }">
          <div v-if="row.contract_id" class="mono dt-nowrap clickable" :title="row.contract_no" @click="go('contract/'+row.contract_id)">{{row.contract_no}}</div>
          <div v-else class="sub-line">不挂合同</div></template>
        <template #cell-status="{ row }"><span class="pill" :class="statusPill[row.status]">{{statusName[row.status]}}</span></template>
        <template #cell-actions="{ row }">
          <button v-if="row.status==='draft'" class="btn btn-ink btn-sm" @click="confirmMove(row)">确认</button>
        </template>
      </data-table>
    </div>
    <movement-form v-if="showNew" @close="showNew=false" @saved="load"></movement-form>
  </div>`,
  data: () => ({ confirming: null, relocating: null, locs: [], list: [], showNew: false, f: { status: '', type: '' }, drafts: 0,
                 typeName: { in: '入库', out: '出库', opening: '期初', adjust: '调整', return_in: '销售退货', return_out: '采购退货', wo_issue: '工单领料', wo_return: '工单退料', wo_receipt: '完工入库', wo_cost: '成本分摊' },
                 typePill: { in: 'p-info', out: 'p-ok', opening: 'p-gray', adjust: 'p-gray', return_in: 'p-draft', return_out: 'p-draft', wo_issue: 'p-red', wo_return: 'p-draft', wo_receipt: 'p-info', wo_cost: 'p-gray' },
                 statusName: { draft: '草稿', confirmed: '已确认', voided: '已作废' },
                 statusPill: { draft: 'p-draft', confirmed: 'p-ok', voided: 'p-gray' } }),
  computed: {
    /* 一列一字段；默认可见列按 1440 宽不出横向滚动挑选，其余在「列设置」打开 */
    cols() {
      const who = m => m.confirmed_by || (m.status === 'confirmed' ? m.created_by : '');
      return [
        { key: 'doc_no', label: '单号', type: 'id', width: 136 },
        { key: 'move_date', label: '日期', type: 'date', width: 92 },
        { key: 'move_type', label: '类型', type: 'status', width: 72, value: m => this.typeName[m.move_type] },
        { key: 'contract_no', label: '合同', type: 'id', width: 116, value: m => m.contract_no || '' },
        { key: 'partner_short', label: '对方', type: 'partner', width: 80, title: m => m.partner_name },
        { key: 'partner_name', label: '对方全称', type: 'text', hidden: true },
        { key: 'material_code', label: '物料编码', type: 'code', hidden: true },
        { key: 'material_name', label: '物料', type: 'text', min: 110 },
        { key: 'spec', label: '规格', type: 'text', min: 110 },
        { key: 'qty', label: '数量', type: 'qty', width: 60 },
        { key: 'unit', label: '单位', type: 'short', hidden: true },
        { key: 'unit_cost', label: '单价·不含税', type: 'money', width: 96 },
        { key: 'amount', label: '金额·不含税', type: 'money', hidden: true, value: m => m.unit_cost == null ? null : Math.round(m.qty * m.unit_cost * 100) / 100 },
        { key: 'location', label: '地点', type: 'text', width: 96, value: m => this.locName(m.location_id) },
        { key: 'created_by', label: '录入人', type: 'person', hidden: true },
        { key: 'confirmed_by', label: '确认人', type: 'person', hidden: true, value: who },
        { key: 'remark', label: '备注', type: 'text', hidden: true },
        { key: 'status', label: '状态', type: 'status', width: 80, value: m => this.statusName[m.status] },
        { key: 'actions', label: '', type: 'actions', width: 96, sortable: false },
      ];
    },
  },
  methods: {
    fmt,
    go(k) { location.hash = '#/' + k; },
    menuItems(m) {
      const a = [];
      if (m.status === 'confirmed' && ['in', 'out'].includes(m.move_type)) a.push({ k: 'return', label: '退货', run: () => this.returnMove(m) });
      if (m.contract_id && !m.contract_line_id) a.push({ k: 'link', label: '补关联合同明细', run: () => this.linkLine(m) });
      if (m.status !== 'voided') a.push({ k: 'loc', label: '改地点', run: () => { this.relocating = m; } });
      if (m.status === 'confirmed') a.push({ k: 'redate', label: '改日期', run: () => this.redate(m) });
      if (m.status !== 'voided') a.push({ k: 'void', label: '作废', danger: true, run: () => this.voidMove(m) });
      return a;
    },
    locName(id) { const l = id ? this.locs.find(x => x.id === id) : this.locs.find(x => x.is_default); return l ? l.name : '—'; },
    async load() {
      this.locs = await api.get('/api/locations');
      this.list = await api.get(`/api/movements?status=${this.f.status}&mtype=${this.f.type}`);
      const s = await api.get('/api/stock'); this.drafts = s.draft_count;
    },
    confirmMove(m) { this.confirming = m; },
    async returnMove(m){const qty=Number(prompt('退货数量'));if(!(qty>0))return;const d=prompt('退货日期',today());if(!d)return;const reason=prompt('退货原因');if(!reason?.trim())return;try{await api.post('/api/movements/'+m.id+'/return',{qty,move_date:d,reason});await this.load()}catch(e){alert(e)}},
    async linkLine(m){try{const c=await api.get('/api/contracts/'+m.contract_id);const lines=c.lines.filter(x=>x.material_id===m.material_id);const id=Number(prompt('对应合同明细ID：'+lines.map(x=>x.id+' / '+x.material_name+' 数量 '+x.qty).join('; ')));if(!id)return;await api.post('/api/movements/'+m.id+'/link-line',{contract_line_id:id});await this.load()}catch(e){alert(e)}},
    async voidMove(m) {
      const reason = prompt('作废 ' + m.doc_no + ' 的原因（必填，记入备注；原单保留、凭证红冲）');
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
  },
  watch: { f: { deep: true, handler() { this.load(); } } },
  mounted() { this.load(); },
});

/* ---------------- 库存调拨：只挪地点，不改成本、不出凭证 ---------------- */
const TransferForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:620px">
      <div class="modal-h"><b>新建调拨</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="form-grid" style="grid-template-columns:1fr 1fr">
        <div class="fg span2"><label>物料（可按编码/规格/对方料号搜）</label>
          <input class="mono" v-model="q" placeholder="搜索" style="margin-bottom:4px">
          <select v-model.number="f.material_id"><option v-for="m in filtered" :key="m.id" :value="m.id">{{m.code}} · {{m.name}} {{m.spec}}</option></select></div>
        <div class="fg"><label>调出地点</label><select v-model.number="f.from_location_id">
          <option v-for="l in active" :key="l.id" :value="l.id">{{l.name}}（现存 {{here(l.id)}}）</option></select></div>
        <div class="fg"><label>调入地点</label><select v-model.number="f.to_location_id">
          <option v-for="l in active" :key="l.id" :value="l.id" :disabled="l.id===f.from_location_id">{{l.name}}</option></select></div>
        <div class="fg"><label>数量</label><input class="mono" type="number" min="0" step="any" v-model.number="f.qty"></div>
        <div class="fg"><label>调拨日期</label><input type="date" v-model="f.move_date"></div>
        <div class="fg span2"><label>备注</label><input v-model="f.remark" placeholder="如：快递单号、经手人"></div>
        <div class="fg span2"><label style="display:flex;gap:6px;align-items:center;color:var(--ink);font-size:13px">
          <input type="checkbox" v-model="f.confirm" style="width:auto">直接确认（不勾则存草稿，货到后再确认）</label></div>
        <div v-if="warn" class="fg span2 hint">{{warn}}</div>
        <div v-if="err" class="fg span2 auth-error">{{err}}</div>
      </div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button>
        <button class="btn btn-ink" :disabled="busy || !(f.qty>0) || !f.material_id" @click="save">{{f.confirm?'确认调拨':'存草稿'}}</button></div>
    </div>
  </div>`,
  emits: ['close', 'saved'],
  data: () => ({ mats: [], locs: [], stock: [], q: '', err: '', busy: false,
                 f: { material_id: null, qty: null, from_location_id: null, to_location_id: null, move_date: today(), remark: '', confirm: true } }),
  computed: {
    active() { return this.locs.filter(l => l.active); },
    filtered() {
      const n = s => (s || '').toUpperCase().replace(/[\s\/／,，;；()（）\[\]【】\-_.·]+/g, '');
      const q = n(this.q);
      return q ? this.mats.filter(m => m.id === this.f.material_id ||
        [m.code, m.name, m.spec, ...(m.aliases || []).flatMap(a => [a.alias_name, a.alias_spec])].map(n).join('|').includes(q)) : this.mats;
    },
    warn() {
      const h = this.here(this.f.from_location_id);
      return this.f.qty > h + 1e-9 ? `调出地点现存 ${h}，调走 ${this.f.qty} 后为负。确认货确实在那里再调。` : '';
    },
  },
  watch: { q() { if (this.filtered.length && !this.filtered.some(m => m.id === this.f.material_id)) this.f.material_id = this.filtered[0].id; } },
  methods: {
    here(lid) { const r = this.stock.find(x => x.material_id === this.f.material_id && x.location_id === lid); return r ? r.qty : 0; },
    async save() {
      this.busy = true; this.err = '';
      try {
        const r = await api.post('/api/transfers', this.f);
        if (r.short_on_date != null) alert(`已调拨。注意：调出地点在 ${this.f.move_date} 当天为 ${r.short_on_date}，请核对货是否确实在那里。`);
        this.$emit('saved'); this.$emit('close');
      } catch (e) { this.err = String(e); } finally { this.busy = false; }
    },
  },
  async mounted() {
    [this.mats, this.locs, this.stock] = await Promise.all([api.get('/api/materials'), api.get('/api/locations'), api.get('/api/locations/stock')]);
    const d = this.locs.find(l => l.is_default);
    const withStock = this.stock[0];
    this.f.material_id = withStock ? withStock.material_id : (this.mats[0] || {}).id;
    this.f.from_location_id = withStock ? withStock.location_id : (d || {}).id;
    this.f.to_location_id = (this.active.find(l => l.id !== this.f.from_location_id) || {}).id;
  },
});

const TransfersView = defineComponent({
  components: { TransferForm, DataTable },
  template: `
  <div>
    <div class="filter-bar">
      <span class="sub">货从一处挪到另一处（如供应商寄存 → 主仓库）；只改地点数量，不影响成本和凭证</span>
      <button class="btn btn-ink" style="margin-left:auto" @click="showNew=true">＋ 新建调拨</button>
    </div>
    <div class="card">
      <data-table view="transfers" date-key="move_date" :columns="cols" :rows="list" export-name="库存调拨" empty="暂无调拨"
                  :row-class="t => t.status==='voided' ? 'dim' : ''" :menu="t => t.status==='voided' ? [] : [{ label: '作废', danger: true, run: () => voidT(t) }]">
        <template #cell-status="{ row }"><span class="pill" :class="row.status==='confirmed'?'p-ok':row.status==='draft'?'p-draft':'p-gray'">{{statusName[row.status]}}</span></template>
        <template #cell-actions="{ row }"><button v-if="row.status==='draft'" class="btn btn-ink btn-sm" @click="confirmT(row)">确认</button></template>
      </data-table>
    </div>
    <transfer-form v-if="showNew" @close="showNew=false" @saved="load"></transfer-form>
  </div>`,
  data: () => ({ list: [], showNew: false, statusName: { draft: '草稿', confirmed: '已确认', voided: '已作废' } }),
  computed: {
    cols() {
      return [
        { key: 'doc_no', label: '单号', type: 'id', width: 136 },
        { key: 'move_date', label: '日期', type: 'date' },
        { key: 'code', label: '物料编码', type: 'code', hidden: true },
        { key: 'material_name', label: '物料', type: 'text', min: 110 },
        { key: 'spec', label: '规格', type: 'text', min: 130 },
        { key: 'qty', label: '数量', type: 'qty', width: 64 },
        { key: 'from_name', label: '调出', type: 'text', width: 120 },
        { key: 'to_name', label: '调入', type: 'text', width: 120 },
        { key: 'remark', label: '备注', type: 'text' },
        { key: 'created_by', label: '录入人', type: 'person', hidden: true },
        { key: 'status', label: '状态', type: 'status', width: 76, value: t => this.statusName[t.status] },
        { key: 'actions', label: '', type: 'actions', width: 96, sortable: false },
      ];
    },
  },
  methods: {
    async load() { this.list = await api.get('/api/transfers'); },
    async confirmT(t) {
      const d = prompt('调拨日期（实际到货日）', t.move_date); if (!d) return;
      try { const r = await api.post(`/api/transfers/${t.id}/confirm`, { move_date: d });
        if (r.short_on_date != null) alert(`已确认。注意：调出地点当天为 ${r.short_on_date}`); this.load(); }
      catch (e) { alert('确认失败：' + e); }
    },
    async voidT(t) { if (confirm('作废该调拨单？')) { try { await api.post(`/api/transfers/${t.id}/void`); this.load(); } catch (e) { alert(e); } } },
  },
  mounted() { this.load(); },
});
