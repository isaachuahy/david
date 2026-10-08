/** Exercise the actual editor and API with disposable state and signed test credentials. */
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');

const root = path.resolve(__dirname, '..');
const token = '12345:synthetic-browser-test-token';
const pythonServer = `
import asyncio, json, signal
from aiohttp import web
from bot.context_editor_server import create_context_editor_app
from persistence.database import init_db

async def serve():
    init_db()
    runner = web.AppRunner(create_context_editor_app(
        bot_token="${token}", allowed_user_id=123), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    print(json.dumps({"port": runner.addresses[0][1]}), flush=True)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    try:
        await stopped.wait()
    finally:
        await runner.cleanup()

asyncio.run(serve())
`;

function signedLaunch(userId = 123, age = 0) {
  const fields = {auth_date: String(Math.floor(Date.now() / 1000) - age), user: JSON.stringify({id: userId})};
  const secret = crypto.createHmac('sha256', 'WebAppData').update(token).digest();
  const message = Object.keys(fields).sort().map(key => `${key}=${fields[key]}`).join('\n');
  fields.hash = crypto.createHmac('sha256', secret).update(message).digest('hex');
  return new URLSearchParams(fields).toString();
}

async function run() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'david-browser-test-'));
  const server = spawn(process.env.PYTHON_BIN || path.join(root, '.venv/bin/python'), ['-B', '-c', pythonServer], {
    cwd: root, stdio: ['ignore', 'pipe', 'pipe'], env: {...process.env,
      PYTHONDONTWRITEBYTECODE: '1', DAVID_CONTEXT_DIR: path.join(directory, 'context'),
      DAVID_DB_PATH: path.join(directory, 'assistant.db'), SENTRY_DSN: '',
    },
  });
  let serverErrors = '';
  server.stderr.on('data', chunk => { serverErrors += chunk.toString(); });
  let browser;
  try {
    const port = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Test API did not start.')), 15000);
      server.stdout.once('data', chunk => { clearTimeout(timeout); resolve(JSON.parse(chunk.toString()).port); });
      server.once('error', error => { clearTimeout(timeout); reject(error); });
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Test API exited: ${code}\n${serverErrors}`)); });
    });
    const origin = `http://127.0.0.1:${port}`;
    const headers = {Authorization: 'tma ' + signedLaunch(), 'Content-Type': 'application/json'};
    async function api(route, body) {
      const response = await fetch(origin + route, {
        headers, method: body ? 'POST' : 'GET', ...(body ? {body: JSON.stringify(body)} : {}),
      });
      assert.equal(response.status, 200, await response.clone().text());
      return response.json();
    }
    const docs = {
      goals: '# Goals\r\n\r\n## Long-Term\r\n- Practice.\r\n## Medium-Term\r\n- Finish.\r\n## Operating Principles\r\n- Focus.\r\n',
      weekly_state: '# Weekly State\n\n## This Week\n### Top Priorities\n- Practice.\n### Carryover\nNone\n### Constraints\nTime\n### Execution Focus\nFocus\n',
      decision_log: '# Decision Log\n\n## Current Rolling Context\n- Practice.\n\n## Recent Decisions (Appended Daily)\n- Keep a plan.\n',
    };
    for (const [id, content] of Object.entries(docs)) {
      await api(`/api/context/${id}`, {content, expected_revision: 'missing', operation_id: crypto.randomUUID()});
    }
    const assets = new Map();
    const html = fs.readFileSync(path.join(root, 'bot/context_editor.html'), 'utf8');
    for (const match of html.matchAll(/src="(https:\/\/cdn\.jsdelivr\.net\/[^\"]+)"[^>]*integrity="([^"]+)"/g)) {
      const response = await fetch(match[1]);
      assert(response.ok, `Preview dependency unavailable: ${match[1]}`);
      const bytes = Buffer.from(await response.arrayBuffer());
      assert.equal('sha384-' + crypto.createHash('sha384').update(bytes).digest('base64'), match[2]);
      assets.set(match[1], bytes);
    }
    assert.equal(assets.size, 2);
    browser = await chromium.launch({headless: true,
      ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH} : {}),
    });
    const errors = [];
    async function openEditor(viewport, launch = signedLaunch(), previewAvailable = true) {
      const page = await browser.newPage({viewport});
      page.on('pageerror', error => errors.push(error.message));
      await page.route('https://telegram.org/js/**', route => route.fulfill({
        contentType: 'application/javascript', body: `window.Telegram={WebApp:{initData:${JSON.stringify(launch)},colorScheme:'light',ready(){},expand(){},onEvent(){},isVersionAtLeast(){return true},enableClosingConfirmation(){},disableClosingConfirmation(){}}};`,
      }));
      await page.route('https://cdn.jsdelivr.net/**', route => previewAvailable ? route.fulfill({
        contentType: 'application/javascript', headers: {'Access-Control-Allow-Origin': '*'},
        body: assets.get(route.request().url()),
      }) : route.abort());
      await page.goto(origin + '/context');
      return page;
    }
    const page = await openEditor({width: 1280, height: 900});
    await page.locator('#editor-view').waitFor({state: 'visible'});
    assert.equal(await page.locator('#editor').inputValue(), docs.goals.replaceAll('\r\n', '\n'));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const draft = docs.goals.replaceAll('\r\n', '\n') + '\n- Browser edit.\n<script>window.__unsafe=true</script>\n[unsafe](javascript:alert(1))\n';
    await page.locator('#editor').fill(draft);
    await page.locator('#preview').getByText('Browser edit.', {exact: true}).waitFor();
    assert.equal(await page.locator('#preview script, #preview a[href^="javascript:"]').count(), 0);
    assert.equal(await page.evaluate(() => window.__unsafe), undefined);
    await page.locator('[data-document="weekly_state"]').click();
    await page.locator('[data-document="goals"]').click();
    assert.equal(await page.locator('#editor').inputValue(), draft);
    assert.equal((await api('/api/context/goals')).document.content, docs.goals);
    await page.locator('#save').click();
    await page.getByText('Saved. David will use this context.', {exact: true}).waitFor();
    const saved = (await api('/api/context/goals')).document;
    assert.equal(saved.content, draft.replaceAll('\n', '\r\n'));
    await page.reload();
    await page.locator('#editor-view').waitFor({state: 'visible'});
    assert.equal(await page.locator('#editor').inputValue(), draft);
    await page.locator('#editor').fill(draft + '\n- Discard this draft.\n');
    await page.locator('#cancel').click();
    await page.locator('#confirm-action').click();
    await page.getByText('Saved context loaded.', {exact: true}).waitFor();
    assert.equal((await api('/api/context/goals')).document.revision, saved.revision);
    await page.locator('#editor').fill(draft + '\n- Keep this draft.\n');
    const newer = await api('/api/context/goals', {
      content: saved.content + '\r\n- Concurrent edit.\r\n', expected_revision: saved.revision, operation_id: crypto.randomUUID(),
    });
    await page.locator('#save').click();
    await page.locator('#conflict').waitFor({state: 'visible'});
    assert.equal((await api('/api/context/goals')).document.revision, newer.document.revision);
    await page.locator('#keep-draft').click();
    const retries = [];
    let dropConfirmation = true;
    await page.route('**/api/context/goals', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      retries.push(route.request().postDataJSON());
      const response = await route.fetch();
      if (dropConfirmation) { dropConfirmation = false; return route.abort(); }
      return route.fulfill({response});
    });
    await page.locator('#save').click();
    await page.getByRole('button', {name: 'Retry save', exact: true}).waitFor();
    const completed = (await api('/api/context/goals')).document;
    await page.locator('#save').click();
    await page.getByText('Saved. David will use this context.', {exact: true}).waitFor();
    assert.equal(retries.length, 2);
    assert.deepEqual(retries[0], retries[1]);
    assert.equal((await api('/api/context/goals')).document.revision, completed.revision);
    await page.unroute('**/api/context/goals');
    await page.locator('#history-button').click();
    const {versions} = await api('/api/context/goals/versions');
    const original = await Promise.all(versions.map(version => api(`/api/context/goals/versions/${version.version_id}`)));
    const originalIndex = original.findIndex(entry => entry.document.content === docs.goals);
    assert(originalIndex >= 0);
    await page.locator('#version-select').selectOption(versions[originalIndex].version_id);
    await page.locator('#version-preview').waitFor({state: 'visible'});
    await page.locator('#save').click();
    await page.locator('#confirm-action').click();
    await page.getByText('Previous version restored. The replaced text is in History.', {exact: true}).waitFor();
    assert.equal((await api('/api/context/goals')).document.content, docs.goals);
    const mobile = await openEditor({width: 375, height: 812});
    await mobile.locator('#editor-view').waitFor({state: 'visible'});
    assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await mobile.locator('#preview-mode').click();
    await mobile.locator('#preview-pane').waitFor({state: 'visible'});
    const expired = await openEditor({width: 375, height: 812}, signedLaunch(123, 3700));
    await expired.locator('#notice').waitFor({state: 'visible'});
    assert.equal(await expired.locator('#editor-view').isVisible(), false);
    const fallback = await openEditor({width: 1280, height: 900}, signedLaunch(), false);
    await fallback.locator('#editor-view').waitFor({state: 'visible'});
    assert.equal(await fallback.locator('#preview').textContent(), docs.goals);
    const outside = await openEditor({width: 375, height: 812}, '');
    await outside.locator('#notice').waitFor({state: 'visible'});
    assert.equal(await outside.locator('#editor-view').isVisible(), false);
    assert.deepEqual(errors, []);
    console.log('PASS: real API browser checks (save/reload, CRLF, preview safety, drafts, cancel, conflicts, idempotent retry, history restore, responsive layouts, expiry, dependency fallback, Telegram-only entry).');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) { const exited = once(server, 'exit'); server.kill('SIGTERM'); await exited; }
    fs.rmSync(directory, {recursive: true, force: true});
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
