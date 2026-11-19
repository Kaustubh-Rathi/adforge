// Record the interactive parts of the AdForge demo with Playwright.
//
//   node capture.js create    -> out/capture/create.webm
//   node capture.js run       -> out/capture/run.webm   (starts the graph via the API)
//   node capture.js approve   -> out/capture/approve.webm
//   node capture.js results   -> out/capture/results.webm
//
// Each scene is a separate recording so the edit can cut cleanly between them.
// The run scene is real: the agent graph is triggered over HTTP and the browser
// polls the Agent Traces page while it executes.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const EXE =
  process.env.AF_CHROME || chromium.executablePath();
const BASE = process.env.AF_DASHBOARD || 'http://127.0.0.1:8501';
const API = process.env.AF_API || 'http://127.0.0.1:8000';
const TENANT = 'demo_tenant';
const REVIEWER = 'reviewer@adforge.local';

const OUT = path.join(__dirname, 'out');
const VIDEO_DIR = path.join(OUT, 'capture_raw');

// A visible cursor. Playwright's video does not capture the OS pointer, so the
// page draws one; mousemove/mousedown events still come from real CDP input.
const CURSOR_SCRIPT = () => {
  const CSS = `
    #af-cursor {
      position: fixed; z-index: 2147483647; left: -100px; top: -100px;
      width: 20px; height: 20px; margin: -10px 0 0 -10px; border-radius: 50%;
      background: rgba(23, 107, 79, 0.92);
      box-shadow: 0 0 0 4px rgba(23, 107, 79, 0.22), 0 3px 10px rgba(0, 0, 0, 0.35);
      pointer-events: none; opacity: 0;
      transition: transform 90ms ease-out, opacity 150ms linear;
    }
    #af-cursor.on { opacity: 1; }
    #af-cursor.click { transform: scale(0.55); }`;
  const mount = () => {
    if (document.getElementById('af-cursor')) return;
    const style = document.createElement('style');
    style.textContent = CSS;
    document.documentElement.appendChild(style);
    const dot = document.createElement('div');
    dot.id = 'af-cursor';
    document.documentElement.appendChild(dot);
    window.addEventListener(
      'mousemove',
      (e) => {
        dot.classList.add('on');
        dot.style.left = `${e.clientX}px`;
        dot.style.top = `${e.clientY}px`;
      },
      true,
    );
    window.addEventListener('mousedown', () => dot.classList.add('click'), true);
    window.addEventListener('mouseup', () => dot.classList.remove('click'), true);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function apiGet(pathname) {
  const res = await fetch(`${API}${pathname}`, { headers: { 'X-AdForge-Tenant': TENANT } });
  if (!res.ok) throw new Error(`${pathname} -> ${res.status}`);
  return res.json();
}

async function newestCampaign() {
  const list = await apiGet('/campaigns');
  return list[0]; // API returns newest first
}

// Move the pointer like a person, then click.
async function humanClick(page, locator, { settle = 260 } = {}) {
  const box = await locator.boundingBox();
  if (!box) throw new Error('locator has no box');
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  const from = page.__lastMouse || { x: 40, y: 840 };
  await page.mouse.move(from.x, from.y);
  await page.mouse.move(x, y, { steps: 22 });
  page.__lastMouse = { x, y };
  await sleep(settle);
  await page.mouse.down();
  await sleep(70);
  await page.mouse.up();
}

async function typeInto(page, locator, text, delay = 14) {
  await humanClick(page, locator, { settle: 120 });
  await locator.fill('');
  await locator.pressSequentially(text, { delay });
}

async function openScene(name) {
  fs.mkdirSync(VIDEO_DIR, { recursive: true });
  const browser = await chromium.launch({ executablePath: EXE, headless: true });
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    recordVideo: { dir: VIDEO_DIR, size: { width: 1600, height: 900 } },
    reducedMotion: 'reduce',
  });
  const page = await context.newPage();
  await page.addInitScript(CURSOR_SCRIPT);
  page.setDefaultTimeout(120000);
  return { browser, context, page, name };
}

async function finishScene({ browser, context, page, name }) {
  const raw = await page.video().path();
  await context.close();
  await browser.close();
  const dest = path.join(OUT, 'capture', `${name}.webm`);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.copyFileSync(raw, dest);
  console.log('captured', dest);
}

async function goto(page, route) {
  await page.goto(`${BASE}${route}`, { waitUntil: 'networkidle', timeout: 120000 });
  await page.waitForTimeout(2600);
}

// --------------------------------------------------------------------------
// Scene: create a campaign through the form
// --------------------------------------------------------------------------
async function sceneCreate() {
  const scene = await openScene('create');
  const { page } = scene;

  await goto(page, '/Campaigns');

  const expander = page.locator('[data-testid="stExpander"] summary').first();
  await humanClick(page, expander);
  await page.waitForTimeout(1400);

  await typeInto(page, page.getByLabel('Campaign name'), 'Atlas Analytics Q1 launch');
  await typeInto(page, page.getByLabel('Product name'), 'Atlas Analytics');
  await typeInto(
    page,
    page.getByLabel('Product description'),
    'Privacy-first product analytics for B2B SaaS teams. Sub-second dashboards and a native dbt integration.',
    8,
  );
  await typeInto(
    page,
    page.getByLabel('Target audience'),
    'Product managers and heads of growth at 20-200 person B2B SaaS companies.',
    8,
  );

  // Budget, duration, objective, channels.
  const budget = page.getByLabel('Budget (USD)');
  await humanClick(page, budget, { settle: 120 });
  await budget.fill('15000');
  await page.waitForTimeout(600);

  // Streamlit now renders selectboxes as React Aria comboboxes: typing filters
  // the list and Enter commits, which is both faster and more legible on video
  // than scrolling a menu.
  const objective = page.getByRole('combobox', { name: 'Objective' });
  await humanClick(page, objective, { settle: 120 });
  await objective.pressSequentially('con', { delay: 60 });
  await page.waitForTimeout(700);
  await objective.press('Enter');
  await page.waitForTimeout(800);

  const channels = page.getByRole('combobox', { name: 'Channels' });
  await humanClick(page, channels, { settle: 140 });
  await page.waitForTimeout(700);
  await page.getByRole('option', { name: 'linkedin', exact: true }).click();
  await page.waitForTimeout(700);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(600);

  const submit = page.getByRole('button', { name: 'Create campaign' });
  await humanClick(page, submit);

  // Streamlit greys the form out and reruns while the campaign is created.
  // Wait for the empty-state block to disappear so the recording ends on the
  // new row, not on a disabled form.
  await page
    .getByText('Nothing here yet')
    .waitFor({ state: 'hidden', timeout: 120000 });
  await page.waitForTimeout(1400);

  // Let the new row be read before cutting.
  await page.mouse.move(980, 640, { steps: 18 });
  await page.waitForTimeout(2800);

  await finishScene(scene);
}

// --------------------------------------------------------------------------
// Scene: trigger the real graph and capture each completed state
// --------------------------------------------------------------------------
async function waitForTraces(page) {
  // The page renders the metrics block only after Streamlit's websocket has
  // painted the whole script; the table (when present) follows immediately.
  await page
    .getByText('Total runs', { exact: true })
    .waitFor({ state: 'visible', timeout: 180000 });
  await page.waitForTimeout(1800);
}

async function reloadTraces(page) {
  await page.reload({ waitUntil: 'networkidle', timeout: 120000 });
  await waitForTraces(page);
}

async function sceneRun() {
  const campaign = await newestCampaign();
  const id = campaign.id;
  console.log('running campaign', id, campaign.name, campaign.status);

  const scene = await openScene('run');
  const { page } = scene;

  // Show the empty ledger first, fully rendered, before the run starts.
  await goto(page, '/Agent_Traces');
  await waitForTraces(page);
  await page.waitForTimeout(3000);

  // Fire the run. This request blocks for the length of the graph, so it is
  // deliberately not awaited until the scene ends.
  const runPromise = fetch(`${API}/campaigns/${id}/run`, {
    method: 'POST',
    headers: { 'X-AdForge-Tenant': TENANT, 'X-AdForge-Campaign': id },
  }).then(async (res) => {
    console.log('run finished', res.status);
    return res.text();
  });

  const started = Date.now();
  let seen = 0;
  let status = campaign.status;

  for (;;) {
    await sleep(2500);
    let count = seen;
    try {
      const runs = await apiGet(`/traces/${id}/runs`);
      count = runs.runs.length;
    } catch {
      /* transient */
    }
    try {
      status = (await apiGet(`/campaigns/${id}`)).status;
    } catch {
      /* transient */
    }

    if (count !== seen) {
      // A new agent finished. Wait for the app to paint the new row, then hold
      // long enough to be usable as a clean segment.
      await reloadTraces(page);
      await page.waitForTimeout(5000);
      seen = count;
      console.log(`HOLD ${Date.now() - started}ms count=${count} status=${status}`);
    }

    const elapsed = Math.round((Date.now() - started) / 1000);
    if (elapsed % 15 < 3) console.log(`poll: status=${status} runs=${count} elapsed=${elapsed}s`);

    if (status === 'reviewing' && seen >= 4) break;
    if (elapsed > 600) {
      console.warn('run did not reach the gate within 10 minutes; cutting anyway');
      break;
    }
  }

  // The final state: all four upstream runs, then the audit trail below.
  await reloadTraces(page);
  await page.waitForTimeout(2500);
  await page.mouse.move(900, 620);
  await page.mouse.wheel(0, 520);
  await page.waitForTimeout(5200);
  await page.mouse.wheel(0, 420);
  await page.waitForTimeout(4800);
  console.log(`HOLD ${Date.now() - started}ms count=${seen} status=${status} final`);

  await Promise.race([runPromise, sleep(1500)]);
  await finishScene(scene);
}

// --------------------------------------------------------------------------
// Scene: approve at the human gate
// --------------------------------------------------------------------------
async function sceneApprove() {
  const pending = await apiGet('/reviews/pending');
  if (!pending.count) throw new Error('review queue is empty; nothing to approve');
  const campaignId = pending.pending[0].id;
  console.log('reviewing', campaignId, pending.pending[0].name);

  const scene = await openScene('approve');
  const { page } = scene;
  await goto(page, '/Review_Queue');
  await page.getByText('Campaigns awaiting review', { exact: true }).waitFor({ timeout: 120000 });
  await page.waitForTimeout(3000);

  // Tour the packet: creative, then media plan, then the compliance verdict.
  await page.mouse.move(1000, 470);
  await page.waitForTimeout(1200);
  await page.mouse.wheel(0, 430);
  await page.waitForTimeout(4300);
  await page.mouse.wheel(0, 440);
  await page.waitForTimeout(4300);
  await page.mouse.wheel(0, 470);
  await page.waitForTimeout(3600);

  const email = page.getByPlaceholder('you@company.com');
  await email.scrollIntoViewIfNeeded();
  await typeInto(page, email, REVIEWER);
  await email.press('Enter');
  await page.waitForTimeout(2500);

  const approve = page.getByRole('button', { name: 'Approve', exact: true });
  await approve.scrollIntoViewIfNeeded();
  await page.waitForTimeout(900);

  // Streamlit can repaint between a boundingBox() read and raw mouse events,
  // which silently moves the click off the button. Playwright's click re-checks
  // actionability at dispatch time, so the decision itself uses that. The
  // dashboard POSTs to the API server-side, so the click is confirmed by the
  // spinner the button renders — never by retrying (a second approve would
  // race the first and surface a 409).
  await approve.hover();
  await page.waitForTimeout(400);
  const clickedAt = Date.now();
  await approve.click();
  console.log('CLICKED approve');

  const spinner = page.getByText('Approving + resuming graph...');
  const started = await spinner
    .waitFor({ timeout: 20000 })
    .then(() => true)
    .catch(() => false);
  console.log('spinner shown:', started);

  // The call blocks while three agents run. Hold on the real spinner, then
  // wait for Streamlit to rerun into the cleared-queue state.
  await page.waitForTimeout(5000);
  const settled = (text, value) =>
    page
      .getByText(text)
      .waitFor({ timeout: 300000 })
      .then(() => value)
      .catch(() => null);
  const result = await Promise.race([
    settled('Queue is clear', 'cleared'),
    settled('Approve failed.', 'failed'),
  ]);
  if (!result) console.warn('neither cleared nor failed within 5 minutes');
  const took = Math.round((Date.now() - clickedAt) / 1000);
  console.log('approve outcome:', result, took + 's');

  await page.waitForTimeout(600);
  await page.mouse.wheel(0, -5000);
  await page.waitForTimeout(6000);
  console.log('final status', (await apiGet('/campaigns/' + campaignId)).status);

  await finishScene(scene);
}

async function sceneResults() {
  const scene = await openScene('results');
  const { page } = scene;

  await goto(page, '/Agent_Traces');
  await page
    .getByText('Total runs', { exact: true })
    .waitFor({ timeout: 120000 })
    .catch(() => console.warn('traces never rendered'));
  await page.mouse.move(900, 520);
  await page.waitForTimeout(4200);
  await page.mouse.wheel(0, 420);
  await page.waitForTimeout(4200);

  await goto(page, '/ROI_Calculator');
  await page
    .getByText('ROI verdict', { exact: true })
    .waitFor({ timeout: 120000 })
    .catch(() => console.warn('roi never rendered'));
  // The verdict settles first, then Streamlit renders the plotly breakdown
  // cards; hold long enough that the tail of the clip is the finished page.
  await page.waitForTimeout(10000);
  await page.mouse.wheel(0, 240);
  await page.waitForTimeout(6000);
  await page.mouse.wheel(0, 520);
  await page.waitForTimeout(14000);

  await finishScene(scene);
}

// --------------------------------------------------------------------------
// Scene: still screenshots at the video's exact 1600x900 frame
// --------------------------------------------------------------------------
async function sceneStills() {
  const browser = await chromium.launch({ executablePath: EXE, headless: true });
  const page = await browser.newPage({
    viewport: { width: 1600, height: 900 },
    deviceScaleFactor: 1,
  });
  page.setDefaultTimeout(120000);

  const pages = {
    home: '/',
    campaigns: '/Campaigns',
    traces: '/Agent_Traces',
    roi: '/ROI_Calculator',
    review: '/Review_Queue',
  };
  const dir = path.join(OUT, 'stills');
  fs.mkdirSync(dir, { recursive: true });

  for (const [name, route] of Object.entries(pages)) {
    await goto(page, route);
    await page.waitForTimeout(2200);
    await page.screenshot({ path: path.join(dir, `${name}.png`) });
    console.log('still', name);
  }
  await browser.close();
}

const SCENES = {
  create: sceneCreate,
  run: sceneRun,
  approve: sceneApprove,
  results: sceneResults,
  stills: sceneStills,
};

(async () => {
  const which = process.argv[2];
  if (!which || !SCENES[which]) {
    console.error(`usage: node capture.js <${Object.keys(SCENES).join('|')}>`);
    process.exit(2);
  }
  await SCENES[which]();
})();
