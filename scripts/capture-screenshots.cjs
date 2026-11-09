/* Documentation screenshots against a disposable, synthetic database. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'repowatch-docs-'));
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const python = process.env.REPOWATCH_SCREENSHOT_PYTHON || path.join(root, '.venv/bin/python');
  const server = spawn(python, [path.join(root, 'scripts/screenshot-fixture.py'), '--db', path.join(tmp, 'demo.db'), '--port', String(port)], {cwd:root, stdio:'inherit'});
  let serverError;
  server.on('error', error => { serverError = error; });
  let browser;
  try {
    let ready = false;
    for (let i=0; i<100; i++) {
      if (serverError) throw serverError;
      if (server.exitCode !== null) throw new Error('Synthetic preview server exited');
      try { ready = (await fetch(`http://127.0.0.1:${port}/api/status`)).ok; } catch (_) {}
      if (ready) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (!ready) throw new Error('Synthetic preview server did not start');
    const options = {headless:true};
    if (process.env.REPOWATCH_CHROMIUM_PATH) options.executablePath = process.env.REPOWATCH_CHROMIUM_PATH;
    browser = await chromium.launch(options);
    const page = await browser.newPage({viewport:{width:1440,height:1100}, colorScheme:'light', locale:'en-US'});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${port}/`, {waitUntil:'networkidle'});
    const destination = path.join(root, 'docs/screenshots');
    fs.mkdirSync(destination, {recursive:true});
    async function capture(name) {
      for (let i=0; i<3; i++) {
        await page.waitForSelector('#repo-rows .repo-name');
        await page.waitForTimeout(300);
        assert.equal(await page.locator('#owner').innerText(), 'demo-account');
        assert.equal(await page.locator('#c-overview').count(), 1);
        const version = await page.evaluate(() => renderVersion);
        await page.screenshot({path:path.join(destination,name), fullPage:true});
        if (version === await page.evaluate(() => renderVersion)) return;
      }
      throw new Error('Could not capture a stable screenshot');
    }
    await page.locator('#theme').selectOption('light');
    await capture('overview-light.png');
    await page.locator('#theme').selectOption('dark');
    await capture('overview-dark.png');
    await page.locator('#theme').selectOption('light');
    await page.setViewportSize({width:390,height:844});
    await capture('overview-mobile.png');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(errors, []);
    console.log('PASS: screenshots generated from synthetic data; desktop, dark theme and mobile; no JavaScript errors.');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {
      const exited = new Promise(resolve => server.once('exit', resolve));
      server.kill('SIGTERM');
      await exited;
    }
    fs.rmSync(tmp, {recursive:true,force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
