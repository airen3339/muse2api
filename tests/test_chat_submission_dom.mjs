/** Offline minimal DOM acceptance; executes the candidate's actual JS string.
 * No jsdom dependency, browser, network, production or generated fixture files.
 * node tests/test_chat_submission_dom.mjs
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';

const sourcePath = process.env.MUSE_ENGINE_SOURCE || fileURLToPath(new URL('../engine.py', import.meta.url));
const source = readFileSync(sourcePath, 'utf8');
const match = source.match(/_CHAT_STATE_JS\s*=\s*r"""([\s\S]*?)"""/);
assert.ok(match, 'candidate must expose its actual state expression');
const template = match[1];
console.log('ENGINE_SHA256=' + createHash('sha256').update(source).digest('hex'));

// Generic subset needed by this expression: tags, comma unions, role and
// exact/contains/prefix attribute selectors. Unsupported CSS fails loudly.
function matches(e, selector) {
  return selector.split(',').some(part => {
    const m = part.trim().match(/^([a-z]+)?(?:\[([\w-]+)(?:(\*=|\^=|=)(?:"([^"]*)"|'([^']*)'|([^\]\s]+)))?(?:\s+(i))?\])?$/i);
    if (!m) throw new Error('fixture unsupported CSS: ' + part);
    if (m[1] && e.tagName !== m[1].toUpperCase()) return false;
    if (!m[2]) return true;
    const actual = e.getAttribute(m[2]);
    if (actual === null) return false;
    if (!m[3]) return true;
    let value = actual, wanted = m[4] ?? m[5] ?? m[6] ?? '';
    if (m[7]) { value = value.toLowerCase(); wanted = wanted.toLowerCase(); }
    return m[3] === '=' ? value === wanted : m[3] === '*=' ? value.includes(wanted) : value.startsWith(wanted);
  });
}
class Element {
  constructor(tag, attrs = {}, text = '') {
    this.tagName = tag.toUpperCase(); this.attrs = {...attrs}; this.text = text;
    this.children = []; this.parentElement = null; this.disabled = false;
    this.readOnly = false; this.hidden = false; this.styleVisibility = 'visible';
    this.clicks = 0; this.events = []; this.onClick = null; this.onEvent = null;
  }
  append(e) { e.parentElement = this; this.children.push(e); return e; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  get className() { return this.attrs.class ?? ''; }
  get innerText() { return [this.text, ...this.children.map(e => e.innerText)].filter(Boolean).join(' '); }
  getClientRects() { for(let e=this;e;e=e.parentElement)if(e.hidden)return []; return [{}]; }
  querySelectorAll(sel) { return this.children.flatMap(e => [...(matches(e, sel) ? [e] : []), ...e.querySelectorAll(sel)]); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { for (let e = this; e; e = e.parentElement) if (matches(e, sel)) return e; return null; }
  focus() { this.focused = true; }
  dispatchEvent(event) { this.events.push(event.type); this.onEvent?.(event); return true; }
  click() { this.clicks++; this.onClick?.(); }
}
class Textarea extends Element {
  constructor() { super('textarea'); this._value = ''; this.truncate = false; }
  get value() { return this._value; }
  set value(value) { this._value = this.truncate ? value.slice(0, -1) : value; }
}
function fixture({label = '傳送', container = 'div'} = {}) {
  const body = new Element('body');
  const main = body.append(new Element('main'));
  const composer = main.append(new Element(container));
  const input = composer.append(new Textarea());
  const button = composer.append(new Element('button', {'aria-label': label}));
  const performance = {timeOrigin: 41};
  const location = {pathname: '/thread/new'};
  const document = {
    body, readyState: 'complete',
    querySelectorAll: sel => body.querySelectorAll(sel),
    querySelector: sel => body.querySelector(sel),
    createTreeWalker(root) {
      const nodes = [];
      function visit(e) { if (e.text) nodes.push({nodeValue:e.text, parentElement:e}); e.children.forEach(visit); }
      visit(root); let index = -1;
      return {currentNode: null, nextNode() { this.currentNode = nodes[++index]; return !!this.currentNode; }};
    },
  };
  const context = vm.createContext({document, performance, location,
    HTMLTextAreaElement: Textarea, Event: class {constructor(type) {this.type = type;}},
    NodeFilter: {SHOW_TEXT: 4}, getComputedStyle: e => ({visibility:e.styleVisibility})});
  const evaluate = (prompt = '', epoch = null, action = 'inspect') => {
    const values = [prompt, epoch, action]; let index = 0;
    const expression = template.replace(/%s/g, () => JSON.stringify(values[index++]));
    return vm.runInContext(expression, context, {timeout: 1000});
  };
  return {body, main, composer, input, button, performance, location, document, evaluate};
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const submit = (f, prompt = 'complete') => { f.evaluate(prompt, 41, 'fill'); return f.evaluate(prompt, 41, 'submit'); };
for (const label of ['傳送', '发送', 'Send', 'Send message', '發送訊息']) {
  test('exact composer label ' + label + ' dispatches once without data-testid', () => {
    const f = fixture({label}); assert.equal(submit(f).dispatched, true);
    assert.equal(f.button.clicks, 1); assert.equal(f.button.getAttribute('data-testid'), null);
  });
}
for (const label of ['Resend', 'send again', 'Please Send', '发送失败']) {
  test('non-send label rejected: ' + label, () => {
    const f = fixture({label}); assert.equal(submit(f).dispatched, false); assert.equal(f.button.clicks, 0);
  });
}
test('external Send outside composer cannot be selected', () => {
  const f = fixture({label:'Resend'});
  const external = f.main.append(new Element('button', {'aria-label':'Send'}));
  assert.equal(submit(f).dispatched, false); assert.equal(external.clicks, 0);
});
test('external Send does not make valid composer ambiguous', () => {
  const f = fixture(); const external = f.body.append(new Element('button', {'aria-label':'Send'}));
  assert.equal(submit(f).dispatched, true); assert.equal(f.button.clicks, 1); assert.equal(external.clicks, 0);
});
test('two visible composer send buttons cannot dispatch', () => {
  const f = fixture(); const second = f.composer.append(new Element('button', {'aria-label':'Send'}));
  const state = submit(f); assert.equal(state.send_candidates, 2); assert.equal(state.dispatched, false);
  assert.equal(second.clicks + f.button.clicks, 0);
});
test('hidden extra button is excluded', () => {
  const f = fixture(); f.composer.append(new Element('button', {'aria-label':'Send'})).hidden = true;
  assert.equal(submit(f).dispatched, true);
});
for (const mode of ['native', 'aria', 'role-aria']) {
  test('disabled composer control blocks submit: ' + mode, () => {
    const f = fixture();
    if (mode === 'native') f.button.disabled = true;
    else f.button.attrs['aria-disabled'] = 'true';
    if (mode === 'role-aria') { f.button.tagName = 'DIV'; f.button.attrs.role = 'button'; }
    assert.equal(submit(f).dispatched, false); assert.equal(f.button.clicks, 0);
  });
}
test('full multiline and special-character prompt survives native setter', () => {
  const f = fixture(); const prompt = '傳送 " \' $() \\ \n    return 1\n' + '完整'.repeat(60000);
  assert.equal(f.evaluate(prompt, 41, 'fill').filled, true); assert.equal(f.input.value, prompt);
  assert.deepEqual(f.input.events, ['input','change']); assert.equal(f.evaluate(prompt,41,'submit').dispatched,true);
});
test('truncated same input cannot dispatch', () => {
  const f = fixture(); f.input.truncate = true;
  assert.equal(f.evaluate('complete',41,'fill').filled,false);
  assert.equal(f.evaluate('complete',41,'submit').dispatched,false); assert.equal(f.button.clicks,0);
});
test('same-length replacement is rejected at action time', () => {
  const f=fixture(); f.evaluate('correct!',41,'fill'); f.input.value='wrong!!!';
  assert.equal(f.evaluate('correct!',41,'submit').dispatched,false); assert.equal(f.button.clicks,0);
});
test('epoch changes between fill and submit block click', () => {
  const f=fixture(); f.evaluate('complete',41,'fill'); f.performance.timeOrigin=42;
  assert.equal(f.evaluate('complete',41,'submit').dispatched,false); assert.equal(f.button.clicks,0);
});
test('late disabled transition is checked in submit evaluation', () => {
  const f=fixture(); f.evaluate('complete',41,'fill'); assert.equal(f.evaluate('complete',41).send_enabled,true);
  f.button.attrs['aria-disabled']='true';
  assert.equal(f.evaluate('complete',41,'submit').dispatched,false); assert.equal(f.button.clicks,0);
});
test('click exception is observable after exactly one invocation', () => {
  const f=fixture(); f.button.onClick=()=>{throw new Error('synthetic click error');};
  assert.throws(()=>submit(f),/synthetic click error/); assert.equal(f.button.clicks,1);
});
for (const [name, setup] of [
  ['loading',f=>f.document.readyState='loading'],
  ['wrong path',f=>f.location.pathname='/thread/old'],
  ['hydrating',f=>f.body.append(new Element('div',{'data-hatch-shell-hydration-state':'hydrating'}))],
  ['disabled input',f=>f.input.disabled=true],
  ['readonly input',f=>f.input.readOnly=true],
  ['aria-disabled input',f=>f.input.attrs['aria-disabled']='true'],
  ['second input',f=>f.composer.append(new Textarea())],
  ['user bubble',f=>f.main.append(new Element('div',{class:'hatch-chat-groupable-bubble chat-user-bubble'},'old'))],
  ['empty assistant',f=>f.main.append(new Element('div',{class:'hatch-chat-groupable-bubble hatch-agent-bubble-bg'}))],
  ['attachment',f=>f.main.append(new Element('div',{'data-testid':'hatch-chat-attachment-presentation-1'}))],
  ['Stop',f=>f.composer.append(new Element('button',{'aria-label':'Stop'}))],
]) {
  test('dirty gate blocks actual JS fill and submit: '+name,()=>{
    const f=fixture();setup(f);assert.equal(f.evaluate('complete',41,'fill').filled,false);
    f.input.value='complete';assert.equal(f.evaluate('complete',41,'submit').dispatched,false);assert.equal(f.button.clicks,0);
  });
}
for (const label of ['Connecting...', 'Still sending', 'Failed to send']) {
  test('visible UI notice blocks actual action: '+label,()=>{
    const f=fixture();f.main.append(new Element('div',{},label));assert.equal(submit(f).dispatched,false);
  });
}
for (const container of ['textarea','user','assistant','nav','aside','hidden']) {
  test('conversation or non-notice text is not classified: '+container,()=>{
    const f=fixture(); const text='Connecting... Still sending Failed to send';
    if(container==='textarea')f.input.text=text;
    else {
      const attrs=container==='user'||container==='assistant'?{class:'hatch-chat-groupable-bubble '+(container==='user'?'chat-user-bubble':'hatch-agent-bubble-bg')}:{};
      const node=f.main.append(new Element(['nav','aside'].includes(container)?container:'div',attrs,text));
      if(container==='hidden')node.hidden=true;
    }
    const state=f.evaluate();assert.equal(state.connecting,false);assert.equal(state.pending,false);assert.equal(state.failed,false);
  });
}
test('form composer with sibling help Send must not become ambiguous',()=>{
  const f=fixture({container:'form'});
  const wrapper=new Element('div'); f.composer.children=[]; f.main.children=[];
  f.main.append(wrapper); wrapper.append(f.composer); f.composer.append(f.input); f.composer.append(f.button);
  wrapper.append(new Element('button',{'aria-label':'Send'}));
  assert.equal(submit(f).dispatched,true);assert.equal(f.button.clicks,1);
});
test('send in a different sibling form must never submit outside the textarea form',()=>{
  const f=fixture({container:'form',label:'Resend'});
  const wrapper=new Element('div');f.main.children=[];
  f.main.append(wrapper);wrapper.append(f.composer);
  const unrelatedForm=wrapper.append(new Element('form'));
  const unrelatedSend=unrelatedForm.append(new Element('button',{'aria-label':'Send'}));
  const state=submit(f);
  assert.equal(unrelatedSend.clicks,0,'unrelated sibling form was clicked');
  assert.equal(state.dispatched,false);
});

let passed=0,failed=0;
for(const [name,fn] of tests){
  try{fn();passed++;console.log('PASS '+name);}
  catch(error){failed++;console.error('FAIL '+name+'\n'+String(error.stack));}
}
console.log('DOM_TOTAL='+tests.length+' DOM_PASSED='+passed+' DOM_FAILED='+failed);
process.exitCode=failed?1:0;
