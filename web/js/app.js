/* owe-erp 前端（由 app.js 机械拆分，经典脚本按序加载，顺序见 index.html；依赖：base 先于 forms 先于 views 先于 app） */

const NAV = [
  { t: '业务', items: [
    { key: 'dash', label: '执行看板', icon: '▣' },
    { key: 'stock', label: '库存', icon: '▦' },
    { key: 'contracts', label: '合同订单', icon: '▤' },
    { key: 'movements', label: '出入库', icon: '⇄' },
    { key: 'transfers', label: '库存调拨', icon: '⇆' },
    { key: 'invoices', label: '发票', icon: '◈' },
    { key: 'payments', label: '收付款', icon: '¥' },
    { key: 'twm', label: '三单匹配', icon: '⇌' },
  ]},
  { t: '财务', items: [
    { key: 'close', label: '月结检查', icon: '☑' },
    { key: 'bank', label: '银行流水', icon: '⌸' },
    { key: 'invdocs', label: '发票档案', icon: '▧' },
    { key: 'statements', label: '会计报表', icon: '▤' },
    { key: 'taxfiling', label: '纳税申报', icon: '▨' },
    { key: 'assets', label: '固定资产', icon: '▥' },
    { key: 'controls', label: '业务核销与对账', icon: '✓' },
    { key: 'vouchers', label: '会计凭证', icon: '≡' },
    { key: 'ledger', label: '明细账', icon: '▤' },
    { key: 'trial', label: '科目余额表', icon: '∑' },
    { key: 'arap', label: '往来账', icon: '⇄' },
    { key: 'cc', label: '成本中心', icon: '◈' },
    { key: 'related', label: '关联交易', icon: '⇋' },
    { key: 'reports', label: '管理报表', icon: '▣' },
  ]},
  { t: '生产', items: [
    { key: 'workorders', label: '工单', icon: '⚙' },
    { key: 'boms', label: 'BOM 管理', icon: '⊞' },
    { key: 'mfgclose', label: '成本结转', icon: '∑' },
    { key: 'quote', label: '报价测算', icon: '¥' },
    { key: 'rdledger', label: '研发辅助账', icon: '✎' },
    { key: 'lots', label: '批次库存', icon: '▦' },
  ]},
  { t: '主数据', items: [
    { key: 'materials', label: '物料', icon: '☰' },
    { key: 'partners', label: '往来单位', icon: '◉' },
    { key: 'locations', label: '存放地点', icon: '⌂' },
    { key: 'prices', label: '供应商价目', icon: '¥' },
  ]},
  { t: '系统', items: [
    { key: 'oplog', label: '操作日志', icon: '☷' },
    { key: 'company', label: '本单位设置', icon: '⌂' },
    { key: 'users', label: '用户管理', icon: '⚿', admin: true },
  ]},
];

/* ---------------- 根应用与登录门 ---------------- */
const App = defineComponent({
  components: { ControlsView, DashView, StockView, ContractsView, ContractDetailView, MovementsView, InvoicesView, PaymentsView, PartnersView, MaterialsView, ReservedView, ContractForm, ArchiveForm, MovementForm, InvoiceForm, PaymentForm, ThreeWayMatchView, VouchersView, LedgerView, TrialBalanceView, ArApView, CostCentersView, ReportsView, UsersView, OpLogView, TransfersView, LocationsView, BankView, AssetsView, InvoiceDocsView, TaxFilingView, StatementsView, CompanyView, CloseView, WorkOrdersView, WorkOrderView, BomView, MfgCloseView, QuoteView, RdLedgerView, LotsView, RelatedPartyView, SupplierPricesView },
  template: `
  <div>
    <div v-if="!authReady" class="auth-screen"><div class="auth-card">正在检查登录状态…</div></div>
    <div v-else-if="!auth.authenticated" class="auth-screen">
      <div class="auth-card">
        <div class="auth-brand"><div class="seal-mark">{{brand.slice(0,1)}}</div><div><b>{{brand}}</b><span>v0.38 · 外网动态码</span></div></div>
        <div class="auth-title">{{auth.needs_setup ? '创建管理员账号' : '登录'}}</div>
        <div v-if="auth.needs_setup" class="hint hint-blue">首次使用请创建管理员账号；账号只保存在本机 ERP 数据库中。</div>
        <form class="auth-form" @submit.prevent="submitAuth">
          <label v-if="auth.needs_setup">姓名<input v-model="displayName" autocomplete="name" placeholder="张三"></label>
          <label>账号<input v-model="username" autocomplete="username" required></label>
          <label>密码<input v-model="password" type="password" :autocomplete="auth.needs_setup ? 'new-password' : 'current-password'" required></label>
          <label v-if="needTotp">动态码<input v-model="totp" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="验证器上的 6 位数" ref="totpInput"></label>
          <div v-if="authError" class="auth-error">{{authError}}</div>
          <button class="btn btn-ink" type="submit" :disabled="authBusy">{{auth.needs_setup ? '创建并进入' : '进入 ERP'}}</button>
        </form>
      </div>
    </div>
    <div v-else class="app">
      <aside class="side">
        <div class="brand">
          <div class="seal-mark">{{brand.slice(0,1)}}</div>
          <div><div class="brand-name">{{brand}}</div><div class="brand-sub">INVENTORY · TRADE</div></div>
        </div>
        <nav>
          <template v-for="g in visibleNav">
            <div class="nav-t clickable" @click="toggleGroup(g.t)" :title="folded[g.t] ? '展开' : '折叠'">{{folded[g.t] && !groupActive(g) ? '▸' : '▾'}} {{g.t}}</div>
            <template v-if="!folded[g.t] || groupActive(g)">
              <div class="nav-i" v-for="item in g.items" :key="item.key" :class="{on:isActive(item.key)}" @click="go(item.key)">{{item.icon}} {{item.label}}</div>
            </template>
          </template>
        </nav>
        <div class="side-foot">v0.38 · 外网动态码</div>
      </aside>
      <div class="main">
        <div class="top">
          <div class="crumb">{{brand}} / <b>{{crumbText}}</b></div>
          <div class="top-actions">
            <span class="user-chip">{{auth.user.display_name || auth.user.username}} · {{auth.user.role_label}}</span>
            <button class="btn btn-ghost btn-sm" @click="openPassword">改密码 / 动态码</button>
            <button class="btn btn-ghost btn-sm" @click="logout">退出</button>
          </div>
        </div>
        <div class="content">
          <component :is="viewComp" :key="route" :id="routeId" v-bind="['UsersView','ControlsView'].includes(viewComp) ? {me: auth.user} : {}"></component>
        </div>
      </div>
    </div>
    <div v-if="passwordOpen" class="modal-mask" @click.self="passwordOpen=false">
      <div class="modal password-modal">
        <div class="modal-h"><b>修改密码 · 外网登录动态码</b><span class="x" @click="passwordOpen=false">×</span></div>
        <form class="form-grid" @submit.prevent="submitPassword">
          <div class="fg span2"><label>原密码<input v-model="oldPassword" type="password" autocomplete="current-password" required></label></div>
          <div class="fg"><label>新密码<input v-model="newPassword" type="password" autocomplete="new-password" minlength="10" required></label></div>
          <div class="fg"><label>确认新密码<input v-model="confirmPassword" type="password" autocomplete="new-password" minlength="10" required></label></div>
          <div v-if="passwordError" class="fg span2 auth-error">{{passwordError}}</div>
          <div class="actions span2"><button class="btn btn-ink" type="submit" :disabled="passwordBusy">保存新密码</button></div>
        </form>
        <div style="border-top:1px solid var(--line);margin-top:10px;padding:12px 18px 4px">
          <b style="font-size:13px">外网登录动态码</b>
          <span class="pill" :class="auth.user && auth.user.totp_enabled ? 'p-ok' : 'p-gray'" style="margin-left:6px">{{auth.user && auth.user.totp_enabled ? '已开通' : '未开通'}}</span>
          <div class="sub-line" style="margin:6px 0">从外网（公司以外）登录要多输一个手机验证器上的 6 位动态码，内网不用。验证器可以用微信小程序「腾讯身份验证器」，或 Microsoft Authenticator、Google Authenticator。</div>
          <template v-if="auth.user && !auth.user.totp_enabled">
            <button v-if="!otp" class="btn btn-ink btn-sm" @click="startTotp">开通</button>
            <div v-else style="display:flex;gap:14px;align-items:flex-start">
              <div style="width:160px;height:160px;background:#fff" v-html="otp.svg"></div>
              <div style="flex:1">
                <div class="sub-line">① 用验证器扫左边二维码（扫不了就手动添加，密钥：<span class="mono" style="user-select:all">{{otp.secret}}</span>）</div>
                <div class="sub-line" style="margin-top:6px">② 输入验证器上现在显示的 6 位数：</div>
                <div style="display:flex;gap:6px;margin-top:4px"><input v-model="otpCode" inputmode="numeric" maxlength="6" style="width:120px" class="mono"><button class="btn btn-ink btn-sm" @click="enableTotp">确认开通</button></div>
              </div>
            </div>
          </template>
          <div v-else style="display:flex;gap:6px;align-items:center"><input v-model="otpPwd" type="password" placeholder="输入登录密码" style="width:180px"><button class="btn btn-ghost btn-sm" @click="disableTotp">关闭（换手机时先关再重新开通）</button></div>
          <div v-if="otpMsg" class="sub-line" :class="{neg: otpErr}" style="margin-top:6px">{{otpMsg}}</div>
        </div>
      </div>
    </div>
  </div>`,
  data: () => ({
    folded: JSON.parse(localStorage.getItem('owe-nav-folded') || '{}'),   // 左栏分组折叠（按浏览器记住；当前页所在分组始终展开）
    nav: NAV, route: 'dash', authReady: false, brand: 'ERP',
    auth: { needs_setup: true, authenticated: false, user: null },
    username: '', display_name: '', password: '', authBusy: false, authError: '',
    passwordOpen: false, passwordBusy: false, passwordError: '',
    needTotp: false, totp: '', otp: null, otpCode: '', otpPwd: '', otpMsg: '', otpErr: false,
    oldPassword: '', newPassword: '', confirmPassword: '',
  }),
  computed: {
    viewComp() {
      const map = { controls:'ControlsView', dash: 'DashView', stock: 'StockView', contracts: 'ContractsView', contract: 'ContractDetailView',
                    movements: 'MovementsView', invoices: 'InvoicesView', payments: 'PaymentsView', twm: 'ThreeWayMatchView',
                    partners: 'PartnersView', materials: 'MaterialsView', reserved: 'ReservedView',
                    vouchers: 'VouchersView', ledger: 'LedgerView', trial: 'TrialBalanceView',
                    arap: 'ArApView', cc: 'CostCentersView', reports: 'ReportsView', users: 'UsersView', oplog: 'OpLogView', transfers: 'TransfersView', locations: 'LocationsView', bank: 'BankView', assets: 'AssetsView', invdocs: 'InvoiceDocsView', taxfiling: 'TaxFilingView', statements: 'StatementsView', company: 'CompanyView', close: 'CloseView',
                    workorders: 'WorkOrdersView', workorder: 'WorkOrderView', boms: 'BomView', mfgclose: 'MfgCloseView', quote: 'QuoteView', rdledger: 'RdLedgerView', lots: 'LotsView', related: 'RelatedPartyView', prices: 'SupplierPricesView' };
      const view = map[this.route.split('/')[0]] || 'DashView';
      return view === 'UsersView' && !this.isAdmin ? 'DashView' : view;
    },
    isAdmin() { return !!(this.auth.user && this.auth.user.role === 'admin'); },
    visibleNav() {
      return this.nav.map(g => ({ ...g, items: g.items.filter(i => !i.admin || this.isAdmin) })).filter(g => g.items.length);
    },
    routeId() { return this.route.includes('/') ? this.route.split('/')[1] : null; },
    crumbText() {
      const m = { controls:'业务核销与对账', dash: '执行看板', stock: '库存', contracts: '合同订单', contract: '合同详情',
                  movements: '出入库', invoices: '发票', payments: '收付款', twm: '三单匹配',
                  partners: '往来单位', materials: '物料', reserved: '预留模块',
                  vouchers: '会计凭证', ledger: '明细账', trial: '科目余额表',
                  arap: '往来账', cc: '成本中心', reports: '财务报表', users: '用户管理', oplog: '操作日志', transfers: '库存调拨', locations: '存放地点', bank: '银行流水', assets: '固定资产', invdocs: '发票档案', taxfiling: '纳税申报', statements: '会计报表（小企业会计准则）', company: '本单位设置', close: '月结检查',
                  workorders: '工单', workorder: '工单详情', boms: 'BOM 管理', mfgclose: '月末成本结转', quote: '报价成本测算', rdledger: '研发支出辅助账', lots: '批次库存', related: '关联交易', prices: '供应商价目表' };
      const k = this.route.split('/')[0];
      if (k === 'users' && !this.isAdmin) return '执行看板';
      return m[k] || '执行看板';
    },
  },
  methods: {
    toggleGroup(t) { this.folded = { ...this.folded, [t]: !this.folded[t] }; localStorage.setItem('owe-nav-folded', JSON.stringify(this.folded)); },
    groupActive(g) { return g.items.some(i => this.isActive(i.key)); },
    async loadAuth() {
      try { this.auth = await api.get('/api/auth/status'); this.brand = this.auth.brand || 'ERP'; document.title = this.brand; }
      catch(e) { this.authError = String(e); }
      finally { this.authReady = true; }
    },
    async submitAuth() {
      if (this.authBusy) return;
      this.authBusy = true; this.authError = '';
      try {
        const body = this.auth.needs_setup
          ? { username:this.username, display_name:this.displayName, password:this.password }
          : { username:this.username, password:this.password, totp: this.needTotp ? this.totp : '' };
        this.auth = await api.post(this.auth.needs_setup ? '/api/auth/setup' : '/api/auth/login', body);
        this.password = ''; this.totp = ''; this.needTotp = false;
      } catch(e) {
        // 外网登录：密码对了，后端要动态码（401 + 提示语），这时才显示动态码输入框
        if (e.status === 401 && String(e).includes('动态码') && !this.needTotp) {
          this.needTotp = true; this.authError = '外网登录：请输入手机验证器上的 6 位动态码';
          this.$nextTick(() => this.$refs.totpInput && this.$refs.totpInput.focus());
        } else { this.authError = String(e); this.totp = ''; }
      }
      finally { this.authBusy = false; }
    },
    openPassword() { this.passwordOpen = true; this.otp = null; this.otpCode = this.otpPwd = this.otpMsg = ''; this.otpErr = false; },
    async startTotp() {
      try { this.otp = await api.post('/api/auth/totp/setup', {}); this.otpMsg = ''; }
      catch (e) { this.otpMsg = String(e); this.otpErr = true; }
    },
    async enableTotp() {
      try { await api.post('/api/auth/totp/enable', { code: this.otpCode }); this.auth.user.totp_enabled = true; this.otp = null;
            this.otpMsg = '已开通。以后从外网登录时，输完密码再输验证器上的动态码。'; this.otpErr = false; }
      catch (e) { this.otpMsg = String(e); this.otpErr = true; }
    },
    async disableTotp() {
      if (!confirm('关闭后就不能从外网登录了（直到重新开通）。确定？')) return;
      try { await api.post('/api/auth/totp/disable', { password: this.otpPwd }); this.auth.user.totp_enabled = false; this.otpPwd = '';
            this.otpMsg = '已关闭。'; this.otpErr = false; }
      catch (e) { this.otpMsg = String(e); this.otpErr = true; }
    },
    async logout() {
      try { this.auth = await api.post('/api/auth/logout', {}); }
      catch(e) { this.authError = String(e); }
    },
    async submitPassword() {
      if (this.passwordBusy) return;
      this.passwordError = '';
      if (this.newPassword !== this.confirmPassword) { this.passwordError = '两次输入的新密码不一致'; return; }
      this.passwordBusy = true;
      try {
        await api.put('/api/auth/password', { old_password:this.oldPassword, new_password:this.newPassword });
        this.passwordOpen = false;
        this.oldPassword = this.newPassword = this.confirmPassword = '';
      } catch(e) { this.passwordError = String(e); }
      finally { this.passwordBusy = false; }
    },
    onUnauthorized() {
      if (!this.auth.authenticated) return;
      this.auth = { needs_setup:false, authenticated:false, user:null };
      this.passwordOpen = false;
      this.authError = '登录已过期，请重新登录';
    },
    go(k) { location.hash = '#/' + k; },
    isActive(k) { const r = this.route.split('/')[0]; return r === k || (k === 'workorders' && r === 'workorder'); },
    onHash() {
      const h = location.hash.replace(/^#\/?/, '') || 'dash';
      this.route = h;
    },
  },
  mounted() {
    window.addEventListener('hashchange', this.onHash);
    window.addEventListener('owe:unauthorized', this.onUnauthorized);
    this.onHash();
    this.loadAuth();
  },
  beforeUnmount() {
    window.removeEventListener('hashchange', this.onHash);
    window.removeEventListener('owe:unauthorized', this.onUnauthorized);
  },
});

const app = createApp(App);
/* 按钮处理函数里未捕获的接口错误由 Vue 接住（不会冒到 unhandledrejection），权限拒绝在这里提示 */
app.config.errorHandler = (err) => {
  if (err instanceof ApiError && err.status === 403) alert(err.message);
  else console.error(err);
};
app.mount('#root');
