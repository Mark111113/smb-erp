/* owe-erp 前端 — 通用列表 DataTable（v0.20，依赖 base.js；在 forms/views 之前加载）

   页面排版原则（2026-09-27 与用户定）：
   1. 一列一字段，不在一格里叠放多个字段；次要字段做成列、默认隐藏，按需在「列设置」打开
   2. 按字段类型统一默认宽度与显示（TYPES），同一字段在各页面长得一样
   3. 行高统一两行文字：单行内容垂直居中，长文本最多两行、超出截断、悬停看全文
   4. 列设置（显示/顺序/排序）跟账号走，存服务器 /api/prefs/{view}；可「恢复默认」
   5. 默认可见列要保证 1440 宽不出横向滚动；用户多开列时才横向滚动，首列与操作列固定
   6. 导出 CSV 按当前可见列、当前过滤条件下的全部行（不受分页影响）
   7. 分页（v0.27）：每页 20/50/100/200/全部，默认 50；日期过滤（传 date-key）：全部/本月/上月/本季/本年/近30天/自定义。
      每页条数与日期预设跟账号走（同列设置一起存 prefs）

   列定义：{ key, label, type, width?, min?, hidden?(默认隐藏), sortable?(默认 true，actions 除外),
            value(row)→原始值（排序/导出/默认显示）, title(row)→悬停文字, text(row)→显示文字（默认按类型格式化） }
   自定义单元格：<template #cell-KEY="{ row, value }">…</template>；操作列 key 固定为 'actions'。
   整行点击：:row-click="r => …"（格内按钮记得 @click.stop）；可展开明细：expandable + <template #expand="{ row }">。
   操作列：主按钮放 #cell-actions；次要操作传 :menu="row => [{ label, run, danger? }]"，组件自动出「⋯」浮层菜单。
*/

const DT_TYPES = {
  id:      { width: 150, cls: 'mono dt-nowrap' },            // 单号、编码：永不换行
  code:    { width: 104, cls: 'mono dt-nowrap' },            // 物料编码等短编码
  date:    { width: 96,  cls: 'mono dt-nowrap' },
  qty:     { width: 70,  cls: 'num dt-nowrap', num: true },
  money:   { width: 104, cls: 'num dt-nowrap', num: true },  // 金额、单价：2 位小数，悬停看完整精度
  status:  { width: 84,  cls: 'dt-nowrap' },
  partner: { width: 96,  cls: 'dt-ellipsis' },               // 往来单位：默认简称，悬停看全称
  person:  { width: 76,  cls: 'dt-ellipsis' },
  short:   { width: 64,  cls: 'dt-nowrap' },                 // 单位等极短字段
  text:    { min: 140,   cls: 'dt-clamp' },                  // 长文本：自适应宽度，最多两行
  actions: { width: 96,  cls: 'dt-actions' },
};

const dtFormat = (col, v) => {
  if (v === null || v === undefined || v === '') return '—';
  if (col.type === 'money') return fmt(v);
  if (col.type === 'qty') return Number.isFinite(+v) ? String(+(+v).toFixed(3)) : String(v);
  return String(v);
};

const DataTable = defineComponent({
  props: {
    view: { type: String, required: true },     // 视图标识，如 'movements'
    columns: { type: Array, required: true },
    rows: { type: Array, default: () => [] },
    rowKey: { type: String, default: 'id' },
    rowClass: { type: Function, default: null },
    empty: { type: String, default: '无记录' },
    exportName: { type: String, default: '' },
    rowClick: { type: Function, default: null },
    expandable: { type: Boolean, default: false },
    menu: { type: Function, default: null },
    dateKey: { type: String, default: '' },     // 行里的日期字段（YYYY-MM-DD），给了就出日期过滤
  },
  template: `
  <div class="dt">
    <div class="dt-bar">
      <slot name="toolbar"></slot>
      <template v-if="dateKey">
        <select v-model="datePreset" @change="onPreset" style="width:96px" title="按日期过滤">
          <option value="">全部日期</option><option value="month">本月</option><option value="last_month">上月</option>
          <option value="quarter">本季</option><option value="year">本年</option><option value="d30">近 30 天</option><option value="custom">自定义</option></select>
        <template v-if="datePreset==='custom'"><input type="date" v-model="dateFrom" style="width:130px"><span class="sub">至</span><input type="date" v-model="dateTo" style="width:130px"></template>
      </template>
      <span class="sub" style="margin-left:auto">{{filtered.length === rows.length ? rows.length + ' 条' : filtered.length + ' / ' + rows.length + ' 条'}}</span>
      <button class="btn btn-ghost btn-sm" @click.stop="panel=!panel">列设置 ▾</button>
      <button v-if="exportName" class="btn btn-ghost btn-sm" @click="exportCsv">导出 CSV</button>
      <div v-if="panel" class="dt-panel" @click.stop>
        <div class="sub" style="margin-bottom:6px">勾选显示；↑↓ 调整顺序。设置跟账号走。</div>
        <div v-for="(c,i) in orderedAll" :key="c.key" class="dt-panel-row">
          <label><input type="checkbox" :checked="visibleKeys.includes(c.key)" :disabled="c.key==='actions'" @change="toggle(c.key)" style="width:auto"> {{c.label || '操作'}}</label>
          <span class="dt-move"><button :disabled="i===0" @click="move(c.key,-1)">↑</button><button :disabled="i===orderedAll.length-1" @click="move(c.key,1)">↓</button></span>
        </div>
        <div style="display:flex;gap:8px;margin-top:8px"><button class="btn btn-ghost btn-sm" @click="reset">恢复默认</button>
          <button class="btn btn-ink btn-sm" style="margin-left:auto" @click="panel=false">完成</button></div>
      </div>
    </div>
    <div class="dt-scroll">
      <table class="dt-table" :style="{minWidth: minWidth + 'px'}">
        <colgroup><col v-for="c in visible" :key="c.key" :style="c._w ? {width: c._w + 'px'} : {}"></colgroup>
        <thead><tr>
          <th v-for="(c,i) in visible" :key="c.key" :class="[thClass(c), stickyClass(c, i)]" @click="sortBy(c)">
            {{c.label}}<span v-if="sort && sort.key===c.key" class="dt-sort">{{sort.dir==='asc'?'↑':'↓'}}</span></th>
        </tr></thead>
        <tbody>
          <template v-for="r in paged" :key="r[rowKey]">
            <tr :class="[rowClass ? rowClass(r) : '', (rowClick || expandable) ? 'clickable-row' : '', expanded[r[rowKey]] ? 'dt-open' : '']" @click="onRow(r)">
              <td v-for="(c,i) in visible" :key="c.key" :class="[cellClass(c), stickyClass(c, i)]">
                <template v-if="c.key==='actions'">
                  <span @click.stop><slot name="cell-actions" :row="r"></slot></span>
                  <button v-if="menu && menu(r).length" class="btn btn-ghost btn-sm" style="margin-left:4px" title="更多操作" @click.stop="openMenu($event, r)">⋯</button>
                </template>
                <slot v-else :name="'cell-' + c.key" :row="r" :value="val(c, r)">
                  <div :class="innerClass(c)" :title="tip(c, r)">{{show(c, r)}}</div>
                </slot>
              </td>
            </tr>
            <tr v-if="expandable && expanded[r[rowKey]]" class="dt-expand"><td :colspan="visible.length"><slot name="expand" :row="r"></slot></td></tr>
          </template>
          <tr v-if="!filtered.length"><td :colspan="visible.length" class="empty">{{rows.length ? '当前日期范围内没有记录' : empty}}</td></tr>
        </tbody>
      </table>
    </div>
    <div class="dt-pager" v-if="filtered.length > 20 || pageSize !== 50">
      <span class="sub">每页</span>
      <select v-model.number="pageSize" @change="page = 1; save()" style="width:76px">
        <option :value="20">20</option><option :value="50">50</option><option :value="100">100</option><option :value="200">200</option><option :value="0">全部</option></select>
      <span style="margin-left:auto" class="sub" v-if="pages > 1">第 {{page}} / {{pages}} 页</span>
      <template v-if="pages > 1">
        <button class="btn btn-ghost btn-sm" :disabled="page<=1" @click="page=1">«</button>
        <button class="btn btn-ghost btn-sm" :disabled="page<=1" @click="page--">‹ 上一页</button>
        <button class="btn btn-ghost btn-sm" :disabled="page>=pages" @click="page++">下一页 ›</button>
        <button class="btn btn-ghost btn-sm" :disabled="page>=pages" @click="page=pages">»</button>
      </template>
    </div>
    <div v-if="menuAt" class="row-menu" :style="{top: menuAt.top + 'px', left: menuAt.left + 'px'}" @click.stop>
      <button v-for="(it, k) in menuAt.items" :key="k" :class="{danger: it.danger}" @click="menuAt = null; it.run()">{{it.label}}</button>
    </div>
  </div>`,
  data: () => ({ menuAt: null, visibleKeys: [], order: [], sort: null, panel: false, loaded: false, saveTimer: null, expanded: {},
                 pageSize: 50, page: 1, datePreset: '', dateFrom: '', dateTo: '' }),
  computed: {
    cols() {
      return this.columns.map(c => {
        const t = DT_TYPES[c.type || 'text'] || DT_TYPES.text;
        return { ...c, _t: t, _w: c.width || t.width || null, _min: c.min || t.min || 0 };
      });
    },
    byKey() { return Object.fromEntries(this.cols.map(c => [c.key, c])); },
    orderedAll() { return this.order.map(k => this.byKey[k]).filter(Boolean); },
    visible() { return this.orderedAll.filter(c => this.visibleKeys.includes(c.key)); },
    minWidth() { return this.visible.reduce((s, c) => s + (c._w || c._min || 120), 0); },
    range() {
      const d = new Date(), y = d.getFullYear(), m = d.getMonth(), iso = x => x.getFullYear() + '-' + String(x.getMonth() + 1).padStart(2, '0') + '-' + String(x.getDate()).padStart(2, '0');
      switch (this.datePreset) {
        case 'month': return [iso(new Date(y, m, 1)), iso(new Date(y, m + 1, 0))];
        case 'last_month': return [iso(new Date(y, m - 1, 1)), iso(new Date(y, m, 0))];
        case 'quarter': { const q = Math.floor(m / 3) * 3; return [iso(new Date(y, q, 1)), iso(new Date(y, q + 3, 0))]; }
        case 'year': return [y + '-01-01', y + '-12-31'];
        case 'd30': return [iso(new Date(y, m, d.getDate() - 29)), iso(d)];
        case 'custom': return [this.dateFrom || '0000-00-00', this.dateTo || '9999-99-99'];
        default: return null;
      }
    },
    filtered() {
      const r = this.range;
      if (!this.dateKey || !r) return this.rows;
      return this.rows.filter(x => { const v = String(x[this.dateKey] || '').slice(0, 10); return v && v >= r[0] && v <= r[1]; });
    },
    pages() { return this.pageSize ? Math.max(1, Math.ceil(this.filtered.length / this.pageSize)) : 1; },
    paged() {
      if (!this.pageSize) return this.sorted;
      const p = Math.min(this.page, this.pages);
      return this.sorted.slice((p - 1) * this.pageSize, p * this.pageSize);
    },
    sorted() {
      if (!this.sort) return this.filtered;
      const c = this.byKey[this.sort.key];
      if (!c) return this.filtered;
      const dir = this.sort.dir === 'asc' ? 1 : -1;
      return [...this.filtered].sort((a, b) => {
        const x = this.val(c, a), y = this.val(c, b);
        if (x == null && y == null) return 0;
        if (x == null) return 1;
        if (y == null) return -1;
        return (c._t.num ? (x - y) : String(x).localeCompare(String(y), 'zh-CN')) * dir;
      });
    },
  },
  methods: {
    defaults() { return this.cols.filter(c => !c.hidden).map(c => c.key); },
    onPreset() { this.page = 1; this.save(); },
    val(c, r) { return c.value ? c.value(r) : r[c.key]; },
    show(c, r) { return c.text ? c.text(r) : dtFormat(c, this.val(c, r)); },
    tip(c, r) {
      if (c.title) return c.title(r);
      const v = this.val(c, r);
      if (c.type === 'money' && v != null && v !== '') return String(v);
      return ['text', 'partner', 'person'].includes(c.type || 'text') ? this.show(c, r) : '';
    },
    thClass(c) { return (c._t.num ? 'num ' : '') + (c.sortable === false || c.key === 'actions' ? '' : 'dt-sortable'); },
    cellClass(c) { return c._t.num ? 'num' : (c.key === 'actions' ? 'dt-actions' : ''); },
    innerClass(c) { return c._t.cls; },
    stickyClass(c, i) { return i === 0 ? 'dt-stick-l' : (c.key === 'actions' ? 'dt-stick-r' : ''); },
    sortBy(c) {
      if (c.sortable === false || c.key === 'actions') return;
      if (!this.sort || this.sort.key !== c.key) this.sort = { key: c.key, dir: c._t.num || c.type === 'date' ? 'desc' : 'asc' };
      else if ((this.sort.dir === 'asc') === !(c._t.num || c.type === 'date')) this.sort = { key: c.key, dir: this.sort.dir === 'asc' ? 'desc' : 'asc' };
      else this.sort = null;
      this.save();
    },
    toggle(k) {
      this.visibleKeys = this.visibleKeys.includes(k) ? this.visibleKeys.filter(x => x !== k) : this.order.filter(x => x === k || this.visibleKeys.includes(x));
      this.save();
    },
    move(k, d) {
      const i = this.order.indexOf(k), j = i + d;
      if (j < 0 || j >= this.order.length) return;
      const o = [...this.order]; [o[i], o[j]] = [o[j], o[i]]; this.order = o;
      this.visibleKeys = this.order.filter(x => this.visibleKeys.includes(x));
      this.save();
    },
    async reset() {
      this.order = this.cols.map(c => c.key); this.visibleKeys = this.defaults(); this.sort = null; this.pageSize = 50; this.datePreset = ''; this.page = 1;
      try { await api.del('/api/prefs/' + this.view); } catch (e) { /* 离线也能用默认 */ }
    },
    save() {
      clearTimeout(this.saveTimer);
      this.saveTimer = setTimeout(() => {
        api.put('/api/prefs/' + this.view, { columns: this.visibleKeys, order: this.order, known: this.cols.map(c => c.key), sort: this.sort,
                                              page_size: this.pageSize, date_preset: this.datePreset === 'custom' ? '' : this.datePreset })
           .catch(() => {});
      }, 400);
    },
    apply(p) {
      const all = this.cols.map(c => c.key);
      const known = new Set(p.known || []);
      // 保存后新增的列：插回默认位置，按默认可见性
      let order = (p.order || []).filter(k => all.includes(k));
      all.forEach((k, i) => { if (!order.includes(k)) order.splice(Math.min(i, order.length), 0, k); });
      let vis = (p.columns || []).filter(k => all.includes(k));
      this.cols.forEach(c => { if (!known.has(c.key) && !c.hidden && !vis.includes(c.key)) vis.push(c.key); });
      if (!vis.includes('actions') && all.includes('actions')) vis.push('actions');
      this.order = order;
      this.visibleKeys = order.filter(k => vis.includes(k));
      this.sort = p.sort && all.includes(p.sort.key) ? p.sort : null;
      if (p.page_size !== undefined) this.pageSize = p.page_size;
      if (p.date_preset) this.datePreset = p.date_preset;
    },
    exportCsv() {
      const cols = this.visible.filter(c => c.key !== 'actions');
      const esc = s => { s = s == null ? '' : String(s); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
      const lines = [cols.map(c => esc(c.label)).join(',')].concat(
        this.sorted.map(r => cols.map(c => esc(c.value ? c.value(r) : (c.text ? c.text(r) : r[c.key]))).join(',')));
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8' }));
      a.download = this.exportName + '_' + today().replaceAll('-', '') + '.csv';
      a.click(); URL.revokeObjectURL(a.href);
    },
    closePanel() { this.panel = false; this.menuAt = null; },
    openMenu(ev, r) {
      if (this.menuAt && this.menuAt.row === r) { this.menuAt = null; return; }
      const items = this.menu(r), b = ev.currentTarget.getBoundingClientRect(), h = items.length * 32 + 10;
      this.menuAt = { row: r, items, left: Math.max(8, b.right - 130), top: b.bottom + h > window.innerHeight ? b.top - h - 4 : b.bottom + 4 };
    },
    onRow(r) {
      if (this.rowClick) return this.rowClick(r);
      if (this.expandable) this.expanded = { ...this.expanded, [r[this.rowKey]]: !this.expanded[r[this.rowKey]] };
    },
  },
  async mounted() {
    this.order = this.cols.map(c => c.key);
    this.visibleKeys = this.defaults();
    document.addEventListener('click', this.closePanel);
    window.addEventListener('scroll', this.closePanel, true);
    try { const p = await api.get('/api/prefs/' + this.view); if (p && (p.columns || p.order || p.page_size !== undefined)) this.apply(p); } catch (e) { /* 用默认 */ }
    this.loaded = true;
  },
  watch: {
    rows() { if (this.page > this.pages) this.page = 1; },
    dateFrom() { this.page = 1; },
    dateTo() { this.page = 1; },
    sort() { this.page = 1; },
  },
  beforeUnmount() { document.removeEventListener('click', this.closePanel); window.removeEventListener('scroll', this.closePanel, true); },
});
