// Render the title/setup/guardrails/limits/close cards to PNG at 1600x900.
// Usage: node cards.js <outDir>
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const EXE =
  process.env.AF_CHROME || chromium.executablePath();

const CARDS = ['title', 'setup', 'setup-run', 'guardrails', 'honest', 'close'];
const HTML = path.join(__dirname, 'cards.html').replace(/\\/g, '/');

(async () => {
  const outDir = process.argv[2] || path.join(__dirname, 'out', 'cards');
  fs.mkdirSync(outDir, { recursive: true });

  const browser = await chromium.launch({ executablePath: EXE, headless: true });
  const page = await browser.newPage({
    viewport: { width: 1600, height: 900 },
    deviceScaleFactor: 1,
  });

  for (const name of CARDS) {
    await page.goto(`file:///${HTML}?card=${name}`, { waitUntil: 'load' });
    await page.waitForFunction(() => document.body.dataset.rendered === '1');
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(200);
    const file = path.join(outDir, `card-${name}.png`);
    await page.screenshot({ path: file });
    console.log('card', name);
  }

  await browser.close();
})();
