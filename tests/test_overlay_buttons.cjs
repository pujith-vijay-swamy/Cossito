const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.join(__dirname, '..');
const [markup, source] = fs.readFileSync(path.join(root, 'overlay_panel.html'), 'utf8').split('<!-- __OMNITASKER_JS__ -->');
const manager = fs.readFileSync(path.join(root, 'overlay_manager.py'), 'utf8');
const pollStrings = manager.match(/cmd_str = await page.evaluate\(([\s\S]*?)\n\s*\)/)[1];
const drain = new Function(`return (${pollStrings.trim().split('\n').map(line => JSON.parse(line.trim())).join('')});`)();

test('overlay controls preserve rapid input and reflect actual pause state', async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 1000, height: 900}});
    await page.setContent(markup);
    await page.evaluate(new Function(`return (${source});`)());
    const update = s => page.evaluate(s => window.__ot_updateUI(s), s);
    await update({phase: 'executing'});
    await page.locator('#__ot_pause_btn').click();
    await update({phase: 'executing', pauseRequested: false}); // stale state before polling
    assert.equal(await page.locator('#__ot_pause_btn').innerText(), 'Pausing…');
    assert.equal(await page.locator('#__ot_pause_btn').isDisabled(), true);
    await page.locator('#__ot_suggest_input').fill('First instruction');
    await page.locator('#__ot_send_btn').click();
    await page.locator('#__ot_suggest_input').fill('Second instruction');
    await page.locator('#__ot_suggest_input').press('Enter');
    assert.deepEqual(await page.evaluate(drain), [
      {action: 'pause', payload: ''},
      {action: 'suggestion', payload: 'First instruction'},
      {action: 'suggestion', payload: 'Second instruction'},
    ]);
    assert.equal(await page.evaluate(drain), null); // no duplicate dispatch
    await update({phase: 'paused_hitl'});
    assert.equal(await page.locator('#__ot_pause_btn').isEnabled(), true);
    await page.locator('#__ot_pause_btn').click();
    await update({phase: 'paused_hitl'});
    assert.equal(await page.locator('#__ot_pause_btn').innerText(), 'Resuming…');
    assert.equal((await page.evaluate(drain))[0].action, 'resume');
    await update({phase: 'executing'});
    assert.equal(await page.locator('#__ot_pause_btn').isEnabled(), true);

    await update({phase: 'executing', failedCode: 'const answer = 42;'});
    await page.evaluate(() => {
      Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: () => Promise.reject(new Error('denied'))}});
      document.execCommand = command => { window.copiedText = document.activeElement.value; return command === 'copy'; };
    });
    await page.locator('#__ot_copy_btn').click();
    assert.equal(await page.evaluate(() => window.copiedText), 'const answer = 42;');
    assert.match(await page.locator('#__ot_copy_btn').innerText(), /Copied/);
    await page.locator('#__ot_collapse_btn').click();
    assert.equal((await page.locator('#__omnitasker_panel').boundingBox()).width, 52);
    await page.locator('#__ot_collapse_btn').press('Enter');
    await page.locator('#__ot_stop_btn').click();
    assert.equal((await page.evaluate(drain))[0].action, 'stop');
    assert.equal(await page.locator('#__ot_stop_btn').isDisabled(), true);
  } finally {
    await browser.close();
  }
});
