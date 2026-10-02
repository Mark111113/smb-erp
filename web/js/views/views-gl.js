/* ---------------- 财务总账：凭证 / 明细账 / 余额表 / 往来账 / 成本中心 / 报表 ---------------- */
const SOURCE_LABELS = { manual: '手工', invoice_output: '销售开票', invoice_input: '采购收票', allocation:'票款核销', stock_adjust:'库存调整', stock_revalue:'成本调整', stock_transfer:'组拆套', goods_in: '收货暂估', goods_out: '出库结转', payment_in: '收款', payment_out: '付款', reversal: '红冲', closing: '期末结转', bank: '银行流水入账', asset: '固定资产入账', depreciation: '计提折旧', vat_deduct: '进项勾选', vat_transfer: '进项转出' };

const VOUCHER_COLS = SL => {
      return [
        { key: 'voucher_no', label: '凭证号', type: 'id', width: 136 },
        { key: 'voucher_date', label: '日期', type: 'date' },
        { key: 'summary', label: '摘要', type: 'text' },
        { key: 'source_type', label: '来源', type: 'status', width: 92, value: v => SL[v.source_type] || v.source_type },
        { key: 'total', label: '金额', type: 'money' },
        { key: 'line_count', label: '行数', type: 'qty', width: 56, hidden: true },
        { key: 'created_by', label: '制单', type: 'person', width: 96,
          value: v => v.created_by ? (v.source_type === 'manual' ? v.created_by : '自动·' + v.created_by) : '',
          title: v => v.source_type === 'manual' ? '' : '业务单据自动过账，记在触发的操作人名下' },
        { key: 'status', label: '状态', type: 'status', width: 76, value: v => v.status === 'posted' ? '已过账' : '已冲销' },
      ];
};

const VouchersView = defineComponent({
  components: { DataTable },
  template: `
  <div>
    <div class="filter-bar">
      <select v-model="f.source" @change="load"><option value="">全部来源</option><option v-for="(l,k) in SL" :value="k">{{l}}</option></select>
      <button class="btn btn-ink" style="margin-left:auto" @click="openNew">＋ 手工凭证</button>
    </div>
    <div class="card">
      <data-table view="vouchers" date-key="voucher_date" :columns="cols" :rows="list" export-name="会计凭证" empty="无凭证"
                  :row-click="v => show(v.id)" :row-class="v => v.status!=='posted' ? 'dim' : ''">
        <template #cell-source_type="{ row }"><span class="pill p-gray" :title="SL[row.source_type]||row.source_type">{{SL[row.source_type]||row.source_type}}</span></template>
        <template #cell-status="{ row }"><span class="pill" :class="row.status==='posted'?'p-ok':'p-red'">{{row.status==='posted'?'已过账':'已冲销'}}</span></template>
      </data-table>
    </div>
    <div class="modal-mask" v-if="detail" @click.self="detail=null">
      <div class="modal" style="width:760px">
        <div class="modal-h"><b>{{detail.voucher_no}} · {{detail.voucher_date}}</b><span class="x" @click="detail=null">✕</span></div>
        <button v-if="detail.source_type==='manual'&&detail.status==='posted'" class="btn btn-ghost" @click="reverseManual">红冲</button><div class="sub" style="margin:2px 0 8px">{{detail.summary}} · {{SL[detail.source_type]||detail.source_type}}{{detail.status==='reversed'?' · 已被红冲':''}}</div>
        <table>
          <thead><tr><th>#</th><th>科目</th><th>往来 / 成本中心</th><th>摘要</th><th class="r">借方</th><th class="r">贷方</th></tr></thead>
          <tbody>
            <tr v-for="l in detail.lines" :key="l.line_no">
              <td class="mono">{{l.line_no}}</td>
              <td class="mono">{{l.account}} {{l.account_name}}</td>
              <td class="sub">{{l.partner_name}}{{l.cost_center_name?(' · '+l.cost_center_name):''}}</td>
              <td class="sub">{{l.summary}}</td>
              <td class="mono r">{{l.debit?fmt(l.debit):''}}</td>
              <td class="mono r">{{l.credit?fmt(l.credit):''}}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div class="modal-mask" v-if="showForm" @click.self="showForm=false">
      <div class="modal" style="width:780px">
        <div class="modal-h"><b>手工凭证</b><span class="x" @click="showForm=false">✕</span></div>
        <div class="form-grid" style="grid-template-columns:160px 1fr">
          <div class="fg"><label>凭证日期</label><input type="date" v-model="form.voucher_date"></div>
          <div class="fg"><label>摘要</label><input v-model="form.summary"></div>
        </div>
        <table style="margin-top:6px">
          <thead><tr><th>科目</th><th>往来单位</th><th>成本中心</th><th class="r">借</th><th class="r">贷</th><th>行摘要</th><th></th></tr></thead>
          <tbody>
            <tr v-for="(l,i) in form.lines" :key="i">
              <td><select v-model="l.account"><option v-for="a in accounts" :value="a.code">{{a.code}} {{a.name}}</option></select></td>
              <td><select v-model="l.partner_id"><option :value="null">—</option><option v-for="p in partners" :value="p.id">{{p.short_name||p.name}}</option></select></td>
              <td><select v-model="l.cost_center_id"><option :value="null">—</option><option v-for="c in ccs" :value="c.id">{{c.full}}</option></select></td>
              <td><input v-model.number="l.debit" style="width:90px" class="r"></td>
              <td><input v-model.number="l.credit" style="width:90px" class="r"></td>
              <td><input v-model="l.summary" style="width:130px"></td>
              <td><button class="btn btn-ghost btn-sm" @click="form.lines.splice(i,1)">✕</button></td>
            </tr>
          </tbody>
        </table>
        <div style="margin:6px 0" class="mono">
          借合计 {{fmt(sumD)}} · 贷合计 {{fmt(sumC)}} · <span :class="balanced?'':'p-red'" style="padding:2px 8px">{{balanced?'平 ✓':'差 ' + fmt(Math.abs(sumD-sumC))}}</span>
        </div>
        <div class="actions">
          <button class="btn btn-ghost btn-sm" @click="form.lines.push({account:accounts[0]?.code,debit:0,credit:0,partner_id:null,cost_center_id:null,summary:''})">＋ 行</button>
          <button class="btn btn-ghost" @click="showForm=false">取消</button>
          <button class="btn btn-ink" :disabled="!balanced" @click="save">过账</button>
        </div>
      </div>
    </div>
  </div>`,
  data: () => ({ SL: SOURCE_LABELS, list: [], detail: null, showForm: false,
    f: { from: '', to: '', source: '' }, form: { voucher_date: today(), summary: '', lines: [] },
    accounts: [], partners: [], ccs: [] }),
  computed: {
    cols() { return VOUCHER_COLS(this.SL); },
    sumD() { return +this.form.lines.reduce((s, l) => s + (+l.debit || 0), 0).toFixed(2); },
    sumC() { return +this.form.lines.reduce((s, l) => s + (+l.credit || 0), 0).toFixed(2); },
    balanced() { return this.form.lines.length >= 2 && Math.abs(this.sumD - this.sumC) < 0.005 && this.sumD > 0; },
  },
  methods: {
    fmt,
    async load() {
      const q = new URLSearchParams(Object.entries({from_date:this.f.from,to_date:this.f.to,source_type:this.f.source}).filter(([,v])=>v)).toString();
      this.list = await api.get('/api/vouchers?' + q);
    },
    async reverseManual(){const reason=prompt('红冲原因');if(!reason?.trim())return;try{await api.post('/api/vouchers/'+this.detail.id+'/reverse',{reason});this.detail=null;await this.load()}catch(e){alert(e)}},
    async show(id) { this.detail = await api.get('/api/vouchers/' + id); },
    async openNew() {
      this.accounts = await api.get('/api/accounts');
      this.partners = await api.get('/api/partners');
      this.ccs = await api.get('/api/cost-centers');
      this.form = { voucher_date: today(), summary: '', lines: [
        { account: this.accounts[0]?.code, debit: 0, credit: 0, partner_id: null, cost_center_id: null, summary: '' },
        { account: this.accounts[0]?.code, debit: 0, credit: 0, partner_id: null, cost_center_id: null, summary: '' },
      ] };
      this.showForm = true;
    },
    async save() {
      try {
        const r = await api.post('/api/vouchers', this.form);
        this.showForm = false; this.load();
      } catch (e) { alert(e.message || '保存失败'); }
    },
  },
  mounted() { this.load(); },
});

const LedgerView = defineComponent({
  template: `
  <div>
    <div class="filter-bar">
      <select v-model="f.account"><option v-for="a in accounts" :value="a.code">{{a.code}} {{a.name}}</option></select>
      <select v-model="f.partner_id"><option :value="null">全部往来单位</option><option v-for="p in partners" :value="p.id">{{p.short_name||p.name}}</option></select>
      <input type="date" v-model="f.from"> ~ <input type="date" v-model="f.to">
      <button class="btn btn-ghost btn-sm" @click="load">查询</button>
    </div>
    <div class="card">
      <table>
        <thead><tr><th>日期</th><th>凭证号</th><th>摘要</th><th>来源</th><th class="r">借方</th><th class="r">贷方</th><th class="r">余额</th></tr></thead>
        <tbody>
          <tr v-for="(r,i) in rows" :key="i">
            <td>{{r.date}}</td><td class="mono">{{r.voucher_no}}</td>
            <td style="max-width:300px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" :title="r.summary">{{r.summary}}</td>
            <td class="sub">{{SL[r.source_type]||''}}</td>
            <td class="mono r">{{r.debit?fmt(r.debit):''}}</td>
            <td class="mono r">{{r.credit?fmt(r.credit):''}}</td>
            <td class="mono r"><b>{{fmt(r.balance)}}</b></td>
          </tr>
          <tr v-if="!rows.length"><td colspan="7" class="empty">无记录</td></tr>
        </tbody>
      </table>
    </div>
  </div>`,
  data: () => ({ SL: SOURCE_LABELS, accounts: [], partners: [], rows: [],
    f: { account: '', partner_id: null, from: '', to: '' } }),
  methods: {
    fmt,
    async load() {
      const q = new URLSearchParams({ account: this.f.account });
      if (this.f.partner_id) q.set('partner_id', this.f.partner_id);
      if (this.f.from) q.set('from_date', this.f.from);
      if (this.f.to) q.set('to_date', this.f.to);
      this.rows = await api.get('/api/finance/ledger?' + q.toString());
    },
  },
  async mounted() {
    this.accounts = await api.get('/api/accounts');
    this.partners = await api.get('/api/partners');
    const preset = sessionStorage.getItem('owe.ledger.preset');
    if (preset) {
      const p = JSON.parse(preset);
      sessionStorage.removeItem('owe.ledger.preset');
      this.f.account = p.account;
      this.f.partner_id = p.partner_id ?? null;
    } else {
      this.f.account = (this.accounts.find(a => a.code === '1122') || this.accounts[0] || {}).code;
    }
    if (this.f.account) this.load();
  },
});

const TrialBalanceView = defineComponent({
  template: `
  <div>
    <div class="filter-bar">
      <input type="date" v-model="from"> ~ <input type="date" v-model="to">
      <button class="btn btn-ghost btn-sm" @click="load">查询</button>
      <span class="sub" style="margin-left:auto">合计借贷差：<b :class="diff===0?'':'p-red'">{{fmt(diff)}}</b></span>
    </div>
    <div class="card">
      <table>
        <thead><tr><th>科目</th><th>名称</th><th>类型</th><th class="r">期初</th><th class="r">本期借</th><th class="r">本期贷</th><th class="r">期末</th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.code">
            <td class="mono">{{r.code}}</td><td>{{r.name}}</td><td class="sub">{{TYPE[r.acc_type]}}</td>
            <td class="mono r">{{fmt(r.opening)}}</td><td class="mono r">{{fmt(r.debit)}}</td>
            <td class="mono r">{{fmt(r.credit)}}</td><td class="mono r"><b>{{fmt(r.closing)}}</b></td>
          </tr>
          <tr v-if="!rows.length"><td colspan="7" class="empty">无数据</td></tr>
        </tbody>
        <tfoot v-if="rows.length">
          <tr><td colspan="3"><b>合计</b></td>
            <td class="mono r"><b>{{fmt(totOpening)}}</b></td><td class="mono r"><b>{{fmt(totDebit)}}</b></td>
            <td class="mono r"><b>{{fmt(totCredit)}}</b></td><td class="mono r"><b>{{fmt(totClosing)}}</b></td></tr>
        </tfoot>
      </table>
    </div>
  </div>`,
  data: () => ({ TYPE: { asset: '资产', liability: '负债', equity: '权益', revenue: '收入', expense: '费用' },
    rows: [], from: '', to: today() }),
  computed: {
    totDebit() { return +this.rows.reduce((s, r) => s + r.debit, 0).toFixed(2); },
    totCredit() { return +this.rows.reduce((s, r) => s + r.credit, 0).toFixed(2); },
    totOpening() { return +this.rows.reduce((s, r) => s + r.opening, 0).toFixed(2); },
    totClosing() { return +this.rows.reduce((s, r) => s + r.closing, 0).toFixed(2); },
    diff() { return +(this.totDebit - this.totCredit).toFixed(2); },
  },
  methods: {
    fmt,
    async load() {
      const q = new URLSearchParams();
      if (this.from) q.set('from_date', this.from);
      if (this.to) q.set('to_date', this.to);
      this.rows = await api.get('/api/finance/trial-balance?' + q.toString());
    },
  },
  mounted() { this.load(); },
});

const ArApView = defineComponent({
  template: `
  <div>
    <div class="filter-bar"><span class="sub">应收/预收按客户、应付/预付按供应商，正=我方债权/债务，负=反向余额</span></div>
    <div class="card">
      <table>
        <thead><tr><th>单位</th><th>科目</th><th class="r">余额</th><th></th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.partner_id + r.account">
            <td>{{r.partner_code}} {{r.partner_name}}</td>
            <td class="mono">{{r.account}} {{r.account_name}}</td>
            <td class="mono r" :class="r.balance<0?'p-red':''"><b>{{fmt(r.balance)}}</b></td>
            <td><button class="btn btn-ghost btn-sm" @click="drill(r)">明细</button></td>
          </tr>
          <tr v-if="!rows.length"><td colspan="4" class="empty">无往来余额</td></tr>
        </tbody>
      </table>
    </div>
  </div>`,
  data: () => ({ rows: [] }),
  methods: {
    fmt,
    async load() { this.rows = await api.get('/api/finance/ar-ap'); },
    drill(r) { location.hash = '#/ledger'; sessionStorage.setItem('owe.ledger.preset', JSON.stringify({ account: r.account, partner_id: r.partner_id })); },
  },
  mounted() { this.load(); },
});

const CostCentersView = defineComponent({
  template: `
  <div>
    <div class="filter-bar">
      <span class="sub">树形架构：费用/损益凭证行挂成本中心归集</span>
      <button class="btn btn-ink" style="margin-left:auto" @click="open()">＋ 新建</button>
    </div>
    <div class="card">
      <table>
        <thead><tr><th>代码</th><th>名称</th><th>层级路径</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-for="c in list" :key="c.id">
            <td class="mono">{{c.code}}</td><td>{{c.name}}</td><td class="sub">{{c.full}}</td>
            <td><span class="pill" :class="c.active?'p-ok':'p-gray'">{{c.active?'在用':'停用'}}</span></td>
            <td><button class="btn btn-ghost btn-sm" @click="open(c)">编辑</button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="modal-mask" v-if="showForm" @click.self="showForm=false">
      <div class="modal" style="width:420px">
        <div class="modal-h"><b>{{editId?'编辑':'新建'}}成本中心</b><span class="x" @click="showForm=false">✕</span></div>
        <div class="form-grid">
          <div class="fg"><label>代码</label><input v-model="f.code"></div>
          <div class="fg"><label>名称</label><input v-model="f.name"></div>
          <div class="fg span2"><label>父级</label>
            <select v-model="f.parent_id"><option :value="null">（顶层）</option><option v-for="c in list.filter(x=>x.id!==editId)" :value="c.id">{{c.full}}</option></select>
          </div>
          <div class="fg span2"><label>备注</label><input v-model="f.memo"></div>
        </div>
        <div class="actions"><button class="btn btn-ghost" @click="showForm=false">取消</button><button class="btn btn-ink" @click="save">保存</button></div>
      </div>
    </div>
  </div>`,
  data: () => ({ list: [], showForm: false, editId: null, f: {} }),
  methods: {
    async load() { this.list = await api.get('/api/cost-centers'); },
    open(c) {
      this.editId = c ? c.id : null;
      this.f = c ? { code: c.code, name: c.name, parent_id: c.parent_id, memo: c.memo } : { code: '', name: '', parent_id: null, memo: '' };
      this.showForm = true;
    },
    async save() {
      if (this.editId) await api.put('/api/cost-centers/' + this.editId, this.f);
      else await api.post('/api/cost-centers', this.f);
      this.showForm = false; this.load();
    },
  },
  mounted() { this.load(); },
});

const ReportsView = defineComponent({
  template: `
  <div>
    <div class="filter-bar">
      <input type="date" v-model="from"> ~ <input type="date" v-model="to">
      <button class="btn btn-ghost btn-sm" @click="load">查询</button>
      <button class="btn btn-ink btn-sm" style="margin-left:auto" @click="exportLedger">导出进销存台账 xlsx</button>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div class="card">
        <h3 style="margin:0 0 8px">利润表</h3>
        <table>
          <tbody>
            <tr><td>主营业务收入</td><td class="mono r">{{fmt(is.revenue)}}</td></tr>
            <tr><td>减：主营业务成本</td><td class="mono r">{{fmt(is.cogs)}}</td></tr>
            <template v-for="(v,k) in is.expenses" :key="k"><tr v-if="Math.abs(v)>0.005"><td class="sub">减：{{expName(k)}}</td><td class="mono r">{{fmt(v)}}</td></tr></template>
            <tr><td><b>净利润</b></td><td class="mono r"><b :class="is.profit>=0?'':'p-red'">{{fmt(is.profit)}}</b></td></tr>
          </tbody>
        </table>
      </div>
      <div class="card">
        <h3 style="margin:0 0 8px">资产负债表 <span class="sub">截至 {{asof}}</span> <span class="pill" :class="bs.balanced?'p-ok':'p-red'">{{bs.balanced?'资产=负债+权益 ✓':'不配平'}}</span></h3>
        <table>
          <tbody>
            <tr v-for="r in bs.assets" :key="r.code"><td>{{r.name}}</td><td class="mono r">{{fmt(r.balance)}}</td></tr>
            <tr><td><b>资产合计</b></td><td class="mono r"><b>{{fmt(bs.total_assets)}}</b></td></tr>
            <tr v-for="r in bs.liabilities" :key="r.code"><td>{{r.name}}</td><td class="mono r">{{fmt(r.balance)}}</td></tr>
            <tr><td><b>负债合计</b></td><td class="mono r"><b>{{fmt(bs.total_liabilities)}}</b></td></tr>
            <tr v-for="r in bs.equity" :key="r.code"><td>{{r.name}}</td><td class="mono r">{{fmt(r.balance)}}</td></tr>
            <tr><td><b>权益合计</b></td><td class="mono r"><b>{{fmt(bs.total_equity)}}</b></td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>`,
  data: () => ({ from: '', to: today(), asof: today(), is: { revenue: 0, cogs: 0, expenses: {}, profit: 0 }, bs: { assets: [], liabilities: [], equity: [], total_assets: 0, total_liabilities: 0, total_equity: 0, balanced: true } }),
  methods: {
    fmt,
    expName(code) { return (this.is.expense_names || {})[code] || ({ '6601': '销售费用', '6602': '管理费用', '6603': '财务费用' }[code] || ('费用 ' + code)); },
    exportLedger() { window.open('/api/ledger-export', '_blank'); },
    async load() {
      const q = new URLSearchParams({ from_date: this.from || '2000-01-01', to_date: this.to });
      this.is = await api.get('/api/finance/income-statement?' + q.toString());
      this.bs = await api.get('/api/finance/balance-sheet?asof=' + this.to);
    },
  },
  mounted() { this.load(); },
});
