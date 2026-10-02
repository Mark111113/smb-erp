/* ---------------- 三单匹配：合同明细履约与票货款闭环 ---------------- */
const ThreeWayMatchView = defineComponent({
  components: { DataTable },
  template: `
  <div>
    <div class="filter-bar">
      <select v-model="ctype" @change="load"><option value="purchase">采购方向</option><option value="sales">销售方向</option></select>
      <span class="sub">口径：合同额/发票为价税合计；采购收货为不含税；销售交付按合同含税价；状态灯看{{ctype==='purchase'?'收货-收票差':'交付-开票差'}}额与未付款</span>
    </div>
    <div class="card">
      <data-table view="twm" :columns="cols" :rows="rows" row-key="contract_no" export-name="三单匹配" empty="无合同"
                  :row-click="r => go('contract/'+r.contract_id)">
        <template #cell-gr_ir_gap="{ row }"><div class="num dt-nowrap" :class="{neg: Math.abs(row.gr_ir_gap)>0.01}">{{fmt(row.gr_ir_gap)}}</div></template>
        <template #cell-unpaid="{ row }"><div class="num dt-nowrap" :class="{neg: row.unpaid>0.01}">{{fmt(row.unpaid)}}</div></template>
        <template #cell-status="{ row }"><span class="pill" :class="ST[row.status].cls">{{ST[row.status].label}}</span></template>
      </data-table>
    </div>
  </div>`,
  data: () => ({ rows: [], ctype: 'purchase',
    ST: { closed: { label: '闭合', cls: 'p-ok' },
          gr_gap: { label: '货到票未到', cls: 'p-draft' },
          ir_gap: { label: '票到货未到', cls: 'p-draft' },
          pay_gap: { label: '未付清', cls: 'p-red' },
          todo: { label: '未执行', cls: 'p-gray' } } }),
  computed: {
    cols() {
      const pur = this.ctype === 'purchase';
      return [
        { key: 'contract_no', label: '合同', type: 'id', width: 136 },
        { key: 'partner_short', label: '对方', type: 'partner', title: r => r.partner_name },
        { key: 'partner_name', label: '对方全称', type: 'text', hidden: true },
        { key: 'order', label: '合同额·含税', type: 'money' },
        { key: 'gr_net', label: pur ? '已收货·不含税' : '已交付·含税', type: 'money', width: 116 },
        { key: 'inv_net', label: pur ? '已收票·不含税' : '已开票·不含税', type: 'money', width: 116 },
        { key: 'inv_total', label: pur ? '已收票·含税' : '已开票·含税', type: 'money', width: 116 },
        { key: 'inv_count', label: '发票张数', type: 'qty', width: 72, hidden: true },
        { key: 'gr_ir_gap', label: pur ? '收货−收票差' : '交付−开票差', type: 'money', width: 116 },
        { key: 'paid', label: pur ? '已付' : '已收', type: 'money' },
        { key: 'unpaid', label: pur ? '未付' : '未收', type: 'money' },
        { key: 'order_gap', label: '合同−开票差', type: 'money', width: 108, hidden: true },
        { key: 'status', label: '状态', type: 'status', width: 100, value: r => this.ST[r.status].label },
      ];
    },
  },
  methods: {
    fmt,
    go(k) { location.hash = '#/' + k; },
    async load() { this.rows = await api.get('/api/three-way-match?contract_type=' + this.ctype); },
  },
  mounted() { this.load(); },
});
