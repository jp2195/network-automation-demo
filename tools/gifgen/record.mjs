// Record a Grafana dashboard as a sequence of PNG frames while an external
// process drives a fault. Used by bin/make-gif.sh to (re)generate the docs
// GIFs (gifski assembles the frames). Headless Chromium via Playwright.
//
//   node record.mjs <dashboard-uid> <frames-dir>
//
// Env: GRAFANA_URL, GRAFANA_USER, GRAFANA_PASS, DURATION (s), INTERVAL (ms),
//      RANGE (Grafana time-range query), REFRESH, WIDTH, HEIGHT.
import { chromium } from 'playwright';
import { mkdir } from 'node:fs/promises';

const GRAFANA  = process.env.GRAFANA_URL  || 'http://grafana.127-0-0-1.nip.io:8080';
const USER     = process.env.GRAFANA_USER || 'admin';
const PASS     = process.env.GRAFANA_PASS || 'admin';
const UID      = process.argv[2] || 'geomap';
const OUT      = process.argv[3] || 'frames';
const DURATION = parseInt(process.env.DURATION || '78', 10) * 1000;
const INTERVAL = parseInt(process.env.INTERVAL || '1500', 10);
const RANGE    = process.env.RANGE   || 'from=now-10m&to=now';
const REFRESH  = process.env.REFRESH || '5s';
const WIDTH    = parseInt(process.env.WIDTH  || '1280', 10);
const HEIGHT   = parseInt(process.env.HEIGHT || '720', 10);

await mkdir(OUT, { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: WIDTH, height: HEIGHT }, deviceScaleFactor: 2 });

// Log in (Grafana's default form: name=user / name=password).
await page.goto(`${GRAFANA}/login`, { waitUntil: 'networkidle' });
try {
  await page.locator('input[name=user]').fill(USER);
  await page.locator('input[name=password]').fill(PASS);
  await page.locator('input[name=password]').press('Enter');
  await page.waitForTimeout(2500);
  // On the default admin creds Grafana forces a "change password" screen —
  // Skip it so we land on the dashboard.
  const skip = page.getByRole('button', { name: /^skip$/i });
  if (await skip.count()) { await skip.first().click(); await page.waitForTimeout(1500); }
} catch (e) { console.error('login note:', e.message); }

// Dashboard in kiosk mode so only the panels show (no chrome), auto-refreshing.
await page.goto(`${GRAFANA}/d/${UID}?${RANGE}&refresh=${REFRESH}&kiosk`, { waitUntil: 'networkidle' });
await page.waitForTimeout(4000); // let panels paint

const start = Date.now();
let i = 0;
while (Date.now() - start < DURATION) {
  await page.screenshot({ path: `${OUT}/frame-${String(i).padStart(4, '0')}.png` });
  i++;
  await page.waitForTimeout(INTERVAL);
}
await browser.close();
console.log(`captured ${i} frames -> ${OUT}`);
