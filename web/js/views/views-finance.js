/* owe-erp 前端（由 app.js 机械拆分，经典脚本按序加载，顺序见 index.html；依赖：base 先于 forms 先于 views 先于 app） */

/* ---------------- 发票 ---------------- */
const InvoicesView = defineComponent({
  components: { InvoiceForm, CorrectionForm, RelinkForm, DataTable },
  template: `
  <div>
    <div class="filter-bar">
      <select v-model="f.dir"><option value="">全部</option><option value="input">进项</option><option value="output">销项</option></select>
      <button class="btn btn-ink" style="margin-left:auto" @click="showNew=true">＋ 登记发票</button>
    </div>
    <div class="card">
      <data-table view="invoices" date-key="invoice_date" :columns="cols" :rows="list" export-name="发票" empty="无发票" :menu="menuItems">
        <template #cell-direction="{ row }"><span class="pill" :class="row.direction==='input'?'p-info':'p-ok'">{{row.direction==='input'?'进项':'销项'}}</span></template>
        <template #cell-contract_no="{ row }">
          <div v-if="row.contract_id" class="mono dt-nowrap clickable" :title="row.contract_no" @click="go('contract/'+row.contract_id)">{{row.contract_no}}</div>
          <div v-else class="sub-line">未挂合同</div></template>
      </data-table>
    </div>
    <correction-form v-if="correction" kind="invoice" :record="correction" @close="correction=null" @saved="load"></correction-form>
    <relink-form v-if="relink" kind="invoice" :doc="relink" @close="relink=null" @saved="load"></relink-form>
    <invoice-form v-if="showNew" @close="showNew=false" @saved="load"></invoice-form>
  </div>`,
  data: () => ({ list: [], showNew: false, correction:null, relink:null, f: { dir: '' } }),
  computed: {
    cols() {
      return [
        { key: 'invoice_no', label: '发票号', type: 'id', width: 176 },
        { key: 'direction', label: '方向', type: 'status', width: 64, value: i => i.direction === 'input' ? '进项' : '销项' },
        { key: 'invoice_date', label: '开票日', type: 'date' },
        { key: 'partner_short', label: '对方', type: 'partner', title: i => i.partner_name },
        { key: 'partner_name', label: '对方全称', type: 'text', hidden: true },
        { key: 'contract_no', label: '合同', type: 'id', width: 124, value: i => i.contract_no || '' },
        { key: 'amount_ex_tax', label: '不含税', type: 'money' },
        { key: 'tax_amount', label: '税额', type: 'money', width: 92 },
        { key: 'amount_tax', label: '价税合计', type: 'money' },
        { key: 'verify_status', label: '查验', type: 'status', width: 72 },
        { key: 'remark', label: '备注', type: 'text' },
        { key: 'file_path', label: '文件', type: 'text', hidden: true },
        { key: 'created_by', label: '录入人', type: 'person', hidden: true },
        { key: 'actions', label: '', type: 'actions', width: 60, sortable: false },
      ];
    },
  },
  methods: {
    menuItems(x) {
      const a = [{ label: '改挂合同', run: () => { this.relink = x; } }];
      if (x.amount_tax > 0) a.push({ label: '开红字', run: () => { this.correction = x; } });
      a.push({ label: '作废', danger: true, run: () => this.voidDoc(x) });
      return a;
    },
    async voidDoc(x){const reason=prompt('作废发票原因（原记录和凭证保留）');if(!reason?.trim())return;try{await api.post('/api/invoices/'+x.id+'/void',{reason});await this.load()}catch(e){alert(e)}}, fmt, go(k) { location.hash = '#/' + k; }, async load() { this.list = await api.get('/api/invoices?direction=' + this.f.dir); } },
  watch: { f: { deep: true, handler() { this.load(); } } },
  mounted() { this.load(); },
});


/* ---------------- 收付款 ---------------- */
const PaymentsView = defineComponent({
  components: { PaymentForm, CorrectionForm, RelinkForm, SplitPaymentForm, DataTable },
  template: `
  <div>
    <div class="filter-bar">
      <select v-model="f.dir"><option value="">全部</option><option value="pay">付款</option><option value="receive">收款</option></select>
      <button class="btn btn-ink" style="margin-left:auto" @click="showNew=true">＋ 登记收付</button>
    </div>
    <div class="card">
      <data-table view="payments" date-key="pay_date" :columns="cols" :rows="list" export-name="收付款" empty="无记录" :menu="menuItems">
        <template #cell-direction="{ row }"><span class="pill" :class="row.direction==='pay'?'p-red':'p-ok'">{{row.direction==='pay'?'付款':'收款'}}</span></template>
        <template #cell-contract_no="{ row }">
          <div v-if="row.contract_id" class="mono dt-nowrap clickable" :title="row.contract_no" @click="go('contract/'+row.contract_id)">{{row.contract_no}}</div>
          <div v-else class="sub-line">未挂合同</div></template>
      </data-table>
    </div>
    <correction-form v-if="correction" kind="payment" :record="correction" @close="correction=null" @saved="load"></correction-form>
    <relink-form v-if="relink" kind="payment" :doc="relink" @close="relink=null" @saved="load"></relink-form>
    <split-payment-form v-if="splitting" :doc="splitting" @close="splitting=null" @saved="load"></split-payment-form>
    <payment-form v-if="showNew" @close="showNew=false" @saved="load"></payment-form>
  </div>`,
  data: () => ({ list: [], showNew: false, correction:null, relink:null, splitting:null, f: { dir: '' } }),
  computed: {
    cols() {
      return [
        { key: 'pay_date', label: '日期', type: 'date' },
        { key: 'direction', label: '方向', type: 'status', width: 64, value: p => p.direction === 'pay' ? '付款' : '收款' },
        { key: 'partner_short', label: '对方', type: 'partner', title: p => p.partner_name },
        { key: 'partner_name', label: '对方全称', type: 'text', hidden: true },
        { key: 'contract_no', label: '合同', type: 'id', width: 124, value: p => p.contract_no || '' },
        { key: 'amount', label: '金额', type: 'money' },
        { key: 'source', label: '来源', type: 'short', width: 72, value: p => p.source === 'bank' ? '银行导入' : '手工' },
        { key: 'remark', label: '备注', type: 'text' },
        { key: 'created_by', label: '录入人', type: 'person', hidden: true },
        { key: 'actions', label: '', type: 'actions', width: 60, sortable: false },
      ];
    },
  },
  methods: {
    menuItems(x) {
      const a = [{ label: '改挂合同', run: () => { this.relink = x; } }];
      if (x.amount > 0) a.push({ label: '拆分到多张合同', run: () => { this.splitting = x; } });
      if (x.amount > 0) a.push({ label: '退款', run: () => { this.correction = x; } });
      a.push({ label: '作废', danger: true, run: () => this.voidDoc(x) });
      return a;
    },
    async voidDoc(x){const reason=prompt('作废收付款原因（原记录和凭证保留）');if(!reason?.trim())return;try{await api.post('/api/payments/'+x.id+'/void',{reason});await this.load()}catch(e){alert(e)}}, fmt, go(k) { location.hash = '#/' + k; }, async load() { this.list = await api.get('/api/payments?direction=' + this.f.dir); } },
  watch: { f: { deep: true, handler() { this.load(); } } },
  mounted() { this.load(); },
});
