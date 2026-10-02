/* 业务闭环：核销、库存转换、期末对账。沿用纸灰与墨色界面。 */
const ControlsView = defineComponent({
  components: { DataTable },
  props: { me: Object },
  template: `
  <div>
    <div class="filter-bar"><button v-for="x in tabs" class="btn" :class="tab===x[0]?'btn-ink':'btn-ghost'" @click="tab=x[0]">{{x[1]}}</button><button class="btn btn-ghost" @click="load">刷新</button></div>
    <div v-if="error" class="card" style="padding:16px;color:var(--red)">{{error}}</div>
    <div v-if="tab==='settle'">
      <div class="card"><div class="card-h"><b>票款核销</b><span class="sub">同单位同方向；支持一款多票、一票多款。未核销款项列预收／预付。</span></div>
        <div class="form-grid">
          <div class="fg"><label>收付款</label><select v-model.number="settle.payment_id"><option :value="null">选择未核销款</option><option v-for="x in cash" :value="x.id">#{{x.id}} {{x.partner_name}} · {{x.direction==='pay'?'付':'收'}} {{fmt(x.remaining)}}</option></select></div>
          <div class="fg"><label>发票</label><select v-model.number="settle.invoice_id"><option :value="null">选择未核销票</option><option v-for="x in bills" :value="x.id">{{x.number}} {{x.partner_name}} · {{fmt(x.remaining)}}</option></select></div>
          <div class="fg"><label>核销金额</label><input type="number" min="0.01" step="0.01" v-model.number="settle.amount"></div>
          <div class="fg"><label>核销日期</label><input type="date" v-model="settle.allocation_date"></div>
        </div><div class="actions"><button class="btn btn-ghost" :disabled="busy" @click="autoSettle" title="只核同合同、同单位、同方向的票款；跨合同的仍在上面手工核销">按合同自动核销</button><button class="btn btn-ink" :disabled="busy" @click="run('/api/allocations',settle)">确认核销</button></div>
      </div>
      <div class="card"><div class="card-h"><b>未清项目与账龄</b><span class="sub">按票款日期计算，非合同逾期天数；红字与退款已扣除。</span></div><table><thead><tr><th>类型／编号</th><th>单位</th><th>日期</th><th class="num">未清金额</th><th>账龄</th></tr></thead><tbody><tr v-for="x in items.filter(x=>Math.abs(x.remaining)>.005)"><td>{{x.kind==='invoice'?'票':'款'}} {{x.number}}</td><td>{{x.partner_name}}</td><td>{{x.date}}</td><td class="num">{{fmt(x.remaining)}}</td><td>{{x.bucket}} 天</td></tr></tbody></table></div>
      <div class="card"><div class="card-h"><b>核销记录</b><span class="sub">一行 = 一笔款冲一张票的金额；核销日期取票款较晚的业务日期，录入时间才是点按钮的时间</span></div>
        <data-table view="allocations" date-key="allocation_date" :columns="allocCols" :rows="allocations" export-name="核销记录" empty="还没有核销"
                    :row-class="a => a.status==='active' ? '' : 'dim'"
                    :menu="a => a.status==='active' ? [{ label: '取消核销', danger: true, run: () => reasonAction('/api/allocations/'+a.id+'/void','取消核销') }] : []">
          <template #cell-status="{ row }"><span class="pill" :class="row.status==='active'?'p-ok':'p-gray'">{{row.status==='active'?'有效':'已取消'}}</span></template>
        </data-table></div>
    </div>
    <div v-if="tab==='convert'">
      <div class="card"><div class="card-h"><b>组套／拆套</b><span class="sub">按已维护BOM执行；拆套默认按子件数量×已有不含税均价分摊，无参考价必须填权重。</span></div>
        <div class="form-grid">
          <div class="fg"><label>类型</label><select v-model="conversion.kind"><option value="disassemble">拆套</option><option value="assemble">组套</option></select></div>
          <div class="fg"><label>日期</label><input type="date" v-model="conversion.move_date"></div>
          <div class="fg"><label>母件（有BOM）</label><select v-model.number="conversion.parent_material_id" @change="setComponents"><option :value="null">请选择</option><option v-for="m in materials.filter(x=>x.bom.length)" :value="m.id">{{m.code}} {{m.name}} {{m.spec}}</option></select></div>
          <div class="fg"><label>套数</label><input type="number" min="0.001" v-model.number="conversion.qty" @change="setComponents"></div>
        </div>
        <table><thead><tr><th>子件</th><th class="num">数量</th><th>成本权重（留空用参考成本）</th></tr></thead><tbody><tr v-for="x in conversion.components"><td>{{materialName(x.material_id)}}</td><td class="num">{{x.qty}}</td><td><input type="number" min="0.000001" v-model.number="x.weight" placeholder="自动"></td></tr></tbody></table>
        <div class="actions"><button class="btn btn-ink" :disabled="busy||!conversion.components.length" @click="saveConversion">确认过账</button></div>
      </div>
      <div class="card"><table><thead><tr><th>单号</th><th>类型</th><th>日期</th><th>母件</th><th>套数</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="x in conversions"><td>CV-{{x.id}}</td><td>{{x.kind==='assemble'?'组套':'拆套'}}</td><td>{{x.move_date}}</td><td>{{materialName(x.detail.parent_material_id)}}</td><td>{{x.detail.qty}}</td><td>{{x.status==='confirmed'?'已确认':'已作废'}}</td><td><button v-if="x.status==='confirmed'" class="btn btn-ghost btn-sm" :disabled="busy" @click="reasonAction('/api/conversions/'+x.id+'/void','整体作废组拆套')">作废</button></td></tr></tbody></table></div>
    </div>
    <div v-if="tab==='check' && reconciliation">
      <div class="kpis" style="grid-template-columns:repeat(3,1fr)"><div class="kpi"><div class="kpi-l">库存明细</div><div class="kpi-v">{{fmt(reconciliation.stock_value)}}</div></div><div class="kpi"><div class="kpi-l">财务库存</div><div class="kpi-v">{{fmt(reconciliation.inventory_gl)}}</div></div><div class="kpi"><div class="kpi-l">库存差额／借贷差额</div><div class="kpi-v">{{fmt(reconciliation.inventory_gap)}} / {{fmt(reconciliation.trial_gap)}}</div></div></div>
      <div class="card"><div class="card-h"><b>跨月发货未开票提示</b><span class="sub">收入继续按开票确认；请核实以下交付与开票差异。</span></div><table><tbody><tr v-for="x in reconciliation.cross_month_unbilled"><td>{{x.contract_no}}</td><td>交付 {{fmt(x.delivered_value)}} / 开票 {{fmt(x.invoiced)}}</td><td>{{x.unlinked.length?'存在未关联明细，请先补关联':''}}</td></tr><tr v-if="!reconciliation.cross_month_unbilled.length"><td class="empty">暂无跨月提示</td></tr></tbody></table></div>
      <div class="card"><div class="card-h"><b>往来明细与总账差额</b></div><table><tbody><tr v-for="x in reconciliation.ar_ap.filter(x=>Math.abs(x.gap)>.005)"><td>单位 {{x.partner_id}} / 科目 {{x.account}}</td><td class="num">{{fmt(x.gap)}}</td></tr><tr v-if="!reconciliation.ar_ap.some(x=>Math.abs(x.gap)>.005)"><td class="empty">往来明细与总账一致</td></tr></tbody></table></div>
      <div class="card"><div class="card-h"><b>采购暂估差额</b><span class="sub">仅对已收齐、票齐合同分摊价差；历史未确认收货仍需核实实物流。</span></div><table><thead><tr><th>合同</th><th class="num">收货净额</th><th class="num">收票净额</th><th class="num">差额</th><th></th></tr></thead><tbody><tr v-for="x in reconciliation.purchase.filter(x=>Math.abs(x.gr_ir_gap)>.01)"><td>{{x.contract_no}}</td><td class="num">{{fmt(x.gr_net)}}</td><td class="num">{{fmt(x.inv_net)}}</td><td class="num">{{fmt(x.gr_ir_gap)}}</td><td><button class="btn btn-ghost btn-sm" :disabled="busy||!x.fulfillment.complete" @click="reasonAction('/api/contracts/'+x.contract_id+'/settle-cost','按原收货金额比例分摊收票价差，将重算后续成本')">分摊价差</button></td></tr></tbody></table></div>
      <div class="card"><div class="card-h"><b>期间控制</b></div><div class="filter-bar"><input type="month" v-model="month"><button class="btn btn-ink" :disabled="busy||periods.some(p=>p.month===month)" @click="reasonAction('/api/periods/'+month+'/lock','锁定期间 '+month)">{{periods.some(p=>p.month===month)?'已锁定':'锁定期间'}}</button><input type="number" v-model.number="year" style="width:90px"><button class="btn btn-ghost" :disabled="busy" @click="reasonAction('/api/finance/close-year/'+year,'年度损益结转')">年末结转</button></div><table><tbody><tr v-for="p in periods"><td>{{p.month}}</td><td>{{p.reason}}</td><td><button v-if="me&&me.role==='admin'" class="btn btn-ghost btn-sm" :disabled="busy" @click="reasonAction('/api/periods/'+p.month+'/unlock','解除期间锁定 '+p.month)">解锁</button><span v-else class="sub">解锁需管理员</span></td></tr></tbody></table></div>
    </div>
    <div v-if="tab==='audit'" class="card"><div class="card-h"><b>操作记录</b><span class="sub">最近200条；原凭证和红冲关联保留。逐字段变更见 系统→操作日志。</span></div><table><thead><tr><th>时间</th><th>操作人</th><th>操作</th><th>明细</th></tr></thead><tbody><tr v-for="a in audits"><td>{{a.created_at}}</td><td>{{a.user_name||'—'}}</td><td>{{a.action}}</td><td><details><summary>查看</summary><pre style="white-space:pre-wrap;max-width:700px">{{JSON.stringify(a.detail,null,2)}}</pre></details></td></tr></tbody></table></div>
  </div>`,
  data:()=>({tab:'settle',tabs:[['settle','核销与账龄'],['convert','组套拆套'],['check','对账与期末'],['audit','操作记录']],busy:false,error:'',items:[],allocations:[],materials:[],conversions:[],reconciliation:null,periods:[],audits:[],month:today().slice(0,7),year:new Date().getFullYear()-1,settle:{payment_id:null,invoice_id:null,amount:0,allocation_date:today()},conversion:{kind:'disassemble',move_date:today(),parent_material_id:null,qty:1,components:[]}}),
  computed:{allocCols() {
      return [
        { key: 'allocation_date', label: '核销日期', type: 'date' },
        { key: 'partner_short', label: '对方', type: 'partner' },
        { key: 'direction', label: '方向', type: 'status', width: 60, value: a => a.direction === 'pay' ? '付款' : '收款' },
        { key: 'contract_no', label: '合同', type: 'id', width: 130 },
        { key: 'invoice_no', label: '发票号', type: 'id', width: 176 },
        { key: 'invoice_amount', label: '票金额', type: 'money', hidden: true },
        { key: 'pay_date', label: '款日期', type: 'date' },
        { key: 'pay_amount', label: '款金额', type: 'money', hidden: true },
        { key: 'amount', label: '核销金额', type: 'money' },
        { key: 'status', label: '状态', type: 'status', width: 70, value: a => a.status === 'active' ? '有效' : '已取消' },
        { key: 'created_at', label: '录入时间', type: 'id', width: 130 },
        { key: 'created_by', label: '录入人', type: 'person' },
        { key: 'actions', label: '', type: 'actions', width: 60, sortable: false },
      ];
    },
cash(){return this.items.filter(x=>x.kind==='payment'&&x.remaining>.005)},bills(){const p=this.cash.find(x=>x.id===this.settle.payment_id);return this.items.filter(x=>x.kind==='invoice'&&x.remaining>.005&&(!p||(x.partner_id===p.partner_id&&((p.direction==='pay')===(x.direction==='input'))))) }},
  methods:{fmt,materialName(id){const m=this.materials.find(x=>x.id===id);return m?m.code+' '+m.name+' '+m.spec:id},
    async autoSettle(){if(!confirm('把同合同、同单位、同方向的未核销票款按日期配对核销？（跨合同的款不动）'))return;this.busy=true;try{const r=await api.post('/api/allocations/auto',{});alert(r.allocated.length?r.allocated.map(x=>x.contract_no+' 票 '+x.invoice_no+' ← 款#'+x.payment_id+' '+fmt(x.amount)).join('\n'):'没有可自动核销的票款');await this.load()}catch(e){alert(e)}finally{this.busy=false}},
    async load(){try{[this.items,this.allocations,this.materials,this.conversions,this.reconciliation,this.periods,this.audits]=await Promise.all(['/api/settlement-open-items','/api/allocations','/api/materials','/api/conversions','/api/reconciliation','/api/periods','/api/audit-events'].map(x=>api.get(x)))}catch(e){this.error=String(e)}},
    async run(url,body){if(this.busy)return;this.busy=true;this.error='';try{await api.post(url,body);await this.load()}catch(e){this.error=String(e);alert(this.error)}finally{this.busy=false}},
    reasonAction(url,title){const reason=prompt(title+'：请填写原因');if(reason?.trim())this.run(url,{reason})},
    setComponents(){const m=this.materials.find(x=>x.id===this.conversion.parent_material_id);this.conversion.components=(m?.bom||[]).map(x=>({material_id:x.child_id,qty:x.qty_per*this.conversion.qty,weight:null}))},
    saveConversion(){const body={...this.conversion,components:this.conversion.components.map(x=>({...x,weight:x.weight===''?null:x.weight}))};this.run('/api/conversions',body)}},
  mounted(){this.load()}
});

const CorrectionForm = defineComponent({
  props:{kind:String,record:Object},emits:['close','saved'],
  template:`<div class="modal-mask"><div class="modal"><div class="modal-h"><b>{{kind==='invoice'?'红字发票':'原款退款'}}</b><span class="x" @click="$emit('close')">✕</span></div><div class="form-grid"><div class="fg"><label>日期</label><input type="date" v-model="f.document_date"></div><div class="fg" v-if="kind==='invoice'"><label>红字发票号</label><input v-model="f.invoice_no"></div><div class="fg"><label>金额（正数）</label><input type="number" v-model.number="f.amount"></div><div class="fg" v-if="kind==='invoice'"><label>不含税金额</label><input type="number" v-model.number="f.net"></div><div class="fg" v-if="kind==='invoice'"><label>税额</label><input type="number" v-model.number="f.tax"></div><div class="fg"><label>原因</label><input v-model="f.reason"></div></div><p style="padding:0 18px" v-if="kind==='invoice'">红字登记会取消原票核销，请按剩余净额重新核销。</p><p style="padding:0 18px;color:var(--red)">{{error}}</p><div class="actions"><button class="btn btn-ghost" @click="$emit('close')">取消</button><button class="btn btn-ink" :disabled="busy" @click="save">确认</button></div></div></div>`,
  data:()=>({f:{document_date:today(),invoice_no:'',amount:0,net:0,tax:0,reason:''},busy:false,error:''}),
  methods:{async save(){if(this.busy)return;this.busy=true;try{await api.post('/api/'+(this.kind==='invoice'?'invoices/':'payments/')+this.record.id+(this.kind==='invoice'?'/credit':'/refund'),this.f);this.$emit('saved');this.$emit('close')}catch(e){this.error=String(e)}finally{this.busy=false}}}
});
