const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');
const source = fs.readFileSync(require('node:path').join(__dirname,'..','platform_adapters.py'),'utf8');
const discover = new Function(`return (${source.match(/COMPLETION_CONTROL_SCRIPT = r"""([\s\S]*?)"""/)[1]});`)();
const finish = new Function(`return (${source.match(/POST_COMPLETION_SCRIPT = r"""([\s\S]*?)"""/)[1]});`)();

test('verified completion dialog marks once then advances without clicking background navigation',async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
 try {
  const page=await browser.newPage();
  await page.setContent('<button onclick="window.background++">Next lesson</button><div role="dialog"><p>You have passed all the test cases.</p><button id="mark">Mark as Completed</button></div>');
  await page.evaluate(()=>{window.background=0;window.marks=0;window.nexts=0;document.querySelector('#mark').onclick=()=>window.marks++;});
  assert.equal(await page.evaluate(finish,{marked:false,advance:true}),'marked');
  assert.equal(await page.evaluate(finish,{marked:true,advance:true}),'waiting');
  await page.evaluate(()=>{document.querySelector('[role="dialog"]').innerHTML='<button onclick="window.nexts++">Next lesson</button>';});
  assert.equal(await page.evaluate(finish,{marked:true,advance:true}),'advanced');
  assert.deepEqual(await page.evaluate(()=>[window.marks,window.nexts,window.background]),[1,1,0]);
 } finally {await browser.close();}
});
const parse = new Function(`return (${source.slice(source.indexOf('    async def parse_result',source.indexOf('class GenericAdapter'))).match(/res = await page.evaluate\(r"""([\s\S]*?)"""/)[1]});`)();
test('completion adapts to validation controls without clicking navigation',async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
 try {
  const page=await browser.newPage();
  for(const label of ['Submit solution','Check Your Code','Verify answer','Run Tests']){
   await page.setContent(`<button onclick="window.clicked=(window.clicked||0)+1">${label}</button><a href="/next">Next Lesson</a>`);
   await page.evaluate(()=>window.clicked=0);
   const result=await page.evaluate(discover);
   assert.equal(result.started,true);
   assert.equal(await page.evaluate(()=>window.clicked),1);
  }
  await page.setContent('<button>Submit Feedback</button><a href="/next" onclick="window.nextClicked=true">Next Lesson</a><div role="status">Lesson completed</div>');
  assert.equal((await page.evaluate(discover)).mode,'navigation');
  assert.equal(await page.evaluate(()=>!!window.nextClicked),false);
  assert.match(await page.evaluate(parse), /Lesson completed/);
  await page.setContent('<button disabled>Check Code</button><a href="/next">Continue</a>');
  assert.equal((await page.evaluate(discover)).mode,'disabled');
  await page.setContent('<main><h1>Preview</h1></main><div id="__omnitasker_panel"><button>Submit</button></div>');
  assert.equal((await page.evaluate(discover)).mode,'automatic');
  await page.setContent('<button onclick="window.validated=true">Evaluate Exercise</button><button>Next Lesson</button>');
  assert.equal((await page.evaluate(discover,'Evaluate Exercise')).started,true);
  assert.equal(await page.evaluate(()=>window.validated),true);
  assert.equal((await page.evaluate(discover,'Next Lesson')).started,false);
  assert.equal((await page.evaluate(discover,'Invented Button')).started,false);
 } finally {await browser.close();}
});
