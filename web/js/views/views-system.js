/* owe-erp 前端 — 系统：用户管理（仅管理员，/api/users 由中间件限管理员）+ 操作日志（全员可看） */

const ROLE_OPTIONS = [
  { v: 'viewer', label: '只读', desc: '可查看全部页面，不能新建、修改、确认或作废' },
  { v: 'editor', label: '可读写', desc: '业务与财务日常操作' },
  { v: 'admin', label: '管理员', desc: '可读写 + 用户管理、账号解锁、期间解锁' },
];
const ROLE_PILL = { admin: 'p-red', editor: 'p-info', viewer: 'p-gray' };

const UsersView = defineComponent({
  components: { DataTable },
  template: `
  <div>
    <div class="filter-bar">
      <span class="sub">账号只能由管理员创建；非管理员账号连续输错 5 次密码即锁定，须管理员解锁；外网登录要验证器动态码（本人在「改密码 / 动态码」里开通）</span>
      <button class="btn btn-ink" style="margin-left:auto" @click="openCreate">＋ 新建用户</button>
    </div>
    <div class="card">
      <data-table view="users" :columns="userCols" :rows="list" empty="无用户"
                  :menu="u => [{ label: '重置密码', run: () => openReset(u) }, { label: 'API 令牌（给 AI/脚本，只能内网用）', run: () => openTokens(u) }].concat(u.locked ? [{ label: '解锁', run: () => unlock(u) }] : []).concat(u.totp_enabled ? [{ label: '重置动态码（手机丢了/换了）', danger: true, run: () => resetTotp(u) }] : [])">
        <template #cell-username="{ row }"><div class="mono dt-nowrap">{{row.username}}<span class="tag-dual" v-if="row.id===me.id">我</span></div></template>
        <template #cell-role="{ row }"><span class="pill" :class="rolePill[row.role]">{{row.role_label}}</span></template>
        <template #cell-state="{ row }">
          <span class="pill p-gray" v-if="!row.active">已停用</span><span class="pill p-red" v-else-if="row.locked">已锁定</span><span class="pill p-ok" v-else>正常</span></template>
        <template #cell-actions="{ row }"><button class="btn btn-ghost btn-sm" @click="openEdit(row)">编辑</button></template>
      </data-table>
    </div>
    <div class="card">
      <div class="card-h"><b>办公室公网地址</b><span class="sub">在公司里经外网域名访问时，来源是公司宽带的公网 IP；列在这里的算内网（不要动态码、API 令牌可用）。宽带换了 IP 就改这里</span></div>
      <div style="padding:8px 14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span v-for="(ip, i) in office.ips" :key="ip" class="pill p-info mono">{{ip}} <a class="clickable" @click="office.ips.splice(i,1); saveOffice()">✕</a></span>
        <span v-if="!office.ips.length" class="sub">（未设置）</span>
        <span class="sub" style="margin-left:12px">你现在的来源地址：<b class="mono">{{office.current || '未知'}}</b></span>
        <button class="btn btn-ghost btn-sm" v-if="office.current && !office.ips.includes(office.current) && !/^(10|127|192\.168|172\.(1[6-9]|2\d|3[01]))\./.test(office.current)" @click="office.ips.push(office.current); saveOffice()">把当前地址加为办公室</button>
        <input v-model="officeAdd" placeholder="手动加一个 IP" style="width:150px" class="mono"><button class="btn btn-ghost btn-sm" @click="officeAdd && (office.ips.push(officeAdd), officeAdd='', saveOffice())">添加</button>
      </div>
    </div>
    <div class="card">
      <div class="card-h"><b>外网登录失败的地址</b><span class="sub">同一外网地址 15 分钟内失败 10 次封 1 小时；内网地址不计</span></div>
      <table><thead><tr><th>地址</th><th>状态</th><th>封到</th><th class="num">累计失败</th><th>最后尝试的账号</th><th></th></tr></thead>
        <tbody><tr v-for="b in ipBlocks" :key="b.ip"><td class="mono">{{b.ip}}</td><td><span class="pill" :class="b.blocked?'p-red':'p-gray'">{{b.blocked?'封禁中':'未封'}}</span></td>
          <td class="mono">{{b.blocked ? b.blocked_until : ''}}</td><td class="num">{{b.total_fails}}</td><td class="mono">{{b.last_username}}</td>
          <td><button v-if="b.blocked" class="btn btn-ghost btn-sm" @click="unblock(b)">解封</button></td></tr>
          <tr v-if="!ipBlocks.length"><td colspan="6" class="empty">没有外网登录失败记录</td></tr></tbody></table>
    </div>

    <div class="modal-mask" v-if="tokUser" @click.self="tokUser=null">
      <div class="modal" style="width:620px">
        <div class="modal-h"><b>API 令牌 · {{tokUser.display_name}}（{{tokUser.username}}）</b><span class="x" @click="tokUser=null">✕</span></div>
        <div class="sub-line" style="margin-bottom:8px">令牌给 AI 助手或脚本调用 ERP 接口（请求头 Authorization: Bearer 令牌），权限与该账号一致，操作记在该账号名下。只显示一次，泄露就吊销重发。</div>
        <div v-if="newTok" class="card" style="padding:10px;background:#FFF8E6"><div class="sub-line">新令牌（只显示这一次）：</div><div class="mono" style="word-break:break-all;user-select:all">{{newTok}}</div></div>
        <table><thead><tr><th>名称</th><th>前缀</th><th>创建</th><th>最近使用</th><th>状态</th><th></th></tr></thead>
          <tbody><tr v-for="t in tokens" :key="t.id"><td>{{t.name}}</td><td class="mono">{{t.prefix}}…</td><td class="mono">{{t.created_at}}</td><td class="mono">{{t.last_seen_at||'—'}}</td>
            <td><span class="pill" :class="t.revoked?'p-gray':'p-ok'">{{t.revoked?'已吊销':'有效'}}</span></td>
            <td><button v-if="!t.revoked" class="btn btn-ghost btn-sm" @click="revokeTok(t)">吊销</button></td></tr>
            <tr v-if="!tokens.length"><td colspan="6" class="empty">没有令牌</td></tr></tbody></table>
        <div class="actions"><input v-model="tokName" placeholder="用途，如 月结agent" style="width:200px"><button class="btn btn-ink" @click="createTok">签发新令牌</button></div>
      </div>
    </div>
    <div class="modal-mask" v-if="mode" @click.self="mode=null">
      <div class="modal" style="width:560px">
        <div class="modal-h"><b>{{title}}</b><span class="x" @click="mode=null">✕</span></div>
        <form class="form-grid" style="grid-template-columns:1fr 1fr" @submit.prevent="save">
          <template v-if="mode!=='reset'">
            <div class="fg"><label>账号</label>
              <input class="mono" v-model="f.username" :disabled="mode==='edit'" required minlength="3" maxlength="32"
                     pattern="[A-Za-z0-9_.\\-]+" title="字母、数字、下划线、点、横线" autocomplete="off"></div>
            <div class="fg"><label>姓名</label><input v-model="f.display_name" maxlength="64"></div>
            <div class="fg span2"><label>权限</label>
              <div class="role-opts">
                <label v-for="r in roles" :key="r.v" class="role-opt" :class="{on:f.role===r.v}">
                  <input type="radio" :value="r.v" v-model="f.role"><b>{{r.label}}</b><span>{{r.desc}}</span>
                </label>
              </div></div>
            <div class="fg span2" v-if="mode==='edit'">
              <label style="display:flex;gap:6px;align-items:center;font-size:13px;color:var(--ink)">
                <input type="checkbox" v-model="f.active" style="width:auto">启用（停用后该账号立即下线，不能登录）</label></div>
            <div class="fg span2" v-if="mode==='edit'">
              <label style="display:flex;gap:6px;align-items:center;font-size:13px;color:var(--ink)">
                <input type="checkbox" v-model="f.allow_external" style="width:auto">允许从外网登录（还需本人开通动态码；关掉则只能在内网用）</label></div>
            <div class="fg span2" v-if="mode==='edit' && f.allow_external && !target.totp_enabled">
              <label>临时免动态码到（含当天，到期自动失效；空＝不放行。只在本人还没开通时有用，开通后照常要码）</label>
              <div style="display:flex;gap:6px"><input type="date" v-model="f.totp_exempt_until" style="width:160px"><button type="button" class="btn btn-ghost btn-sm" @click="f.totp_exempt_until=''">清除</button></div></div>
          </template>
          <template v-if="mode!=='edit'">
            <div class="fg"><label>{{mode==='reset'?'新密码':'初始密码'}}</label>
              <input type="password" v-model="f.password" minlength="10" maxlength="128" required autocomplete="new-password"></div>
            <div class="fg"><label>确认密码</label>
              <input type="password" v-model="f.confirm" minlength="10" maxlength="128" required autocomplete="new-password"></div>
            <div class="fg span2 sub-line" v-if="mode==='reset'">重置后该账号的其他登录会话全部下线，锁定同时解除。</div>
          </template>
          <div v-if="err" class="fg span2 auth-error">{{err}}</div>
          <div class="actions span2"><button class="btn btn-ghost" type="button" @click="mode=null">取消</button>
            <button class="btn btn-ink" type="submit" :disabled="busy">保存</button></div>
        </form>
      </div>
    </div>
  </div>`,
  props: { me: { type: Object, default: () => ({}) } },
  data: () => ({ tokUser: null, tokens: [], newTok: '', tokName: 'agent', list: [], ipBlocks: [], office: { ips: [], current: '' }, officeAdd: '', mode: null, target: null, f: {}, err: '', busy: false,
                 roles: ROLE_OPTIONS, rolePill: ROLE_PILL }),
  computed: {
    userCols() {
      return [
        { key: 'username', label: '账号', type: 'code', width: 120 },
        { key: 'display_name', label: '姓名', type: 'person', width: 110 },
        { key: 'role', label: '权限', type: 'status', width: 84, value: u => u.role_label },
        { key: 'state', label: '状态', type: 'status', width: 80, value: u => !u.active ? '已停用' : u.locked ? '已锁定' : '正常' },
        { key: 'totp', label: '动态码', type: 'status', width: 70, value: u => u.totp_enabled ? '已开通' : '—' },
        { key: 'external', label: '外网', type: 'status', width: 100, value: u => !u.allow_external ? '禁止' : (!u.totp_enabled && u.totp_exempt_until) ? '免码至' + u.totp_exempt_until.slice(5) : '允许' },
        { key: 'last_login_at', label: '最近登录', type: 'id', width: 140, value: u => this.short(u.last_login_at) },
        { key: 'created_at', label: '创建', type: 'id', width: 140, value: u => this.short(u.created_at) },
        { key: 'actions', label: '', type: 'actions', width: 104, sortable: false },
      ];
    },
    title() {
      if (this.mode === 'create') return '新建用户';
      if (this.mode === 'edit') return '编辑用户 · ' + this.target.username;
      return '重置密码 · ' + (this.target && this.target.username);
    },
  },
  methods: {
    async openTokens(u) { this.tokUser = u; this.newTok = ''; this.tokens = await api.get('/api/users/' + u.id + '/tokens'); },
    async createTok() { const r = await api.post('/api/users/' + this.tokUser.id + '/tokens', { name: this.tokName }); this.newTok = r.token; this.tokens = await api.get('/api/users/' + this.tokUser.id + '/tokens'); },
    async revokeTok(t) { if (!confirm('吊销令牌 ' + t.prefix + '…？用它的 AI/脚本将立即失效。')) return; await api.post('/api/users/tokens/' + t.id + '/revoke', {}); this.tokens = await api.get('/api/users/' + this.tokUser.id + '/tokens'); },
    short(s) { return s ? s.slice(0, 16) : '—'; },
    async load() { [this.list, this.ipBlocks, this.office] = await Promise.all([api.get('/api/users'), api.get('/api/users/ip-blocks'), api.get('/api/users/office-ips')]); },
    async saveOffice() { try { const r = await api.put('/api/users/office-ips', { ips: this.office.ips }); this.office.ips = r.ips; } catch (e) { alert(String(e)); this.load(); } },
    async resetTotp(u) { if (!confirm(`重置 ${u.username} 的动态码？重置后他要在内网重新开通才能从外网登录。`)) return; await api.post(`/api/users/${u.id}/totp/reset`, {}); this.load(); },
    async unblock(b) { await api.post('/api/users/ip-blocks/unblock', { ip: b.ip }); this.load(); },
    openCreate() { this.mode = 'create'; this.target = null; this.err = '';
      this.f = { username: '', display_name: '', role: 'editor', password: '', confirm: '' }; },
    openEdit(u) { this.mode = 'edit'; this.target = u; this.err = '';
      this.f = { username: u.username, display_name: u.display_name, role: u.role, active: u.active, allow_external: u.allow_external, totp_exempt_until: u.totp_exempt_until || '' }; },
    openReset(u) { this.mode = 'reset'; this.target = u; this.err = ''; this.f = { password: '', confirm: '' }; },
    async unlock(u) {
      try { await api.post(`/api/users/${u.id}/unlock`, {}); this.load(); }
      catch (e) { alert('解锁失败：' + e); }
    },
    async save() {
      if (this.busy) return;
      this.err = '';
      if (this.mode !== 'edit' && this.f.password !== this.f.confirm) { this.err = '两次输入的密码不一致'; return; }
      this.busy = true;
      try {
        if (this.mode === 'create') {
          await api.post('/api/users', { username: this.f.username, display_name: this.f.display_name,
                                         role: this.f.role, password: this.f.password });
        } else if (this.mode === 'edit') {
          await api.put('/api/users/' + this.target.id, { display_name: this.f.display_name,
                                                          role: this.f.role, active: this.f.active, allow_external: this.f.allow_external,
                                                          totp_exempt_until: this.f.totp_exempt_until || '' });
        } else {
          await api.post(`/api/users/${this.target.id}/password`, { new_password: this.f.password });
        }
        this.mode = null;
        this.load();
      } catch (e) { this.err = String(e); }
      finally { this.busy = false; }
    },
  },
  mounted() { this.load(); },
});

/* ---------------- 操作日志：按请求分组的数据变更（change_log） ---------------- */
const OP_LABEL = { insert: '新建', update: '修改', delete: '删除' };
const OP_PILL = { insert: 'p-ok', update: 'p-info', delete: 'p-red' };
const FIELD_LABEL = {
  status: '状态', name: '名称', short_name: '简称', code: '编码', spec: '规格', unit: '单位', qty: '数量',
  unit_cost: '成本', tax_rate: '税率', amount: '金额', amount_tax: '价税合计', amount_ex_tax: '不含税',
  tax_amount: '税额', remark: '备注', summary: '摘要', move_date: '日期', move_type: '类型',
  invoice_no: '发票号', invoice_date: '开票日', pay_date: '日期', direction: '方向', partner_id: '往来单位ID',
  contract_id: '合同ID', material_id: '物料ID', contract_no: '合同号', sign_date: '签订日', active: '启用',
  role: '权限', display_name: '姓名', username: '账号', password_hash: '密码', locked_until: '锁定至',
  debit: '借', credit: '贷', account_code: '科目', voucher_no: '凭证号', voucher_date: '凭证日期',
  voucher_id: '凭证ID', line_no: '行号', source_type: '来源', source_id: '来源单据ID', confirmed_at: '确认时间',
  reversal_of: '冲销原凭证', doc_no: '单号', cost_center_id: '成本中心ID', verify_status: '查验',
};

const OpLogView = defineComponent({
  components: { DataTable },
  template: `
  <div>
    <div class="filter-bar">
      <select v-model.number="q.user_id"><option :value="0">全部操作人</option>
        <option v-for="u in facets.users" :key="u.id" :value="u.id">{{u.name}}</option></select>
      <select v-model="q.table"><option value="">全部对象</option>
        <option v-for="t in facets.tables" :key="t.key" :value="t.key">{{t.label}}</option></select>
      <input type="date" v-model="q.date_from"><span class="sub">至</span><input type="date" v-model="q.date_to">
      <button class="btn btn-ink btn-sm" @click="load">查询</button>
      <span class="sub" style="margin-left:auto">v0.11 起记录；自动过账等连带改动记在触发的操作人名下</span>
    </div>
    <div class="card">
      <data-table view="oplog" date-key="at" :columns="logCols" :rows="list" row-key="request_id" export-name="操作日志" expandable
                  :empty="loading ? '加载中…' : '无记录'">
        <template #cell-action="{ row }"><div class="dt-nowrap"><b>{{row.action}}</b></div></template>
        <template #expand="{ row }">
          <div class="sub-line mono" style="margin-bottom:6px">{{row.method}} {{row.path}}</div>
          <div v-for="(it, k) in row.items" :key="k" class="op-item">
            <span class="pill" :class="opPill[it.op]">{{opLabel[it.op]}}</span>
            <b>{{it.table_label}}</b> <span class="mono">#{{it.row_key}}</span>
            <div class="op-fields">
              <template v-if="it.op==='update'">
                <div v-for="(v, f) in it.changes" :key="f"><span class="op-f">{{fieldLabel(f)}}</span>
                  <span class="op-old">{{show(v[0])}}</span> → <span class="op-new">{{show(v[1])}}</span></div>
              </template>
              <template v-else>
                <span v-for="(v, f) in compact(it.changes)" :key="f" class="op-kv"><span class="op-f">{{fieldLabel(f)}}</span>{{show(v)}}</span>
              </template>
            </div>
          </div>
        </template>
      </data-table>
    </div>
  </div>`,
  computed: {
    logCols() {
      return [
        { key: 'at', label: '时间', type: 'id', width: 150 },
        { key: 'user_name', label: '操作人', type: 'person', width: 110 },
        { key: 'action', label: '操作', type: 'text', width: 150 },
        { key: 'brief', label: '涉及（点击行展开明细）', type: 'text', sortable: false, value: g => this.brief(g) },
        { key: 'path', label: '接口', type: 'text', hidden: true, value: g => (g.method || '') + ' ' + (g.path || '') },
      ];
    },
  },
  data: () => ({ list: [], facets: { users: [], tables: [] }, open: {}, loading: false,
                 q: { user_id: 0, table: '', date_from: '', date_to: '' },
                 opLabel: OP_LABEL, opPill: OP_PILL }),
  methods: {
    async load() {
      this.loading = true;
      try {
        const p = new URLSearchParams({ limit: 200 });
        for (const [k, v] of Object.entries(this.q)) if (v) p.set(k, v);
        this.list = await api.get('/api/change-log?' + p);
        this.open = {};
      } finally { this.loading = false; }
    },
    toggle(id) { this.open = { ...this.open, [id]: !this.open[id] }; },
    brief(g) {
      const c = {};
      for (const it of g.items) {
        const k = it.table_label + ' ' + OP_LABEL[it.op];
        (c[k] = c[k] || []).push(it.row_key);
      }
      return Object.entries(c).map(([k, keys]) => keys.length === 1 ? `${k} #${keys[0]}` : `${k} ×${keys.length}`).join(' · ');
    },
    compact(o) {
      const out = {};
      for (const [k, v] of Object.entries(o || {})) if (v !== null && v !== '' && k !== 'id') out[k] = v;
      return out;
    },
    fieldLabel(f) { return FIELD_LABEL[f] || f; },
    show(v) {
      if (v === null || v === undefined || v === '') return '空';
      if (typeof v === 'boolean') return v ? '是' : '否';
      if (typeof v === 'object') return JSON.stringify(v);
      const s = String(v);
      return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s) ? s.slice(0, 19).replace('T', ' ') : s;
    },
  },
  async mounted() {
    this.facets = await api.get('/api/change-log/facets');
    this.load();
  },
});

/* ---------------- 本单位设置（v0.29）：名称/税号/开户行（多账户对应 1002xx）/会计准则 ---------------- */
const CompanyView = defineComponent({
  template: `
  <div>
    <div class="card" v-if="f">
      <div class="card-h"><b>本单位设置</b><span class="sub">发票方向判断、报表抬头、银行流水账号对科目都读这里</span></div>
      <div class="form-grid" style="grid-template-columns:repeat(3,1fr);padding:14px 18px">
        <div class="fg span2"><label>单位名称（与发票、税务登记一致）</label><input v-model="f.name"></div>
        <div class="fg"><label>简称</label><input v-model="f.short_name"></div>
        <div class="fg"><label>统一社会信用代码 / 税号</label><input v-model="f.tax_no" class="mono"></div>
        <div class="fg"><label>法定代表人</label><input v-model="f.legal_rep"></div>
        <div class="fg"><label>电话</label><input v-model="f.phone"></div>
        <div class="fg span2"><label>地址</label><input v-model="f.address"></div>
        <div class="fg"><label>执行的会计准则</label><select v-model="f.accounting_standard"><option>小企业会计准则</option><option>企业会计准则</option></select></div>
        <div class="fg"><label>往来单位里的本单位编码</label><input v-model="f.partner_code" class="mono"></div>
      </div>
      <div class="card-h" style="border-top:1px solid var(--line)"><b>开户账户</b><span class="sub">每个账户对应一个 1002xx 银行存款末级科目；银行流水按账号自动入对应科目；收付款默认走「默认」账户</span></div>
      <table>
        <thead><tr><th>开户行</th><th>账号</th><th>对应科目</th><th>默认</th><th></th></tr></thead>
        <tbody><tr v-for="(a,i) in f.bank_accounts" :key="i">
          <td><input v-model="a.bank"></td><td><input v-model="a.account_no" class="mono"></td>
          <td><select v-model="a.account_code"><option v-for="x in bankAccs" :key="x.code" :value="x.code">{{x.code}} {{x.name}}</option></select></td>
          <td><input type="radio" name="defacc" :checked="a.default" @change="f.bank_accounts.forEach((b,j)=>b.default=(j===i))" style="width:auto"></td>
          <td><span class="x clickable" v-if="f.bank_accounts.length>1" @click="f.bank_accounts.splice(i,1)">✕</span></td></tr></tbody>
      </table>
      <div style="padding:8px 18px" class="sub-line"><button class="btn btn-ghost btn-sm" @click="f.bank_accounts.push({bank:'',account_no:'',account_code:'',default:false})">＋ 添加账户</button>
        新开户时先在科目表里加一个 1002 下的明细科目（如 100202 某银行），再在这里添加账户。</div>
      <div v-if="msg" class="sub-line" style="padding:0 18px" :class="{'auth-error': err}">{{msg}}</div>
      <div class="actions" style="padding:0 18px 14px"><button class="btn btn-ink" @click="save">保存</button></div>
    </div>
    <div class="card">
      <div class="card-h"><b>电子印章</b><span class="sub">合同详情「合同原件」PDF 旁点「盖章」使用；尺寸按章图像素 ÷ DPI 自动算（实物尺寸），盖章时不随对方章缩放。换章图只限管理员。</span></div>
      <table><thead><tr><th>印章</th><th class="num">宽 mm</th><th class="num">高 mm</th><th class="num">透明度</th><th>文件</th></tr></thead>
        <tbody><tr v-for="s in seals" :key="s.name"><td>{{s.name}}</td><td class="num">{{s.width_mm}}</td><td class="num">{{s.height_mm}}</td><td class="num">{{s.opacity}}</td><td :class="s.exists?'':'neg'">{{s.exists?'✓':'缺失'}}</td></tr>
          <tr v-if="!seals.length"><td colspan="5" class="empty">还没有印章</td></tr></tbody></table>
      <div style="padding:10px 18px;display:flex;gap:8px;align-items:center" class="sub-line">
        <input v-model="sealName" placeholder="印章名称，如 合同章" style="width:150px">
        <input type="number" v-model.number="sealDpi" style="width:80px" title="章图 DPI"><span>dpi</span>
        <input type="number" step="0.01" v-model.number="sealOpacity" style="width:70px" title="透明度"><span>透明度</span>
        <label class="btn btn-ghost btn-sm">上传透明 PNG<input type="file" accept=".png" style="display:none" @change="uploadSeal"></label>
      </div>
    </div>
  </div>`,
  data: () => ({ f: null, accounts: [], msg: '', err: false, seals: [], sealName: '合同章', sealDpi: 600, sealOpacity: 0.52 }),
  computed: {
    bankAccs() { const par = new Set(this.accounts.map(a => a.parent_code).filter(x => x)); return this.accounts.filter(a => a.code.startsWith('1002') && !par.has(a.code)); },
  },
  methods: {
    async uploadSeal(ev) {
      const file = ev.target.files[0]; ev.target.value = '';
      if (!file || !this.sealName) return;
      const buf = new Uint8Array(await file.arrayBuffer());
      let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
      try { await api.post('/api/seals', { name: this.sealName, content_b64: btoa(bin), dpi: this.sealDpi, opacity: this.sealOpacity }); this.seals = await api.get('/api/seals'); this.msg = '印章已更新'; this.err = false; }
      catch (e) { this.msg = String(e); this.err = true; }
    },
    async save() {
      try { await api.put('/api/company', this.f); this.msg = '已保存'; this.err = false; }
      catch (e) { this.msg = String(e); this.err = true; }
    },
  },
  async mounted() {
    [this.f, this.accounts, this.seals] = await Promise.all([api.get('/api/company'), api.get('/api/accounts'), api.get('/api/seals')]);
    if (!this.f.bank_accounts.length) this.f.bank_accounts.push({ bank: '', account_no: '', account_code: '100201', default: true });
  },
});
