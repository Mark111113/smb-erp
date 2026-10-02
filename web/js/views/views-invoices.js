/* owe-erp 前端 — 发票档案（v0.26）：导入归档、查看原件、入账/抵扣标记、增值税台账 */

const INV_CATS = [['', '未分类'], ['trade', '贸易'], ['expense', '费用'], ['prepay', '工程/预付'], ['asset', '固定资产'], ['none', '不入账']];
const INV_DEDUCT = [['pending', '未勾选'], ['deducted', '已勾选'], ['nondeductible', '不可抵扣'], ['transferred', '已转出']];

const InvoiceDocForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:720px">
      <div class="modal-h"><b>发票标记 · <span class="mono">{{doc.invoice_no}}</span></b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="sub-line" style="margin-bottom:8px">{{doc.issue_date}} · {{doc.counterparty}} · 价税合计 {{fmt(doc.amount_tax)}}，税额 {{fmt(doc.tax_amount)}}（{{doc.tax_rates}}）· {{doc.kind}}</div>
      <div class="form-grid" style="grid-template-columns:repeat(3,1fr)">
        <div class="fg"><label>类别</label><select v-model="f.category"><option v-for="c in cats" :key="c[0]" :value="c[0]">{{c[1]}}</option></select></div>
        <div class="fg"><label>入账凭证号（非贸易）</label><input v-model="f.voucher_no" placeholder="记-202605-0012" :disabled="!!doc.invoice_id"></div>
        <div class="fg"><label>入账月份</label><input type="month" v-model="f.booked_month"></div>
        <template v-if="doc.direction==='in'">
          <div class="fg"><label>抵扣状态</label><select v-model="f.deduct_status"><option v-for="c in deducts" :key="c[0]" :value="c[0]">{{c[1]}}</option></select></div>
          <div class="fg"><label>勾选所属期</label><input type="month" v-model="f.deduct_period" :disabled="!['deducted','transferred'].includes(f.deduct_status)"></div>
          <div class="fg"><label>有效抵扣税额</label><input type="number" step="0.01" v-model.number="f.deduct_tax" :disabled="!['deducted','transferred'].includes(f.deduct_status)"></div>
          <template v-if="f.deduct_status==='transferred'">
            <div class="fg"><label>转出所属期</label><input type="month" v-model="f.transfer_period"></div>
            <div class="fg span2"><label>转出计入科目</label><input v-model="f.transfer_account" placeholder="660208 职工福利费"></div>
          </template>
        </template>
        <div class="fg span3" style="grid-column:1/-1"><label>备注</label><input v-model="f.note"></div>
      </div>
      <div class="sub-line">贸易票自动挂 ERP 发票（{{doc.invoice_contract || '—'}}），非贸易票填入账凭证号。标记「已勾选」后，系统在所属期末自动出凭证 借 进项税额 / 贷 待认证进项税额；「已转出」再出 借 费用 / 贷 进项税额转出。</div>
      <div v-if="err" class="auth-error">{{err}}</div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button><button class="btn btn-ink" :disabled="busy" @click="save">保存</button></div>
    </div>
  </div>`,
  props: { doc: Object },
  emits: ['close', 'saved'],
  data() {
    const d = this.doc;
    return { cats: INV_CATS, deducts: INV_DEDUCT, err: '', busy: false,
             f: { category: d.category || '', voucher_no: d.voucher_no || '', booked_month: d.booked_month || '',
                  deduct_status: d.deduct_status || 'pending', deduct_period: d.deduct_period || '', deduct_tax: d.deduct_tax || d.tax_amount,
                  transfer_period: d.transfer_period || '', transfer_account: d.transfer_account || '', note: d.note || '' } };
  },
  methods: {
    fmt,
    async save() {
      this.busy = true; this.err = '';
      const body = { ...this.f };
      if (this.doc.invoice_id) delete body.voucher_no;
      if (this.doc.direction !== 'in') { delete body.deduct_status; delete body.deduct_period; delete body.deduct_tax; delete body.transfer_period; delete body.transfer_account; }
      try { await api.put('/api/invoice-docs/' + this.doc.id, body); this.$emit('saved'); this.$emit('close'); }
      catch (e) { this.err = String(e); } finally { this.busy = false; }
    },
  },
});

const InvoiceDocsView = defineComponent({
  components: { DataTable, InvoiceDocForm },
  template: `
  <div>
    <div class="filter-bar">
      <select v-model="f.dir"><option value="">全部方向</option><option value="in">收到的票</option><option value="out">开出的票</option></select>
      <select v-model="f.cat"><option value="*">全部类别</option><option v-for="c in cats" :key="c[0]" :value="c[0]">{{c[1]}}</option></select>
      <select v-model="f.booked"><option value="">入账：全部</option><option value="no">未入账</option><option value="yes">已入账</option></select>
      <select v-model="f.deduct"><option value="">抵扣：全部</option><option v-for="c in deducts" :key="c[0]" :value="c[0]">{{c[1]}}</option></select>
      <span style="margin-left:auto"></span>
      <button class="btn btn-ghost" @click="showVat=!showVat">{{showVat?'收起':'增值税台账'}}</button>
      <select v-model="upDir" style="width:110px"><option value="in">收到的票</option><option value="out">开出的票</option></select>
      <label class="btn btn-ink">导入发票 / 税务导出<input type="file" multiple accept=".pdf,.xml,.ofd,.zip,.xlsx,.png,.jpg" style="display:none" @change="upload"></label>
    </div>
    <div v-if="msg" class="card" style="padding:10px 14px;white-space:pre-wrap">{{msg}}</div>
    <div class="card" v-if="showVat">
      <div class="card-h"><b>增值税台账</b><span class="sub">销项按开票月；进项按勾选所属期（= 申报表）；未勾选专票 {{vat.pending_count}} 张，税额 {{fmt(vat.pending_tax)}}</span></div>
      <table>
        <thead><tr><th>所属期</th><th class="num">销项税额</th><th class="num">进项（已勾选）</th><th class="num">进项转出</th><th class="num">上期留抵</th><th class="num">应纳税额</th><th class="num">期末留抵</th><th class="num">当月实缴</th></tr></thead>
        <tbody><tr v-for="m in vat.months" :key="m.month">
          <td class="mono">{{m.month}}</td><td class="num">{{fmt(m.output)}}</td><td class="num">{{fmt(m.input)}}</td><td class="num">{{fmt(m.transfer_out)}}</td>
          <td class="num">{{fmt(m.carry_in)}}</td><td class="num" :class="{neg: m.payable>0.005}">{{fmt(m.payable)}}</td><td class="num">{{fmt(m.carry_out)}}</td><td class="num">{{fmt(m.paid_in_month)}}</td></tr></tbody>
      </table>
      <div class="sub-line" style="padding:8px 0">应纳税额次月申报缴纳（记 222103 已交税金）；要少缴就在所属期内多勾选未勾选的专票。</div>
    </div>
    <div class="card">
      <data-table view="invoice-docs" date-key="issue_date" :columns="cols" :rows="rows" export-name="发票档案" empty="还没有导入发票"
                  :row-click="r => edit(r)" :menu="menu">
        <template #cell-direction="{ row }"><span class="pill" :class="row.direction==='out'?'p-ok':'p-info'">{{row.direction==='out'?'开出':'收到'}}</span></template>
        <template #cell-deduct_label="{ row }"><span v-if="row.direction==='in'" class="pill" :class="dpill[row.deduct_status]">{{row.deduct_label}}</span><span v-else class="sub-line">—</span></template>
        <template #cell-booked_ref="{ row }">
          <div class="dt-nowrap mono" v-if="row.invoice_id">{{row.invoice_contract || ('票#'+row.invoice_id)}}</div>
          <div class="dt-nowrap mono" v-else-if="row.voucher_no">{{row.voucher_no}}</div>
          <div class="sub-line" v-else-if="row.category==='none'">不入账</div>
          <div class="neg" v-else>未入账</div></template>
        <template #cell-files="{ row }">
          <div class="dt-nowrap"><a v-for="fl in row.files" :key="fl.id" :href="'/api/invoice-docs/files/'+fl.id" target="_blank" @click.stop style="margin-right:6px">{{fl.type.toUpperCase()}}</a>
            <span v-if="!row.files.length" class="sub-line">缺文件</span></div></template>
      </data-table>
    </div>
    <invoice-doc-form v-if="editing" :doc="editing" @close="editing=null" @saved="load"></invoice-doc-form>
    <div class="modal-mask" v-if="reg" @click.self="reg=null">
      <div class="modal" style="width:600px">
        <div class="modal-h"><b>登记为贸易发票 · <span class="mono">{{reg.doc.invoice_no}}</span></b><span class="x" @click="reg=null">✕</span></div>
        <div class="sub-line" style="margin-bottom:8px">{{reg.doc.issue_date}} · {{reg.doc.counterparty}} · 价税合计 {{fmt(reg.doc.amount_tax)}}（金额 {{fmt(reg.doc.amount_ex_tax)}} + 税额 {{fmt(reg.doc.tax_amount)}}）</div>
        <div class="form-grid" style="grid-template-columns:1fr">
          <div class="fg"><label>往来单位</label><select v-model="reg.partner_id"><option :value="null">选单位</option><option v-for="p in partners" :key="p.id" :value="p.id">{{p.short_name||p.name}}</option></select></div>
          <div class="fg"><label>挂合同</label><select v-model="reg.contract_id"><option :value="null">不挂合同</option>
            <option v-for="c in regContracts" :key="c.id" :value="c.id">{{c.contract_no}}（{{c.sign_date||'无日期'}} · {{fmt(c.amount_tax)}} · 已{{reg.doc.direction==='in'?'收':'开'}}票 {{fmt(c.invoice_sum)}}）</option></select></div>
        </div>
        <div class="sub-line">等同发票页「登记发票」：{{reg.doc.direction==='in' ? '进项税先记待认证，勾选后转进项' : '确认收入和销项'}}，凭证自动生成，档案自动挂上。</div>
        <div v-if="regErr" class="auth-error">{{regErr}}</div>
        <div class="actions"><button class="btn btn-ghost" @click="reg=null">取消</button><button class="btn btn-ink" :disabled="!reg.partner_id" @click="doRegister">登记</button></div>
      </div>
    </div>
  </div>`,
  data: () => ({ all: [], vat: { months: [] }, showVat: false, msg: '', editing: null, upDir: 'in', cats: INV_CATS, deducts: INV_DEDUCT,
                 reg: null, regErr: '', partners: [], contracts: [],
                 f: { dir: '', month: '', cat: '*', booked: '', deduct: '' },
                 dpill: { pending: 'p-draft', deducted: 'p-ok', nondeductible: 'p-gray', transferred: 'p-red' } }),
  computed: {
    months() { return [...new Set(this.all.map(r => r.issue_month).filter(x => x))].sort().reverse(); },
    regContracts() {
      if (!this.reg) return [];
      const t = this.reg.doc.direction === 'in' ? 'purchase' : 'sales';
      return this.contracts.filter(c => c.contract_type === t && c.partner_id === this.reg.partner_id);
    },
    rows() {
      const f = this.f;
      return this.all.filter(r => (!f.dir || r.direction === f.dir) && (!f.month || r.issue_month === f.month)
        && (f.cat === '*' || (r.category || '') === f.cat) && (!f.booked || (f.booked === 'yes') === r.booked)
        && (!f.deduct || r.deduct_status === f.deduct));
    },
    cols() {
      return [
        { key: 'issue_date', label: '开票日期', type: 'date' },
        { key: 'invoice_no', label: '发票号码', type: 'id', width: 176 },
        { key: 'direction', label: '方向', type: 'status', width: 60, value: r => r.direction === 'out' ? '开出' : '收到' },
        { key: 'kind', label: '票种', type: 'short', width: 76 },
        { key: 'counterparty', label: '对方', type: 'partner', width: 120, text: r => r.partner_short || r.counterparty, title: r => r.counterparty },
        { key: 'amount_ex_tax', label: '金额', type: 'money' },
        { key: 'tax_amount', label: '税额', type: 'money', width: 92 },
        { key: 'amount_tax', label: '价税合计', type: 'money' },
        { key: 'tax_rates', label: '税率', type: 'short', width: 60 },
        { key: 'category_label', label: '类别', type: 'short', width: 76 },
        { key: 'booked_ref', label: '入账', type: 'id', width: 130, value: r => r.invoice_contract || r.voucher_no || (r.category === 'none' ? '不入账' : '') },
        { key: 'booked_month', label: '入账月', type: 'short', width: 70 },
        { key: 'deduct_label', label: '抵扣', type: 'status', width: 76 },
        { key: 'deduct_period', label: '所属期', type: 'short', width: 70 },
        { key: 'files', label: '原件', type: 'short', width: 84, sortable: false, value: r => r.files.length },
        { key: 'status_label', label: '票状态', type: 'status', width: 70, hidden: true },
        { key: 'items', label: '品目', type: 'text', hidden: true },
        { key: 'face_remark', label: '票面备注', type: 'text', hidden: true },
        { key: 'note', label: '备注', type: 'text', hidden: true },
        { key: 'actions', label: '', type: 'actions', width: 60, sortable: false },
      ];
    },
  },
  methods: {
    fmt,
    edit(r) { this.editing = r; },
    async openRegister(r) {
      this.regErr = '';
      if (!this.partners.length) [this.partners, this.contracts] = await Promise.all([api.get('/api/partners'), api.get('/api/contracts?status=active')]);
      this.reg = { doc: r, partner_id: r.partner_id, contract_id: null };
    },
    async doRegister() {
      try { await api.post('/api/invoice-docs/' + this.reg.doc.id + '/register', { partner_id: this.reg.partner_id, contract_id: this.reg.contract_id }); this.reg = null; this.load(); }
      catch (e) { this.regErr = String(e); }
    },
    menu(r) {
      const a = [];
      if (!r.invoice_id && r.status === 'normal' && !r.voucher_id) a.push({ label: '登记为贸易发票（挂合同）', run: () => this.openRegister(r) });
      return a.concat([{ label: '标记 / 关联入账', run: () => this.edit(r) },
              { label: '重新自动关联', run: async () => { await api.post('/api/invoice-docs/' + r.id + '/relink', {}); this.load(); } }]);
    },
    async upload(ev) {
      const files = [...ev.target.files]; ev.target.value = '';
      const out = [];
      for (const f of files) {
        const buf = new Uint8Array(await f.arrayBuffer());
        let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
        try {
          const res = await api.post('/api/invoice-docs/upload', { name: f.name, content_b64: btoa(bin), direction: this.upDir });
          for (const r of res) out.push(r.ok ? (r.type ? (f.name + '：' + r.type + ' ' + JSON.stringify(r)) : (r.file + '：' + r.invoice_no + (r.new_file ? '' : '（文件已存在）'))) : (r.file + '：' + r.msg));
        } catch (e) { out.push(f.name + '：' + e); }
      }
      this.msg = out.join('\n');
      await this.load();
    },
    async load() {
      this.all = await api.get('/api/invoice-docs');
      if (this.showVat) this.vat = await api.get('/api/vat-ledger');
    },
  },
  watch: { showVat(v) { if (v) this.load(); } },
  mounted() { this.load(); },
});

/* ---------------- 纳税申报口径（v0.26）：已申报数登记 + 本期 = ERP 本年累计 − 上期已申报累计 ---------------- */
const TaxFilingView = defineComponent({
  template: `
  <div>
    <div class="filter-bar">
      <label class="sub">申报期间</label>
      <select v-model="period"><option v-for="p in periods" :key="p" :value="p">{{p}}</option></select>
      <span class="sub" v-if="r.start">{{r.start}} ~ {{r.end}}（本年累计）</span>
      <span style="margin-left:auto"></span>
      <a class="btn btn-ink" v-if="period.includes('-Q')" :href="'/api/tax-filings/export.xlsx?period=' + period" title="税局「财务报表报送与信息采集（小企业会计准则）」导入格式">① 导出税局导入文件</a>
      <label class="btn btn-ghost" title="申报后把实际提交的那份 xls/xlsx 传回来（网页上改过数先改文件）">② 导入已申报文件<input type="file" accept=".xls,.xlsx" style="display:none" @change="importFiled"></label>
      <button class="btn btn-ghost" @click="openEdit(period)">手工登记</button>
    </div>
    <div class="card" v-if="r.unlocked && r.unlocked.length && period.includes('-Q')" style="padding:10px 14px"><span class="neg">{{r.unlocked.join('、')}} 还没锁定：先结账锁期再导出，免得申报后账又变了。</span></div>
    <div class="card" v-if="imp" style="padding:10px 14px">
      <b>已登记 {{imp.period}}</b>（{{imp.start}} ~ {{imp.end}}，原件 {{imp.file}}）。
      <span v-if="!imp.diffs.length">与 ERP 导出数一致。</span>
      <span v-else class="neg">与 ERP 当前数不同的 {{imp.diffs.length}} 格（税局网页上改过的，或 ERP 后来又改了账）：</span>
      <table v-if="imp.diffs.length" style="margin-top:6px"><thead><tr><th>表</th><th class="num">行次</th><th>项目</th><th>列</th><th class="num">申报</th><th class="num">ERP</th></tr></thead>
        <tbody><tr v-for="(d, i) in imp.diffs.slice(0, 60)" :key="i"><td>{{d.sheet}}</td><td class="num">{{d.row}}</td><td>{{d.label}}</td><td>{{d.col}}</td><td class="num">{{fmt(d.filed)}}</td><td class="num">{{fmt(d.erp)}}</td></tr></tbody></table>
    </div>
    <div class="card" v-if="r.prior_missing" style="padding:10px 14px"><span class="neg">上期 {{r.prior}} 还没登记已申报数，本期数暂按上期 0 计算。</span></div>
    <div class="card">
      <div class="card-h"><b>利润表 · 申报口径</b><span class="sub">季度预缴按本年累计申报；本期 = ERP 本年累计 − 上期已申报累计，以前季度与 ERP 的差异全部落在本期</span></div>
      <table>
        <thead><tr><th>项目</th><th class="num">ERP 本年累计</th><th class="num">上期已申报累计（{{r.prior||'—'}}）</th><th class="num">本期申报数</th><th class="num">本期已登记申报累计</th></tr></thead>
        <tbody><tr v-for="x in r.rows" :key="x.key" :style="['profit_total','net_profit'].includes(x.key)?'font-weight:600':''">
          <td>{{x.label}}</td><td class="num">{{fmt(x.erp_ytd)}}</td><td class="num">{{fmt(x.prior_filed_ytd)}}</td>
          <td class="num">{{fmt(x.current)}}</td><td class="num">{{x.filed_ytd==null?'—':fmt(x.filed_ytd)}}</td></tr></tbody>
      </table>
    </div>
    <div class="card">
      <div class="card-h"><b>资产负债表 · 期末</b></div>
      <table><thead><tr><th>项目</th><th class="num">ERP</th><th class="num">已登记申报</th></tr></thead>
        <tbody><tr v-for="x in r.balance" :key="x.key"><td>{{x.label}}</td><td class="num">{{fmt(x.erp)}}</td><td class="num">{{x.filed==null?'—':fmt(x.filed)}}</td></tr></tbody></table>
      <div class="sub-line" v-if="r.assets_open != null" style="margin-top:6px">所得税预缴（A200000）按季度填报信息：资产总额 季初 {{fmt(r.assets_open)}}（{{fmt(r.assets_open/10000)}} 万元）· 季末 {{fmt(r.assets_close)}}（{{fmt(r.assets_close/10000)}} 万元）；从业人数按实际（现 1 人）</div>
      <div class="sub-line" v-if="r.payroll">附报事项「职工薪酬」（本年累计）：已计入成本费用 {{fmt(r.payroll.expensed)}}（工资 + 单位社保 + 单位公积金 + 职工福利）· 实际支付 {{fmt(r.payroll.paid)}}（银行发工资 + 缴社保公积金）</div>
    </div>
    <div class="card">
      <div class="card-h"><b>已登记的申报</b><span class="sub">均为本年累计数；2025 为年度</span></div>
      <table><thead><tr><th>期间</th><th class="num">营业收入</th><th class="num">利润总额</th><th class="num">所得税</th><th class="num">资产总额</th><th>申报日期</th><th>备注</th><th></th></tr></thead>
        <tbody><tr v-for="t in list" :key="t.id"><td class="mono">{{t.period}}</td><td class="num">{{fmt(t.data.revenue||0)}}</td><td class="num">{{fmt(t.data.profit_total||0)}}</td>
          <td class="num">{{fmt(t.data.income_tax||0)}}</td><td class="num">{{fmt(t.data.total_assets||0)}}</td><td class="mono">{{t.filed_date}}</td>
          <td class="sub-line">{{t.remark}} <a v-for="f in (t.data.files||[])" :key="f" class="clickable" :href="'/api/tax-filings/file?path=' + encodeURIComponent(f)" style="margin-left:6px">原件</a></td>
          <td><button class="btn btn-ghost btn-sm" @click="openEdit(t.period)">编辑</button></td></tr>
          <tr v-if="!list.length"><td colspan="8" class="empty">还没有登记</td></tr></tbody></table>
    </div>
    <div class="modal-mask" v-if="form" @click.self="form=null">
      <div class="modal" style="width:620px">
        <div class="modal-h"><b>登记已申报数 · {{form.period}}（本年累计）</b><span class="x" @click="form=null">✕</span></div>
        <div class="form-grid" style="grid-template-columns:1fr 1fr">
          <div class="fg" v-for="l in lines" :key="l.key"><label>{{l.label}}</label><input type="number" step="0.01" v-model.number="form.data[l.key]"></div>
          <div class="fg"><label>申报日期</label><input type="date" v-model="form.filed_date"></div>
          <div class="fg"><label>备注</label><input v-model="form.remark"></div>
        </div>
        <div class="sub-line"><button class="btn btn-ghost btn-sm" @click="fillErp">按 ERP 数预填</button> 按申报表实际填报的数填写，差异由下一期自动吸收。</div>
        <div v-if="err" class="auth-error">{{err}}</div>
        <div class="actions"><button class="btn btn-ghost" @click="form=null">取消</button><button class="btn btn-ink" @click="save">保存</button></div>
      </div>
    </div>
  </div>`,
  data() {
    const y = new Date().getFullYear(), q = Math.floor(new Date().getMonth() / 3) + 1;
    const pq = q === 1 ? (y - 1) + '-Q4' : y + '-Q' + (q - 1);   // 默认上一季度（季初申报上季）
    return { period: pq, r: { rows: [], balance: [] }, list: [], lines: [], form: null, err: '', imp: null };
  },
  computed: {
    periods() { const y = new Date().getFullYear(); return [(y - 1) + '-Q4', y + '-Q1', y + '-Q2', y + '-Q3', y + '-Q4', String(y), String(y - 1)]; },
  },
  watch: { period() { this.load(); } },
  methods: {
    fmt,
    async load() {
      [this.r, this.list] = await Promise.all([api.get('/api/tax-filings/report?period=' + this.period), api.get('/api/tax-filings')]);
      if (!this.lines.length) this.lines = await api.get('/api/tax-filings/lines');
    },
    openEdit(p) {
      const t = this.list.find(x => x.period === p);
      this.err = '';
      this.form = { period: p, data: { ...(t ? t.data : {}) }, filed_date: t ? t.filed_date || null : null, remark: t ? t.remark : '' };
    },
    async importFiled(ev) {
      const f = ev.target.files[0]; ev.target.value = '';
      if (!f) return;
      const buf = new Uint8Array(await f.arrayBuffer());
      let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
      try { this.imp = await api.post('/api/tax-filings/import', { name: f.name, content_b64: btoa(bin) }); this.period = this.imp.period; await this.load(); }
      catch (e) { alert(String(e)); }
    },
    async fillErp() {
      const r = await api.get('/api/tax-filings/report?period=' + this.form.period);
      for (const x of r.rows) this.form.data[x.key] = x.erp_ytd;
      for (const x of r.balance) this.form.data[x.key] = x.erp;
    },
    async save() {
      const data = {}; for (const [k, v] of Object.entries(this.form.data)) if (v !== '' && v !== null && v !== undefined) data[k] = Number(v);
      try { await api.put('/api/tax-filings', { ...this.form, data, filed_date: this.form.filed_date || null }); this.form = null; this.load(); }
      catch (e) { this.err = String(e); }
    },
  },
  mounted() { this.load(); },
});

/* ---------------- 小企业会计准则正式报表（v0.28）：会小企01表 / 会小企02表 ---------------- */
const StatementsView = defineComponent({
  template: `
  <div>
    <div class="filter-bar">
      <label class="sub">报表期间</label>
      <select v-model="period"><option v-for="p in periods" :key="p" :value="p">{{p}}</option></select>
      <span class="sub" v-if="d.end">{{d.standard}} · 资产负债表日 {{d.end}} · 利润表本期 {{d.start}} ~ {{d.end}}</span>
      <span class="pill" :class="d.balanced ? 'p-ok' : 'p-red'" v-if="d.end">{{d.balanced ? '资产 = 负债 + 所有者权益 ✓' : '不平衡'}}</span>
      <span style="margin-left:auto"></span>
      <a class="btn btn-ink" :href="'/api/statements/small.xlsx?period=' + period">导出 xlsx</a>
    </div>
    <div class="card" v-if="d.prior && !d.prior_filed" style="padding:10px 14px"><span class="neg">上期 {{d.prior}} 还没在「纳税申报」登记已申报数，利润表「本期（申报口径）」暂按账面上期累计计算。</span></div>
    <div class="card">
      <div class="card-h"><b>资产负债表</b><span class="sub">会小企01表 · 单位：元</span></div>
      <table class="stmt">
        <thead><tr><th>资产</th><th class="num">行次</th><th class="num">期末余额</th><th class="num">年初余额</th>
          <th>负债和所有者权益</th><th class="num">行次</th><th class="num">期末余额</th><th class="num">年初余额</th></tr></thead>
        <tbody><tr v-for="(x, i) in pairs" :key="i">
          <td :class="{b: x[0] && x[0].total}">{{x[0] ? x[0].label : ''}}</td><td class="num sub">{{x[0] ? x[0].row : ''}}</td>
          <td class="num" :class="{b: x[0] && x[0].total}">{{x[0] ? z(x[0].amount) : ''}}</td><td class="num sub">{{x[0] ? z(x[0].begin) : ''}}</td>
          <td :class="{b: x[1] && x[1].total}">{{x[1] ? x[1].label : ''}}</td><td class="num sub">{{x[1] ? x[1].row : ''}}</td>
          <td class="num" :class="{b: x[1] && x[1].total}">{{x[1] ? z(x[1].amount) : ''}}</td><td class="num sub">{{x[1] ? z(x[1].begin) : ''}}</td></tr></tbody>
      </table>
    </div>
    <div class="card">
      <div class="card-h"><b>利润表</b><span class="sub">会小企02表 · 单位：元 · 「本期（申报口径）」= 本年累计 − 上期已申报累计，填报用；「本期（账面）」= 本期实际发生额</span></div>
      <table class="stmt">
        <thead><tr><th>项目</th><th class="num">行次</th><th class="num">本年累计金额</th><th class="num">本期金额（申报口径）</th><th class="num">本期金额（账面）</th></tr></thead>
        <tbody><tr v-for="x in d.income_statement" :key="x.row">
          <td :class="{b: x.bold, ind: x.label.startsWith('其中') || /^(营业税|城市|资源|土地|城镇|教育|广告|业务招待|研究|无法|自然|税收|坏账|政府)/.test(x.label)}">{{x.label}}</td>
          <td class="num sub">{{x.row}}</td><td class="num" :class="{b: x.bold}">{{z(x.ytd)}}</td>
          <td class="num" :class="{b: x.bold}">{{z(x.period_filed)}}</td><td class="num sub">{{z(x.period_book)}}</td></tr></tbody>
      </table>
    </div>
  </div>`,
  data() {
    const y = new Date().getFullYear(), q = Math.floor(new Date().getMonth() / 3) + 1;
    return { period: y + '-Q' + q, d: { balance_sheet: [], income_statement: [] } };
  },
  computed: {
    periods() { const y = new Date().getFullYear(); return [y + '-Q1', y + '-Q2', y + '-Q3', y + '-Q4', String(y), String(y - 1)]; },
    pairs() {
      const a = this.d.balance_sheet.filter(x => x.side === 'asset'), l = this.d.balance_sheet.filter(x => x.side === 'liab');
      return Array.from({ length: Math.max(a.length, l.length) }, (_, i) => [a[i], l[i]]);
    },
  },
  watch: { period() { this.load(); } },
  methods: {
    z(v) { return v === null || v === undefined ? '' : (Math.abs(v) < 0.005 ? '' : fmt(v)); },
    async load() { this.d = await api.get('/api/statements/small?period=' + this.period); },
  },
  mounted() { this.load(); },
});

/* ---------------- 月结检查（v0.30）：与 docs/MONTH_END_CLOSE.md 步骤一一对应 ---------------- */
const CloseView = defineComponent({
  template: `
  <div>
    <div class="filter-bar">
      <label class="sub">结账月份</label>
      <input type="month" v-model="month" style="width:140px">
      <span class="sub" v-if="d.end">{{d.start}} ~ {{d.end}}</span>
      <span class="pill" :class="d.locked ? 'p-ok' : (d.todo ? 'p-draft' : 'p-info')" v-if="d.end">{{d.locked ? '已结账（已锁定）' : ('待处理 ' + d.todo + ' 项 · 关注 ' + d.warn + ' 项')}}</span>
      <span style="margin-left:auto" class="sub">步骤见仓库 docs/MONTH_END_CLOSE.md；资料放 月结/{{month}}/ 资料目录</span>
    </div>
    <div class="card">
      <table class="stmt">
        <thead><tr><th style="width:44px">步骤</th><th style="width:300px">检查项</th><th style="width:90px">状态</th><th class="num" style="width:70px">数量</th><th>说明</th><th style="width:70px"></th></tr></thead>
        <tbody><tr v-for="x in d.items" :key="x.key">
          <td class="mono sub">{{x.step}}</td><td>{{x.label}}</td>
          <td><span class="pill" :class="pill[x.status]">{{label[x.status]}}</span></td>
          <td class="num">{{x.count === null || x.count === undefined ? '' : x.count}}</td>
          <td class="sub-line" style="white-space:normal">{{x.detail}}
            <div v-for="r in (x.refs || []).slice(0, 15)" :key="r.type + r.id" style="margin-top:4px;color:var(--ink)">
              <template v-if="r.type==='bank_txn'"><span class="mono">{{r.date}}</span> {{r.counterparty}} <span class="mono">{{fmt(r.amount)}}</span>
                <div v-for="(s, i) in r.suggestions" :key="i" class="sub" style="padding-left:14px">→ {{s.text}}</div></template>
              <template v-else-if="r.type==='invoice_doc'">{{r.counterparty}} <span class="mono">{{r.invoice_no}}</span> {{r.issue_date}} <span class="mono">{{fmt(r.amount_tax)}}</span></template>
              <template v-else-if="r.type==='movement'"><span class="mono">{{r.doc_no}}</span> {{r.contract_no}} {{r.material}} ×{{r.qty}} <span class="sub">{{r.move_date}} {{r.remark}}</span></template>
            </div>
            <div v-if="(x.refs || []).length > 15" class="sub">…共 {{x.refs.length}} 条</div></td>
          <td><a v-if="x.link" class="clickable" @click="go(x.link)">去处理 ›</a></td></tr></tbody>
      </table>
    </div>
  </div>`,

  data() {
    const d = new Date(); d.setDate(0);          // 默认上个月（月初结上月）；月底当天可手动选本月
    const cur = new Date();
    const m = cur.getDate() >= 25 ? cur : d;
    return { month: m.getFullYear() + '-' + String(m.getMonth() + 1).padStart(2, '0'), d: { items: [] },
             pill: { ok: 'p-ok', todo: 'p-draft', warn: 'p-red', info: 'p-info' }, label: { ok: '完成', todo: '待处理', warn: '需关注', info: '提示' } };
  },
  watch: { month() { this.load(); } },
  methods: {
    fmt,
    go(k) { location.hash = '#/' + k; },
    async load() { this.d = await api.get('/api/close-check?month=' + this.month); },
  },
  mounted() { this.load(); },
});
