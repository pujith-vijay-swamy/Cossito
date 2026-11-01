"""Exercise actual injected JavaScript and strict Playwright argument handling."""
import json
import os
import unittest
from playwright.async_api import async_playwright
from editor_adapters import safe_write, get_all_editors_summary, MonacoAdapter, CodeMirror5Adapter, CodeMirror6Adapter, AceAdapter, TextareaAdapter

class BrowserEditorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        self.browser = await self.pw.chromium.launch(executable_path=executable, channel=None if executable else 'chrome', headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def monaco(self, duplicate=False):
        await self.page.set_content('<div id="html"></div><div id="css"></div>')
        await self.page.evaluate('''(duplicate) => {
            const make = (path, lang) => ({uri:{path}, getLanguageId:()=>lang, value:'original '+lang,
                getValue(){return this.value}, setValue(v){this.value=v}});
            window.models=[make('/index.html','html'),make('/theme.css','css'),make('/app.js','javascript')];
            if(duplicate) models.push(make('/other.css','css'));
            window.editors=models.map(m=>({getModel:()=>m,getDomNode:()=>document.querySelector('#html'),
                setModel(){throw Error('must not rebind')},setValue:v=>m.setValue(v),getValue:()=>m.getValue()}));
            window.monaco={editor:{getModels:()=>models,getEditors:()=>editors}};
        }''', duplicate)

    async def test_three_files_and_repeated_task(self):
        await self.monaco()
        for iteration in range(2):
            code=f'```html\n<p>{iteration}</p>\n```\n```css\np {{color:red}}\n```\n```js\nlet n={iteration};\n```'
            ok,_,_=await safe_write(self.page,code,target="html" if iteration else None)
            self.assertTrue(ok)
            self.assertEqual(await self.page.evaluate('() => models.map(m=>m.value)'),[f'<p>{iteration}</p>','p {color:red}',f'let n={iteration};'])

    async def test_ambiguous_language_requires_file_key(self):
        await self.monaco(True)
        ok,_,_=await safe_write(self.page,'p {color:blue}',target='css')
        self.assertFalse(ok)
        self.assertEqual(await self.page.evaluate('() => models[1].value'),'original css')
        ok,_,_=await safe_write(self.page,'p {color:blue}',target='/other.css')
        self.assertTrue(ok)
        self.assertEqual(await self.page.evaluate('() => models[1].value'),'original css')

    async def test_single_html_preserves_inline_style(self):
        await self.page.set_content('<textarea aria-label="HTML"></textarea>')
        code='<html><style>p {color:red}</style><p>Hello</p></html>'
        self.assertTrue((await safe_write(self.page,code))[0])
        self.assertEqual(await self.page.input_value('textarea'),code)

    async def test_reversed_textareas(self):
        await self.page.set_content('<section><h3>CSS</h3><textarea>old css</textarea></section><section><h3>HTML</h3><textarea>old html</textarea></section>')
        inventory=await get_all_editors_summary(self.page)
        self.assertEqual([e['language'] for e in inventory],['css','html'])
        self.assertTrue((await safe_write(self.page,'```html\n<p>Hello</p>\n```\n```css\np {color:red}\n```'))[0])
        self.assertEqual(await self.page.locator('textarea').evaluate_all('(els)=>els.map(e=>e.value)'),['p {color:red}','<p>Hello</p>'])

    async def test_json_serialized_inventory(self):
        await self.monaco()
        page=self.page
        class BrowserUseResult:
            async def evaluate(self, script, arg=None):
                value=await page.evaluate(script,arg)
                return json.dumps(value) if isinstance(value,(list,dict)) else value
        self.assertEqual(len(await get_all_editors_summary(BrowserUseResult())),3)

    async def test_targeted_editor_families(self):
        for adapter,cls,setup in [
            (CodeMirror5Adapter(), 'CodeMirror', "el.CodeMirror={setValue:v=>el.value=v,getValue:()=>el.value,getOption:()=>el.dataset.lang}"),
            (CodeMirror6Adapter(), 'cm-editor', "el.cmView={view:{state:{doc:{length:3,toString:()=>el.value}},dispatch:({changes})=>el.value=changes.insert}}"),
            (AceAdapter(), 'ace_editor', "el.env={editor:{setValue:v=>el.value=v,getValue:()=>el.value,getSession:()=>({getMode:()=>({$id:el.dataset.lang})})}}"),
        ]:
            with self.subTest(editor=cls):
                await self.page.set_content(f'<div class="{cls}" data-lang="css"></div><div class="{cls}" data-lang="html"></div>')
                await self.page.evaluate('(setup)=>document.querySelectorAll("[data-lang]").forEach(el=>{el.value="old";new Function("el",setup)(el)})',setup)
                self.assertTrue((await safe_write(self.page,'p {color:red}',adapter=adapter,target='css'))[0])
                self.assertEqual(await self.page.evaluate('()=>Array.from(document.querySelectorAll("[data-lang]")).map(e=>e.value)'),['p {color:red}','old'])

if __name__ == '__main__': unittest.main()
