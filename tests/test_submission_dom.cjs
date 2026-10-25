// NODE_PATH must resolve Playwright. Optionally set PLAYWRIGHT_CHROMIUM_EXECUTABLE.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const source = fs.readFileSync(path.join(__dirname, '..', 'platform_adapters.py'), 'utf8');
const script = source.match(/res = await page\.evaluate\(r"""([\s\S]*?)"""\)/)[1];
const readStatus = new Function(`return (${script});`)();
const resultScript = source.slice(source.indexOf('    async def parse_result', source.indexOf('class GenericAdapter'))).match(/res = await page\.evaluate\(r"""([\s\S]*?)"""\)/)[1];
const readResult = new Function(`return (${resultScript});`)();

test('submission busy state ignores unrelated loaders and honors actual test activity', async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage();
    await page.setContent('<div class="editor"><span class="spinner">Loading editor</span></div><div class="test-results">All tests passed</div>');
    assert.equal(await page.evaluate(readStatus), 'All tests passed');
    await page.setContent('<div id="__omnitasker_panel"><span class="spinner">Working</span></div><div class="test-results">All tests passed</div>');
    assert.equal(await page.evaluate(readStatus), 'All tests passed');
    await page.setContent('<button data-loading="false">Submit</button><div class="test-results">All tests passed</div>');
    assert.equal(await page.evaluate(readStatus), 'All tests passed');
    await page.setContent('<div class="test-results"><span class="spinner">Running</span>2/5 passed</div>');
    assert.equal(await page.evaluate(readStatus), 'evaluating');
    await page.setContent('<main>To be accepted, all tests passed must appear.</main><div id="__omnitasker_panel"><div role="alert">Accepted</div></div><table><tr><td>Old submission: Accepted</td></tr></table>');
    assert.equal(await page.evaluate(readResult), '');
    await page.setContent('<div class="test-results" hidden>Accepted</div><div class="test-results">Wrong Answer: 2/5 tests passed</div>');
    assert.equal(await page.evaluate(readResult), 'Wrong Answer: 2/5 tests passed');
    await page.setContent('<button data-loading="true">Submit</button><div class="test-results">Old result: All tests passed</div>');
    assert.equal(await page.evaluate(readStatus), 'evaluating');
  } finally {
    await browser.close();
  }
});
