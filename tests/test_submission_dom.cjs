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
    await page.setContent('<div class="group/sidebar-wrapper"><main><div><button>Previous</button><button disabled><span>Completed</span></button><button>Next Lesson</button></div></main></div>');
    assert.match(await page.evaluate(readResult), /Current task completed/);
    await page.setContent('<aside><button disabled>Completed</button><button>Next Lesson</button></aside><main><button>Mark as completed</button><button>Next Lesson</button></main>');
    assert.equal(await page.evaluate(readResult), '');
    await page.setContent('<main><div class="celebration"><h2>Hooray! You completed this block!</h2><button>Next lesson</button></div></main>');
    assert.match(await page.evaluate(readResult), /You completed this block/);
    await page.setContent('<main><p>When you completed this block, you should have seen a message.</p><div hidden>You completed this block!</div><div id="__omnitasker_panel"><p>You completed this block!</p></div></main>');
    assert.equal(await page.evaluate(readResult), '');
    await page.setContent('<h2>You completed this block!</h2><div class="test-results">2 passed, 1 failed</div>');
    assert.match(await page.evaluate(readResult), /1 failed/);
    await page.setContent('<ul id="tests"><li><span aria-label="Passed"></span>List exists</li><li><span title="Passed"></span>Items exist</li></ul>');
    assert.equal(await page.evaluate(readResult), '2 passed, 0 failed');
    await page.setContent('<ul class="test-results"><li>Passed: List exists</li><li>Failed: Items exist</li></ul>');
    assert.equal(await page.evaluate(readResult), '1 passed, 1 failed');
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
