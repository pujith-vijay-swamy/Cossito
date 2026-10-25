const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'recon_engine.py'), 'utf8');
const navigate = new Function(`return (${source.match(/nav_script = r"""([\s\S]*?)"""/)[1]});`)();
test('task selection clicks once and does not reopen an active question', async () => {
 const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
 try {
  const page = await browser.newPage();
  await page.setContent('<div class="question-item"><a href="/task/2">Question 2</a></div>');
  await page.evaluate(() => {
   window.clicks = 0;
   document.querySelector('a').onclick = e => { e.preventDefault(); window.clicks++; e.currentTarget.setAttribute('aria-current','page'); };
  });
  assert.match(await page.evaluate(navigate, {taskKey:'2',taskTitle:'Question 2'}), /^Clicked/);
  assert.equal(await page.evaluate(() => window.clicks), 1);
  assert.match(await page.evaluate(navigate, {taskKey:'2',taskTitle:'Question 2'}), /^Already selected/);
  assert.equal(await page.evaluate(() => window.clicks), 1);
 } finally {await browser.close();}
});
