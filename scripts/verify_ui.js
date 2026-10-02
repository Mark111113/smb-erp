const { chromium } = require('playwright');

(async () => {
  const base = process.env.OWE_BASE || 'http://127.0.0.1:8123';
  const executablePath = process.env.OWE_BROWSER || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
  const browser = await chromium.launch({ headless: true, executablePath });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  const loginPage = await context.newPage();
  await loginPage.goto(base + '/', { waitUntil: 'networkidle' });
  const loginText = await loginPage.locator('body').innerText();
  if (!loginText.includes('ERP') || !(loginText.includes('登录') || loginText.includes('创建管理员账号'))) {
    problems.push('auth screen: missing login gate');
  }
  await loginPage.screenshot({ path: 'data/ui-login.png' });
  await loginPage.close();
  const authUser = process.env.OWE_UI_USER || 'tester';
  const authPassword = process.env.OWE_UI_PASSWORD || 'unit-test-2026';
  const statusResponse = await context.request.get(base + '/api/auth/status');
  const authStatus = await statusResponse.json();
  const authResponse = await context.request.post(base + (authStatus.needs_setup ? '/api/auth/setup' : '/api/auth/login'), {
    data: authStatus.needs_setup
      ? { username: authUser, display_name: 'UI 检查', password: authPassword }
      : { username: authUser, password: authPassword },
  });
  if (!authResponse.ok()) {
    throw new Error(`auth failed: ${authResponse.status()} ${await authResponse.text()}`);
  }
  const checks = [
    ['#/controls', ['票款核销', '组套拆套', '对账与期末']],
    ['#/contracts', ['合同订单', '新建合同']],
    ['#/contract/1', ['按行履约', '编辑']],
    ['#/movements', ['出入库', '退货']],
    ['#/invoices', ['红字']],
    ['#/payments', ['退款']],
    ['#/vouchers', ['会计凭证', '成本调整']],
    ['#/twm', ['三单匹配']],
  ];
  for (const [route, markers] of checks) {
    const page = await context.newPage();
    page.on('pageerror', error => problems.push(`pageerror: ${error.message}`));
    page.on('console', message => {
      if (message.type() === 'error') problems.push(`console: ${message.text()}`);
    });
    page.on('response', response => {
      if (process.env.OWE_DEBUG_UI && response.url().includes('/api/')) {
        console.log(`response: ${response.status()} ${response.url()}`);
      }
    });
    await page.goto(base + '/' + route, { waitUntil: 'networkidle' });
    const html = await page.locator('body').innerText();
    for (const marker of markers) {
      if (!html.includes(marker)) {
        problems.push(`${route}: missing ${marker}`);
        if (process.env.OWE_DEBUG_UI) console.log(`--- ${route} ---\n${html.slice(0, 2000)}`);
      }
    }
    if (route === '#/controls') {
      await page.screenshot({ path: 'data/ui-controls-desktop.png', fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      const overflow = await page.evaluate(() => ({
        document: document.documentElement.scrollWidth,
        viewport: window.innerWidth,
      }));
      if (overflow.document > overflow.viewport + 1) {
        problems.push(`mobile overflow: ${overflow.document}px > ${overflow.viewport}px`);
      }
      await page.screenshot({ path: 'data/ui-controls-mobile.png', fullPage: true });
    }
    if (route === '#/contract/1') {
      await page.screenshot({ path: 'data/ui-contract.png', fullPage: true });
    }
    await page.close();
  }
  await browser.close();
  if (problems.length) {
    console.error(problems.join('\n'));
    process.exit(1);
  }
  console.log('UI DOM checks passed');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
