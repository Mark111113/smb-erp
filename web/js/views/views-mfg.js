/* 制造（v0.35）：BOM 管理、工单（自制/委外/研发领料）、月末成本结转、报价成本测算、研发辅助账、批次库存、关联交易
   规划：docs/PLAN_manufacturing.md；后端 app/mfg.py */

const MFG_KIND = { production: '自制', subcontract: '委外', rd: '研发领料' };
const MFG_STATUS = { released: '进行中', closed: '已关闭', cancelled: '已取消' };
const MFG_MOVE = { wo_issue: '领料', wo_return: '退料', wo_receipt: '完工入库', wo_cost: '成本分摊' };
const MFG_RULE = '编码规则：改动影响外形、安装或功能（与旧件不能混用）= 建新料号；不影响互换的内部改动（换替代料、调位号）= 只升 BOM 版本';
const readFileB64 = async (f) => {
  const buf = new Uint8Array(await f.arrayBuffer());
  let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return btoa(bin);
};

/* 物料搜索下拉：编码 / 名称 / 规格 / 制造商料号，最多列 60 条 */
const MatPick = defineComponent({
  props: { modelValue: Number, materials: Array, placeholder: { type: String, default: '搜编码 / 名称 / 规格 / 制造商料号' } },
  emits: ['update:modelValue'],
  template: `<div style="display:flex;gap:4px;min-width:260px">
    <input v-model="q" :placeholder="placeholder" style="width:42%">
    <select :value="modelValue" @change="$emit('update:modelValue', Number($event.target.value))" style="flex:1">
      <option v-if="!modelValue" :value="0">（选物料）</option>
      <option v-for="m in options" :key="m.id" :value="m.id">{{m.code}} {{m.name}} {{m.spec}}{{m.mpn ? ' · ' + m.mpn : ''}}</option>
    </select></div>`,
  data: () => ({ q: '' }),
  computed: {
    options() {
      const q = this.q.trim().toUpperCase();
      const hit = m => !q || [m.code, m.name, m.spec, m.mpn].some(x => (x || '').toUpperCase().includes(q));
      const list = (this.materials || []).filter(hit).slice(0, 60);
      const cur = (this.materials || []).find(m => m.id === this.modelValue);
      return cur && !list.includes(cur) ? [cur, ...list] : list;
    },
  },
});

/* ---------------- BOM 管理 ---------------- */
const BomView = defineComponent({
  components: { DataTable, MatPick },
  template: `
  <div>
    <div class="filter-bar">
      <label class="sub">产品</label>
      <mat-pick v-model="productId" :materials="products" placeholder="搜产品"></mat-pick>
      <span class="sub" v-if="productId">版本 {{versions.length}} 个</span>
      <span style="margin-left:auto"></span>
      <button class="btn btn-ghost" :disabled="!productId" @click="newVersion(null)">＋ 新建空白版本</button>
      <label class="btn btn-ghost" :class="{disabled: !productId}">导入 BOM（Excel/CSV）<input type="file" accept=".xlsx,.xls,.csv" style="display:none" :disabled="!productId" @change="pickImport"></label>
    </div>
    <div class="card" v-if="!productId" style="padding:14px"><span class="sub">先选产品。BOM 一个产品可以有多个版本：草稿可改，启用后锁定；要改生效版本就「复制为新版本」改完再启用（旧版自动作废，工单仍按下单时的版本）。<br><b>{{rule}}</b>。</span></div>
    <div class="card" v-if="productId">
      <table><thead><tr><th>版本</th><th>状态</th><th>生效日期</th><th class="num">行数</th><th>变更说明</th><th>来源</th><th></th></tr></thead>
        <tbody><tr v-for="v in versions" :key="v.id" :class="{ 'dt-open': cur && cur.id===v.id }" class="clickable-row" @click="openVersion(v.id)">
          <td class="mono">{{v.version}}</td><td><span class="pill" :class="v.status==='active'?'p-ok':v.status==='draft'?'p-draft':'p-gray'">{{statusName[v.status]}}</span></td>
          <td class="mono">{{v.effective_date}}</td><td class="num">{{v.line_count}}</td><td class="sub-line">{{v.change_note}}</td><td class="sub-line">{{v.source}}</td>
          <td><button class="btn btn-ghost btn-sm" @click.stop="newVersion(v.id)">复制为新版本</button></td></tr>
          <tr v-if="!versions.length"><td colspan="7" class="empty">这个产品还没有 BOM</td></tr></tbody></table>
    </div>
    <div class="card" v-if="cur">
      <div class="card-h"><b>{{cur.product_code}} {{cur.product_name}} · {{cur.version}}</b>
        <span class="pill" :class="cur.status==='active'?'p-ok':cur.status==='draft'?'p-draft':'p-gray'">{{statusName[cur.status]}}</span>
        <span class="sub">损耗率空着就用物料默认损耗率；同一替代组只有「主料」参与领料和成本</span>
        <span style="margin-left:auto"></span>
        <template v-if="cur.status==='draft'">
          <button class="btn btn-ghost btn-sm" @click="rows.push(blank())">＋ 加一行</button>
          <button class="btn btn-ghost btn-sm" @click="saveLines">保存草稿</button>
          <button class="btn btn-ink btn-sm" @click="activate">启用此版本</button>
          <button class="btn btn-ghost btn-sm" @click="remove">删除草稿</button>
        </template>
      </div>
      <div class="fg" v-if="cur.status==='draft'" style="padding:0 14px"><label>变更说明（ECN）</label><input v-model="note"></div>
      <table>
        <thead><tr><th style="width:36px">#</th><th>物料</th><th class="num" style="width:80px">单台用量</th><th class="num" style="width:80px">损耗率</th><th style="width:160px">位号</th><th style="width:60px">面</th><th style="width:70px">替代组</th><th style="width:50px">主料</th><th></th></tr></thead>
        <tbody>
          <tr v-for="(l, i) in rows" :key="i">
            <td class="mono sub">{{i+1}}</td>
            <td><mat-pick v-if="cur.status==='draft'" v-model="l.material_id" :materials="materials"></mat-pick>
                <span v-else><span class="mono">{{l.code}}</span> {{l.name}} <span class="sub">{{l.spec}} {{l.mpn}}</span></span></td>
            <td class="num"><input v-if="cur.status==='draft'" v-model.number="l.qty" style="width:70px;text-align:right"><span v-else>{{l.qty}}</span></td>
            <td class="num"><input v-if="cur.status==='draft'" v-model="l.loss_rate" placeholder="默认" style="width:64px;text-align:right"><span v-else>{{pct(l.eff_loss)}}</span></td>
            <td><input v-if="cur.status==='draft'" v-model="l.ref_des" style="width:150px"><span v-else class="sub-line">{{l.ref_des}}</span></td>
            <td><select v-if="cur.status==='draft'" v-model="l.side"><option value=""></option><option>TOP</option><option>BOT</option><option>THT</option></select><span v-else>{{l.side}}</span></td>
            <td><input v-if="cur.status==='draft'" v-model="l.alt_group" style="width:60px"><span v-else>{{l.alt_group}}</span></td>
            <td><input v-if="cur.status==='draft'" type="checkbox" v-model="l.is_primary" style="width:auto"><span v-else>{{l.alt_group ? (l.is_primary ? '主' : '替') : ''}}</span></td>
            <td><button v-if="cur.status==='draft'" class="btn btn-ghost btn-sm" @click="rows.splice(i,1)">✕</button></td>
          </tr>
          <tr v-if="!rows.length"><td colspan="9" class="empty">没有明细</td></tr>
        </tbody>
      </table>
    </div>
    <div class="modal-mask" v-if="imp" @click.self="imp=null">
      <div class="modal" style="width:900px">
        <div class="modal-h"><b>导入 BOM：{{imp.name}}</b><span class="x" @click="imp=null">✕</span></div>
        <div class="form-grid" style="grid-template-columns:1fr 1fr 1fr">
          <div class="fg"><label>新版本号</label><input v-model="imp.version"></div>
          <div class="fg"><label>没对上的物料</label><select v-model="imp.create_group"><option value="">先不建（导入会被拒）</option><option v-for="(n, g) in groups" :key="g" :value="g">按「{{g}} {{n}}」新建</option></select></div>
          <div class="fg"><label>&nbsp;</label><span class="sub">共 {{imp.res ? imp.res.total : '…'}} 行，没对上 {{imp.res ? imp.res.missing : '…'}} 行</span></div>
        </div>
        <div style="max-height:380px;overflow:auto">
          <table v-if="imp.res"><thead><tr><th>位号</th><th>编码</th><th>制造商料号</th><th>名称 / 规格</th><th class="num">用量</th><th>对上的物料</th></tr></thead>
            <tbody><tr v-for="(r, i) in imp.res.rows" :key="i" :class="{neg: !r.material_id}">
              <td class="sub-line">{{r.ref_des}}</td><td class="mono">{{r.code}}</td><td class="mono">{{r.mpn}}</td><td>{{r.name}} <span class="sub">{{r.spec}}</span></td>
              <td class="num">{{r.qty}}</td><td>{{r.matched || '（没对上）'}}</td></tr></tbody></table>
        </div>
        <div class="sub-line">识别的表头：物料编码 / 制造商料号（至少一列）、用量（必需）、位号、名称、规格、封装、损耗、替代组、贴装面。导入生成的是草稿版本，核对后再启用。</div>
        <div class="actions"><button class="btn btn-ghost" @click="imp=null">取消</button><button class="btn btn-ink" @click="applyImport">生成草稿版本</button></div>
      </div>
    </div>
  </div>`,
  props: { id: [String, Number] },
  data: () => ({ materials: [], productId: 0, versions: [], cur: null, rows: [], note: '', imp: null, groups: {}, rule: MFG_RULE,
                 statusName: { draft: '草稿', active: '生效', obsolete: '作废' } }),
  computed: {
    products() { return this.materials.filter(m => ['finished', 'semi', 'goods'].includes(m.material_type || 'goods')); },
  },
  watch: { productId() { this.cur = null; this.loadVersions(); } },
  methods: {
    pct(x) { return x ? (x * 100).toFixed(1) + '%' : ''; },
    blank() { return { material_id: 0, qty: 1, loss_rate: '', ref_des: '', side: '', alt_group: '', is_primary: true }; },
    async loadVersions() { this.versions = this.productId ? await api.get('/api/boms?product_id=' + this.productId) : []; },
    async openVersion(id) {
      this.cur = await api.get('/api/boms/' + id);
      this.note = this.cur.change_note;
      this.rows = this.cur.lines.map(l => ({ ...l, loss_rate: l.loss_rate == null ? '' : l.loss_rate }));
    },
    async newVersion(copyFrom) {
      const n = this.versions.length + 1;
      const v = prompt('新版本号', 'V' + n);
      if (!v) return;
      try { const r = await api.post('/api/boms', { product_id: this.productId, version: v, copy_from: copyFrom, change_note: copyFrom ? '复制自 ' + (this.versions.find(x => x.id === copyFrom) || {}).version : '' }); await this.loadVersions(); this.openVersion(r.id); }
      catch (e) { alert(String(e)); }
    },
    payload() {
      return this.rows.filter(l => l.material_id).map(l => ({ material_id: l.material_id, qty: Number(l.qty), loss_rate: l.loss_rate === '' || l.loss_rate == null ? null : Number(l.loss_rate),
        ref_des: l.ref_des || '', side: l.side || '', alt_group: l.alt_group || '', is_primary: !!l.is_primary || !l.alt_group }));
    },
    async saveLines() { try { await api.put('/api/boms/' + this.cur.id + '/lines', { lines: this.payload(), change_note: this.note }); await this.openVersion(this.cur.id); this.loadVersions(); } catch (e) { alert(String(e)); } },
    async activate() {
      if (!confirm('启用后这个版本锁定，产品原来的生效版本自动作废。确定？')) return;
      try { await api.put('/api/boms/' + this.cur.id + '/lines', { lines: this.payload(), change_note: this.note }); await api.post('/api/boms/' + this.cur.id + '/activate', {}); await this.openVersion(this.cur.id); this.loadVersions(); }
      catch (e) { alert(String(e)); }
    },
    async remove() { if (!confirm('删除这个草稿版本？')) return; try { await api.del('/api/boms/' + this.cur.id); this.cur = null; this.loadVersions(); } catch (e) { alert(String(e)); } },
    async pickImport(ev) {
      const f = ev.target.files[0]; ev.target.value = '';
      if (!f) return;
      this.imp = { name: f.name, b64: await readFileB64(f), version: 'V' + (this.versions.length + 1), create_group: '', res: null };
      try { this.imp.res = await api.post('/api/boms/import', { product_id: this.productId, version: this.imp.version, name: f.name, content_b64: this.imp.b64 }); }
      catch (e) { alert(String(e)); this.imp = null; }
    },
    async applyImport() {
      try {
        const r = await api.post('/api/boms/import', { product_id: this.productId, version: this.imp.version, name: this.imp.name, content_b64: this.imp.b64, apply: true, create_group: this.imp.create_group });
        this.imp = null; await this.loadVersions(); this.materials = await api.get('/api/materials'); this.openVersion(r.bom_id);
      } catch (e) { alert(String(e)); }
    },
  },
  async mounted() {
    [this.materials, this.groups] = await Promise.all([api.get('/api/materials'), api.get('/api/mfg/meta').then(m => m.component_groups)]);
    if (this.id) this.productId = Number(this.id);
  },
});

/* ---------------- 工单列表 ---------------- */
const WorkOrdersView = defineComponent({
  components: { DataTable, MatPick },
  template: `
  <div>
    <div class="filter-bar">
      <button v-for="(n, k) in kinds" :key="k" class="btn btn-sm" :class="kind===k?'btn-ink':'btn-ghost'" @click="kind=k">{{n}}</button>
      <select v-model="status" style="width:110px"><option value="">全部状态</option><option value="released">进行中</option><option value="closed">已关闭</option><option value="cancelled">已取消</option></select>
      <span style="margin-left:auto"></span>
      <button class="btn btn-ink" @click="openNew">＋ 新建工单</button>
    </div>
    <div class="card">
      <data-table view="workorders" date-key="plan_start" :columns="cols" :rows="list" export-name="工单" empty="还没有工单" :row-click="w => go('workorder/' + w.id)">
        <template #cell-status="{ row }"><span class="pill" :class="row.status==='released'?'p-draft':row.status==='closed'?'p-ok':'p-gray'">{{statusName[row.status]}}</span></template>
      </data-table>
    </div>
    <div class="modal-mask" v-if="f" @click.self="f=null">
      <div class="modal" style="width:680px">
        <div class="modal-h"><b>新建工单</b><span class="x" @click="f=null">✕</span></div>
        <div class="form-grid" style="grid-template-columns:1fr 1fr">
          <div class="fg"><label>类型</label><select v-model="f.kind"><option value="production">自制</option><option value="subcontract">委外加工</option><option value="rd">研发领料</option></select></div>
          <div class="fg"><label>计划开工</label><input type="date" v-model="f.plan_start"></div>
          <div class="fg span2" v-if="f.kind!=='rd'"><label>产品</label><mat-pick v-model="f.product_id" :materials="products" placeholder="搜产品"></mat-pick></div>
          <div class="fg" v-if="f.kind!=='rd'"><label>BOM 版本</label><select v-model="f.bom_id"><option :value="null">{{f.kind==='subcontract'?'（不按 BOM 发料）':'（用生效版本）'}}</option><option v-for="b in boms" :key="b.id" :value="b.id">{{b.version}} {{b.status==='active'?'· 生效':''}}</option></select></div>
          <div class="fg" v-if="f.kind!=='rd'"><label>计划数量</label><input type="number" v-model.number="f.qty"></div>
          <div class="fg" v-if="f.kind==='production'"><label>生产线（成本中心）</label><select v-model="f.cost_center_id"><option :value="null">（默认 生产）</option><option v-for="c in meta.production_centers" :key="c.id" :value="c.id">{{c.code}} {{c.name}}</option></select></div>
          <div class="fg" v-if="f.kind==='subcontract'"><label>委外加工厂</label><select v-model="f.partner_id"><option v-for="p in partners" :key="p.id" :value="p.id">{{p.short_name || p.name}}</option></select></div>
          <div class="fg" v-if="f.kind==='subcontract'"><label>加工费单价（不含税）</label><input type="number" step="0.01" v-model.number="f.fee_price"></div>
          <div class="fg span2" v-if="f.kind==='rd'"><label>研发项目</label><select v-model="f.cost_center_id"><option v-for="c in meta.rd_projects" :key="c.id" :value="c.id">{{c.code}} {{c.name}}</option></select>
            <span class="sub" v-if="!meta.rd_projects.length">还没有研发项目：到 财务 → 成本中心，在「CC20 研发」下加子项（一个项目一个）</span></div>
          <div class="fg span2"><label>备注</label><input v-model="f.remark"></div>
        </div>
        <div class="actions"><button class="btn btn-ghost" @click="f=null">取消</button><button class="btn btn-ink" @click="create">创建</button></div>
      </div>
    </div>
  </div>`,
  data: () => ({ list: [], kind: '', status: 'released', f: null, materials: [], partners: [], boms: [],
                 meta: { production_centers: [], rd_projects: [] }, kinds: { '': '全部', production: '自制', subcontract: '委外', rd: '研发领料' },
                 statusName: MFG_STATUS }),
  computed: {
    products() { return this.materials.filter(m => ['finished', 'semi', 'goods'].includes(m.material_type || 'goods')); },
    cols() {
      return [
        { key: 'wo_no', label: '工单号', type: 'id' },
        { key: 'kind', label: '类型', type: 'status', width: 70, value: w => MFG_KIND[w.kind] },
        { key: 'status', label: '状态', type: 'status', width: 70, value: w => MFG_STATUS[w.status] },
        { key: 'product', label: '产品 / 研发项目', type: 'text', min: 200, value: w => w.kind === 'rd' ? w.cost_center : (w.product_code + ' ' + w.product_name + ' ' + w.product_spec) },
        { key: 'bom_version', label: 'BOM', type: 'short', width: 52 },
        { key: 'qty', label: '计划', type: 'qty' },
        { key: 'received', label: '已入库', type: 'qty' },
        { key: 'hours', label: '工时', type: 'qty' },
        { key: 'plan_start', label: '计划开工', type: 'date' },
        { key: 'partner', label: '委外厂', type: 'partner', hidden: true },
        { key: 'cost_center', label: '成本中心', type: 'short', hidden: true },
        { key: 'remark', label: '备注', type: 'text', hidden: true },
      ];
    },
  },
  watch: { kind() { this.load(); }, status() { this.load(); }, 'f.product_id'(v) { this.loadBoms(v); } },
  methods: {
    go(k) { location.hash = '#/' + k; },
    async load() { this.list = await api.get(`/api/work-orders?kind=${this.kind}&status=${this.status}`); },
    async loadBoms(pid) { this.boms = pid ? (await api.get('/api/boms?product_id=' + pid)).filter(b => b.status !== 'draft') : []; },
    async openNew() {
      if (!this.materials.length) [this.materials, this.partners, this.meta] = await Promise.all([api.get('/api/materials'), api.get('/api/partners'), api.get('/api/mfg/meta')]);
      this.f = { kind: this.kind || 'production', product_id: 0, bom_id: null, qty: 1, plan_start: today(), cost_center_id: null, partner_id: null, fee_price: 0, remark: '' };
    },
    async create() {
      const d = { ...this.f, product_id: this.f.product_id || null };
      try { const w = await api.post('/api/work-orders', d); this.f = null; this.go('workorder/' + w.id); }
      catch (e) { alert(String(e)); }
    },
  },
  mounted() { this.load(); },
});

/* ---------------- 工单详情 ---------------- */
const WorkOrderView = defineComponent({
  components: { MatPick },
  props: { id: [String, Number] },
  template: `
  <div v-if="w">
    <div class="filter-bar">
      <a class="clickable sub" @click="go('workorders')">‹ 工单</a>
      <b class="mono">{{w.wo_no}}</b><span class="pill p-info">{{kindName[w.kind]}}</span>
      <span class="pill" :class="w.status==='released'?'p-draft':w.status==='closed'?'p-ok':'p-gray'">{{statusName[w.status]}}</span>
      <span v-if="w.kind!=='rd'">{{w.product_code}} {{w.product_name}} <span class="sub">{{w.product_spec}}</span> · BOM {{w.bom_version || '—'}} · 计划 {{w.qty}} · 已入库 {{w.received}}</span>
      <span v-else>研发项目 {{w.cost_center}}</span>
      <span v-if="w.kind==='subcontract'" class="sub">· 委外 {{w.partner}} 加工费 {{fmt(w.fee_price)}}/件</span>
      <span style="margin-left:auto"></span>
      <template v-if="w.status==='released'">
        <button class="btn btn-ink btn-sm" v-if="w.bom_id" @click="issueBom">按 BOM 领料</button>
        <button class="btn btn-ghost btn-sm" @click="openLines('issue')">手工领料</button>
        <button class="btn btn-ghost btn-sm" @click="openLines('return')">退料</button>
        <button class="btn btn-ghost btn-sm" v-if="w.kind==='production'" @click="report">报工</button>
        <button class="btn btn-ghost btn-sm" v-if="w.kind!=='rd'" @click="receipt">完工入库</button>
        <button class="btn btn-ghost btn-sm" @click="act('close', '关闭后最后一张完工入库单吃掉全部在制余额，不能再领料。确定关闭？')">关闭</button>
        <button class="btn btn-ghost btn-sm" @click="act('cancel', '取消工单？（要求没有领料净额和完工入库）')">取消</button>
      </template>
      <button v-if="w.status==='closed'" class="btn btn-ghost btn-sm" @click="act('reopen', '重开后可继续领料、作废入库单；关单时成本会重新计算。确定？')">重开</button>
    </div>
    <div class="kpis" style="grid-template-columns:repeat(5,1fr)">
      <div class="kpi"><div class="kpi-l">已领材料（净额）</div><div class="kpi-v">{{fmt(w.material_cost)}}</div></div>
      <div class="kpi"><div class="kpi-l">在制余额</div><div class="kpi-v" :class="{neg: w.wip > 0.005 && w.status==='closed'}">{{fmt(w.wip)}}</div></div>
      <div class="kpi"><div class="kpi-l">完工入库金额</div><div class="kpi-v">{{fmt(w.receipt_value)}}</div></div>
      <div class="kpi"><div class="kpi-l">分摊人工+制造费用</div><div class="kpi-v">{{fmt(w.allocated)}}</div></div>
      <div class="kpi"><div class="kpi-l">单位成本</div><div class="kpi-v">{{w.unit_cost == null ? '—' : fmt(w.unit_cost)}}</div><div class="kpi-d">工时 {{w.hours}} · 良品 {{w.good_qty}} · 不良 {{w.bad_qty}}</div></div>
    </div>
    <div class="card">
      <div class="card-h"><b>用料</b><span class="sub">需领 = 单台用量 × 计划数量 ×（1 + 损耗率）；在库为当前库存</span></div>
      <table><thead><tr><th>物料</th><th class="num">单台</th><th class="num">损耗</th><th class="num">需领</th><th class="num">已领净额</th><th class="num">未领</th><th class="num">在库</th></tr></thead>
        <tbody><tr v-for="r in w.requirements" :key="r.material_id">
          <td><span class="mono">{{r.code}}</span> {{r.name}} <span class="sub">{{r.spec}}</span> <span v-if="r.lot_control" class="pill p-info">批次</span></td>
          <td class="num">{{r.per_unit}}</td><td class="num">{{r.loss_rate ? (r.loss_rate*100).toFixed(1)+'%' : ''}}</td><td class="num">{{r.required}}</td>
          <td class="num">{{r.issued}}</td><td class="num" :class="{neg: r.remaining > 0}">{{r.remaining}}</td><td class="num" :class="{neg: r.on_hand < r.remaining}">{{r.on_hand}}</td></tr>
          <tr v-if="!w.requirements.length"><td colspan="7" class="empty">还没有用料</td></tr></tbody></table>
    </div>
    <div class="card">
      <div class="card-h"><b>单据</b><span class="sub">都是已过账的出入库单；作废在「出入库」页（工单关闭后要先重开）</span></div>
      <table><thead><tr><th>单号</th><th>类型</th><th>日期</th><th>仓</th><th>物料</th><th class="num">数量</th><th class="num">单价</th><th class="num">金额</th><th>批号</th></tr></thead>
        <tbody><tr v-for="m in w.movements" :key="m.id"><td class="mono">{{m.doc_no}}</td><td>{{moveName[m.move_type]}}</td><td class="mono">{{m.move_date}}</td><td class="sub">{{m.location}}</td>
          <td><span class="mono">{{m.material_code}}</span> {{m.material_name}}</td><td class="num">{{m.qty}}</td><td class="num">{{fmt(m.unit_cost)}}</td><td class="num">{{fmt(m.amount)}}</td><td class="mono">{{m.lot_no}}</td></tr>
          <tr v-if="!w.movements.length"><td colspan="9" class="empty">还没有单据</td></tr></tbody></table>
    </div>
    <div class="card" v-if="w.kind==='production'">
      <div class="card-h"><b>报工</b><span class="sub">月末按工时把人工和制造费用分摊到当月有完工入库的工单</span></div>
      <table><thead><tr><th>日期</th><th class="num">良品</th><th class="num">不良</th><th class="num">工时</th><th>备注</th><th></th></tr></thead>
        <tbody><tr v-for="r in w.reports" :key="r.id"><td class="mono">{{r.report_date}}</td><td class="num">{{r.good_qty}}</td><td class="num">{{r.bad_qty}}</td><td class="num">{{r.hours}}</td><td>{{r.remark}}</td>
          <td><button v-if="w.status==='released'" class="btn btn-ghost btn-sm" @click="voidReport(r)">作废</button></td></tr>
          <tr v-if="!w.reports.length"><td colspan="6" class="empty">还没有报工</td></tr></tbody></table>
    </div>
    <div class="modal-mask" v-if="rf" @click.self="rf=null">
      <div class="modal" style="width:560px">
        <div class="modal-h"><b>完工入库</b><span class="x" @click="rf=null">✕</span></div>
        <div class="form-grid" style="grid-template-columns:1fr 1fr">
          <div class="fg"><label>日期</label><input type="date" v-model="rf.move_date"></div>
          <div class="fg"><label>数量</label><input type="number" v-model.number="rf.qty"></div>
          <div class="fg"><label>入哪个仓</label><select v-model="rf.location_id"><option :value="null">（默认：成品仓）</option><option v-for="l in locs" :key="l.id" :value="l.id">{{l.name}}</option></select></div>
          <div class="fg"><label>批号（产品按批次管理时必填）</label><input v-model="rf.lot_no"></div>
        </div>
        <div class="actions"><button class="btn btn-ghost" @click="rf=null">取消</button><button class="btn btn-ink" @click="submitReceipt">入库</button></div>
      </div>
    </div>
    <div class="modal-mask" v-if="lf" @click.self="lf=null">
      <div class="modal" style="width:820px">
        <div class="modal-h"><b>{{lf.mode==='issue'?'领料':'退料'}}</b><span class="x" @click="lf=null">✕</span></div>
        <div class="form-grid" style="grid-template-columns:1fr 1fr 2fr"><div class="fg"><label>日期</label><input type="date" v-model="lf.date"></div>
          <div class="fg"><label>{{lf.mode==='issue'?'从哪个仓领':'退回哪个仓'}}</label><select v-model="lf.location_id"><option :value="null">（默认：原材料仓）</option><option v-for="l in locs" :key="l.id" :value="l.id">{{l.name}}</option></select></div>
          <div class="fg"><label>备注</label><input v-model="lf.remark"></div></div>
        <div v-for="(l, i) in lf.lines" :key="i" style="display:flex;gap:6px;align-items:center;margin:4px 14px">
          <mat-pick v-model="l.material_id" :materials="materials" style="flex:1"></mat-pick>
          <input type="number" v-model.number="l.qty" style="width:80px" placeholder="数量">
          <input v-model="l.lot_no" style="width:110px" placeholder="批号">
          <button class="btn btn-ghost btn-sm" @click="lf.lines.splice(i,1)">✕</button>
        </div>
        <button class="btn btn-ghost btn-sm" style="margin:4px 14px" @click="lf.lines.push({material_id:0, qty:1, lot_no:''})">＋ 一行</button>
        <div class="actions"><button class="btn btn-ghost" @click="lf=null">取消</button><button class="btn btn-ink" @click="submitLines">过账</button></div>
      </div>
    </div>
  </div>`,
  data: () => ({ w: null, lf: null, rf: null, materials: [], locs: [], kindName: MFG_KIND, statusName: MFG_STATUS, moveName: MFG_MOVE }),
  methods: {
    fmt, go(k) { location.hash = '#/' + k; },
    async load() { this.w = await api.get('/api/work-orders/' + this.id); },
    async call(path, body) { try { this.w = await api.post('/api/work-orders/' + this.id + '/' + path, body); } catch (e) { alert(String(e)); } },
    async issueBom() { const d = prompt('领料日期', today()); if (d) this.call('issue', { move_date: d, from_bom: true }); },
    async openLines(mode) {
      if (!this.materials.length) this.materials = await api.get('/api/materials');
      const lines = mode === 'issue' ? this.w.requirements.filter(r => r.remaining > 0).map(r => ({ material_id: r.material_id, qty: r.remaining, lot_no: '' }))
                                     : this.w.requirements.filter(r => r.issued > 0).map(r => ({ material_id: r.material_id, qty: 0, lot_no: '' }));
      this.lf = { mode, date: today(), remark: '', location_id: null, lines: lines.length ? lines : [{ material_id: 0, qty: 1, lot_no: '' }] };
    },
    async submitLines() {
      const lines = this.lf.lines.filter(l => l.material_id && Number(l.qty) > 0).map(l => ({ material_id: l.material_id, qty: Number(l.qty), lot_no: l.lot_no || '' }));
      if (!lines.length) return alert('没有有效的明细');
      await this.call(this.lf.mode, { move_date: this.lf.date, lines, remark: this.lf.remark, location_id: this.lf.location_id });
      this.lf = null;
    },
    async report() {
      const d = prompt('报工日期', today()); if (!d) return;
      const good = prompt('良品数', String(Math.max(this.w.qty - this.w.good_qty, 0))); if (good === null) return;
      const bad = prompt('不良数', '0'); if (bad === null) return;
      const hours = prompt('工时（小时，按成本中心分摊人工和制造费用用）', '0'); if (hours === null) return;
      this.call('report', { report_date: d, good_qty: Number(good), bad_qty: Number(bad), hours: Number(hours) });
    },
    receipt() { this.rf = { move_date: today(), qty: Math.max(this.w.qty - this.w.received, 0), location_id: null, lot_no: '' }; },
    async submitReceipt() { await this.call('receipt', { ...this.rf, qty: Number(this.rf.qty) }); this.rf = null; },
    async act(path, msg) { if (confirm(msg)) this.call(path, {}); },
    async voidReport(r) { if (!confirm('作废这条报工？')) return; try { await api.post('/api/work-orders/reports/' + r.id + '/void', {}); this.load(); } catch (e) { alert(String(e)); } },
  },
  async mounted() { this.load(); this.locs = (await api.get('/api/locations')).filter(l => l.active); },
});

/* ---------------- 月末成本结转 ---------------- */
const MfgCloseView = defineComponent({
  template: `
  <div>
    <div class="filter-bar">
      <label class="sub">月份</label><input type="month" v-model="month" style="width:140px">
      <span class="pill" :class="d.done ? 'p-ok' : 'p-draft'" v-if="d.month">{{d.done ? '已结转' : '未结转'}}</span>
      <span style="margin-left:auto"></span>
      <button class="btn btn-ink" v-if="!d.done" :disabled="!d.pool_total && !d.rd_total" @click="run">结转 {{month}}</button>
      <button class="btn btn-ghost" v-if="d.history && d.history.length" @click="undo">撤销最近一次（{{d.history[0].month}}）</button>
    </div>
    <div class="card" v-for="w in (d.warnings || [])" :key="w" style="padding:10px 14px"><span class="neg">{{w}}</span></div>
    <div class="card">
      <div class="card-h"><b>待分摊：人工 + 制造费用 {{fmt(d.pool_total)}}</b><span class="sub">月末余额（含以前月没分完的）；按{{d.basis==='hours'?'报工工时':'完工数量'}}分摊到当月有完工入库的自制工单</span></div>
      <table><thead><tr><th>科目</th><th>成本中心</th><th class="num">金额</th></tr></thead>
        <tbody><tr v-for="p in d.pools" :key="p.account + '-' + p.cost_center_id"><td class="mono">{{p.account}} {{acctName(p.account)}}</td><td>{{ccName(p.cost_center_id)}}</td><td class="num">{{fmt(p.amount)}}</td></tr>
          <tr v-if="!(d.pools||[]).length"><td colspan="3" class="empty">没有待分摊的人工和制造费用</td></tr></tbody></table>
    </div>
    <div class="card">
      <div class="card-h"><b>分摊到工单</b><span class="sub">在库比例 = 月末在库数 ÷ 当月入库数（封顶 100%）；在库部分加到产品成本，已出库部分直接进主营业务成本</span></div>
      <table><thead><tr><th>工单</th><th>产品</th><th class="num">当月入库</th><th class="num">工时</th><th class="num">分摊</th><th class="num">每件增加</th><th class="num">在库比例</th><th class="num">进库存</th><th class="num">进成本</th></tr></thead>
        <tbody><tr v-for="o in d.orders" :key="o.id"><td class="mono clickable" @click="go('workorder/' + o.id)">{{o.wo_no}}</td><td>{{o.product}}</td><td class="num">{{o.received}}</td><td class="num">{{o.hours}}</td>
          <td class="num">{{fmt(o.x)}}</td><td class="num">{{fmt(o.unit_add)}}</td><td class="num">{{(o.on_hand_ratio*100).toFixed(1)}}%</td><td class="num">{{fmt(o.inventory)}}</td><td class="num">{{fmt(o.cogs)}}</td></tr>
          <tr v-if="!(d.orders||[]).length"><td colspan="9" class="empty">本月没有自制工单完工入库</td></tr></tbody></table>
      <div class="sub-line" v-if="d.rates && d.rates.rate_per_hour">本月实际费率 {{fmt(d.rates.rate_per_hour)}} 元/工时（报价测算默认用最近一次的费率）</div>
    </div>
    <div class="card">
      <div class="card-h"><b>研发支出转研究费用 {{fmt(d.rd_total)}}</b><span class="sub">5301 研发支出按项目转 660211 研究费用（利润表「研究费用」行）</span></div>
      <table><thead><tr><th>研发项目</th><th class="num">金额</th></tr></thead>
        <tbody><tr v-for="r in d.rd" :key="r.cost_center_id"><td>{{r.project}}</td><td class="num">{{fmt(r.amount)}}</td></tr>
          <tr v-if="!(d.rd||[]).length"><td colspan="2" class="empty">没有研发支出</td></tr></tbody></table>
    </div>
  </div>`,
  data() {
    const d = new Date(); d.setDate(0);
    return { month: d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0'), d: {}, accounts: [], ccs: [] };
  },
  watch: { month() { this.load(); } },
  methods: {
    fmt, go(k) { location.hash = '#/' + k; },
    acctName(c) { const a = this.accounts.find(x => x.code === c); return a ? a.name : ''; },
    ccName(id) { const c = this.ccs.find(x => x.id === id); return c ? c.code + ' ' + c.name : '—'; },
    async load() { this.d = await api.get('/api/mfg/close?month=' + this.month); },
    async run() { if (!confirm(`结转 ${this.month}：生成分摊凭证和研发支出转费用凭证。确定？`)) return; try { this.d = await api.post('/api/mfg/close', { month: this.month }); this.load(); } catch (e) { alert(String(e)); } },
    async undo() { if (!confirm('撤销最近一次成本结转（红冲凭证、作废成本分摊单）？')) return; try { await api.post('/api/mfg/close/undo', {}); this.load(); } catch (e) { alert(String(e)); } },
  },
  async mounted() { [this.accounts, this.ccs] = await Promise.all([api.get('/api/accounts'), api.get('/api/cost-centers')]); this.load(); },
});

/* ---------------- 报价成本测算 ---------------- */
const QuoteView = defineComponent({
  components: { MatPick },
  template: `
  <div>
    <div class="card">
      <div class="card-h"><b>报价成本测算</b><span class="sub">材料价取库存均价，没库存取最近收货价，都没有就手填；人工费率空着用最近一次成本结转的实际费率</span></div>
      <div class="form-grid" style="grid-template-columns:2fr 1fr 1fr 1fr 1fr 1fr">
        <div class="fg"><label>按 BOM（产品）</label><mat-pick v-model="productId" :materials="products" placeholder="搜产品"></mat-pick></div>
        <div class="fg"><label>BOM 版本</label><select v-model="q.bom_id"><option :value="null">（不用 BOM）</option><option v-for="b in boms" :key="b.id" :value="b.id">{{b.version}} {{statusName[b.status]}}</option></select></div>
        <div class="fg"><label>单件工时（小时）</label><input type="number" step="0.01" v-model="q.hours_per_unit" placeholder="空 = 产品标准工时"></div>
        <div class="fg"><label>人工费率（元/时）</label><input type="number" step="0.01" v-model="q.labor_rate" :placeholder="meta.last_rate ? '实际 ' + meta.last_rate : '未结转过'"></div>
        <div class="fg"><label>制造费用率（元/时）</label><input type="number" step="0.01" v-model.number="q.overhead_rate"></div>
        <div class="fg"><label>目标毛利率</label><input type="number" step="0.01" v-model.number="q.margin"></div>
      </div>
      <div style="padding:0 14px"><b style="font-size:13px">额外材料 / 手填价</b>
        <div v-for="(l, i) in q.lines" :key="i" style="display:flex;gap:6px;align-items:center;margin:4px 0">
          <mat-pick v-model="l.material_id" :materials="materials" style="flex:1"></mat-pick>
          <input type="number" v-model.number="l.qty" style="width:70px" placeholder="用量">
          <input type="number" v-model="l.price" style="width:90px" placeholder="单价(空=自动)">
          <button class="btn btn-ghost btn-sm" @click="q.lines.splice(i,1)">✕</button>
        </div>
        <button class="btn btn-ghost btn-sm" @click="q.lines.push({material_id:0, qty:1, price:''})">＋ 一行</button>
        <button class="btn btn-ink btn-sm" style="margin-left:8px" @click="calc">测算</button>
      </div>
    </div>
    <div class="card" v-if="r">
      <div class="kpis" style="grid-template-columns:repeat(5,1fr)">
        <div class="kpi"><div class="kpi-l">材料</div><div class="kpi-v">{{fmt(r.material)}}</div></div>
        <div class="kpi"><div class="kpi-l">人工</div><div class="kpi-v">{{fmt(r.labor)}}</div><div class="kpi-d">{{r.hours}} 时 × {{fmt(r.labor_rate)}} 元/时</div></div>
        <div class="kpi"><div class="kpi-l">制造费用</div><div class="kpi-v">{{fmt(r.overhead)}}</div></div>
        <div class="kpi"><div class="kpi-l">单位成本</div><div class="kpi-v">{{fmt(r.unit_cost)}}</div></div>
        <div class="kpi"><div class="kpi-l">建议售价（不含税 / 含税）</div><div class="kpi-v">{{fmt(r.price_ex_tax)}}</div><div class="kpi-d">{{fmt(r.price_tax)}}</div></div>
      </div>
      <div v-if="r.no_price.length" class="sub-line neg" style="padding:0 14px">没有价格的物料（按 0 计）：{{r.no_price.join('、')}}，请手填单价</div>
      <table><thead><tr><th>物料</th><th class="num">用量</th><th class="num">损耗</th><th class="num">单价</th><th>价格来源</th><th class="num">金额</th></tr></thead>
        <tbody><tr v-for="(l, i) in r.lines" :key="i"><td><span class="mono">{{l.code}}</span> {{l.name}}</td><td class="num">{{l.qty}}</td><td class="num">{{l.loss_rate ? (l.loss_rate*100).toFixed(1)+'%' : ''}}</td>
          <td class="num">{{l.price}}</td><td class="sub">{{l.price_source}}</td><td class="num">{{fmt(l.amount)}}</td></tr></tbody></table>
    </div>
  </div>`,
  data: () => ({ materials: [], boms: [], productId: 0, meta: {}, r: null, statusName: { draft: '草稿', active: '生效', obsolete: '作废' },
                 q: { bom_id: null, hours_per_unit: '', labor_rate: '', overhead_rate: 0, margin: 0.2, lines: [] } }),
  computed: { products() { return this.materials.filter(m => ['finished', 'semi', 'goods'].includes(m.material_type || 'goods')); } },
  watch: { async productId(v) { this.boms = v ? await api.get('/api/boms?product_id=' + v) : []; const a = this.boms.find(b => b.status === 'active'); this.q.bom_id = a ? a.id : null; } },
  methods: {
    fmt,
    async calc() {
      const body = { ...this.q, labor_rate: this.q.labor_rate === '' ? null : Number(this.q.labor_rate),
                     hours_per_unit: this.q.hours_per_unit === '' || this.q.hours_per_unit == null ? null : Number(this.q.hours_per_unit),
                     lines: this.q.lines.filter(l => l.material_id).map(l => ({ material_id: l.material_id, qty: Number(l.qty), price: l.price === '' ? null : Number(l.price) })) };
      try { this.r = await api.post('/api/mfg/quote', body); } catch (e) { alert(String(e)); }
    },
  },
  async mounted() { [this.materials, this.meta] = await Promise.all([api.get('/api/materials'), api.get('/api/mfg/meta')]); },
});

/* ---------------- 研发支出辅助账 ---------------- */
const RdLedgerView = defineComponent({
  template: `
  <div>
    <div class="filter-bar">
      <label class="sub">年度</label><input type="number" v-model.number="year" style="width:90px">
      <span class="sub">按研发项目（成本中心 CC20 下的子项）归集 5301 研发支出；供研发费用加计扣除、高新技术企业申报用</span>
    </div>
    <div class="card">
      <table><thead><tr><th>项目</th><th v-for="c in d.categories" :key="c" class="num">{{c}}</th><th class="num">合计</th><th class="num">已转研究费用</th></tr></thead>
        <tbody><tr v-for="p in d.projects" :key="p.project_id"><td><span class="mono">{{p.code}}</span> {{p.name}}</td>
          <td v-for="c in d.categories" :key="c" class="num">{{fmt(p[c])}}</td><td class="num"><b>{{fmt(p.total)}}</b></td><td class="num">{{fmt(p.expensed)}}</td></tr>
          <tr v-if="!(d.projects||[]).length"><td :colspan="(d.categories||[]).length + 3" class="empty">还没有研发项目：到 财务 → 成本中心，在「CC20 研发」下加子项</td></tr></tbody></table>
      <div class="sub-line">分类口径：研发领料 = 直接投入；同一凭证里有应付职工薪酬 = 人员人工；有累计折旧 = 折旧；其余 = 其他相关费用。研发人员工资计提时借方选 5301 并挂项目。</div>
    </div>
  </div>`,
  data: () => ({ year: new Date().getFullYear(), d: {} }),
  watch: { year() { this.load(); } },
  methods: { fmt, async load() { this.d = await api.get('/api/mfg/rd-ledger?year=' + this.year); } },
  mounted() { this.load(); },
});

/* ---------------- 批次库存 ---------------- */
const LotsView = defineComponent({
  components: { DataTable },
  template: `
  <div>
    <div class="filter-bar"><span class="sub">只统计填了批号的单据；成本仍是全公司移动加权，批次只管数量和追溯</span></div>
    <div class="card"><data-table view="lots" :columns="cols" :rows="list" export-name="批次库存" empty="还没有带批号的库存"></data-table></div>
  </div>`,
  data: () => ({ list: [] }),
  computed: { cols() { return [
    { key: 'code', label: '物料编码', type: 'code' }, { key: 'name', label: '名称', type: 'text', min: 160 },
    { key: 'lot_no', label: '批号', type: 'id' }, { key: 'lot_date', label: '生产日期', type: 'date' },
    { key: 'first', label: '首次入库', type: 'date' }, { key: 'qty', label: '数量', type: 'qty' } ]; } },
  async mounted() { this.list = await api.get('/api/stock/lots'); },
});

/* ---------------- 关联交易 ---------------- */
const RelatedPartyView = defineComponent({
  template: `
  <div>
    <div class="filter-bar">
      <label class="sub">年度</label><input type="number" v-model.number="year" style="width:90px">
      <span class="sub">往来单位勾了「关联方」的才统计；企业所得税年度汇算填《关联业务往来报告表》用。定价依据（报价单、市场价对比）请留在合同原件里</span>
    </div>
    <div class="card">
      <table><thead><tr><th>关联方</th><th>税号</th><th class="num">销售（不含税）</th><th class="num">采购（不含税）</th><th class="num">收款</th><th class="num">付款</th><th class="num">年末应收</th><th class="num">年末应付</th></tr></thead>
        <tbody><tr v-for="r in d.rows" :key="r.partner_id"><td>{{r.name}}</td><td class="mono">{{r.tax_no}}</td><td class="num">{{fmt(r.sales_ex_tax)}}</td><td class="num">{{fmt(r.purchase_ex_tax)}}</td>
          <td class="num">{{fmt(r.received)}}</td><td class="num">{{fmt(r.paid)}}</td><td class="num">{{fmt(r.ar)}}</td><td class="num">{{fmt(r.ap)}}</td></tr>
          <tr v-if="!(d.rows||[]).length"><td colspan="8" class="empty">还没有标记关联方：往来单位 → 编辑 → 勾「关联方」</td></tr></tbody></table>
    </div>
  </div>`,
  data: () => ({ year: new Date().getFullYear(), d: {} }),
  watch: { year() { this.load(); } },
  methods: { fmt, async load() { this.d = await api.get('/api/related-party?year=' + this.year); } },
  mounted() { this.load(); },
});

/* ---------------- 供应商价目表（v0.37） ---------------- */
const SupplierPricesView = defineComponent({
  components: { DataTable, MatPick },
  template: `
  <div>
    <div class="filter-bar">
      <span class="sub">物料 × 供应商的不含税单价、最小起订量、交期；报价测算先取「首选」，没有首选取最低价</span>
      <label style="display:flex;gap:4px;font-size:13px;color:var(--ink);margin-left:12px"><input type="checkbox" v-model="all" style="width:auto">含停用</label>
      <button class="btn btn-ink" style="margin-left:auto" @click="open()">＋ 新增价格</button>
    </div>
    <div class="card">
      <data-table view="supplier_prices" :columns="cols" :rows="list" export-name="供应商价目表" empty="还没有价目" :row-click="open" :row-class="r => r.active ? '' : 'dim'">
        <template #cell-preferred="{ row }"><span v-if="row.preferred" class="pill p-ok">首选</span></template>
      </data-table>
    </div>
    <div class="modal-mask" v-if="f" @click.self="f=null">
      <div class="modal" style="width:680px">
        <div class="modal-h"><b>{{f.id ? '修改' : '新增'}}供应商价格</b><span class="x" @click="f=null">✕</span></div>
        <div class="form-grid" style="grid-template-columns:1fr 1fr">
          <div class="fg span2"><label>物料</label><mat-pick v-model="f.material_id" :materials="materials"></mat-pick></div>
          <div class="fg"><label>供应商</label><select v-model="f.partner_id"><option v-for="p in suppliers" :key="p.id" :value="p.id">{{p.short_name || p.name}}</option></select></div>
          <div class="fg"><label>供应商料号</label><input class="mono" v-model="f.supplier_pn"></div>
          <div class="fg"><label>不含税单价</label><input type="number" step="0.0001" v-model.number="f.price"></div>
          <div class="fg"><label>税率</label><input type="number" step="0.01" v-model.number="f.tax_rate"></div>
          <div class="fg"><label>最小起订量</label><input type="number" v-model.number="f.moq"></div>
          <div class="fg"><label>交期（天）</label><input type="number" v-model.number="f.lead_days"></div>
          <div class="fg"><label>生效日期</label><input type="date" v-model="f.valid_from"></div>
          <div class="fg"><label>&nbsp;</label><div style="display:flex;gap:14px;padding-top:8px">
            <label style="display:flex;gap:5px;font-size:13px;color:var(--ink)"><input type="checkbox" v-model="f.preferred" style="width:auto">首选</label>
            <label style="display:flex;gap:5px;font-size:13px;color:var(--ink)"><input type="checkbox" v-model="f.active" style="width:auto">有效</label></div></div>
          <div class="fg span2"><label>备注（报价单号、阶梯价等）</label><input v-model="f.remark"></div>
        </div>
        <div class="actions"><button class="btn btn-ghost" @click="f=null">取消</button><button class="btn btn-ink" @click="save">保存</button></div>
      </div>
    </div>
  </div>`,
  data: () => ({ list: [], materials: [], partners: [], f: null, all: false }),
  computed: {
    suppliers() { return this.partners.filter(p => p.is_supplier); },
    cols() { return [
      { key: 'code', label: '物料编码', type: 'code' }, { key: 'name', label: '名称', type: 'text', min: 140 },
      { key: 'spec', label: '规格', type: 'text', hidden: true }, { key: 'mpn', label: '制造商料号', type: 'code', hidden: true },
      { key: 'partner', label: '供应商', type: 'partner' }, { key: 'supplier_pn', label: '供应商料号', type: 'code', hidden: true },
      { key: 'price', label: '单价（不含税）', type: 'money' }, { key: 'price_tax', label: '含税', type: 'money', hidden: true },
      { key: 'moq', label: '起订量', type: 'qty' }, { key: 'lead_days', label: '交期（天）', type: 'qty' },
      { key: 'preferred', label: '首选', type: 'status', width: 60, value: r => r.preferred ? '首选' : '' },
      { key: 'valid_from', label: '生效', type: 'date' }, { key: 'remark', label: '备注', type: 'text', hidden: true } ]; },
  },
  watch: { all() { this.load(); } },
  methods: {
    async load() { this.list = await api.get('/api/supplier-prices' + (this.all ? '?all=true' : '')); },
    open(r) {
      this.f = r ? { ...r } : { material_id: 0, partner_id: (this.suppliers[0] || {}).id, price: 0, tax_rate: 0.13, moq: 0, lead_days: 0,
                                supplier_pn: '', preferred: false, valid_from: today(), remark: '', active: true };
    },
    async save() {
      const keys = ['material_id', 'partner_id', 'price', 'tax_rate', 'moq', 'lead_days', 'supplier_pn', 'preferred', 'valid_from', 'remark', 'active'];
      const d = Object.fromEntries(keys.map(k => [k, this.f[k]])); d.valid_from = d.valid_from || null;
      try { if (this.f.id) await api.put('/api/supplier-prices/' + this.f.id, d); else await api.post('/api/supplier-prices', d); this.f = null; this.load(); }
      catch (e) { alert(String(e)); }
    },
  },
  async mounted() { [this.materials, this.partners] = await Promise.all([api.get('/api/materials'), api.get('/api/partners')]); this.load(); },
});
