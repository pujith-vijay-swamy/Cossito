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
const expand = new Function(`return (${source.match(/expand_script = r"""([\s\S]*?)"""/)[1]});`)();

test('task discovery excludes static labels but retains real navigation', async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  });
  try {
    const page = await browser.newPage();
    await page.setContent('<aside><h3>Chapter A</h3><div><a href="https://example.test/a/4"><li><button data-active="true">Part 004</button></li></a></div><h3>Chapter B</h3><div><a href="https://example.test/b/4"><li><button>Part 004</button></li></a></div></aside>');
    const nested = await page.evaluate(scanner, '');
    assert.equal(nested.length, 2);
    assert.ok(nested.every(q=>q.url && !q.selector));
    assert.equal(nested[0].active, true);
    assert.equal(nested[1].active, false);
    // Real lesson count must not depend on wrapper or responsive-copy count.
    await page.setContent('<aside>' + Array.from({length:339},(_,i)=>`<div class="question-item"><button>Task ${i+1}</button></div>`).join('') + '</aside><aside hidden>' + Array.from({length:339},(_,i)=>`<button>Task ${i+1}</button>`).join('') + '</aside>');
    assert.equal((await page.evaluate(scanner, '')).length,339);
    await page.setContent('<aside><h3>Chapter A</h3><div><button>Part 001</button></div><h3>Chapter B</h3><div><button>Part 001</button></div></aside>');
    const scopedParts = await page.evaluate(scanner, '');
    assert.equal(scopedParts.length,2);
    assert.notEqual(scopedParts[0].identity,scopedParts[1].identity);
    await page.setContent('<aside><button aria-expanded="false">Chapter 1</button><a class="mantine-NavLink-root" href="https://example.test/1">Part 001</a></aside>');
    await page.evaluate(()=>{window.leafClicks=0;window.expands=0;document.querySelector('a').onclick=e=>{e.preventDefault();window.leafClicks++;};document.querySelector('button').onclick=()=>window.expands++;});
    await page.evaluate(expand);
    assert.deepEqual(await page.evaluate(()=>[window.leafClicks,window.expands]), [0,1]);
    await page.setContent(`<aside>
      <button aria-expanded="true">Part 1</button>
      <div><a href="https://example.test/a/1">Part 001</a><a href="https://example.test/a/2">Part 002</a></div>
      <button aria-expanded="true">Chapter 2</button>
      <div><a href="https://example.test/b/1">Part 001</a><button id="leaf">Lesson 3</button></div>
      <a href="https://example.test/a/1">Part 001</a>
      <a href="https://example.test/problem/named">Named problem</a>
    </aside>`);
    const parts = await page.evaluate(scanner, 'Part 002');
    assert.deepEqual(parts.map(q=>q.text), ['Part 001','Part 002','Part 001','Lesson 3','Named problem']);
    assert.equal(parts[0].url, 'https://example.test/a/1');
    assert.equal(parts[2].url, 'https://example.test/b/1');
    assert.equal(parts[3].selector, '#leaf');
    assert.ok(parts.every(q=>q.is_leaf));
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
