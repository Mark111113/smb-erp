/* owe-erp 前端（由 app.js 机械拆分，经典脚本按序加载，顺序见 index.html；依赖：base 先于 forms 先于 views 先于 app） */

/* ---------------- 往来单位 ---------------- */
const PartnersView = defineComponent({
  components: { DataTable },
  template: `
  <div>
    <div class="filter-bar">
      <span class="sub">同一单位可同时具备「客户」「供应商」角色（如既卖货给你又从你这买货的合作方）</span>
      <button class="btn btn-ink" style="margin-left:auto" @click="open()">＋ 新建单位</button>
    </div>
    <div class="card">
      <data-table view="partners" :columns="cols" :rows="list" export-name="往来单位" empty="无单位">
        <template #cell-roles="{ row }"><div class="dt-nowrap"><span class="pill p-ok" v-if="row.is_customer">客户</span> <span class="pill p-info" v-if="row.is_supplier">供应商</span> <span class="pill p-red" v-if="row.is_related">关联</span></div></template>
        <template #cell-actions="{ row }"><button class="btn btn-ghost btn-sm" @click="open(row)">编辑</button></template>
      </data-table>
    </div>
    <div class="modal-mask" v-if="showForm" @click.self="showForm=false">
      <div class="modal" style="width:640px">
        <div class="modal-h"><b>{{editId?'编辑':'新建'}}往来单位</b><span class="x" @click="showForm=false">✕</span></div>
        <div class="form-grid" style="grid-template-columns:1fr 1fr">
          <div class="fg"><label>全称</label><input v-model="f.name"></div>
          <div class="fg"><label>简称</label><input v-model="f.short_name"></div>
          <div class="fg"><label>角色</label><div style="display:flex;gap:14px;padding-top:8px">
            <label style="display:flex;gap:5px;font-size:13px;color:var(--ink)"><input type="checkbox" v-model="f.is_customer">客户</label>
            <label style="display:flex;gap:5px;font-size:13px;color:var(--ink)"><input type="checkbox" v-model="f.is_supplier">供应商</label>
            <label style="display:flex;gap:5px;font-size:13px;color:var(--ink)" title="持股 25% 以上、同一控制等关联关系；年度汇算报《关联业务往来报告表》"><input type="checkbox" v-model="f.is_related">关联方</label></div></div>
          <div class="fg"><label>税号</label><input class="mono" v-model="f.tax_no"></div>
          <div class="fg"><label>单位电话</label><input v-model="f.phone"></div>
          <div class="fg"><label>联系人</label><input v-model="f.contact"></div>
          <div class="fg"><label>联系电话</label><input v-model="f.contact_phone"></div>
          <div class="fg span2"><label>地址</label><input v-model="f.address"></div>
          <div class="fg"><label>开户行</label><input v-model="f.bank_name"></div>
          <div class="fg"><label>账号</label><input class="mono" v-model="f.bank_account"></div>
          <div class="fg span2"><label>备注</label><input v-model="f.remark"></div>
        </div>
        <div class="actions"><button class="btn btn-ghost" @click="showForm=false">取消</button><button class="btn btn-ink" @click="save">保存</button></div>
      </div>
    </div>
  </div>`,
  data: () => ({ list: [], showForm: false, editId: null, f: {} }),
  computed: {
    cols() {
      return [
        { key: 'code', label: '编码', type: 'code', width: 88 },
        { key: 'name', label: '名称', type: 'text', min: 150 },
        { key: 'short_name', label: '简称', type: 'partner', width: 84 },
        { key: 'roles', label: '角色', type: 'status', width: 120, value: p => [p.is_customer && '客户', p.is_supplier && '供应商', p.is_related && '关联方'].filter(Boolean).join('+') },
        { key: 'contact', label: '联系人', type: 'person', width: 80 },
        { key: 'contact_phone', label: '联系电话', type: 'code', width: 112, value: p => p.contact_phone || p.phone || '' },
        { key: 'phone', label: '单位电话', type: 'code', width: 120, hidden: true },
        { key: 'tax_no', label: '税号', type: 'code', width: 156 },
        { key: 'bank_name', label: '开户行', type: 'text', min: 130 },
        { key: 'bank_account', label: '账号', type: 'code', width: 156 },
        { key: 'address', label: '地址', type: 'text', hidden: true },
        { key: 'remark', label: '备注', type: 'text', hidden: true },
        { key: 'actions', label: '', type: 'actions', width: 68, sortable: false },
      ];
    },
  },
  methods: {
    async load() { this.list = await api.get('/api/partners'); },
    open(p) {
      this.editId = p ? p.id : null;
      this.f = p ? { ...p } : { name: '', short_name: '', is_customer: false, is_supplier: false, is_related: false, tax_no: '', address: '', phone: '', contact: '', contact_phone: '', bank_name: '', bank_account: '', remark: '', code: '' };
      this.showForm = true;
    },
    async save() {
      const d = { ...this.f }; delete d.id; delete d.created_at; delete d.active; delete d.aliases;
      if (this.editId) await api.put('/api/partners/' + this.editId, d);
      else await api.post('/api/partners', d);
      this.showForm = false; this.load();
    },
  },
  mounted() { this.load(); },
});

/* ---------------- 物料 ---------------- */
const MaterialsView = defineComponent({
  components: { DataTable },
  template: `
  <div>
    <div class="filter-bar">
      <span class="sub" :title="rule">同一物料可挂多方料号别名（各方料号自动归一）｜编码 OZ-组码+组内序号｜<b>新料号还是新版本？</b>悬停看规则</span>
      <label class="btn btn-ghost" style="margin-left:auto">批量导入（Excel/CSV）<input type="file" accept=".xlsx,.xls,.csv" style="display:none" @change="pickImport"></label>
      <button class="btn btn-ink" @click="open()">＋ 新建物料</button>
    </div>
    <div class="card">
      <data-table view="materials" :columns="cols" :rows="list" export-name="物料主数据" empty="无物料">
        <template #cell-group="{ row }"><div class="dt-nowrap"><span class="mono">{{row.mat_group}}</span> {{groupName(row.mat_group)}}</div></template>
        <template #cell-bom="{ row }"><span v-if="row.bom && row.bom.length" class="pill p-red" :title="'子件：'+row.bom.map(b=>b.child_code+'×'+b.qty_per).join('、')">BOM×{{row.bom.length}}</span></template>
        <template #cell-aliases="{ row }">
          <div class="dt-tags" :title="(row.aliases||[]).map(a => partnerName(a.partner_id) + '：' + (a.alias_name||'') + (a.alias_spec?' '+a.alias_spec:'')).join('；')">
            <button class="dt-tag-add" @click="addAlias(row)" title="加别名">＋</button>
            <span v-for="a in row.aliases" :key="a.id" class="dt-tag" @click="delAlias(a)">{{a.alias_name}}{{a.alias_spec?' '+a.alias_spec:''}} ✕</span>
          </div></template>
        <template #cell-active="{ row }"><span class="pill" :class="row.active?'p-ok':'p-gray'">{{row.active?'在用':'停用'}}</span></template>
        <template #cell-actions="{ row }"><button class="btn btn-ghost btn-sm" @click="open(row)">编辑</button></template>
      </data-table>
    </div>
    <div class="modal-mask" v-if="showForm" @click.self="showForm=false">
      <div class="modal" style="width:680px">
        <div class="modal-h"><b>{{editId?'编辑':'新建'}}物料</b><span class="x" @click="showForm=false">✕</span></div>
        <div class="form-grid" style="grid-template-columns:1fr 1fr">
          <div class="fg span2"><label>名称</label><input v-model="f.name"></div>
          <div class="fg span2"><label>规格型号</label><input v-model="f.spec"></div>
          <div class="fg"><label>物料组</label>
            <select v-model="f.mat_group"><option value="">（未分组）</option><option v-for="g in groups" :value="g.code">{{g.code}} {{g.name}}</option></select>
          </div>
          <div class="fg"><label>单位</label><select v-model="f.unit"><option v-for="u in units" :key="u">{{u}}</option></select></div>
          <div class="fg"><label>物料类型（决定存货科目）</label><select v-model="f.material_type"><option value="">（按物料组默认）</option><option v-for="(n, k) in types" :key="k" :value="k">{{n}}</option></select></div>
          <div class="fg"><label>损耗率（BOM 没填时用）</label><input type="number" step="0.001" v-model.number="f.loss_rate"></div>
          <div class="fg" v-if="['finished','semi','goods',''].includes(f.material_type)"><label>单件标准工时（小时，报价默认带出）</label><input type="number" step="0.01" v-model.number="f.std_hours"></div>
          <div class="fg" v-if="['finished','semi','goods',''].includes(f.material_type)"><label>贴片点数</label><input type="number" v-model.number="f.smt_points"></div>
          <div class="fg span2 sub">编码规则：改动影响外形、安装或功能（与旧件不能混用）= 建新料号；不影响互换的内部改动（换替代料、调位号）= 只升 BOM 版本。名称用统一品类词（如「贴片电容」「IPM 模块」），规格写型号 + 参数，制造商料号单独填。</div>
          <div class="fg span2" style="margin-top:4px"><b style="font-size:13px">制造属性</b> <span class="sub">元器件填；外购成品可空</span></div>
          <div class="fg"><label>制造商</label><input v-model="f.manufacturer"></div>
          <div class="fg"><label>制造商料号（MPN）</label><input class="mono" v-model="f.mpn"></div>
          <div class="fg"><label>封装</label><input v-model="f.package"></div>
          <div class="fg"><label>湿敏等级（MSL）</label><input v-model="f.msl" placeholder="如 3"></div>
          <div class="fg"><label>最小包装数量</label><input type="number" v-model.number="f.min_pack"></div>
          <div class="fg"><label>包装单位</label><input v-model="f.pack_unit" placeholder="卷 / 盘 / 包"></div>
          <div class="fg span2"><div style="display:flex;gap:18px">
            <label style="display:flex;gap:5px;font-size:13px;color:var(--ink)"><input type="checkbox" v-model="f.lot_control">批次管理（收发料要填批号）</label>
            <label style="display:flex;gap:5px;font-size:13px;color:var(--ink)"><input type="checkbox" v-model="f.key_part">关键件（追溯到序列号）</label></div></div>
          <div class="fg"><label>开票项目名称（空=用名称）</label><input v-model="f.invoice_name" :placeholder="f.name"></div>
          <div class="fg"><label>税收分类编码（空=默认 1090131050000000000）</label><input v-model="f.tax_code" class="mono" maxlength="19"></div>
          <div class="fg span2"><label>备注</label><input v-model="f.remark"></div>
          <div class="fg span2 sub">编码留空自动按组生成（当前组：{{groupName(f.mat_group)||'未分组'}}）</div>
        </div>
        <div style="margin:12px 0 2px" v-if="editId"><b>BOM</b> <span class="sub">（生效版本，只读）</span>
          <a class="clickable" style="margin-left:8px" @click="goBom">到 生产 → BOM 管理 维护（版本、位号、损耗、替代料、导入）›</a></div>
        <div v-if="editId" class="sub-line">{{bomText}}</div>
        <div class="actions"><button class="btn btn-ghost" @click="showForm=false">取消</button><button class="btn btn-ink" @click="save">保存</button></div>
      </div>
    </div>
    <div class="modal-mask" v-if="imp" @click.self="imp=null">
      <div class="modal" style="width:1000px">
        <div class="modal-h"><b>批量导入物料：{{imp.name}}</b><span class="x" @click="imp=null">✕</span></div>
        <div class="form-grid" style="grid-template-columns:1fr 1fr 1fr 1fr">
          <div class="fg"><label>表里的「物料编码」列是</label><select v-model="imp.code_is" @change="preview"><option value="own">我方编码（OZ-…）</option><option value="alias">对方料号（记成别名）</option><option value="ignore">不用</option></select></div>
          <div class="fg"><label>对方单位</label><select v-model="imp.partner_id" :disabled="imp.code_is!=='alias'" @change="preview"><option :value="null">—</option><option v-for="p in partners" :key="p.id" :value="p.id">{{p.short_name || p.name}}</option></select></div>
          <div class="fg"><label>行里没写物料组时</label><select v-model="imp.default_group" @change="preview"><option value="">（必须在表里写）</option><option v-for="g in groups" :key="g.code" :value="g.code">{{g.code}} {{g.name}}</option></select></div>
          <div class="fg"><label>&nbsp;</label><div style="display:flex;gap:12px;padding-top:6px">
            <label style="display:flex;gap:4px;font-size:13px;color:var(--ink)"><input type="checkbox" v-model="imp.fill_blank" @change="preview" style="width:auto">已有物料补空字段</label>
            <label style="display:flex;gap:4px;font-size:13px;color:var(--ink)"><input type="checkbox" v-model="imp.force_dup" @change="preview" style="width:auto">疑似重复也新建</label></div></div>
        </div>
        <div class="sub-line neg" v-if="imp.code_is==='alias' && !imp.partner_id">选好对方单位后出预览</div>
        <div class="sub-line" v-if="imp.res">新建 {{imp.res.summary.create}} · 已有 {{imp.res.summary.exists}} · 疑似重复 {{imp.res.summary.dup}}（不导） · 有错 {{imp.res.summary.error}}（要先改表）</div>
        <div style="max-height:400px;overflow:auto">
          <table v-if="imp.res"><thead><tr><th>行</th><th>处理</th><th>编码</th><th>名称</th><th>规格</th><th>组</th><th>制造商料号</th><th>对上的 / 说明</th></tr></thead>
            <tbody><tr v-for="r in imp.res.rows" :key="r.line" :class="{neg: r.action==='error'}">
              <td class="mono sub">{{r.line}}</td><td><span class="pill" :class="{create:'p-ok', exists:'p-info', dup:'p-draft', error:'p-red'}[r.action]">{{ {create:'新建', exists:'已有', dup:'疑似重复', error:'有错'}[r.action] }}</span></td>
              <td class="mono">{{r.code}}</td><td>{{r.name}}</td><td class="sub-line">{{r.spec}}</td><td class="mono">{{r.group}}</td><td class="mono">{{r.mpn}}</td>
              <td class="sub-line">{{r.errors.length ? r.errors.join('；') : r.matched}}{{r.alias ? '；加别名 ' + r.alias.alias_spec : ''}}{{Object.keys(r.fill||{}).length ? '；补 ' + Object.keys(r.fill).join('、') : ''}}</td></tr></tbody></table>
        </div>
        <div class="sub-line">表头认这些：物料编码、物料名称（必需）、规格型号、物料组、物料类型、单位、制造商、制造商料号、封装、湿敏等级、最小包装、包装单位、损耗率、批次管理、关键件、单件工时、贴片点数、备注。匹配顺序：编码 → 对方料号别名 → 制造商料号；规格相似的标「疑似重复」不导。</div>
        <div class="actions"><button class="btn btn-ghost" @click="imp=null">取消</button><button class="btn btn-ink" :disabled="!imp.res || imp.res.summary.error > 0" @click="applyImport">导入</button></div>
      </div>
    </div>
  </div>`,
  data: () => ({
    imp: null, rule: '编码规则：改动影响外形、安装或功能（与旧件不能混用）= 建新料号；不影响互换的内部改动（换替代料、调位号）= 只升 BOM 版本',
    list: [], partners: [], groups: [], showForm: false, editId: null, f: {}, bomRows: [], types: {},
    units: ['个', '套', '批', '卷', '盘', '米', '千克', '片', '块', '根', '张', '支', '包', '瓶'],
  }),
  computed: {
    bomText() { return this.bomRows.length ? this.bomRows.map(b => b.child_code + ' ×' + b.qty_per).join('、') : '还没有 BOM'; },
    cols() {
      return [
        { key: 'code', label: '编码', type: 'code' },
        { key: 'group', label: '物料组', type: 'status', width: 100, value: m => m.mat_group + ' ' + this.groupName(m.mat_group) },
        { key: 'name', label: '名称', type: 'text', min: 120 },
        { key: 'spec', label: '规格型号', type: 'text', min: 200 },
        { key: 'invoice_name', label: '开票名称', type: 'text', hidden: true, value: m => m.invoice_name || m.name },
        { key: 'tax_code', label: '税收编码', type: 'id', width: 170, hidden: true },
        { key: 'unit', label: '单位', type: 'short', width: 52 },
        { key: 'material_type', label: '类型', type: 'status', width: 70, value: m => this.types[m.material_type || 'goods'] || '' },
        { key: 'mpn', label: '制造商料号', type: 'code', hidden: true },
        { key: 'manufacturer', label: '制造商', type: 'short', hidden: true },
        { key: 'package', label: '封装', type: 'short', hidden: true },
        { key: 'bom', label: 'BOM', type: 'short', width: 76, value: m => m.bom && m.bom.length ? 'BOM×' + m.bom.length : '' },
        { key: 'aliases', label: '料号别名', type: 'text', min: 220, sortable: false,
          value: m => (m.aliases || []).map(a => (a.alias_name || '') + (a.alias_spec ? ' ' + a.alias_spec : '')).join('；') },
        { key: 'remark', label: '备注', type: 'text', hidden: true },
        { key: 'active', label: '状态', type: 'status', width: 68, hidden: true, value: m => m.active ? '在用' : '停用' },
        { key: 'actions', label: '', type: 'actions', width: 68, sortable: false },
      ];
    },
  },
  methods: {
    async load() {
      this.list = await api.get('/api/materials');
      this.partners = await api.get('/api/partners');
      this.groups = await api.get('/api/material-groups');
      if (!Object.keys(this.types).length) this.types = (await api.get('/api/mfg/meta')).material_types;
    },
    goBom() { this.showForm = false; location.hash = '#/boms/' + this.editId; },
    async pickImport(ev) {
      const f = ev.target.files[0]; ev.target.value = '';
      if (!f) return;
      this.imp = { name: f.name, b64: await readFileB64(f), code_is: 'own', partner_id: null, default_group: '', fill_blank: true, force_dup: false, res: null };
      this.preview();
    },
    body(apply) { const { res, b64, ...o } = this.imp; return { ...o, content_b64: b64, apply }; },
    async preview() {
      if (this.imp.code_is === 'alias' && !this.imp.partner_id) { this.imp.res = null; return; }   // 先选对方单位再预览
      try { this.imp.res = await api.post('/api/materials/import', this.body(false)); } catch (e) { alert(String(e)); }
    },
    async applyImport() {
      try { const r = await api.post('/api/materials/import', this.body(true)); alert(`已导入：新建 ${r.created} 个`); this.imp = null; this.load(); }
      catch (e) { alert(String(e)); }
    },
    groupName(g) { const x = this.groups.find(x => x.code === g); return x ? x.name : ''; },
    partnerName(pid) { const p = this.partners.find(x => x.id === pid); return p ? p.name : '通用'; },
    open(m) {
      this.editId = m ? m.id : null;
      const mf = ['material_type', 'manufacturer', 'mpn', 'package', 'msl', 'min_pack', 'pack_unit', 'loss_rate', 'lot_control', 'key_part', 'std_hours', 'smt_points'];
      const blank = { material_type: '', manufacturer: '', mpn: '', package: '', msl: '', min_pack: 0, pack_unit: '', loss_rate: 0, lot_control: false, key_part: false, std_hours: 0, smt_points: 0 };
      this.f = m ? { name: m.name, spec: m.spec, unit: m.unit, mat_group: m.mat_group || '', remark: m.remark, code: m.code, invoice_name: m.invoice_name || '', tax_code: m.tax_code || '',
                     ...Object.fromEntries(mf.map(k => [k, m[k] ?? blank[k]])) }
                 : { name: '', spec: '', unit: '个', mat_group: '', remark: '', code: '', invoice_name: '', tax_code: '', ...blank };
      this.bomRows = m ? (m.bom || []) : [];
      this.showForm = true;
    },
    async save() {
      const d = { ...this.f };
      if (this.editId) await api.put('/api/materials/' + this.editId, d);
      else {
        let r;
        try { r = await api.post('/api/materials', d); }
        catch (e) {
          // 疑似重复：同一实物的对方叫法应加别名，不要另建物料
          if (!(e instanceof ApiError && e.status === 409)) throw e;
          if (!confirm(e.message + '\n\n取消 = 回到列表，在已有物料上点「＋」加别名\n确定 = 确认是新实物，仍然新建')) return;
          r = await api.post('/api/materials', { ...d, force: true });
        }
        this.editId = r.id;
      }
      // BOM 不在这里改（v0.35 起在 生产 → BOM 管理 按版本维护），保存物料不动 BOM
      this.showForm = false; this.load();
    },
    async addAlias(m) {
      const name = prompt('别名名称（该方对此物料的叫法）：');
      if (!name) return;
      const spec = prompt('别名规格（可空）：') || '';
      const pnames = ['通用'].concat(this.partners.map(p => p.short_name || p.name));
      const pi = prompt('归属单位序号：0=通用 ' + pnames.slice(1).map((n, i) => `${i + 1}=${n}`).join(' '), '0');
      const pid = Number(pi) > 0 ? this.partners[Number(pi) - 1].id : null;
      await api.post('/api/material-aliases', { material_id: m.id, partner_id: pid, alias_name: name, alias_spec: spec });
      this.load();
    },
    async delAlias(a) { if (confirm('删除该别名？')) { await api.del('/api/material-aliases/' + a.id); this.load(); } },
  },
  mounted() { this.load(); },
});

/* ---------------- 存放地点 ---------------- */
const LocationsView = defineComponent({
  components: { DataTable },
  template: `
  <div>
    <div class="filter-bar">
      <span class="sub">货放在哪：自有仓库，或寄存在往来单位处。出入库不填地点时记到「默认」地点。备注会显示在库存页（如账实差异）</span>
      <button class="btn btn-ink" style="margin-left:auto" @click="open()">＋ 新建地点</button>
    </div>
    <div class="card">
      <data-table view="locations" :columns="cols" :rows="list" empty="无地点" :row-class="l => l.active ? '' : 'dim'">
        <template #cell-name="{ row }"><div class="dt-clamp">{{row.name}} <span v-if="row.is_default" class="pill p-info">默认</span><span v-if="!row.active" class="pill p-gray">停用</span></div></template>
        <template #cell-actions="{ row }"><button class="btn btn-ghost btn-sm" @click="open(row)">编辑</button></template>
      </data-table>
    </div>
    <div class="modal-mask" v-if="showForm" @click.self="showForm=false">
      <div class="modal" style="width:600px">
        <div class="modal-h"><b>{{editId?'编辑':'新建'}}存放地点</b><span class="x" @click="showForm=false">✕</span></div>
        <div class="form-grid" style="grid-template-columns:1fr 1fr">
          <div class="fg"><label>编码</label><input class="mono" v-model="f.code" placeholder="如 BDR-HOLD"></div>
          <div class="fg"><label>名称</label><input v-model="f.name" placeholder="如 供应商寄存（王工）"></div>
          <div class="fg"><label>类型</label><select v-model="f.kind"><option value="own">自有</option><option value="third_party">寄存在往来单位处</option></select></div>
          <div class="fg"><label>仓库用途（工单领料默认从原材料仓出、完工进成品仓）</label><select v-model="f.purpose"><option v-for="(n, k) in purposes" :key="k" :value="k">{{n}}</option></select></div>
          <div class="fg"><label>寄存单位</label><select v-model="f.partner_id" :disabled="f.kind!=='third_party'">
            <option :value="null">—</option><option v-for="p in partners" :key="p.id" :value="p.id">{{p.short_name||p.name}}</option></select></div>
          <div class="fg span2"><label>地址</label><input v-model="f.address"></div>
          <div class="fg"><label>联系人</label><input v-model="f.contact"></div>
          <div class="fg"><label>&nbsp;</label><div style="display:flex;gap:14px;padding-top:8px">
            <label style="display:flex;gap:5px;font-size:13px;color:var(--ink)"><input type="checkbox" v-model="f.is_default" style="width:auto">默认地点</label>
            <label style="display:flex;gap:5px;font-size:13px;color:var(--ink)"><input type="checkbox" v-model="f.active" style="width:auto">启用</label></div></div>
          <div class="fg span2"><label>备注（显示在库存页，如账实差异）</label><input v-model="f.remark"></div>
          <div v-if="err" class="fg span2 auth-error">{{err}}</div>
        </div>
        <div class="actions"><button class="btn btn-ghost" @click="showForm=false">取消</button><button class="btn btn-ink" @click="save">保存</button></div>
      </div>
    </div>
  </div>`,
  data: () => ({ list: [], partners: [], showForm: false, editId: null, f: {}, err: '',
                 purposes: { general: '通用', raw: '原材料仓', wip: '线边仓', finished: '成品仓', defect: '不良品仓', rd: '研发' } }),
  computed: {
    cols() {
      return [
        { key: 'code', label: '编码', type: 'code', width: 110 },
        { key: 'purpose', label: '用途', type: 'status', width: 80, value: l => this.purposes[l.purpose || 'general'] },
        { key: 'name', label: '名称', type: 'text', min: 160 },
        { key: 'kind', label: '类型', type: 'status', width: 120, value: l => l.kind === 'own' ? '自有' : '寄存 · ' + l.partner_name },
        { key: 'address', label: '地址', type: 'text' },
        { key: 'contact', label: '联系人', type: 'text', width: 140 },
        { key: 'sku_count', label: '有货物料', type: 'qty', width: 80 },
        { key: 'remark', label: '备注', type: 'text' },
        { key: 'actions', label: '', type: 'actions', width: 68, sortable: false },
      ];
    },
  },
  methods: {
    async load() { [this.list, this.partners] = await Promise.all([api.get('/api/locations'), api.get('/api/partners')]); },
    open(l) {
      this.err = ''; this.editId = l ? l.id : null;
      this.f = l ? { code: l.code, name: l.name, kind: l.kind, purpose: l.purpose || 'general', partner_id: l.partner_id, address: l.address, contact: l.contact,
                     is_default: l.is_default, active: l.active, remark: l.remark }
                 : { code: '', name: '', kind: 'own', purpose: 'general', partner_id: null, address: '', contact: '', is_default: false, active: true, remark: '' };
      this.showForm = true;
    },
    async save() {
      this.err = '';
      const d = { ...this.f, partner_id: this.f.kind === 'third_party' ? this.f.partner_id : null };
      try {
        if (this.editId) await api.put('/api/locations/' + this.editId, d); else await api.post('/api/locations', d);
        this.showForm = false; this.load();
      } catch (e) { this.err = String(e); }
    },
  },
  mounted() { this.load(); },
});
