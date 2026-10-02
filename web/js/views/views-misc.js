/* owe-erp 前端（由 app.js 机械拆分，经典脚本按序加载，顺序见 index.html；依赖：base 先于 forms 先于 views 先于 app） */

/* ---------------- 预留模块 ---------------- */
const ReservedView = defineComponent({
  template: `
  <div>
    <div class="kpis" style="grid-template-columns:repeat(3,1fr)">
      <div class="kpi c-info"><div class="kpi-l">银行交易导入</div><div class="kpi-v">二期</div><div class="kpi-d">HISQRY CSV → 自动/手工匹配</div></div>
      <div class="kpi c-warn"><div class="kpi-l">费用管理</div><div class="kpi-v">三期</div><div class="kpi-d">表结构已预留</div></div>
      <div class="kpi c-ok"><div class="kpi-l">固定资产</div><div class="kpi-v">三期</div><div class="kpi-d">表结构已预留</div></div>
    </div>
    <div class="card"><div class="card-body" style="color:var(--ink-2);font-size:13px">
      数据库已建好 <span class="mono">bank_txn / expense / asset</span> 三张预留表，接口未开放。<br>
      银行导入将支持：中行 HISQRY CSV 上传 → 按附言合同号 &gt; 单位+金额精确 &gt; 单位+30%/70% 分期 三级自动匹配，未匹配的进人工匹配队列。
    </div></div>
  </div>`,
});
