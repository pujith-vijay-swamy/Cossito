const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');
const source = fs.readFileSync(require('node:path').join(__dirname,'..','dom_extraction.py'),'utf8');
const read = new Function(`return (${source.match(/PAGE_READINESS_SCRIPT = r"""([\s\S]*?)"""/)[1]});`)();
const extract = new Function(`return (${source.match(/raw_eval = await page.evaluate\(r"""([\s\S]*?)"""/)[1]});`)();
test('readiness reads current visible content and excludes overlay and hidden splash', async()=>{
 const browser = await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
 try {
  const page=await browser.newPage();
  await page.setContent('<main aria-busy="true">Loading your workspace</main><div id="__omnitasker_panel">Build a task now</div>');
  assert.deepEqual(await page.evaluate(read),{text:'Loading your workspace',busy:true});
  await page.evaluate(()=>{document.querySelector('main').innerHTML='<div style="display:none">Loading your workspace</div><h1>Build a form</h1><p>Use HTML and CSS for a contact form.</p>';document.querySelector('main').removeAttribute('aria-busy');});
  assert.deepEqual(await page.evaluate(read),{text:'Build a form Use HTML and CSS for a contact form.',busy:false});
  await page.evaluate(()=>{document.querySelector('main').innerHTML='<div class="skeleton">Preparing content</div>';});
  assert.equal((await page.evaluate(read)).busy,true);
  await page.setContent('<div class="problem-statement" style="display:none">Loading the workspace. Please wait while we prepare the coding challenge.</div><main><div class="problem-description">Build a contact form using HTML and CSS. Include a name field and a submit button.</div></main>');
  const result = await page.evaluate(extract, null);
  assert.match(result.text, /Build a contact form/);
  assert.doesNotMatch(result.text, /Loading the workspace/);
 } finally {await browser.close();}
});

const finish = new Function(`return (${fs.readFileSync(require('node:path').join(__dirname,'..','platform_adapters.py'),'utf8').match(/POST_COMPLETION_SCRIPT = r"""([\s\S]*?)"""/)[1]});`)();
test('completion dialog hiding a loaded lesson is finalized without clicking Next', async()=>{
 const browser = await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
 try {
  const page=await browser.newPage();
  await page.setContent('<div id="workspace" aria-hidden="true"><main><p>Change the width property to 80%.</p><button id="next">Next Lesson</button></main></div><div role="dialog"><p>You have passed all the test cases.</p><button id="mark">Mark as Completed</button></div>');
  await page.evaluate(()=>{window.nextClicks=0;document.querySelector('#next').onclick=()=>window.nextClicks++;document.querySelector('#mark').onclick=()=>{document.querySelector('[role=dialog]').remove();document.querySelector('#workspace').removeAttribute('aria-hidden');};});
  const blocked=await page.evaluate(read);
  assert.equal(blocked.text,'');
  assert.match(blocked.dialog,/passed all/);
  assert.equal(await page.evaluate(finish,{marked:false,advance:false}),'marked');
  const ready=await page.evaluate(read);
  assert.match(ready.text,/Change the width/);
  assert.equal(ready.busy,false);
  assert.equal(ready.dialog,undefined);
  assert.equal(await page.evaluate(()=>window.nextClicks),0);
 } finally {await browser.close();}
});
