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
  await page.setContent('<aside><h3>Chapter B</h3><div><a href="https://example.test/b/4"><li><button data-active="true">Part 004</button></li></a></div></aside>');
  assert.match(await page.evaluate(navigate,{taskKey:'https://example.test/b/4',taskTitle:'Part 004',taskUrl:'https://example.test/b/4'}),/^Already selected/);
  await page.setContent('<aside><button aria-expanded="false">Chapter B <span>25%</span></button><div id="lessons"></div></aside>');
  await page.evaluate(()=>{window.selected='';document.querySelector('button').onclick=e=>{e.currentTarget.setAttribute('aria-expanded','true');document.querySelector('#lessons').innerHTML='<button id="mounted">Part 001</button>';document.querySelector('#mounted').onclick=()=>window.selected='mounted';};});
  const collapsedArgs={taskKey:JSON.stringify(['Chapter B 0%','part 001']),taskTitle:'Part 001',taskSelector:'#obsolete'};
  assert.equal(await page.evaluate(navigate,collapsedArgs),'Expanded task section');
  assert.match(await page.evaluate(navigate,collapsedArgs),/^Clicked/);
  assert.equal(await page.evaluate(()=>window.selected),'mounted');
  await page.setContent('<aside><h3>Chapter B</h3><div><button id="visible">Part 001</button></div></aside><aside hidden><h3>Chapter B</h3><div><button>Part 001</button></div></aside>');
  await page.evaluate(()=>{window.selected='';document.querySelector('#visible').onclick=()=>window.selected='visible';});
  assert.match(await page.evaluate(navigate,{taskKey:JSON.stringify(['Chapter B','part 001']),taskTitle:'Part 001',taskSelector:'#obsolete'}),/^Clicked/);
  assert.equal(await page.evaluate(()=>window.selected),'visible');
  await page.setContent('<aside><h3>Chapter A</h3><div><button id="old">Part 001</button></div><h3>Chapter B</h3><div><button id="new">Part 001</button></div></aside>');
  await page.evaluate(()=>{window.selected='';document.querySelectorAll('button').forEach(b=>b.onclick=()=>window.selected=b.id);});
  assert.match(await page.evaluate(navigate,{taskKey:JSON.stringify(['Chapter B','part 001']),taskTitle:'Part 001',taskSelector:'#old'}), /^Clicked/);
  assert.equal(await page.evaluate(()=>window.selected),'new');
  await page.setContent('<aside><a href="https://example.test/a/1">Part 001</a><a href="https://example.test/b/1">Part 001</a></aside>');
  await page.evaluate(()=>{window.selected='';document.querySelectorAll('a').forEach(a=>a.onclick=e=>{e.preventDefault();window.selected=a.href;});});
  assert.match(await page.evaluate(navigate,{taskKey:'2',taskTitle:'Part 001',taskUrl:'https://example.test/b/1'}), /^Clicked/);
  assert.equal(await page.evaluate(()=>window.selected), 'https://example.test/b/1');
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
