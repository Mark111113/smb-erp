/* owe-erp 前端（由 app.js 机械拆分，经典脚本按序加载，顺序见 index.html；依赖：base 先于 forms 先于 views 先于 app） */

/* ERP 前端 — Vue3 无构建直载 */
const { createApp, defineComponent } = Vue;

/* 接口错误：String(e) 仍是后端 detail 原文，调用方 '保存失败：' + e 的写法不受影响 */
class ApiError extends Error {
  constructor(detail, status) {
    super(Array.isArray(detail) ? detail.map(d => d.msg || JSON.stringify(d)).join('；') : String(detail));
    this.status = status;
  }
  toString() { return this.message; }
}
/* Vue 之外（定时器、裸 Promise）未捕获的权限拒绝兜底提示；Vue 事件里的见 app.js errorHandler */
window.addEventListener('unhandledrejection', (ev) => {
  if (ev.reason instanceof ApiError && ev.reason.status === 403) { ev.preventDefault(); alert(ev.reason.message); }
});

const api = {
  async request(url, options={}) {
    const r = await fetch(url, { credentials: 'same-origin', ...options });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || r.status;
      if (r.status === 401 && !url.startsWith('/api/auth/')) {
        window.dispatchEvent(new CustomEvent('owe:unauthorized'));
      }
      throw new ApiError(detail, r.status);
    }
    return r.json();
  },
  async get(url) { return this.request(url); },
  async post(url, body) { return this.request(url, { headers: { 'Content-Type': 'application/json' }, method: 'POST', body: JSON.stringify(body) }); },
  async put(url, body) { return this.request(url, { headers: { 'Content-Type': 'application/json' }, method: 'PUT', body: JSON.stringify(body) }); },
  async del(url) { return this.request(url, { method: 'DELETE' }); },
};
/* 收发货返回的库存提示：出库日库存不足（成本先按 0 暂估）、最终负库存（红字） */
const stockWarnings = (r, nameOf = () => '') => {
  const out = [];
  for (const s of r.short_on_date || [])
    out.push(`${s.doc_no} ${nameOf(s.material_id)}：${s.move_date} 当天库存不足（日终 ${s.qty_on_date}），出库成本先按 0 暂估，到货后自动调整。若日期填早了，请作废后按实际日期重做。`);
  for (const n of r.negative || []) out.push(`${nameOf(n.material_id)} 现存量 ${n.qty}，为负（红字）。`);
  return out.length ? '\n注意：\n' + out.join('\n') : '';
};
const fmt = (n) => (n == null ? '' : Number(n).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const today = () => { const d=new Date(); return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); };
