/* owe-erp 前端（由 app.js 机械拆分，经典脚本按序加载，顺序见 index.html；依赖：base 先于 forms 先于 views 先于 app） */

/* ---- 表单组件（先定义后使用）---- */
const ContractForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal">
      <div class="modal-h"><b>{{reviseFrom?'合同改版':editId?'编辑合同':'新建合同'}}</b><span class="x" @click="$emit('close')">✕</span></div>
      <div v-if="reviseFrom" class="hint hint-blue" style="margin:14px 18px 0">新版保存后原合同标记为「被替代」。方向和往来单位不能改；要换单位请另立新合同。</div>
      <div class="form-grid">
        <div class="fg"><label>类型</label><select v-model="form.contract_type" :disabled="!!reviseFrom"><option value="purchase">采购</option><option value="sales">销售</option></select></div>
        <div class="fg"><label>合同号</label><input class="mono" v-model="form.contract_no" placeholder="OYL…/CF…/BDR…"></div>
        <div class="fg"><label>对方单位</label><select v-model="form.partner_id" :disabled="!!reviseFrom"><option v-for="p in partners" :value="p.id">{{p.short_name||p.name}}（{{p.is_customer&&p.is_supplier?'供+客':p.is_customer?'客':p.is_supplier?'供':'—'}}）</option></select></div>
        <div class="fg"><label>签订日期</label><input type="date" v-model="form.sign_date"></div>
        <div class="fg"><label>金额（含税，可自动算）</label><input class="mono" v-model="form.amount_tax"></div>
        <div class="fg" v-if="form.contract_type==='purchase'"><label>参考销售合同号</label><input class="mono" v-model="form.ref_contract_no"></div>
        <div class="fg span2"><label>项目</label><input v-model="form.project"></div>
        <div class="fg span2"><label>备注</label><textarea v-model="form.remark" rows="2" style="resize:vertical"></textarea></div>
        <div class="fg span2" v-if="reviseFrom"><label style="display:flex;gap:6px;align-items:center;color:var(--ink);font-size:13px">
          <input type="checkbox" v-model="moveDocuments" style="width:auto">把原合同的发票、收付款、出入库单一并转到新版（出入库按同物料明细行对应）</label></div>
      </div>
      <div style="padding:0 18px">
        <b style="font-size:13px">明细行</b>
        <table style="margin-top:8px">
          <thead><tr><th>物料</th><th class="num">数量</th><th class="num">含税单价</th><th class="num">金额</th><th></th></tr></thead>
          <tbody>
            <tr v-for="(l,i) in form.lines" :key="i">
              <td style="min-width:320px">
                <input class="mono" v-model="lineQ[i]" @input="onSearch(i, l)" placeholder="搜编码 / 规格 / 对方料号" style="width:100%;margin-bottom:4px">
                <select v-model="l.material_id" style="width:100%"><option v-for="m in optionsFor(i, l)" :key="m.id" :value="m.id">{{matLabel(m)}}</option></select>
                <div class="sub-line" v-if="partnerAlias(l.material_id)">{{partnerShort}}料号：{{partnerAlias(l.material_id)}}</div>
              </td>
              <td><input class="mono" style="width:70px;text-align:right" v-model.number="l.qty"></td>
              <td><input class="mono" style="width:90px;text-align:right" v-model.number="l.price_tax"></td>
              <td class="num">{{fmt(l.qty*l.price_tax)}}</td>
              <td><button class="btn btn-ghost btn-sm" @click="form.lines.splice(i,1)">✕</button></td>
            </tr>
          </tbody>
        </table>
        <button class="btn btn-ghost btn-sm" style="margin:8px 0" @click="form.lines.push({material_id:materials[0]?.id,qty:1,price_tax:0,remark:''})">＋ 加一行</button>
      </div>
      <div class="actions">
        <button class="btn btn-ghost" @click="$emit('close')">取消</button>
        <button class="btn btn-ink" @click="save">保存</button>
      </div>
    </div>
  </div>`,
  props: { editId: Number, reviseFrom: Number },
  emits: ['close', 'saved'],
  data: () => ({ moveDocuments: true, lineQ: {}, form: { contract_no: '', contract_type: 'purchase', partner_id: null, sign_date: today(), amount_tax: 0, ref_contract_no: '', project: '', remark: '', lines: [] }, partners: [], materials: [] }),
  computed: {
    partnerShort() { const p = this.partners.find(x => x.id === this.form.partner_id); return p ? (p.short_name || p.name) : '对方'; },
  },
  methods: {
    fmt,
    /* 同一实物一条物料；对方料号挂在别名上。按对方料号也能搜到，下拉里显示当前对方的料号 */
    norm(s) { return (s || '').toUpperCase().replace(/[\s\/／,，;；()（）\[\]【】\-_.·]+/g, ''); },
    partnerAlias(mid) {
      const m = this.materials.find(x => x.id === mid);
      const a = m && (m.aliases || []).find(x => x.partner_id === this.form.partner_id);
      return a ? (a.alias_spec || a.alias_name) : '';
    },
    matLabel(m) { const a = this.partnerAlias(m.id); return `${m.code} · ${m.name} ${m.spec}` + (a ? ` ｜${this.partnerShort}料号 ${a}` : ''); },
    matches(m, q) {
      const hay = [m.code, m.name, m.spec, ...(m.aliases || []).flatMap(a => [a.alias_name, a.alias_spec])].map(this.norm).join('|');
      return hay.includes(q);
    },
    optionsFor(i, l) {
      const q = this.norm(this.lineQ[i]);
      if (!q) return this.materials;
      return this.materials.filter(m => m.id === l.material_id || this.matches(m, q));
    },
    onSearch(i, l) {
      const q = this.norm(this.lineQ[i]);
      const hits = q ? this.materials.filter(m => this.matches(m, q)) : [];
      if (hits.length && !hits.some(m => m.id === l.material_id)) l.material_id = hits[0].id;
    },
    async loadMasters() {
      this.partners = await api.get('/api/partners');
      this.materials = await api.get('/api/materials');
      if (!this.form.partner_id && this.partners.length) this.form.partner_id = this.partners[0].id;
      if (!this.form.lines.length && this.materials.length)
        this.form.lines.push({ material_id: this.materials[0].id, qty: 1, price_tax: 0, remark: '' });
    },
    async save() {
      if(this.form.lines.length) this.form.amount_tax = +this.form.lines.reduce((s,l)=>s+Math.round((l.qty||0)*(l.price_tax||0)*100)/100,0).toFixed(2);
      try {
        if (this.reviseFrom) {
          const r = await api.post(`/api/contracts/${this.reviseFrom}/revise`, { ...this.form, status: 'active', move_documents: this.moveDocuments });
          const m = r.moved;
          alert(`已生成新版。转入：发票 ${m.invoices}、收付款 ${m.payments}、出入库 ${m.movements}` + (r.kept.length ? '\n留在原合同：\n' + r.kept.join('\n') : ''));
          this.$emit('saved', r.id); this.$emit('close');
          return;
        }
        if (this.editId) await api.put(`/api/contracts/${this.editId}`, this.form);
        else await api.post('/api/contracts', this.form);
        this.$emit('saved'); this.$emit('close');
      } catch (e) { alert('保存失败：' + e); }
    },
  },
  async mounted() {
    await this.loadMasters();
    const srcId = this.editId || this.reviseFrom;
    if (srcId) {
      const c = await api.get(`/api/contracts/${srcId}`);
      this.form = { contract_no: this.reviseFrom ? c.contract_no + '-新' : c.contract_no, contract_type: c.contract_type, partner_id: c.partner_id,
        sign_date: c.sign_date, amount_tax: c.amount_tax, status: c.status, ref_contract_no: c.ref_contract_no,
        project: c.project, remark: c.remark,
        lines: c.lines.map(l => ({ id: this.reviseFrom ? undefined : l.id, material_id: l.material_id, qty: l.qty, price_tax: l.price_tax, remark: l.remark })) };
      if (this.reviseFrom) this.form.status = 'active';
    }
  },
});

const ArchiveForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:560px">
      <div class="modal-h"><b>登记存档文件</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="form-grid" style="grid-template-columns:1fr 1fr">
        <div class="fg"><label>关联编号</label><input class="mono" v-model="f.rel_no"></div>
        <div class="fg"><label>类型</label><select v-model="f.doc_type"><option>contract</option><option>invoice</option><option>payment</option><option>other</option></select></div>
        <div class="fg span2"><label>文件路径（NAS 路径）</label><input class="mono" v-model="f.file_path" placeholder="/mnt/fn/Books/…"></div>
        <div class="fg span2"><label>备注</label><input v-model="f.remark"></div>
      </div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button><button class="btn btn-ink" @click="save">登记</button></div>
    </div>
  </div>`,
  props: { rel_no: String },
  emits: ['close', 'saved'],
  data: () => ({ f: { rel_no: '', doc_type: 'contract', file_path: '', remark: '' } }),
  methods: {
    async save() {
      this.f.rel_no = this.f.rel_no || this.rel_no;
      await api.post('/api/archives', this.f);
      this.$emit('saved'); this.$emit('close');
    },
  },
  mounted() { if (this.rel_no) this.f.rel_no = this.rel_no; },
});

const MovementForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:640px">
      <div class="modal-h"><b>新建出入库单</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="form-grid" style="grid-template-columns:1fr 1fr">
        <div class="fg"><label>类型</label><select v-model="f.move_type"><option value="in">采购入库</option><option value="out">销售出库</option><option value="opening">期初建账</option><option value="adjust">库存调整</option></select></div>
        <div class="fg"><label>日期</label><input type="date" v-model="f.move_date"></div>
        <div class="fg"><label>合同（可选）</label><select v-model="f.contract_id"><option :value="null">不挂合同</option><option v-for="c in contracts" :value="c.id">{{c.contract_no}}（{{c.contract_type==='sales'?'销':'采'}}）</option></select></div>
        <div class="fg"><label>对方单位（可选）</label><select v-model="f.partner_id"><option :value="null">—</option><option v-for="p in partners" :value="p.id">{{p.short_name||p.name}}</option></select></div>
        <div class="fg"><label>物料</label><select v-model="f.material_id"><option v-for="m in materials" :value="m.id">{{m.code}} · {{m.name}} {{m.spec}}</option></select></div>
        <div class="fg"><label>数量</label><input class="mono" v-model.number="f.qty"></div>
        <div class="fg" v-if="f.move_type==='in'||f.move_type==='opening'"><label>含税单位成本</label><input class="mono" v-model.number="f.price_tax"></div>
        <div class="fg" v-if="f.move_type==='in'||f.move_type==='opening'"><label>税率</label><input class="mono" v-model.number="f.tax_rate"><div class="sub">不含税 {{(f.price_tax/(1+(f.tax_rate||0))).toFixed(4)}}</div></div>
        <div class="fg"><label>{{f.move_type==='out'?'从哪里发出':'存放地点'}}</label><select v-model.number="f.location_id">
          <option v-for="l in locs" :key="l.id" :value="l.id">{{l.name}}{{l.is_default?'（默认）':''}}</option></select></div>
        <div class="fg"><label>备注</label><input v-model="f.remark"></div>
      </div>
      <div class="actions">
        <button class="btn btn-ghost" @click="save('draft')">存草稿</button>
        <button class="btn btn-ink" @click="save('confirmed')">直接确认过账</button>
      </div>
    </div>
  </div>`,
  emits: ['close', 'saved'],
  data: () => ({ f: { move_type: 'in', move_date: today(), contract_id: null, partner_id: null, material_id: null, qty: 1, price_tax: 0, tax_rate: 0.13, remark: '', location_id: null }, contracts: [], partners: [], materials: [], locs: [] }),
  methods: {
    async save(status) {
      try {
        const { price_tax, tax_rate, ...rest } = this.f;
        const payload = { ...rest, status, tax_rate: Number(tax_rate) };
        if (rest.move_type === 'in' || rest.move_type === 'opening')
          payload.unit_cost = Number(price_tax) / (1 + (Number(tax_rate) || 0));  // 含税→不含税账面成本
        const r = await api.post('/api/movements', payload);
        alert(r.will_negative ? '已保存。注意：该物料库存为负，红字标示。' : '已保存');
        this.$emit('saved'); this.$emit('close');
      } catch (e) { alert('保存失败：' + e); }
    },
  },
  async mounted() {
    this.contracts = await api.get('/api/contracts?status=active');
    this.partners = await api.get('/api/partners');
    this.materials = await api.get('/api/materials');
    if (this.materials.length) this.f.material_id = this.materials[0].id;
    this.locs = (await api.get('/api/locations')).filter(l => l.active);
    this.f.location_id = (this.locs.find(l => l.is_default) || {}).id || null;
  },
});

const InvoiceForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:640px">
      <div class="modal-h"><b>登记发票</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="form-grid" style="grid-template-columns:1fr 1fr">
        <div class="fg"><label>方向</label><select v-model="f.direction"><option value="input">进项（收到）</option><option value="output">销项（开出）</option></select></div>
        <div class="fg"><label>发票号</label><input class="mono" v-model="f.invoice_no"></div>
        <div class="fg"><label>开票日期</label><input type="date" v-model="f.invoice_date"></div>
        <div class="fg"><label>对方单位</label><select v-model="f.partner_id"><option :value="null">—</option><option v-for="p in partners" :value="p.id">{{p.short_name||p.name}}</option></select></div>
        <div class="fg span2"><label>挂合同</label><select v-model="f.contract_id"><option :value="null">不挂</option><option v-for="c in contracts" :value="c.id">{{c.contract_no}}（{{fmt(c.amount_tax)}}）</option></select></div>
        <div class="fg"><label>不含税金额</label><input class="mono" v-model.number="f.amount_ex_tax"></div>
        <div class="fg"><label>税额</label><input class="mono" v-model.number="f.tax_amount"></div>
        <div class="fg"><label>价税合计</label><input class="mono" v-model.number="f.amount_tax"></div>
        <div class="fg"><label>查验状态</label><select v-model="f.verify_status"><option>未查验</option><option>已查验</option></select></div>
        <div class="fg span2"><label>文件路径</label><input class="mono" v-model="f.file_path"></div>
      </div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button><button class="btn btn-ink" @click="save">保存</button></div>
    </div>
  </div>`,
  emits: ['close', 'saved'],
  data: () => ({ f: { direction: 'input', invoice_no: '', invoice_date: today(), partner_id: null, contract_id: null, amount_ex_tax: 0, tax_amount: 0, amount_tax: 0, file_path: '', verify_status: '未查验', remark: '' }, partners: [], contracts: [] }),
  methods: {
    fmt,
    async save() {
      if (!this.f.amount_tax) this.f.amount_tax = (this.f.amount_ex_tax || 0) + (this.f.tax_amount || 0);
      try { await api.post('/api/invoices', this.f); this.$emit('saved'); this.$emit('close'); }
      catch (e) { alert('保存失败：' + e); }
    },
  },
  async mounted() {
    this.partners = await api.get('/api/partners');
    this.contracts = await api.get('/api/contracts?status=active');
  },
});

const PaymentForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:560px">
      <div class="modal-h"><b>登记收付款</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="form-grid" style="grid-template-columns:1fr 1fr">
        <div class="fg"><label>方向</label><select v-model="f.direction"><option value="pay">付款（我付对方）</option><option value="receive">收款（对方付我）</option></select></div>
        <div class="fg"><label>日期</label><input type="date" v-model="f.pay_date"></div>
        <div class="fg"><label>对方单位</label><select v-model="f.partner_id"><option :value="null">—</option><option v-for="p in partners" :value="p.id">{{p.short_name||p.name}}</option></select></div>
        <div class="fg"><label>金额</label><input class="mono" v-model.number="f.amount"></div>
        <div class="fg span2"><label>挂合同</label><select v-model="f.contract_id"><option :value="null">不挂</option><option v-for="c in contracts" :value="c.id">{{c.contract_no}}（{{c.partner_short}} {{fmt(c.amount_tax)}}）</option></select></div>
        <div class="fg span2"><label>备注</label><input v-model="f.remark"></div>
      </div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button><button class="btn btn-ink" @click="save">保存</button></div>
    </div>
  </div>`,
  emits: ['close', 'saved'],
  data: () => ({ f: { direction: 'pay', partner_id: null, contract_id: null, amount: 0, pay_date: today(), remark: '' }, partners: [], contracts: [] }),
  methods: {
    fmt,
    async save() { await api.post('/api/payments', this.f); this.$emit('saved'); this.$emit('close'); },
    watchContract() {
      const c = this.contracts.find(x => x.id === this.f.contract_id);
      if (c) { this.f.partner_id = c.partner_id; this.f.direction = c.contract_type === 'purchase' ? 'pay' : 'receive'; }
    },
  },
  watch: { 'f.contract_id'() { this.watchContract(); } },
  async mounted() {
    this.partners = await api.get('/api/partners');
    this.contracts = await api.get('/api/contracts?status=active');
  },
});

/* 发票/收付款改挂合同：只列同单位、同方向的有效合同 */
const RelinkForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:520px">
      <div class="modal-h"><b>改挂合同 · {{title}}</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="form-grid" style="grid-template-columns:1fr">
        <div class="fg"><label>当前</label><div class="mono" style="padding-top:4px">{{currentNo || '未挂合同'}}</div></div>
        <div class="fg"><label>改挂到</label><select v-model="target">
          <option :value="null">不挂合同</option>
          <option v-for="c in candidates" :key="c.id" :value="c.id">{{c.contract_no}}（{{c.sign_date||'无日期'}} · {{fmt(c.amount_tax)}}）</option></select></div>
        <div class="fg sub-line">只改单据归属，不改金额和凭证；核销关系保持不变。锁定期间内的单据不能改挂。</div>
        <div v-if="err" class="fg auth-error">{{err}}</div>
      </div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button>
        <button class="btn btn-ink" :disabled="busy || target===currentId" @click="save">保存</button></div>
    </div>
  </div>`,
  props: { kind: String, doc: Object },   // kind: invoice / payment；doc 需含 id、partner_id、direction、contract_id
  emits: ['close', 'saved'],
  data: () => ({ contracts: [], target: null, err: '', busy: false }),
  computed: {
    title() { return this.kind === 'invoice' ? '发票 ' + (this.doc.invoice_no || '') : '收付款 ' + fmt(this.doc.amount); },
    ctype() { return ['input', 'pay'].includes(this.doc.direction) ? 'purchase' : 'sales'; },
    currentId() { return this.doc.contract_id || null; },
    currentNo() { return this.doc.contract_no || ''; },
    candidates() { return this.contracts.filter(c => c.contract_type === this.ctype && c.partner_id === this.doc.partner_id); },
  },
  methods: {
    fmt,
    async save() {
      this.busy = true; this.err = '';
      try {
        await api.post(`/api/${this.kind === 'invoice' ? 'invoices' : 'payments'}/${this.doc.id}/relink`, { contract_id: this.target });
        this.$emit('saved'); this.$emit('close');
      } catch (e) { this.err = String(e); }
      finally { this.busy = false; }
    },
  },
  async mounted() {
    this.target = this.currentId;
    this.contracts = await api.get('/api/contracts?status=active');
  },
});

/* 收付款拆分：一笔银行款分到多张合同（原笔保留余额，拆出部分同日同单位同方向） */
const SplitPaymentForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:640px">
      <div class="modal-h"><b>拆分 · {{doc.direction==='pay'?'付款':'收款'}} {{fmt(doc.amount)}}</b><span class="x" @click="$emit('close')">✕</span></div>
      <table>
        <thead><tr><th style="width:44%">合同</th><th class="num" style="width:22%">金额</th><th>备注</th><th></th></tr></thead>
        <tbody>
          <tr><td><span class="mono">{{doc.contract_no || '未挂合同'}}</span> <span class="sub-line">原笔保留</span></td>
            <td class="num" :class="{neg: rest<=0}">{{fmt(rest)}}</td><td class="sub-line">{{doc.pay_date}} · {{doc.partner_short}}</td><td></td></tr>
          <tr v-for="(x,i) in parts" :key="i">
            <td><select v-model="x.contract_id"><option :value="null">不挂合同</option>
              <option v-for="c in candidates" :key="c.id" :value="c.id">{{c.contract_no}}（{{fmt(c.amount_tax)}}）</option></select></td>
            <td><input type="number" step="0.01" v-model.number="x.amount" style="text-align:right"></td>
            <td><input v-model="x.remark"></td>
            <td><span class="x clickable" v-if="parts.length>1" @click="parts.splice(i,1)">✕</span></td></tr>
        </tbody>
      </table>
      <div style="padding:8px 0"><button class="btn btn-ghost btn-sm" @click="add">＋ 再拆一笔</button></div>
      <div class="sub-line">原笔改为余额，拆出部分新建收付款（备注注明拆自哪笔）；凭证自动红冲重过。原笔已有核销不得超过余额。</div>
      <div v-if="err" class="auth-error">{{err}}</div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button>
        <button class="btn btn-ink" :disabled="busy || rest<=0 || !valid" @click="save">拆分</button></div>
    </div>
  </div>`,
  props: { doc: Object },
  emits: ['close', 'saved'],
  data: () => ({ contracts: [], parts: [], err: '', busy: false }),
  computed: {
    ctype() { return this.doc.direction === 'pay' ? 'purchase' : 'sales'; },
    candidates() { return this.contracts.filter(c => c.contract_type === this.ctype && c.partner_id === this.doc.partner_id); },
    rest() { return Math.round((this.doc.amount - this.parts.reduce((s, x) => s + (Number(x.amount) || 0), 0)) * 100) / 100; },
    valid() { return this.parts.every(x => Number(x.amount) > 0); },
  },
  methods: {
    fmt,
    add() { this.parts.push({ contract_id: null, amount: null, remark: '' }); },
    async save() {
      this.busy = true; this.err = '';
      try {
        await api.post('/api/payments/' + this.doc.id + '/split', { parts: this.parts });
        this.$emit('saved'); this.$emit('close');
      } catch (e) { this.err = String(e); }
      finally { this.busy = false; }
    },
  },
  async mounted() {
    this.add();
    this.contracts = await api.get('/api/contracts?status=active');
  },
});

/* 单张出入库确认：可改日期（迁移暂估草稿的日期是按签订日推定的，常需改成实际收发货日） */
const ConfirmMoveForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:480px">
      <div class="modal-h"><b>确认{{verb}} · {{m.doc_no}}</b><span class="x" @click="$emit('close')">✕</span></div>
      <div v-if="estimated" class="hint" style="margin:14px 18px 0">这是迁移生成的暂估草稿，日期 {{m.move_date}} 是按合同签订日推定的，请改为实际{{verb}}日。</div>
      <div class="form-grid" style="grid-template-columns:1fr 1fr">
        <div class="fg"><label>物料</label><div style="padding-top:4px">{{m.material_name}} × {{m.qty}}</div></div>
        <div class="fg"><label>实际{{verb}}日期</label><input type="date" v-model="d"></div>
        <div class="fg span2 sub-line">确认后计入库存并自动过账；{{m.move_type==='in' ? '入库按单据成本入账' : '出库成本按该日期的不含税加权均价结转'}}。</div>
        <div v-if="err" class="fg span2 auth-error">{{err}}</div>
      </div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button>
        <button class="btn btn-ink" :disabled="busy || !d" @click="save">确认过账</button></div>
    </div>
  </div>`,
  props: { m: Object },
  emits: ['close', 'saved'],
  data: () => ({ d: '', err: '', busy: false }),
  computed: {
    verb() { return ['in', 'return_in', 'opening'].includes(this.m.move_type) ? '收货' : '发货'; },
    estimated() { return (this.m.remark || '').includes('暂估'); },
  },
  methods: {
    async save() {
      this.busy = true; this.err = '';
      try {
        const r = await api.post('/api/movements/batch-confirm', { ids: [this.m.id], move_date: this.d });
        const w = stockWarnings(r, () => this.m.material_name);
        if (w) alert('已确认。' + w);
        this.$emit('saved'); this.$emit('close');
      } catch (e) { this.err = String(e); }
      finally { this.busy = false; }
    },
  },
  mounted() { this.d = this.m.move_date; },
});

/* 合同原件盖章（v0.31）：文字锚点 / 镜像对方章 / 定点，先预览再盖；尺寸按印章档案，不按对方章 */
const StampForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:980px;max-width:96vw">
      <div class="modal-h"><b>盖章 · {{file.file_name}}</b><span class="x" @click="$emit('close')">✕</span></div>
      <div style="display:grid;grid-template-columns:300px 1fr;gap:14px">
        <div class="form-grid" style="grid-template-columns:1fr;align-content:start">
          <div class="fg"><label>印章</label><select v-model="f.seal"><option v-for="s in seals" :key="s.name" :value="s.name">{{s.name}}（{{s.width_mm}}×{{s.height_mm}}mm，透明度 {{s.opacity}}）</option></select></div>
          <div class="fg"><label>定位方式</label><select v-model="f.mode">
            <option value="anchor">文字锚点（{{contractType==='purchase'?'甲方/需方':'乙方/供方'}}「盖章」字样）</option>
            <option value="mirror">镜像对方章（对方已盖章时）</option>
            <option value="fixed">定点（手填位置）</option></select></div>
          <div class="fg"><label>页码（0 = 末页）</label><input type="number" min="0" v-model.number="f.page"></div>
          <template v-if="f.mode==='anchor'">
            <div class="fg"><label>锚点文字（| 分隔，空=默认）</label><input v-model="f.anchors" :placeholder="contractType==='purchase'?'甲方（盖章）|需方（盖章）…':'乙方（盖章）|供方（盖章）…'"></div>
            <div class="fg"><label>位置</label><select v-model="f.placement"><option value="right">锚点右侧</option><option value="center">盖在锚点上</option></select></div>
            <div class="fg" v-if="f.placement==='right'"><label>距锚点（mm）</label><input type="number" v-model.number="f.gap_mm"></div>
          </template>
          <template v-if="f.mode==='fixed'">
            <div class="fg"><label>章中心距页面左边（mm）</label><input type="number" v-model.number="f.x_mm"></div>
            <div class="fg"><label>章中心距页面上边（mm）</label><input type="number" v-model.number="f.y_mm"></div>
          </template>
          <div class="fg"><label>微调：右移 / 下移（mm，可负）</label>
            <div style="display:flex;gap:6px"><input type="number" v-model.number="f.offset_x_mm"><input type="number" v-model.number="f.offset_y_mm"></div></div>
          <div class="sub-line">尺寸固定按印章实物（不按对方章大小）。镜像盖完即记为「双方签署版」；其余记为「我方盖章，待对方回签」。原文件不动，另存一份。</div>
          <div v-if="err" class="auth-error">{{err}}</div>
          <div class="actions" style="padding:0"><button class="btn btn-ghost" :disabled="busy" @click="doPreview">预览</button>
            <button class="btn btn-ink" :disabled="busy || !previewed" @click="doStamp">确认盖章</button></div>
        </div>
        <div style="background:#fff;border:1px solid var(--line);min-height:420px;display:flex;align-items:flex-start;justify-content:center;overflow:auto;max-height:74vh">
          <img v-if="img" :src="img" style="max-width:100%">
          <div v-else class="empty" style="margin-top:160px">点「预览」看盖章位置</div>
        </div>
      </div>
      <div v-if="info" class="sub-line" style="margin-top:6px">第 {{info.page}} / {{info.pages}} 页 · {{info.anchor ? '锚点「' + info.anchor + '」' : (info.counter_stamp ? '已找到对方章' : '定点')}} · 章位置 {{info.rect_mm.join(', ')}} mm</div>
    </div>
  </div>`,
  props: { file: Object, contractType: String },
  emits: ['close', 'saved'],
  data: () => ({ seals: [], img: '', info: null, err: '', busy: false, previewed: false,
                 f: { seal: '合同章', mode: 'anchor', page: 0, anchors: '', placement: 'right', gap_mm: 10, offset_x_mm: 0, offset_y_mm: 0, x_mm: null, y_mm: null } }),
  watch: { f: { deep: true, handler() { this.previewed = false; } } },
  methods: {
    body() { const b = { ...this.f }; if (b.mode !== 'fixed') { b.x_mm = null; b.y_mm = null; } return b; },
    async doPreview() {
      this.busy = true; this.err = '';
      try {
        const r = await api.post('/api/contract-files/' + this.file.id + '/stamp-preview', this.body());
        this.img = 'data:image/png;base64,' + r.png_b64; this.info = r;
        this.$nextTick(() => { this.previewed = true; });
      } catch (e) { this.err = String(e); this.img = ''; this.info = null; }
      finally { this.busy = false; }
    },
    async doStamp() {
      if (!confirm('确认用「' + this.f.seal + '」盖章？会另存一份新文件并记入操作日志。')) return;
      this.busy = true; this.err = '';
      try { await api.post('/api/contract-files/' + this.file.id + '/stamp', this.body()); this.$emit('saved'); this.$emit('close'); }
      catch (e) { this.err = String(e); } finally { this.busy = false; }
    },
  },
  async mounted() {
    this.seals = await api.get('/api/seals');
    if (this.seals.length && !this.seals.some(s => s.name === this.f.seal)) this.f.seal = this.seals[0].name;
    if (this.contractType === 'sales') this.f.mode = 'mirror';     // 客户合同多数对方先盖章
  },
});

/* 销售合同 → 税局批量开票导入 Excel（v0.32）：默认带出全部明细，可改数量/项目名称/规格型号 */
const InvoiceExportForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:1080px;max-width:96vw">
      <div class="modal-h"><b>导出批量开票 Excel · {{c.contract_no}}</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="sub-line" style="margin-bottom:8px">购方：{{d.buyer.name}} · 税号 {{d.buyer.tax_no||'（缺）'}} · {{d.buyer.address||'地址缺'}} {{d.buyer.phone||''}} · {{d.buyer.bank_name||'开户行缺'}} {{d.buyer.bank_account||''}}
        <span v-if="!d.buyer.tax_no" class="neg">（开专票必须有税号，先去往来单位补）</span></div>
      <div class="form-grid" style="grid-template-columns:180px 160px 1fr">
        <div class="fg"><label>发票流水号（≤20 字符）</label><input v-model="serial" maxlength="20" class="mono"></div>
        <div class="fg"><label>发票类型</label><select v-model="invType"><option>增值税专用发票</option><option>普通发票</option></select></div>
        <div class="fg"><label>备注</label><input v-model="remark"></div>
      </div>
      <table>
        <thead><tr><th style="width:28px"></th><th>物料</th><th>项目名称</th><th>规格型号</th><th>单位</th><th class="num" style="width:90px">数量</th><th class="num">含税单价</th><th class="num">金额</th><th class="num" style="width:70px">税率</th></tr></thead>
        <tbody><tr v-for="l in rows" :key="l.line_id" :style="l.on?'':'opacity:.45'">
          <td><input type="checkbox" v-model="l.on" style="width:auto"></td>
          <td class="sub-line">{{l.material_code}}<br>{{l.material_name}}</td>
          <td><input v-model="l.item_name"></td><td><input v-model="l.spec"></td><td>{{l.unit}}</td>
          <td><input type="number" v-model.number="l.qty" style="text-align:right"></td>
          <td class="num">{{l.price_tax}}</td><td class="num">{{fmt(l.qty * l.price_tax)}}</td>
          <td><input type="number" step="0.01" v-model.number="l.tax_rate" style="text-align:right"></td></tr></tbody>
        <tfoot><tr><td colspan="7" class="num"><b>价税合计</b></td><td class="num"><b>{{fmt(total)}}</b></td><td></td></tr></tfoot>
      </table>
      <div class="sub-line" style="margin-top:6px">合同额 {{fmt(c.amount_tax)}}，已开票 {{fmt(c.invoice_sum)}}<span v-if="total + c.invoice_sum > c.amount_tax + 0.005" class="neg">（本次加已开超过合同额，检查数量）</span>。
        导出后在电子税务局 → 开票业务 → 蓝字发票开具 → 批量开具 导入；开出的发票下载后在发票档案导入即自动挂回本合同。项目名称/税收编码的默认值在物料里维护。</div>
      <div v-if="err" class="auth-error">{{err}}</div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button><button class="btn btn-ink" :disabled="busy || !rows.some(l=>l.on)" @click="download">导出 Excel</button></div>
    </div>
  </div>`,
  props: { c: Object },
  emits: ['close'],
  data: () => ({ d: { buyer: {}, lines: [] }, rows: [], serial: '', remark: '', invType: '增值税专用发票', err: '', busy: false }),
  computed: { total() { return Math.round(this.rows.filter(l => l.on).reduce((s, l) => s + l.qty * l.price_tax, 0) * 100) / 100; } },
  methods: {
    fmt,
    async download() {
      this.busy = true; this.err = '';
      const body = { serial: this.serial, invoice_type: this.invType, remark: this.remark,
                     lines: this.rows.filter(l => l.on).map(l => ({ line_id: l.line_id, qty: l.qty, item_name: l.item_name, spec: l.spec, tax_rate: l.tax_rate })) };
      try {
        const r = await fetch('/api/contracts/' + this.c.id + '/invoice-export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || r.status); }
        const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob());
        a.download = '批量开票-' + this.c.contract_no + '.xlsx'; a.click(); URL.revokeObjectURL(a.href);
        this.$emit('close');
      } catch (e) { this.err = String(e.message || e); } finally { this.busy = false; }
    },
  },
  async mounted() {
    this.d = await api.get('/api/contracts/' + this.c.id + '/invoice-export');
    this.serial = this.d.serial; this.remark = this.d.remark;
    this.rows = this.d.lines.map(l => ({ ...l, on: true }));
  },
});
