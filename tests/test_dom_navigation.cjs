// Run with: node --test tests/test_dom_navigation.cjs
// Requires Playwright; PLAYWRIGHT_CHROMIUM_EXECUTABLE may select local Chrome.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

// Exercise the production scanner, rather than a copy of its selection logic.
const source = fs.readFileSync(path.join(__dirname, '..', 'dom_extraction.py'), 'utf8');
const scannerSource = source.match(/raw_nav = await page\.evaluate\(r"""([\s\S]*?)""", active_problem_title\)/)[1];
const scanner = new Function(`return (${scannerSource});`)();

test('task discovery excludes static labels but retains real navigation', async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <main>
        <div class="flex items-center">
          <div class="flex items-center">Problem Description</div>
          <div class="flex items-center">Gas Station</div>
        </div>
        <h1>Gas Station</h1>
        <ul><li>1. Read the input array</li><li>2. Print the result</li></ul>
        <button>Run Code</button><button>Submit Code</button>
      </main>`);
    assert.deepEqual(await page.evaluate(scanner, 'Gas Station'), []);

    await page.setContent('<button>Question 1</button><button>Question 2</button>');
    assert.deepEqual((await page.evaluate(scanner, '')).map(q => q.text), ['Question 1', 'Question 2']);

    await page.setContent('<aside><a href="/problem/one">Gas Station</a><a href="/problem/two">Two Sum</a></aside>');
    assert.deepEqual((await page.evaluate(scanner, 'Gas Station')).map(q => q.text), ['Gas Station', 'Two Sum']);
  } finally {
    await browser.close();
  }
});
