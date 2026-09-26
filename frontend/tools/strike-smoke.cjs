// One-shot browser check of the STRIKE picker on an MCX symbol:
// login with a minted session cookie, open /trading, load CRUDEOIL (MCX),
// click STRIKE, pick a CE, and report whether a contract loads or the
// "No contract found" toast appears.
const fs = require('fs');
const path = require('path');
const { chromium } = require(path.join(
  'C:', 'Terminal', 'openalgo', 'frontend', 'node_modules', 'playwright-core'));

const BASE = 'http://137.23.32.190:5000';
const COOKIE = process.env.OA_SESSION || '';

(async () => {
  if (!COOKIE) { console.error('set OA_SESSION'); process.exit(2); }

  const root = process.env.LOCALAPPDATA + '/ms-playwright';
  const cands = [
    root + '/chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe',
    root + '/chromium-1234/chrome-win64/chrome.exe',
  ];
  const exe = cands.find((p) => fs.existsSync(p));

  const browser = await chromium.launch({ executablePath: exe });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await ctx.addCookies([{
    name: 'oa_session_fyers', value: COOKIE, url: BASE, httpOnly: true, sameSite: 'Lax',
  }]);
  // The SPA gate reads zustand-persisted auth from localStorage; seed it the
  // same way a real logged-in browser would have it.
  await ctx.addInitScript(() => {
    localStorage.setItem('openalgo-auth', JSON.stringify({
      state: { user: { username: 'balajigb', broker: 'fyers', isLoggedIn: true, loginTime: new Date().toISOString() },
               apiKey: null, isAuthenticated: true },
      version: 0 }));
  });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error') console.log('console:', m.text().slice(0, 120)); });

  await page.goto(BASE + '/trading', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(6000);

  // Load CRUDEOIL on MCX through the symbol search (pane pill).
  try {
    await page.locator('button[title="Search symbol"]').first().click({ timeout: 8000 });
  } catch (e) { console.log('search open failed:', String(e).slice(0, 100)); }
  await page.waitForTimeout(800);
  await page.keyboard.type('CRUDEOIL', { delay: 60 });
  await page.waitForTimeout(3000);
  // Pick the MCX CRUDEOIL row (futures contract) if present.
  const row = page.getByText(/^CRUDEOIL$/i).first();
  try { await row.click({ timeout: 6000 }); } catch (e) { console.log('row pick failed:', String(e).slice(0, 100)); }
  await page.waitForTimeout(5000);

  // The STRIKE button should now be visible for the MCX root.
  const strike = page.locator('button[title="Pick a strike, then CE or PE"]').first();
  const visible = await strike.isVisible().catch(() => false);
  console.log('STRIKE_BUTTON_VISIBLE', visible);
  if (!visible) {
    await page.screenshot({ path: 'tools/_strike_no_button.png' });
    await browser.close();
    process.exit(1);
  }

  await strike.click();
  await page.waitForTimeout(1200);
  const menu = await page.evaluate(() => document.body.innerText.includes('CE'));
  console.log('STRIKE_MENU_OPEN', menu);
  await page.screenshot({ path: 'tools/_strike_menu.png' });

  // Pick the first CE row.
  const ce = page.getByRole('menuitem', { name: /CE$/ }).first();
  await ce.click({ timeout: 6000 }).catch((e) => console.log('ce click failed:', String(e).slice(0, 100)));
  await page.waitForTimeout(6000);

  const toast = await page.evaluate(() => {
    const t = document.body.innerText;
    return {
      noContract: /No contract found/i.test(t) || /No \d+(\.\d+)?CE contract/i.test(t),
      loaded: /CRUDEOIL\d{2}[A-Z]{3}\d{2}\d+CE/i.test(t),
    };
  });
  console.log('RESULT', JSON.stringify(toast));
  await page.screenshot({ path: 'tools/_strike_after.png' });
  await browser.close();
  process.exit(toast.noContract && !toast.loaded ? 1 : 0);
})();
