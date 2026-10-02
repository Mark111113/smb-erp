/* owe-erp 前端（由 app.js 机械拆分，经典脚本按序加载，顺序见 index.html；依赖：base 先于 forms 先于 views 先于 app） */

/* ---------------- 执行看板 ---------------- */
const DashView = defineComponent({
  template: `
  <div>
    <div class="kpis">
      <div class="kpi c-info"><div class="kpi-l">库存货值·不含税（移动加权）</div><div class="kpi-v">{{fmt(d.kpi.total_value)}}<small>元</small></div><div class="kpi-d">负库存 {{d.kpi.neg_count}} 项</div></div>
      <div class="kpi c-ok"><div class="kpi-l">应收账款</div><div class="kpi-v">{{fmt(d.kpi.ar)}}<small>元</small></div><div class="kpi-d">销售合同未收讫合计</div></div>
      <div class="kpi c-red"><div class="kpi-l">应付账款</div><div class="kpi-v">{{fmt(d.kpi.ap)}}<small>元</small></div><div class="kpi-d">采购合同未付讫合计</div></div>
      <div class="kpi c-warn"><div class="kpi-l">待确认单据</div><div class="kpi-v">{{d.kpi.draft_count}}<small>张</small></div><div class="kpi-d">暂估出入库草稿</div></div>
    </div>
    <div class="card">
      <div class="card-h"><b>合同执行四流</b><span class="sub">签约 → 收发货 → 开票 → 收付款 · 只列未闭合的 {{d.pipeline.length}} 张</span><span class="more" @click="go('contracts')">全部合同 ›</span></div>
      <table>
        <thead><tr><th>合同</th><th>对方单位</th><th class="num">金额（含税）</th><th class="num">开票</th><th class="num">收付</th><th>四流</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="c in d.pipeline" :key="c.id" class="clickable" @click="go('contract/'+c.id)">
            <td><span class="mono">{{c.contract_no}}</span><br><span class="sub-line">{{c.contract_type==='sales'?'销售':'采购'}} · {{c.sign_date||'无签订日'}}</span></td>
            <td :title="c.partner_name">{{c.partner_short}}</td>
            <td class="num">{{fmt(c.amount_tax)}}</td>
            <td class="num">{{fmt(c.inv_sum)}}</td>
            <td class="num" :class="{'pos-ok':c.pay_sum+0.005>=c.amount_tax}">{{fmt(c.pay_sum)}}</td>
            <td><div class="flows">
              <div class="flow" :class="'on'+(i+1)" v-for="(f,i) in c.flows" :key="i" v-show="f"></div>
              <div class="flow" v-for="i in (4-c.flows.filter(x=>x).length)" :key="'e'+i"></div>
            </div></td>
            <td><span class="pill" :class="c.stage_cls">{{c.stage}}</span></td>
          </tr>
        </tbody>
      </table>
      <div class="flow-legend">
        <span><i style="background:var(--ink)"></i>已签约</span><span><i style="background:var(--info)"></i>收发货</span>
        <span><i style="background:var(--ok)"></i>开票</span><span><i style="background:var(--seal)"></i>收付款</span>
        <span style="margin-left:auto">点击行进入合同详情<template v-if="d.closed_count"> · 已闭合 {{d.closed_count}} 张在 <a class="clickable" @click="go('contracts')">合同订单</a> 页（状态选「已闭合」）</template></span>
      </div>
    </div>
    <div class="card">
      <div class="card-h"><b>待办 · 风险</b><span class="sub">手工待办在上（系统算不出来的事，如账实差异、待补单）；自动风险在下（负库存 / 收付差额）</span>
        <span class="more" style="margin-left:auto" @click="showDone=!showDone">{{showDone ? '收起已完成' : '看已完成'}}</span>
        <button class="btn btn-ink btn-sm" style="margin-left:10px" @click="openTodo()">＋ 记一条</button></div>
      <table><tbody>
        <tr v-for="t in todos" :key="'t'+t.id" :style="t.status==='done' ? 'opacity:.5' : ''">
          <td style="width:28px"><input type="checkbox" :checked="t.status==='done'" @change="toggle(t)" style="width:auto" :title="t.status==='done'?'重开':'标记完成'"></td>
          <td style="width:70px"><span class="pill" :class="levelPill[t.level]">{{levelText[t.level]}}</span></td>
          <td><b :style="t.status==='done'?'text-decoration:line-through':''">{{t.title}}</b>
            <span v-if="t.contract_no" class="mono clickable" style="margin-left:8px" @click="go('contract/'+t.contract_id)">{{t.contract_no}} ›</span>
            <div v-if="t.detail" class="sub-line" style="white-space:pre-wrap">{{t.detail}}</div></td>
          <td class="sub-line" style="white-space:nowrap;width:150px">{{t.due_date ? '期限 '+t.due_date : ''}}<div>{{t.created_by||'—'}} · {{t.created_at.slice(5,10)}}</div></td>
          <td style="white-space:nowrap;text-align:right;width:110px">
            <button class="btn btn-ghost btn-sm" @click="openTodo(t)">编辑</button><button class="btn btn-ghost btn-sm" @click="delTodo(t)">删除</button></td>
        </tr>
        <tr v-for="(r,i) in d.risks" :key="'r'+i">
          <td></td>
          <td><span class="pill" :class="r.level==='red'?'p-red':'p-draft'">{{r.level==='red'?'红字':'关注'}}</span></td>
          <td>{{r.text}}</td><td class="sub-line">自动</td><td></td>
        </tr>
        <tr v-if="!todos.length && !d.risks.length"><td colspan="5" class="empty">没有待办</td></tr>
      </tbody></table>
    </div>
    <div class="modal-mask" v-if="todoForm" @click.self="todoForm=null">
      <div class="modal" style="width:600px">
        <div class="modal-h"><b>{{todoForm.id ? '编辑待办' : '记一条待办'}}</b><span class="x" @click="todoForm=null">✕</span></div>
        <div class="form-grid" style="grid-template-columns:1fr 1fr">
          <div class="fg span2"><label>事项</label><input v-model="todoForm.title" maxlength="120" placeholder="如：寄存处账实差 1 台，待补合同"></div>
          <div class="fg"><label>紧急度</label><select v-model="todoForm.level"><option value="red">紧急</option><option value="warn">关注</option><option value="info">备忘</option></select></div>
          <div class="fg"><label>期限（可空）</label><input type="date" v-model="todoForm.due_date"></div>
          <div class="fg span2"><label>关联合同（可空，点击可跳转）</label><select v-model="todoForm.contract_id">
            <option :value="null">不关联</option><option v-for="c in contracts" :key="c.id" :value="c.id">{{c.contract_no}} · {{c.partner_short||c.partner_name}}</option></select></div>
          <div class="fg span2"><label>说明</label><textarea v-model="todoForm.detail" rows="3"></textarea></div>
          <div v-if="todoErr" class="fg span2 auth-error">{{todoErr}}</div>
        </div>
        <div class="actions"><button class="btn btn-ghost" @click="todoForm=null">取消</button><button class="btn btn-ink" @click="saveTodo">保存</button></div>
      </div>
    </div>
  </div>`,
  data: () => ({ d: { kpi: {}, pipeline: [], risks: [] }, todos: [], showDone: false, todoForm: null, todoErr: '', contracts: [],
                 levelPill: { red: 'p-red', warn: 'p-draft', info: 'p-info' }, levelText: { red: '紧急', warn: '关注', info: '备忘' } }),
  watch: { showDone() { this.loadTodos(); } },
  methods: {
    fmt,
    async load() { this.d = await api.get('/api/dashboard'); this.loadTodos(); },
    async loadTodos() { this.todos = await api.get('/api/todos?status=' + (this.showDone ? 'all' : 'open')); },
    async openTodo(t) {
      this.todoErr = '';
      if (!this.contracts.length) this.contracts = await api.get('/api/contracts?status=active');
      this.todoForm = t ? { id: t.id, title: t.title, detail: t.detail, level: t.level, contract_id: t.contract_id, due_date: t.due_date || null }
                        : { title: '', detail: '', level: 'warn', contract_id: null, due_date: null };
    },
    async saveTodo() {
      const { id, ...body } = this.todoForm;
      if (!body.due_date) body.due_date = null;
      try { if (id) await api.put('/api/todos/' + id, body); else await api.post('/api/todos', body); this.todoForm = null; this.loadTodos(); }
      catch (e) { this.todoErr = String(e); }
    },
    async toggle(t) { await api.post(`/api/todos/${t.id}/${t.status === 'done' ? 'reopen' : 'done'}`); this.loadTodos(); },
    async delTodo(t) { if (confirm('删除这条待办？（完成请勾选，删除只用于记错了的）')) { await api.del('/api/todos/' + t.id); this.loadTodos(); } },
    go(k) { location.hash = '#/' + k; },
    statusName(s) { return { active: '有效', void: '作废', replaced: '被替代', draft: '草稿' }[s] || s; },
  },
  mounted() { this.load(); },
});
