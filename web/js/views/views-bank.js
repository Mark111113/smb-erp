/* owe-erp 前端 — 银行流水（v0.24）：导入中行 CSV、匹配收付款、非贸易分类入账、月末余额核对 */

const BANK_CATEGORIES = ['股东投资', '工程/装修', '固定资产', '房产', '费用', '税费', '工资社保', '利息/手续费', '往来/其他'];

/* 流水关联收付款：列出候选（同单位临近日期或同金额），合计须等于流水金额 */
const BankLinkForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:720px">
      <div class="modal-h"><b>关联收付款 · {{txn.txn_date}} {{txn.counterparty_name}} {{fmt(Math.abs(txn.amount))}}</b><span class="x" @click="$emit('close')">✕</span></div>
      <table>
        <thead><tr><th></th><th>日期</th><th>对方</th><th>合同</th><th class="num">金额</th><th>备注</th></tr></thead>
        <tbody>
          <tr v-for="p in list" :key="p.id">
            <td><input type="checkbox" style="width:auto" :value="p.id" v-model="picked"></td>
            <td class="mono">{{p.pay_date}}</td><td>{{p.partner_short}}</td><td class="mono">{{p.contract_no||'—'}}</td>
            <td class="num">{{fmt(p.amount)}}</td><td class="sub-line">{{p.remark}}</td></tr>
          <tr v-if="!list.length"><td colspan="6" class="empty">没有候选收付款：贸易款请先在收付款页登记（或拆分），非贸易款用「入账」</td></tr>
        </tbody>
      </table>
      <div class="sub-line" style="padding-top:8px">已选合计 <b class="mono">{{fmt(sum)}}</b>，须等于流水金额 {{fmt(Math.abs(txn.amount))}}</div>
      <div v-if="err" class="auth-error">{{err}}</div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button>
        <button class="btn btn-ink" :disabled="busy || !picked.length" @click="save">关联</button></div>
    </div>
  </div>`,
  props: { txn: Object },
  emits: ['close', 'saved'],
  data: () => ({ list: [], picked: [], err: '', busy: false }),
  computed: { sum() { return this.list.filter(p => this.picked.includes(p.id)).reduce((s, p) => s + p.amount, 0); } },
  methods: {
    fmt,
    async save() {
      this.busy = true; this.err = '';
      try { await api.post('/api/bank/txns/' + this.txn.id + '/link', { payment_ids: this.picked }); this.$emit('saved'); this.$emit('close'); }
      catch (e) { this.err = String(e); } finally { this.busy = false; }
    },
  },
  async mounted() { this.list = await api.get('/api/bank/txns/' + this.txn.id + '/candidates'); },
});

/* 流水入账：银行存款一侧自动，另一侧选科目（可多行拆分） */
const BankPostForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:820px">
      <div class="modal-h"><b>入账 · {{txn.txn_date}} {{txn.amount>0?'收入':'支出'}} {{fmt(Math.abs(txn.amount))}}</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="sub-line" style="margin-bottom:8px">{{txn.counterparty_name}} · {{[txn.biz_type, txn.purpose, txn.memo].filter(x=>x).join(' · ')}}</div>
      <div class="form-grid" style="grid-template-columns:1fr 2fr">
        <div class="fg"><label>类别</label><select v-model="category"><option v-for="c in cats" :key="c" :value="c">{{c}}</option></select></div>
        <div class="fg"><label>凭证摘要</label><input v-model="summary" :placeholder="txn.counterparty_name"></div>
      </div>
      <table>
        <thead><tr><th style="width:34%">{{txn.amount>0?'贷方':'借方'}}科目（银行存款在另一方，自动）</th><th style="width:16%" class="num">金额</th><th style="width:20%">往来单位</th><th>行摘要</th><th></th></tr></thead>
        <tbody>
          <tr v-for="(l,i) in lines" :key="i">
            <td><select v-model="l.account"><option value="">选科目</option><option v-for="a in leafs" :key="a.code" :value="a.code">{{a.code}} {{a.name}}</option></select></td>
            <td><input type="number" step="0.01" v-model.number="l.amount" style="text-align:right"></td>
            <td><select v-model="l.partner_id"><option :value="null">—</option><option v-for="p in partners" :key="p.id" :value="p.id">{{p.short_name||p.name}}</option></select></td>
            <td><input v-model="l.summary"></td>
            <td><span class="x clickable" v-if="lines.length>1" @click="lines.splice(i,1)">✕</span></td></tr>
        </tbody>
      </table>
      <div style="padding:8px 0"><button class="btn btn-ghost btn-sm" @click="addLine">＋ 再加一行</button>
        <span class="sub-line" style="margin-left:12px">合计 <b class="mono">{{fmt(total)}}</b> / 流水 {{fmt(Math.abs(txn.amount))}}</span></div>
      <div class="sub-line">生成的凭证可在「会计凭证」查看；入错了用「撤销入账」（红冲）后重入。没有合适科目先在科目余额表旁的科目维护里加。</div>
      <div v-if="err" class="auth-error">{{err}}</div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button>
        <button class="btn btn-ink" :disabled="busy || !ok" @click="save">入账</button></div>
    </div>
  </div>`,
  props: { txn: Object },
  emits: ['close', 'saved'],
  data: () => ({ accounts: [], partners: [], lines: [], category: '费用', summary: '', err: '', busy: false, cats: BANK_CATEGORIES }),
  computed: {
    leafs() {
      const parents = new Set(this.accounts.map(a => a.parent_code).filter(x => x));
      return this.accounts.filter(a => a.active && !parents.has(a.code) && a.code !== '100201');
    },
    total() { return Math.round(this.lines.reduce((s, l) => s + (Number(l.amount) || 0), 0) * 100) / 100; },
    ok() { return this.lines.every(l => l.account && Number(l.amount) > 0) && Math.abs(this.total - Math.abs(this.txn.amount)) < 0.005; },
  },
  methods: {
    fmt,
    addLine() {
      const rest = Math.round((Math.abs(this.txn.amount) - this.total) * 100) / 100;
      this.lines.push({ account: '', amount: rest > 0 ? rest : null, partner_id: null, summary: '' });
    },
    async save() {
      this.busy = true; this.err = '';
      try {
        await api.post('/api/bank/txns/' + this.txn.id + '/post', { category: this.category, summary: this.summary, lines: this.lines });
        this.$emit('saved'); this.$emit('close');
      } catch (e) { this.err = String(e); } finally { this.busy = false; }
    },
  },
  async mounted() {
    this.addLine();
    [this.accounts, this.partners] = await Promise.all([api.get('/api/accounts'), api.get('/api/partners')]);
  },
});

const BankView = defineComponent({
  components: { DataTable, BankLinkForm, BankPostForm },
  template: `
  <div>
    <div class="filter-bar">
      <select v-model="f.status"><option value="">全部</option><option value="unmatched">未处理</option><option value="matched">已匹配收付款</option>
        <option value="posted">已入账</option><option value="ignored">已对冲/忽略</option></select>
      <span class="sub">未处理 {{counts.unmatched||0}} · 已匹配 {{counts.matched||0}} · 已入账 {{counts.posted||0}} · 忽略 {{counts.ignored||0}}</span>
      <span style="margin-left:auto"></span>
      <button class="btn btn-ghost" @click="showRec=!showRec">{{showRec?'收起':'月末余额核对'}}</button>
      <button class="btn btn-ghost" @click="autoMatch">自动匹配</button>
      <label class="btn btn-ink" style="margin-left:6px">导入中行流水 CSV<input type="file" accept=".csv" multiple style="display:none" @change="importFiles"></label>
    </div>
    <div v-if="msg" class="card" style="padding:10px 14px">{{msg}}</div>
    <div class="card" v-if="showRec">
      <div class="card-h"><b>月末余额核对</b><span class="sub">流水余额（按当月流水累计，与银行「交易后余额」互校）vs 账面 100201 银行存款</span></div>
      <table>
        <thead><tr><th>账户</th><th>月份</th><th class="num">流水月末余额</th><th class="num">银行交易后余额</th><th class="num">账面银行存款</th><th class="num">差额</th><th class="num">未处理笔数</th></tr></thead>
        <tbody><tr v-for="r in rec" :key="r.account_no + r.month">
          <td class="mono sub-line">{{r.account_no}}</td><td class="mono">{{r.month}}</td><td class="num">{{fmt(r.statement)}}</td><td class="num">{{fmt(r.last_balance)}}</td>
          <td class="num">{{fmt(r.book)}}</td><td class="num" :class="{neg: Math.abs(r.gap)>0.005}">{{fmt(r.gap)}}</td>
          <td class="num">{{r.unmatched}} / {{r.total}}</td></tr></tbody>
      </table>
    </div>
    <div class="card">
      <data-table view="bank" date-key="txn_date" :columns="cols" :rows="list" export-name="银行流水" empty="还没有导入流水" :menu="menuItems">
        <template #cell-status="{ row }"><span class="pill" :class="pill[row.status]">{{row.status_label}}</span></template>
        <template #cell-link="{ row }">
          <div v-if="row.payments.length" class="dt-clamp">
            <span v-for="p in row.payments" :key="p.id" class="mono clickable" style="margin-right:6px" @click.stop="go(p.contract_id ? 'contract/'+p.contract_id : 'payments')">{{p.contract_no||('款#'+p.id)}}</span></div>
          <div v-else-if="row.voucher_no" class="mono dt-nowrap">{{row.voucher_no}} <span class="sub-line">{{row.category}}</span></div>
          <div v-else-if="row.note" class="sub-line dt-clamp" :title="row.note">{{row.note}}</div>
          <div v-else class="sub-line">—</div></template>
      </data-table>
    </div>
    <bank-link-form v-if="linking" :txn="linking" @close="linking=null" @saved="load"></bank-link-form>
    <bank-post-form v-if="posting" :txn="posting" @close="posting=null" @saved="load"></bank-post-form>
  </div>`,
  data: () => ({ all: [], rec: [], showRec: false, msg: '', linking: null, posting: null, f: { status: '' },
                 pill: { unmatched: 'p-draft', matched: 'p-ok', posted: 'p-info', ignored: 'p-gray' } }),
  computed: {
    counts() { const c = {}; for (const r of this.all) c[r.status] = (c[r.status] || 0) + 1; return c; },
    list() { return this.f.status ? this.all.filter(r => r.status === this.f.status) : this.all; },
    cols() {
      return [
        { key: 'txn_date', label: '日期', type: 'date' },
        { key: 'txn_time', label: '时间', type: 'short', width: 70, hidden: true },
        { key: 'income', label: '收入', type: 'money' },
        { key: 'expense', label: '支出', type: 'money' },
        { key: 'balance', label: '交易后余额', type: 'money', width: 116, hidden: true },
        { key: 'counterparty_name', label: '对方户名', type: 'text', min: 160 },
        { key: 'purpose', label: '用途/附言', type: 'text', value: r => [r.purpose, r.memo].filter(x => x).join(' · ') || r.summary },
        { key: 'biz_type', label: '业务类型', type: 'short', width: 76 },
        { key: 'status', label: '状态', type: 'status', value: r => r.status_label },
        { key: 'link', label: '关联合同 / 凭证', type: 'text', width: 160, value: r => r.payments.map(p => p.contract_no).join(' ') || r.voucher_no || r.note },
        { key: 'category', label: '类别', type: 'short', width: 80, hidden: true },
        { key: 'counterparty_account', label: '对方账号', type: 'id', hidden: true },
        { key: 'counterparty_bank', label: '对方开户行', type: 'text', hidden: true },
        { key: 'txn_no', label: '交易流水号', type: 'id', hidden: true },
        { key: 'source_file', label: '来源文件', type: 'text', hidden: true },
        { key: 'actions', label: '', type: 'actions', width: 60, sortable: false },
      ];
    },
  },
  methods: {
    fmt,
    go(k) { location.hash = '#/' + k; },
    menuItems(r) {
      const a = [];
      if (r.status === 'unmatched') {
        a.push({ label: '关联收付款', run: () => { this.linking = r; } });
        a.push({ label: '入账（非贸易）', run: () => { this.posting = r; } });
        a.push({ label: '忽略', run: () => this.act(r, 'ignore', prompt('忽略原因（如：与某笔对冲、测试转账）')) });
      }
      if (r.status === 'matched') {
        a.push({ label: '补关联收付款', run: () => { this.linking = r; } });
        a.push({ label: '取消关联', danger: true, run: () => this.act(r, 'unlink') });
      }
      if (r.status === 'posted') a.push({ label: '撤销入账（红冲）', danger: true, run: () => this.act(r, 'unpost', prompt('撤销原因')) });
      if (r.status === 'ignored') a.push({ label: '恢复为未处理', run: () => this.act(r, 'reopen') });
      return a;
    },
    async act(r, what, note) {
      if ((what === 'ignore' || what === 'unpost') && !(note || '').trim()) return;
      try { await api.post('/api/bank/txns/' + r.id + '/' + what, { note: note || '' }); await this.load(); }
      catch (e) { alert(e); }
    },
    async importFiles(ev) {
      const files = [...ev.target.files]; ev.target.value = '';
      const out = [];
      for (const f of files) {
        try {
          const r = await api.post('/api/bank/import', { filename: f.name, content: await f.text() });
          out.push(`${f.name}：解析 ${r.parsed} 笔，新增 ${r.added}，重复 ${r.duplicate}，自动匹配 ${r.matched}`);
        } catch (e) { out.push(`${f.name}：${e}`); }
      }
      this.msg = out.join('；');
      await this.load();
    },
    async autoMatch() { const r = await api.post('/api/bank/auto-match', {}); this.msg = `自动匹配 ${r.matched} 笔，冲正对冲 ${r.ignored} 笔`; await this.load(); },
    async load() {
      this.all = await api.get('/api/bank/txns');
      if (this.showRec) this.rec = await api.get('/api/bank/reconcile');
    },
  },
  watch: { showRec(v) { if (v) this.load(); } },
  mounted() { this.load(); },
});

/* ---------------- 固定资产台账与折旧（v0.25） ---------------- */
const AssetForm = defineComponent({
  template: `
  <div class="modal-mask" @click.self="$emit('close')">
    <div class="modal" style="width:820px">
      <div class="modal-h"><b>登记固定资产</b><span class="x" @click="$emit('close')">✕</span></div>
      <div class="form-grid" style="grid-template-columns:repeat(4,1fr)">
        <div class="fg span2"><label>名称</label><input v-model="f.name" placeholder="如：生产车间电力增容"></div>
        <div class="fg"><label>资产科目</label><select v-model="f.account"><option v-for="a in assetAccs" :key="a.code" :value="a.code">{{a.code}} {{a.name}}</option></select></div>
        <div class="fg"><label>折旧费用科目</label><select v-model="f.expense_account"><option v-for="a in expAccs" :key="a.code" :value="a.code">{{a.code}} {{a.name}}</option></select></div>
        <div class="fg"><label>取得（转固）日期</label><input type="date" v-model="f.acquired_date"></div>
        <div class="fg"><label>原值·不含税</label><input type="number" step="0.01" v-model.number="f.cost"></div>
        <div class="fg"><label>年限（年）</label><input type="number" step="1" v-model.number="years"></div>
        <div class="fg"><label>残值率</label><input type="number" step="0.01" v-model.number="f.residual_rate"></div>
        <div class="fg"><label>起折月（空=次月）</label><input type="month" v-model="f.start_month"></div>
        <div class="fg"><label>供应商</label><select v-model="f.partner_id"><option :value="null">—</option><option v-for="p in partners" :key="p.id" :value="p.id">{{p.short_name||p.name}}</option></select></div>
        <div class="fg span2"><label>备注</label><input v-model="f.remark"></div>
      </div>
      <div class="sub-line" style="margin:10px 0 4px"><label style="display:inline"><input type="checkbox" v-model="post" style="width:auto"> 同时生成入账凭证（借 资产科目 / 贷 下列科目，如 1123 预付转固、2202 应付尾款）</label></div>
      <table v-if="post">
        <thead><tr><th style="width:40%">贷方科目</th><th class="num" style="width:18%">金额</th><th style="width:22%">往来单位</th><th></th></tr></thead>
        <tbody><tr v-for="(l,i) in f.credit_lines" :key="i">
          <td><select v-model="l.account"><option value="">选科目</option><option v-for="a in leafs" :key="a.code" :value="a.code">{{a.code}} {{a.name}}</option></select></td>
          <td><input type="number" step="0.01" v-model.number="l.amount" style="text-align:right"></td>
          <td><select v-model="l.partner_id"><option :value="null">—</option><option v-for="p in partners" :key="p.id" :value="p.id">{{p.short_name||p.name}}</option></select></td>
          <td><span class="x clickable" v-if="f.credit_lines.length>1" @click="f.credit_lines.splice(i,1)">✕</span></td></tr></tbody>
      </table>
      <div v-if="post" style="padding:6px 0"><button class="btn btn-ghost btn-sm" @click="addCredit">＋ 再加一行</button></div>
      <div v-if="err" class="auth-error">{{err}}</div>
      <div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button><button class="btn btn-ink" :disabled="busy" @click="save">登记</button></div>
    </div>
  </div>`,
  emits: ['close', 'saved'],
  data: () => ({ accounts: [], partners: [], years: 10, post: true, err: '', busy: false,
                 f: { name: '', account: '160102', expense_account: '660209', acquired_date: today(), cost: null, residual_rate: 0,
                      start_month: '', partner_id: null, remark: '', credit_lines: [{ account: '1123', amount: null, partner_id: null }] } }),
  computed: {
    leafs() { const par = new Set(this.accounts.map(a => a.parent_code).filter(x => x)); return this.accounts.filter(a => a.active && !par.has(a.code)); },
    assetAccs() { return this.leafs.filter(a => a.code.startsWith('1601')); },
    expAccs() { return this.leafs.filter(a => a.acc_type === 'expense'); },
  },
  methods: {
    addCredit() { this.f.credit_lines.push({ account: '', amount: null, partner_id: this.f.partner_id }); },
    async save() {
      this.busy = true; this.err = '';
      const body = { ...this.f, life_months: Math.round(this.years * 12), start_month: this.f.start_month || null,
                     credit_lines: this.post ? this.f.credit_lines : [] };
      try { await api.post('/api/assets', body); this.$emit('saved'); this.$emit('close'); }
      catch (e) { this.err = String(e); } finally { this.busy = false; }
    },
  },
  async mounted() { [this.accounts, this.partners] = await Promise.all([api.get('/api/accounts'), api.get('/api/partners')]); },
});

const AssetsView = defineComponent({
  components: { DataTable, AssetForm },
  template: `
  <div>
    <div class="filter-bar">
      <span class="sub">已计提至 <b class="mono">{{last || '—'}}</b> · 原值合计 {{fmt(sum('cost'))}} · 累计折旧 {{fmt(sum('accumulated'))}} · 净值 {{fmt(sum('net'))}}</span>
      <span style="margin-left:auto"></span>
      <input type="month" v-model="period" style="width:140px">
      <button class="btn btn-ink" @click="depreciate">计提折旧至该月</button>
      <button class="btn btn-ghost" :disabled="!last" @click="undo">撤销 {{last}}</button>
      <button class="btn btn-ink" style="margin-left:6px" @click="showNew=true">＋ 登记资产</button>
    </div>
    <div v-if="msg" class="card" style="padding:10px 14px">{{msg}}</div>
    <div class="card">
      <data-table view="assets" :columns="cols" :rows="list" export-name="固定资产" empty="还没有固定资产" expandable
                  :menu="a => [{ label: '改备注', run: () => editRemark(a) }]">
        <template #expand="{ row }"><div class="sub-line" style="white-space:pre-wrap;padding:6px 4px;color:var(--ink)">{{row.remark || '（无备注）'}}</div></template>
      </data-table>
    </div>
    <asset-form v-if="showNew" @close="showNew=false" @saved="load"></asset-form>
  </div>`,
  data: () => ({ list: [], last: '', period: today().slice(0, 7), msg: '', showNew: false }),
  computed: {
    cols() {
      return [
        { key: 'code', label: '编号', type: 'code' },
        { key: 'name', label: '名称', type: 'text' },
        { key: 'account_name', label: '类别', type: 'partner', title: a => a.account },
        { key: 'acquired_date', label: '取得日期', type: 'date' },
        { key: 'cost', label: '原值', type: 'money', width: 116 },
        { key: 'life_years', label: '年限', type: 'qty', width: 56 },
        { key: 'start_month', label: '起折月', type: 'short', width: 72 },
        { key: 'monthly', label: '月折旧', type: 'money' },
        { key: 'accumulated', label: '累计折旧', type: 'money', width: 116 },
        { key: 'net', label: '净值', type: 'money', width: 116 },
        { key: 'last_period', label: '提至', type: 'short', width: 72 },
        { key: 'residual_rate', label: '残值率', type: 'qty', width: 60, hidden: true },
        { key: 'partner_short', label: '供应商', type: 'partner', hidden: true },
        { key: 'voucher_no', label: '入账凭证', type: 'id', hidden: true },
        { key: 'remark', label: '备注', type: 'text', hidden: true },
        { key: 'actions', label: '', type: 'actions', width: 60, sortable: false },
      ];
    },
  },
  methods: {
    fmt,
    sum(k) { return this.list.reduce((s, a) => s + (a[k] || 0), 0); },
    async editRemark(a) {
      const v = prompt('备注（权证号、单元号、占地面积、税源口径等；点行可展开看全文）', a.remark || '');
      if (v === null) return;
      try { await api.put('/api/assets/' + a.id + '/remark', { remark: v }); this.load(); } catch (e) { alert(String(e)); }
    },
    async load() { const r = await api.get('/api/assets'); this.list = r.items; this.last = r.last_period; },
    async depreciate() {
      try {
        const r = await api.post('/api/assets/depreciate', { period: this.period });
        this.msg = r.months.length ? r.months.map(m => m.period + ' ' + fmt(m.amount) + '（' + m.voucher_no + '）').join('；') : '没有需要计提的月份';
        await this.load();
      } catch (e) { alert(e); }
    },
    async undo() {
      if (!confirm('撤销 ' + this.last + ' 的折旧？（红冲该月凭证）')) return;
      try { await api.post('/api/assets/depreciate/undo', {}); this.msg = '已撤销 ' + this.last; await this.load(); } catch (e) { alert(e); }
    },
  },
  mounted() { this.load(); },
});
