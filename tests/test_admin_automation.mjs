/**
 * Run: node tests/test_admin_automation.mjs
 * Optional visual fixture: node tests/test_admin_automation.mjs --serve
 * No dependencies, upstream Muse requests, browser credentials or disk writes.
 * The fixture binds loopback only and rejects non-fixture Authorization headers.
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createServer} from 'node:http';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

const html = readFileSync(fileURLToPath(new URL('../admin.html',import.meta.url)),'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script,{filename:'admin-inline.js'});
function section(start,end){
  const from = script.indexOf(start), to = script.indexOf(end,from);
  assert.ok(from >= 0 && to > from,'Missing source section: '+start);
  return script.slice(from,to);
}
const translationSource = section('const I18N_EN = {','function translateTree(');
const apiSource = section('async function api(path, opts){','function fmtBytes(');
const automationSource = section('/* ================= 管理自动化：','/* ================= 接入信息 / API Key ================= */');
const tasksSource = section('let tasksHaveWork = false,','async function delTask(');
const studioSource = section('/* ================= 在线测试：','/* ================= 总览刷新 ================= */');
const cardsSource = section('/* ================= 账号额度卡片 ================= */','/* ================= 额度显示 ================= */') + section('function proxyStatus(account){','async function loadPool(){');
const poolLoadSource = section('async function loadPool(){','async function addAccount(');
const settings = {keepalive_enabled:true,keepalive_interval_minutes:30,quota_enabled:true,quota_interval_minutes:60,include_disabled:false};
const state = {running:false,last_run:1794558000,next_run:1794561600,last_keepalive_at:1794557000,last_quota_at:1794557100,last_result:{ok:true}};
const cleanupDefaults = {enabled:false,check_time:'03:00',timezone:'Asia/Shanghai',media_enabled:true,media_max_mb:10240,media_target_mb:8192,logs_enabled:true,logs_max_mb:100,logs_target_mb:80};
const cleanupState = {last_checked_date:null,last_run:null,last_result:null,next_run:null};
const fixtureBatch = status => ({id:'fixture-batch',status,items:[{index:0,account:'u***@example.com',label:'Fixture',status:status==='completed'?'succeeded':'waiting_otp',stage:'waiting_otp'}]});

if (process.argv.includes('--serve')) {
  let saved = {...settings}, cleanupSaved = {...cleanupDefaults}, batch = null, checks = 0;
  const server = createServer(async (req,res) => {
    const url = new URL(req.url,'http://127.0.0.1');
    res.setHeader('Cache-Control','no-store');
    if (url.pathname==='/' || url.pathname==='/admin') {res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html);return;}
    res.setHeader('Content-Type','application/json');
    if (req.headers.authorization!=='Bearer fixture-only') {res.statusCode=401;res.end(JSON.stringify({detail:'Fixture key required'}));return;}
    let raw = ''; for await (const part of req) raw += part;
    let body;try {body=JSON.parse(raw||'{}');} catch {res.statusCode=400;res.end('{}');return;}
    let data = {};
    if (url.pathname==='/admin/maintenance/settings') {if(req.method==='PUT')saved=body;data={settings:saved,state};}
    else if (url.pathname==='/admin/cleanup/settings') {if(req.method==='PUT')cleanupSaved=body;data={settings:cleanupSaved,state:cleanupState};}
    else if (url.pathname==='/admin/maintenance/run') {res.statusCode=202;checks=2;data={status:'queued'};}
    else if (url.pathname==='/admin/keepalive/status') data={settings:saved,...state,running:checks-->0};
    else if (url.pathname==='/admin/login/batches' && req.method==='POST') {batch=fixtureBatch('running');res.statusCode=202;data=batch;}
    else if (url.pathname.endsWith('/otp')) {if(!/^[0-9]{6}$/.test(body.code||'')){res.statusCode=400;data={detail:'Invalid code'};}else{batch=fixtureBatch('completed');data=batch;}}
    else if (url.pathname.endsWith('/cancel')) {batch={...fixtureBatch('cancelled'),items:[]};data=batch;}
    else if (url.pathname.startsWith('/admin/login/batches/')) data=batch;
    else if (url.pathname==='/admin/logs' || /\/admin\/tasks\/.*\/logs/.test(url.pathname)) data={entries:[{time:state.last_run,level:'ERROR',event:'extract_failed',task_id:'fixture-task',account_id:'fixture-account',stage:'extract',message:'Synthetic failure; inspect the retained task details.',details:{attempt:1,cookie:'never-display',url:'https://fixture.invalid/?token=never-display'}}],total:1,offset:0,limit:100,next_offset:null};
    else if (url.pathname==='/admin/tasks') data={tasks:[{id:'fixture-task',kind:'image',status:'failed',prompt:'Synthetic test only',error:'Synthetic extraction failed. Retained full error details.',elapsed:5,created_at:state.last_run}]};
    else if (url.pathname==='/admin/accounts') data={accounts:[],stats:{total:0}};
    else if (url.pathname==='/admin/apikey') data={api_key:'fixture-only',base_url:'http://127.0.0.1:'+server.address().port+'/v1'};
    else if (/\/admin\/(repo\/status|update\/check)/.test(url.pathname)) data={local_version:'fixture',remote_version:'fixture',up_to_date:true,has_update:false,recent_commits:[]};
    else if (url.pathname==='/admin/status') data={browser_running:false,media_count:0};
    res.end(JSON.stringify(data));
  });
  server.listen(0,'127.0.0.1',() => console.log('FIXTURE_URL=http://127.0.0.1:'+server.address().port+'/?key=fixture-only'));
  process.on('SIGTERM',()=>server.close());
} else {
  const decode = text => String(text).replace(/&(amp|lt|gt|quot|#39);/g,(_,key)=>({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'"}[key]));
  const encode = text => String(text).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  class Element {
    constructor(tag='div') {this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attributes={};this.listeners={};this._text='';this.value='';this.disabled=false;this.checked=false;this.className='';this.parentElement=null;this.style={};}
    get classList(){const el=this;return {contains:name=>el.className.split(/\s+/).includes(name),add(name){if(!this.contains(name))el.className+=' '+name;},remove(name){el.className=el.className.split(/\s+/).filter(v=>v!==name).join(' ');},toggle(name,on){on??=!this.contains(name);if(on)this.add(name);else this.remove(name);}};}
    setAttribute(key,value){this.attributes[key]=String(value);if(key==='class')this.className=value;if(key==='id')this.id=value;if(key.startsWith('data-'))this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;}
    getAttribute(key){return this.attributes[key]??null;}
    removeAttribute(key){delete this.attributes[key];}
    closest(selector){return this.matches(selector)?this:this.parentElement?.closest(selector)||null;}
    append(...children){for(const child of children){if(child.parentElement)child.parentElement.children=child.parentElement.children.filter(v=>v!==child);child.parentElement=this;this.children.push(child);}}
    replaceChildren(...children){for(const child of this.children)child.parentElement=null;this.children=[];this._text='';this.append(...children);}
    get lastElementChild(){return this.children.at(-1);}
    set textContent(value){this.replaceChildren();this._text=String(value??'');}
    get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
    set innerHTML(value){
      this.replaceChildren();const stack=[this];
      for(const part of String(value).match(/<[^>]*>|[^<]+/g)||[]) {
        if(part.startsWith('</')){if(stack.length>1)stack.pop();continue;}
        if(part.startsWith('<')){const tag=part.match(/^<([\w-]+)/)?.[1];if(!tag)continue;const el=new Element(tag);for(const attr of part.matchAll(/([\w-]+)="([^"]*)"/g))el.setAttribute(attr[1],decode(attr[2]));stack.at(-1).append(el);if(!['INPUT','BR','IMG','HR'].includes(el.tagName))stack.push(el);}
        else stack.at(-1)._text+=decode(part);
      }
    }
    get innerHTML(){return encode(this._text)+this.children.map(child=>'<'+child.tagName.toLowerCase()+Object.entries(child.attributes).map(([key,value])=>' '+key+'="'+encode(value)+'"').join('')+'>'+child.innerHTML+'</'+child.tagName.toLowerCase()+'>').join('');}
    matches(selector){const parts=selector.match(/^([\w-]+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/);if(!parts)return false;return (!parts[1]||this.tagName===parts[1].toUpperCase())&&(!parts[2]||(parts[2] in this.attributes||parts[2].startsWith('data-')&&parts[2].slice(5) in this.dataset))&&(!parts[3]||this.getAttribute(parts[2])===parts[3]);}
    querySelectorAll(selector){return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]);}
    querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
    focus(){this.focused=true;}
    setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}
    addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
    remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(child=>child!==this);}
  }
  function harness(language='zh') {
    const elements=new Map([...html.matchAll(/<([\w-]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)].map(match=>{const el=new Element(match[1]);el.id=match[2];return [el.id,el];}));
    elements.get('tab-pool').className='tab active';
    const timers=new Map(),events=new Map(),requests=[],storageWrites=[],toasts=[],revoked=[],fileReads=[];let nextTimer=1;
    const TestURL=class extends URL {};TestURL.createObjectURL=file=>'blob:fixture/'+file.name;TestURL.revokeObjectURL=url=>revoked.push(url);
    class FileReader {readAsDataURL(file){fileReads.push(file.name);this.result='data:'+file.type+';base64,'+Buffer.from(file.name).toString('base64');queueMicrotask(()=>this.onload?.());}abort(){this.onabort?.();}}
    for(const kind of ['img','vid']){elements.get(kind+'Ratio').value=kind==='img'?'auto':'16:9';elements.get(kind+'Resolution').value='1K';elements.get(kind+'CustomRatio').value='7:5';elements.get(kind+'Prompt').value='Fixture prompt';elements.get(kind+'Width').value='1024';elements.get(kind+'Height').value='1024';}elements.get('vidDur').value='5';
    const document={hidden:false,getElementById:id=>elements.get(id),createElement:tag=>new Element(tag),querySelectorAll(selector){const match=selector.match(/^#([\w-]+) (.+)$/);return match?elements.get(match[1]).querySelectorAll(match[2]):[];},addEventListener(name,fn){events.set(name,fn);}};
    const context={LANG:language,KEY:'fixture-key',document,window:{addEventListener(name,fn){events.set(name,fn);}},location:{origin:'http://fixture.invalid'},URL:TestURL,URLSearchParams,AbortController,FileReader,confirm:()=>true,
      $:id=>elements.get(id),setAuth:()=>{},toast:(...args)=>toasts.push(args),loadPool:async()=>{},goTab:name=>activate(name),delTask:async()=>{},fmtBytes:value=>String(value??''),localStorage:{setItem:(...args)=>storageWrites.push(args)},
      setTimeout(fn,delay){const id=nextTimer++;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id)};
    let respond=async()=>({});
    context.fetch=async(path,options={})=>{requests.push({path,options});const data=await respond(path,options);return {status:data?.__status||200,ok:!data?.__status||data.__status<400,text:async()=>JSON.stringify(data)};};
    vm.createContext(context);
    vm.runInContext(translationSource+apiSource+automationSource+tasksSource+cardsSource+studioSource+'\nlet accounts=[];',context);
    function activate(name){for(const [id,element] of elements)if(id.startsWith('tab-'))element.className='tab'+(id==='tab-'+name?' active':'');}
    const run=code=>vm.runInContext(code,context);
    return {context,elements,timers,events,requests,storageWrites,toasts,revoked,fileReads,activate,run,setRespond(fn){respond=fn;},async fireTimer(){const match=[...timers].find(([,timer])=>timer.delay!==20000&&timer.delay<=30000);assert.ok(match,'Expected a poll timer');timers.delete(match[0]);await match[1].fn();await flush();}};
  }
  const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
  async function bootPage(language){
    const base=harness(language), document=base.context.document, buttons=[];
    for(const match of html.matchAll(/<button\b[^>]*data-t="([^"]+)"[^>]*>([^<]*)<\/button>/g)) {
      const button=new Element('button');button.dataset.t=match[1];button.textContent=match[2];button.className=button.dataset.t==='pool'?'active':'';button.click=()=>button.onclick?.();buttons.push(button);
    }
    const query=document.querySelectorAll.bind(document);
    document.body=new Element('body');document.body.appendChild=child=>document.body.append(child);
    document.documentElement=new Element('html');document.title='muse2api';document.createTreeWalker=()=>({nextNode:()=>false});
    document.querySelectorAll=selector=>selector==='nav.tabs button'?buttons:selector==='.tab'?[...base.elements.values()].filter(element=>element.id?.startsWith('tab-')):query(selector);
    document.querySelector=selector=>buttons.find(button=>selector==='nav.tabs button[data-t="'+button.dataset.t+'"]')||null;
    const store=new Map([['muse2api_lang',language],['muse2api_key','fixture-key']]), intervals=[],observers=[];
    const context={...base.context,localStorage:{getItem:key=>store.get(key)||null,setItem:(key,value)=>{base.storageWrites.push([key,value]);store.set(key,value);}},navigator:{language,clipboard:{writeText:async()=>{}}},location:{origin:'http://fixture.invalid',search:'',reload(){}},NodeFilter:{SHOW_TEXT:4},MutationObserver:class {observe(){observers.push(true);}},setInterval(fn,delay){intervals.push({fn,delay});return intervals.length;}};
    context.window={...context.window,alert(){},confirm(){return true;},prompt(){return null;}};
    base.setRespond(async path=>{
      if(path==='/admin/maintenance/settings')return {settings,state};
      if(path==='/admin/cleanup/settings')return {settings:cleanupDefaults,state:cleanupState};
      if(path==='/admin/status')return {browser_running:false,media_count:0};
      if(path==='/admin/accounts')return {accounts:[],stats:{total:0}};
      if(path==='/admin/apikey')return {api_key:'fixture-key',base_url:'http://fixture.invalid/v1'};
      if(path==='/admin/tasks?limit=60')return {tasks:[]};
      if(path.startsWith('/admin/logs?'))return {entries:[],offset:0,total:0,next_offset:null};
      if(/repo\/status|update\/check/.test(path))return {up_to_date:true,has_update:false,local_sha:'fixture',remote_sha:'fixture',local_version:'fixture',recent_commits:[]};
      throw new Error('Unexpected page request '+path);
    });
    vm.createContext(context);vm.runInContext(script,context,{filename:'admin-full-page.js'});await flush();
    return {...base,context,buttons,intervals,observers};
  }
  let passed=0;
  async function test(name,fn){await fn();passed++;console.log('PASS '+name);}

  await test('HTML script compiles; automation tabs, accessibility, HTTPS notice and responsive containment exist',()=>{
    for(const name of ['login','maintenance','logs']) {assert.ok(html.includes('data-t="'+name+'"'));assert.ok(html.includes('id="tab-'+name+'"'));}
    assert.match(html,/role="status" aria-live="polite"/);assert.match(html,/HTTPS/);assert.match(html,/@media\(max-width:600px\)/);assert.match(html,/\.tblwrap\{overflow-x:auto/);
    assert.doesNotMatch(automationSource,/localStorage|sessionStorage|console\./);
  });
  await test('entire inline page starts and new tabs work in Chinese and English without background hidden requests',async()=>{
    for(const language of ['zh','en']) {
      const h=await bootPage(language);assert.equal(h.elements.get('maintenanceSave').disabled,false);
      for(const name of ['maintenance','login','logs','tasks']){h.buttons.find(button=>button.dataset.t===name).click();await flush();assert.ok(h.elements.get('tab-'+name).classList.contains('active'));}
      assert.ok(h.requests.some(request=>request.path==='/admin/maintenance/settings'));assert.ok(h.requests.some(request=>request.path==='/admin/tasks?limit=60'));
      h.context.document.hidden=true;const count=h.requests.length;for(const timer of h.intervals)timer.fn();await flush();assert.equal(h.requests.length,count);
      if(language==='en')assert.ok(h.observers.length);
    }
  });
  await test('compact account cards contain plan, reset, remaining quota and only enable/delete actions',()=>{
    const h=harness(), account={id:'card-1',label:'Main',enabled:true,ok:true,essential_ok:true,cookie_count:4,expires_at:state.last_run,last_used:state.last_run-500,last_keepalive:state.last_run-100,quota:{plan:'Free plan',weekly_used_pct:12.5,weekly_reset:'10月30日',extra_used_pct:0,extra_left:'2B tokens left',extra_expires:'never',checked_at:state.last_run}};
    const body=h.elements.get('poolBody');body.innerHTML=h.context.accountCard(account);assert.equal(body.querySelectorAll('article').length,1);assert.equal(body.querySelectorAll('tr').length,0);assert.match(body.textContent,/Free plan/);assert.match(body.textContent,/87.5%/);assert.match(body.textContent,/10月30日/);assert.match(body.textContent,/2B tokens left/);assert.match(body.textContent,/Asia\/Shanghai/);assert.equal(body.querySelectorAll('button[data-account-action]').length,2);assert.equal(body.querySelector('div[role="progressbar"]').getAttribute('aria-valuenow'),'87.5');assert.doesNotMatch(body.innerHTML,/onclick=|reset-quota|credit-toggle/);assert.match(html,/repeat\(auto-fill,minmax\(min\(100%,350px\),1fr\)\)/);
  });
  await test('unknown quota is never zero, invalid percentages cannot enter CSS, and raw reset strings never acquire a guessed date',()=>{
    const h=harness();for(const value of [undefined,null,'50',NaN,Infinity,-1,101,'0%;background:url(evil)']){assert.equal(h.context.quotaPercent(value),null);const output=h.context.quotaProgress('周额度用量',value);assert.match(output,/未知/);assert.doesNotMatch(output,/aria-valuenow|50%|background:url/);}
    assert.equal(h.context.quotaPercent(0),0);assert.equal(h.context.quotaPercent(100),100);assert.match(h.context.quotaProgress('周剩余额度',100),/hsl\(0,70%,50%\)/);assert.equal(h.context.quotaRawText('Sep 30'),'Sep 30');assert.equal(h.context.quotaRawText('2026-10-30'),'2026-10-30');assert.match(h.context.quotaRawText(state.last_run),/Asia\/Shanghai/);assert.equal(h.context.accountTime('Sep 30'),'未知');
    const output=h.context.accountCard({id:'unknown',enabled:false,quota:{}});assert.match(output,/未知/);assert.doesNotMatch(output,/0%|倒计时|remaining days/);
  });
  await test('account card fields and proxy metadata escape XSS, redact credentials, and retain old quota on error',()=>{
    const h=harness(),body=h.elements.get('poolBody');body.innerHTML=h.context.accountCard({id:'bad" onclick="evil',label:'<img src=x>',enabled:true,ok:false,quota:{plan:'<script>evil()</script>',weekly_used_pct:'100%;color:red',weekly_reset:'<img src=x>',extra_left:'<iframe>bad</iframe>'},quota_error:'Failed cookie=SECRET',note:'Authorization: Bearer SECRET',proxy:{server:'socks5://user:SECRET@proxy.example:1080?token=SECRET'},proxy_status:{proxy_reachable:true}});
    assert.equal(body.querySelectorAll('img').length,0);assert.equal(body.querySelectorAll('script').length,0);assert.equal(body.querySelectorAll('iframe').length,0);assert.doesNotMatch(body.innerHTML,/SECRET|onclick="evil"|width:100%;color:red/);assert.doesNotMatch(body.textContent,/proxy.example|代理未测试/);assert.match(body.textContent,/保留上次成功快照/);
  });
  await test('delegated card actions preserve exact account identity and labels without embedding JavaScript handlers',async()=>{
    const h=harness(),calls=[];h.run('accounts=[{id:"acc/1",label:"quoted label",enabled:false}]');
    for(const name of ['testAcc','keepaliveOne','queryQuota','openProxy','toggleAcc','renameAcc','updateCookies','delAcc'])h.context[name]=(...args)=>calls.push({name,args});
    for(const name of ['test','keepalive','quota','proxy','toggle','rename','cookies','delete'])await h.context.handleAccountAction({target:{closest:()=>({disabled:false,dataset:{accountAction:name,accountId:'acc/1'}})}});
    assert.equal(calls.length,8);assert.ok(calls.every(call=>call.args[0]==='acc/1'));assert.equal(calls.find(call=>call.name==='toggleAcc').args[1],true);assert.equal(calls.find(call=>call.name==='renameAcc').args[1],'quoted label');
  });
  await test('account API renders cards, business timestamp is not changed by a quota snapshot refresh, and cards translate',async()=>{
    const h=harness('en');h.run(poolLoadSource);const account={id:'account-1',label:'Account',enabled:true,ok:true,last_used:state.last_run,quota:{plan:'Free plan',weekly_used_pct:15,weekly_reset:'Sep 30',checked_at:state.last_run+1000}};
    h.setRespond(async()=>({accounts:[account],stats:{total:1,healthy:1}}));await h.context.loadPool();assert.equal(h.elements.get('poolBody').querySelectorAll('article').length,1);assert.match(h.elements.get('poolBody').textContent,/Last quota query/);assert.match(h.elements.get('poolBody').textContent,/Weekly quota remaining/);assert.equal(h.run('accounts[0].last_used'),state.last_run);
    for(const copy of ['套餐','周剩余额度','额度重置','额外剩余额度','额外剩余','额外额度到期','上游原文','Cookie 有效期','最近保活成功','刷新额度','账号卡片'])assert.notEqual(h.context.t(copy),copy,'Missing English '+copy);
  });
  await test('line parser accepts manual email/phone and OTP URL + label without inventing password login',()=>{
    const h=harness();const result=h.run("parseLoginAccounts('one@example.com | https://mail.invalid/inbox?token=secret | one\\n+15551234567\\ntwo@example.com || manual')");
    assert.equal(result.length,3);assert.equal(result[0].otp_url,'https://mail.invalid/inbox?token=secret');assert.equal(result[1].account,'+15551234567');assert.equal(result[2].label,'manual');
    for(const input of ['',Array(21).fill('a@example.com').join('\n'),'user@example.com | password','a@example.com | https://u:p@mail.invalid','a@example.com | file:///tmp/otp','a@example.com:password','a@example.com | https://mail.invalid | x | extra'])assert.throws(()=>h.context.parseLoginAccounts(input));
    assert.throws(()=>h.context.parseLoginAccounts('a@example.com | secret-password'),error=>!error.message.includes('secret-password'));
  });
  await test('batch creation follows authenticated contract, clears secrets before request, and keeps only safe snapshot fields',async()=>{
    const h=harness();h.activate('login');h.elements.get('loginAccounts').value='one@example.com | https://mail.invalid?token=secret | main';
    h.setRespond(async(path,options)=>{assert.equal(path,'/admin/login/batches');assert.equal(h.elements.get('loginAccounts').value,'');assert.equal(options.headers.Authorization,'Bearer fixture-key');assert.deepEqual(JSON.parse(options.body),{accounts:[{account:'one@example.com',otp_url:'https://mail.invalid?token=secret',label:'main'}]});return {...fixtureBatch('running'),cookie:'TOP_SECRET',otp_url:'https://bad.invalid?token=TOP_SECRET',items:[{...fixtureBatch('running').items[0],cookie:'TOP_SECRET',password:'TOP_SECRET'}]};});
    await h.context.startLoginBatch();assert.equal(h.elements.get('loginAccounts').value,'');assert.equal(h.storageWrites.length,0);assert.doesNotMatch(h.run('JSON.stringify(loginBatch)'),/TOP_SECRET|otp_url|cookie|password/);assert.equal(h.timers.size,1);assert.ok(h.elements.get('loginStart').disabled);
  });
  await test('rejected batch clears textarea and redacts URL, auth, OTP and cookie values from errors',async()=>{
    const h=harness();h.elements.get('loginAccounts').value='one@example.com | https://mail.invalid?token=verysecret';
    h.setRespond(async()=>{throw new Error('https://mail.invalid?token=verysecret Bearer fixture-key OTP=123456 cookie=hatch_sess=verysecret');});
    await h.context.startLoginBatch();assert.equal(h.elements.get('loginAccounts').value,'');assert.doesNotMatch(h.elements.get('loginMessage').textContent,/verysecret|fixture-key|123456/);assert.equal(h.elements.get('loginStart').disabled,false);
  });
  await test('manual OTP form survives polling, posts index/code only and clears code even on failure',async()=>{
    const h=harness();h.activate('login');h.run('loginBatch=normalizeLoginBatch('+JSON.stringify(fixtureBatch('running'))+');renderLoginBatch()');
    const form=h.elements.get('loginBody').querySelector('form'),input=form.querySelector('input');input.value='123456';input.selectionStart=2;input.selectionEnd=4;h.context.document.activeElement=input;h.run('renderLoginBatch()');assert.equal(h.elements.get('loginBody').querySelector('form'),form);assert.equal(input.value,'123456');assert.ok(input.focused);assert.equal(input.selectionStart,2);assert.equal(input.selectionEnd,4);
    h.setRespond(async(path,options)=>{if(path.endsWith('/otp')){assert.equal(input.value,'');assert.deepEqual(JSON.parse(options.body),{index:0,code:'123456'});return {};}return fixtureBatch('completed');});
    await h.context.submitLoginOTP(0,form);assert.equal(input.value,'');assert.equal(h.run('loginBatch.status'),'completed');assert.equal(h.timers.size,0);
    input.value='654321';h.setRespond(async()=>{throw new Error('otp=654321');});await h.context.submitLoginOTP(0,form);assert.equal(input.value,'');assert.doesNotMatch(h.elements.get('loginMessage').textContent,/654321/);
    const count=h.requests.length;input.value='12x';await h.context.submitLoginOTP(0,form);assert.equal(h.requests.length,count);assert.equal(input.value,'');
  });
  await test('manual/captcha is explicit, not succeeded; malicious snapshot strings are escaped',()=>{
    const h=harness();h.run('loginBatch=normalizeLoginBatch('+JSON.stringify({id:'bad<id>',status:'completed',items:[{index:0,status:'needs_manual',stage:'captcha',account:'<img src=x onerror=evil()>',label:'<script>evil()</script>',error:'Captcha https://mail.invalid?token=secret cookie=secret'}]})+');renderLoginBatch()');
    const body=h.elements.get('loginBody');assert.match(body.textContent,/需要人工处理/);assert.doesNotMatch(body.textContent,/token=secret|cookie=secret/);assert.equal(body.querySelectorAll('span').some(span=>span.textContent==='登录成功'),false);assert.equal(body.querySelectorAll('img').length,0);assert.equal(body.querySelectorAll('script').length,0);assert.equal(body.querySelectorAll('form').length,0);
  });
  await test('cancellation targets encoded batch ID and clears all input',async()=>{
    const h=harness();h.run('loginBatch=normalizeLoginBatch('+JSON.stringify({...fixtureBatch('running'),id:'id/with space'})+');renderLoginBatch()');h.elements.get('loginAccounts').value='sensitive';
    h.setRespond(async(path)=>{assert.equal(path,'/admin/login/batches/id%2Fwith%20space/cancel');return fixtureBatch('cancelled');});await h.context.cancelLoginBatch();assert.equal(h.elements.get('loginAccounts').value,'');assert.equal(h.run('loginBatch.status'),'cancelled');assert.equal(h.timers.size,0);
  });
  await test('poller stops while hidden/terminal, bounds five failures and 300 attempts, and never overlaps',async()=>{
    const h=harness();h.activate('login');let calls=0;h.run('loginBatch=normalizeLoginBatch('+JSON.stringify(fixtureBatch('running'))+')');
    h.setRespond(async()=>{calls++;return fixtureBatch('running');});h.run('loginPoller.schedule()');assert.equal(h.timers.size,1);h.context.document.hidden=true;h.events.get('visibilitychange')();assert.equal(h.timers.size,0);h.run('loginPoller.schedule()');assert.equal(h.timers.size,0);
    h.context.document.hidden=false;h.events.get('visibilitychange')();await h.fireTimer();assert.equal(calls,1);h.setRespond(async()=>fixtureBatch('completed'));await h.fireTimer();assert.equal(h.timers.size,0);
    const failures=harness();failures.activate('login');failures.run('loginBatch=normalizeLoginBatch('+JSON.stringify(fixtureBatch('running'))+')');failures.setRespond(async()=>{throw new Error('offline');});for(let i=0;i<6;i++)await failures.run('loginPoller.run()');assert.equal(failures.requests.length,5);assert.match(failures.elements.get('loginMessage').textContent,/自动刷新已停止/);assert.equal(failures.timers.size,0);
    const bounded=harness();let refreshes=0,stops=0,release;bounded.context.canRun=true;bounded.context.work=true;bounded.context.refresh=async()=>{refreshes++;};bounded.context.stop=()=>stops++;bounded.run('globalThis.poll=boundedPoller(()=>canRun,()=>work,refresh,stop)');for(let i=0;i<301;i++)await bounded.run('poll.run()');assert.equal(refreshes,300);assert.ok(stops>0);assert.equal(bounded.timers.size,0);
    bounded.run('poll.reset()');bounded.context.refresh=()=>new Promise(resolve=>{release=resolve;});bounded.run('poll=boundedPoller(()=>canRun,()=>work,refresh,stop)');const first=bounded.run('poll.run()');const second=bounded.run('poll.run()');await second;release();await first;
  });
  await test('request timeout aborts fetch and is caught without a raw rejection',async()=>{
    const h=harness();h.setRespond((path,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('Aborted')),{once:true})));
    const pending=h.context.automationApi('/slow');const timer=[...h.timers].find(([,value])=>value.delay===20000);assert.ok(timer);h.timers.delete(timer[0]);timer[1].fn();await assert.rejects(pending,/请求超时/);assert.equal(h.timers.size,0);
  });
  await test('maintenance reads exact settings/state, validates integer range and saves a bare five-field object',async()=>{
    const h=harness();h.setRespond(async(path,options)=>{assert.equal(path,'/admin/maintenance/settings');if(options.method==='PUT'){const body=JSON.parse(options.body);assert.deepEqual(body,{...settings,keepalive_interval_minutes:1,quota_interval_minutes:10080,include_disabled:true});return {settings:body,state};}return {settings,state};});
    await h.context.loadMaintenanceSettings();assert.equal(h.elements.get('maintenanceKeepaliveInterval').value,30);assert.equal(h.elements.get('maintenanceSave').disabled,false);assert.notEqual(h.elements.get('maintenanceLastKeepalive').textContent,'—');assert.notEqual(h.elements.get('maintenanceLastQuota').textContent,'—');
    for(const value of ['0','10081','1.5','']){h.elements.get('maintenanceKeepaliveInterval').value=value;await h.context.saveMaintenanceSettings();}assert.equal(h.requests.length,1);
    h.elements.get('maintenanceKeepaliveInterval').value='1';h.elements.get('maintenanceQuotaInterval').value='10080';h.elements.get('maintenanceIncludeDisabled').checked=true;await h.context.saveMaintenanceSettings();assert.equal(h.requests.at(-1).options.method,'PUT');assert.match(h.elements.get('maintenanceMessage').textContent,/立即生效/);assert.match(h.elements.get('keepaliveBadge').textContent,/10080/);
  });
  await test('maintenance 202 queues once, polls status until idle and preserves drafts',async()=>{
    const h=harness();h.activate('maintenance');h.run('populateMaintenanceSettings('+JSON.stringify({settings,state})+')');h.elements.get('maintenanceQuotaInterval').value='123';
    let checks=0;h.setRespond(async(path)=>path==='/admin/maintenance/run'?{__status:202,status:'queued'}:{settings,...state,running:checks++===0});
    await h.context.runMaintenance();await h.context.runMaintenance();assert.equal(h.requests.length,1);assert.equal(h.requests[0].path,'/admin/maintenance/run');assert.ok(h.elements.get('maintenanceRun').disabled);await h.fireTimer();await h.fireTimer();assert.equal(h.timers.size,0);assert.equal(h.elements.get('maintenanceQuotaInterval').value,'123');assert.equal(h.elements.get('maintenanceRun').disabled,false);
  });
  await test('diagnostic JSON and log rows redact nested credentials and escape every external field',async()=>{
    const h=harness();const malicious={time:state.last_run,level:'<img src=x onerror=evil()>',event:'<script>evil()</script>',stage:'<b>stage</b>',task_id:'<task>',account_id:'<account>',message:'safe https://mail.invalid/?token=TOP_SECRET',details:{Cookie:'TOP_SECRET',authorization:'TOP_SECRET',password:'TOP_SECRET',otp:'123456',url:'https://mail.invalid/?token=TOP_SECRET',traceback:'line 2 <script>evil()</script>'}};
    h.setRespond(async(path)=>{assert.equal(path,'/admin/logs?limit=100&offset=0');return {entries:[malicious],total:1,offset:0,next_offset:null};});await h.context.loadLogs();const body=h.elements.get('logsBody');assert.equal(body.querySelectorAll('img').length,0);assert.equal(body.querySelectorAll('script').length,0);assert.doesNotMatch(body.textContent,/TOP_SECRET|123456/);assert.match(body.textContent,/REDACTED/);assert.match(body.textContent,/traceback/);assert.doesNotMatch(h.context.safeAutomationText('JSON {"password":"TOP_SECRET"} access_token=TOP_SECRET Cookie: hatch_sess=TOP_SECRET'),/TOP_SECRET/);
  });
  await test('log persistence health failures are explicit rather than a false empty-log success',async()=>{
    const h=harness();h.setRespond(async()=>({entries:[],total:0,offset:0,next_offset:null,health:{ok:false,error_type:'PermissionError'}}));await h.context.loadLogs();assert.match(h.elements.get('logsMessage').textContent,/日志持久化异常/);assert.match(h.elements.get('logsMessage').textContent,/PermissionError/);
  });
  await test('actual journal reason-only records are visible in log summaries',async()=>{
    const h=harness();h.setRespond(async()=>({entries:[{time:state.last_run,level:'error',event:'extract.failed',reason:'Reason from journal',traceback:[{file:'engine.py',line:12}]}],total:1,offset:0,next_offset:null}));await h.context.loadLogs();assert.match(h.elements.get('logsBody').querySelector('div[class="automation-message"]').textContent,/Reason from journal/);
  });
  await test('log filters use encoded task endpoint and pagination follows server next_offset',async()=>{
    const h=harness();h.elements.get('logsTaskId').value='task/a ?';h.elements.get('logsAccountId').value='account & b';h.elements.get('logsLevel').value='error';
    h.setRespond(async(path)=>{const url=new URL(path,'http://fixture.invalid');assert.equal(url.pathname,'/admin/tasks/task%2Fa%20%3F/logs');assert.equal(url.searchParams.get('account_id'),'account & b');assert.equal(url.searchParams.get('level'),'error');const offset=Number(url.searchParams.get('offset'));return {entries:[],total:210,offset,next_offset:offset===0?100:offset===100?200:null};});
    await h.context.loadLogs();await h.context.nextLogsPage();assert.equal(h.elements.get('logsOffset').textContent,'100');await h.context.previousLogsPage();assert.equal(h.elements.get('logsOffset').textContent,'0');assert.ok(h.elements.get('logsPrev').disabled);
    h.context.openTaskLogs('task-2');assert.equal(h.elements.get('logsTaskId').value,'task-2');assert.equal(h.elements.get('logsAccountId').value,'');assert.ok(h.elements.get('tab-logs').classList.contains('active'));
  });
  await test('log refresh ignores aborted stale responses and errors are handled',async()=>{
    const h=harness();let release;h.setRespond(async()=>new Promise(resolve=>{release=resolve;}));const first=h.context.loadLogs();await flush();h.setRespond(async()=>({entries:[],total:0,offset:0,next_offset:null}));await h.context.loadLogs();release({entries:[{message:'STALE'}],total:1,offset:0});await first;assert.doesNotMatch(h.elements.get('logsBody').textContent,/STALE/);h.setRespond(async()=>{throw new Error('https://mail.invalid?token=SECRET');});await h.context.loadLogs();assert.match(h.elements.get('logsMessage').textContent,/日志加载失败/);assert.doesNotMatch(h.elements.get('logsMessage').textContent,/SECRET/);
  });
  await test('task errors retain full safe details and log buttons without inline untrusted handlers or unsafe result URLs',async()=>{
    const h=harness();h.activate('tasks');const error='Synthetic failure '+'.'.repeat(200)+' full-tail cookie=SECRET';h.setRespond(async()=>({tasks:[{id:"task'\"><img src=x>",kind:'image',prompt:'<script>bad()</script>',status:'failed',error,elapsed:'<img>',created_at:state.last_run},{id:'unsafe-url',status:'completed',result:{url:'javascript:evil()'}},{id:'safe-url',status:'completed',result:{url:'/media/fixture.png',bytes:12}}]}));
    await h.context.loadTasks();const body=h.elements.get('taskBody');assert.match(body.textContent,/full-tail/);assert.doesNotMatch(body.textContent,/SECRET/);assert.equal(body.querySelectorAll('img').length,0);assert.equal(body.querySelectorAll('script').length,0);assert.equal(body.querySelectorAll('button[data-task-logs]').length,3);assert.doesNotMatch(body.innerHTML,/onclick=|javascript:/);assert.equal(h.timers.size,0);assert.equal(h.context.safeTaskResultURL('data:text/html,bad'),'');
    h.setRespond(async()=>({tasks:[{id:'working',status:'processing'}]}));await h.context.loadTasks();assert.equal(h.timers.size,1);h.setRespond(async()=>({tasks:[{id:'working',status:'succeeded'}]}));await h.fireTimer();assert.equal(h.timers.size,0);
  });
  await test('new Chinese UI copy has English translations and both language modes render statuses',()=>{
    const zh=harness(),en=harness('en');for(const text of ['批量登录','维护设置','诊断日志','启用自动保活','最近业务使用','最近保活','最近额度查询','保存维护设置','提交验证码','查看日志','需要人工处理验证，未确认登录成功。','自动刷新已停止，请手动刷新状态。'])assert.notEqual(en.context.t(text),text,text+' missing English');
    // Advanced login contains rich data-i18n blocks translated as whole HTML, not leaf strings.
    const staticSection=html.slice(html.indexOf('<!-- ==================== 维护设置'),html.indexOf('<!-- ==================== 任务记录'));
    for(const part of staticSection.matchAll(/>([^<>]+)</g)){const text=decode(part[1]).trim();if(/[\u3400-\u9fff]/.test(text))assert.notEqual(en.context.t(text),text,'Missing English: '+text);}
    assert.equal(zh.context.loginStatus('waiting_otp'),'等待验证码');assert.equal(en.context.loginStatus('waiting_otp'),'Waiting for code');assert.equal(en.context.loginStatus('needs_manual'),'Manual action required');
  });
  await test('remaining balance width, text and continuous green-to-red color use the same validated percentage',()=>{
    const h=harness();
    for(const [used,remaining,hue] of [[0,100,120],[25,75,90],[50,50,60],[75,25,30],[95,5,6],[100,0,0],[12.5,87.5,105]]){
      assert.equal(h.context.quotaRemaining(used),remaining);
      const output=h.context.quotaProgress('周剩余额度',used);
      assert.ok(output.includes('width:'+remaining+'%'));
      assert.ok(output.includes('aria-valuenow="'+remaining+'"'));
      assert.ok(output.includes('hsl('+hue+',70%,50%)'));
      assert.ok(output.includes(remaining+'%'));
    }
    assert.match(h.context.quotaSummary({plan:'Free plan',weekly_used_pct:95,extra_used_pct:10}),/周剩余额度: 5%/);
    assert.match(h.context.quotaSummary({weekly_used_pct:null}),/未知/);
    const en=harness('en');assert.match(en.context.quotaSummary({weekly_used_pct:25}),/Weekly quota remaining: 75%/);
  });
  await test('login has one primary tab and advanced Cookie tools, without the old homepage upgrade box',async()=>{
    assert.doesNotMatch(html,/data-t="cookie"|id="tab-cookie"|id="repoCommitsBox"|id="btnRepoPull"/);
    assert.doesNotMatch(script,/function loadRepoStatus|function upgradeNow|promptUpgrade|api\.github\.com\/repos|\/admin\/update\/upgrade/);
    const start=html.indexOf('id="tab-login"'),end=html.indexOf('id="tab-maintenance"');
    assert.ok(start>0&&end>start);
    for(const id of ['loginAccounts','loginAdvanced','newCookie','mCookie','extensionUpgradeNotice']) {
      assert.ok(html.slice(start,end).includes('id="'+id+'"'),id+' must be inside Account login');
      assert.equal([...html.matchAll(new RegExp('id="'+id+'"','g'))].length,1,'Duplicate '+id);
    }
    const h=await bootPage('zh');
    for(const id of ['loginAccounts','newCookie','newLabel','mCookie','mLabel'])h.elements.get(id).value='sensitive-input';
    h.context.goTab('cookie');assert.ok(h.elements.get('tab-login').classList.contains('active'));
    h.context.goTab('pool');for(const id of ['loginAccounts','newCookie','newLabel','mCookie','mLabel'])assert.equal(h.elements.get(id).value,'');
    assert.ok(!h.requests.some(x=>/update\/check|repo\/status/.test(x.path)),'Version checks must not run on boot or tabs');
  });
  await test('daily cleanup reads and saves strict independent settings without calling a deletion endpoint',async()=>{
    const h=harness();h.setRespond(async(path,options)=>{
      assert.equal(path,'/admin/cleanup/settings');
      if(options.method==='PUT')return {settings:JSON.parse(options.body),state:cleanupState};
      return {settings:cleanupDefaults,state:cleanupState};
    });
    await h.context.loadCleanupSettings();assert.equal(h.elements.get('cleanupEnabled').checked,false);
    assert.equal(h.elements.get('cleanupSave').disabled,false);
    for(const [id,value] of [['cleanupMediaMax','0'],['cleanupMediaTarget','10240'],['cleanupCheckTime','25:00'],['cleanupTimezone','Invalid/Moon']]){
      const old=h.elements.get(id).value;h.elements.get(id).value=value;
      await h.context.saveCleanupSettings();h.elements.get(id).value=old;
    }
    assert.equal(h.requests.length,1);
    h.elements.get('cleanupCheckTime').value='04:30';h.elements.get('cleanupMediaMax').value='100';h.elements.get('cleanupMediaTarget').value='80';
    await h.context.saveCleanupSettings();
    const sent=JSON.parse(h.requests.at(-1).options.body);assert.deepEqual(sent,{...cleanupDefaults,check_time:'04:30',media_max_mb:100,media_target_mb:80});
    assert.equal(h.requests.at(-1).options.method,'PUT');assert.ok(h.requests.every(x=>x.path==='/admin/cleanup/settings'));
    assert.match(h.elements.get('cleanupMessage').textContent,/下一轮后台检查/);
    assert.notEqual(harness('en').context.t('保存每日清理设置'),'保存每日清理设置');
  });
  await test('cleanup results preserve partial failures, protected files and the configured time zone',()=>{
    const h=harness();h.context.populateCleanupSettings({settings:cleanupDefaults,state:{...cleanupState,last_checked_date:'2026-11-14',last_run:'2026-11-13T20:00:00+00:00',next_run:'2026-11-14T19:00:00+00:00',last_result:{status:'partial',media:{status:'partial',bytes_removed:1024,files_removed:1,target_not_reached:true},logs:{status:'error',errors:[{error_type:'PermissionError'}]}}}});
    assert.equal(h.elements.get('cleanupLastDate').textContent,'2026-11-14');assert.match(h.elements.get('cleanupLastRun').textContent,/Asia\/Shanghai/);
    assert.match(h.elements.get('cleanupResult').textContent,/未达到目标占用/);assert.match(h.elements.get('cleanupResult').textContent,/PermissionError/);
  });
  await test('release checks only execute on explicit action, are authenticated, and distinguish no published release',async()=>{
    const h=harness();assert.equal(h.requests.length,0);
    h.setRespond(async(path,options)=>{assert.equal(path,'/admin/update/check');assert.equal(options.headers.Authorization,'Bearer fixture-key');return {local_version:'1.6.12',remote_version:null,status:'no_releases',has_update:false,up_to_date:null};});
    await h.context.checkNewVersion();assert.equal(h.requests.length,1);assert.match(h.elements.get('versionMessage').textContent,/尚未发布版本/);
    h.setRespond(async()=>({local_version:'1.6.12',remote_version:'v1.7.0',has_update:true}));await h.context.checkNewVersion();assert.match(h.elements.get('versionMessage').textContent,/手动更新/);
    h.setRespond(async()=>{throw Error('network token=hidden');});await h.context.checkNewVersion();assert.doesNotMatch(h.elements.get('versionMessage').textContent,/hidden/);
    assert.ok(h.requests.every(x=>x.path==='/admin/update/check'&&!x.options.method));
  });
  await test('studio geometry computes coherent ratios and dimensions for every preset and validates custom inputs',()=>{
    const h=harness(),compute=h.context.computeGenerationGeometry;
    for(const kind of ['image','video'])for(const ratio of ['auto','1:1','16:9','9:16','4:3','3:4','21:9','custom'])for(const res of ['1K','2K']){
      const g=compute(kind,ratio,res,'7:5');const [a,b]=g.aspect_ratio.split(':').map(Number);
      assert.equal(g.width*b,g.height*a);assert.equal(g.size,g.resolution);assert.equal(g.size,g.width+'x'+g.height);
      assert.ok(g.width>=64&&g.height>=64&&Math.max(g.width,g.height)<=(res==='2K'?2048:1024));
    }
    assert.equal(compute('image','16:9','1K').size,'1024x576');
    assert.equal(compute('video','9:16','2K').size,'1152x2048');
    for(const size of ['1024x1024','1920x1080','1080x1920','1280x720','720x1280','2048x2048'])assert.equal(compute('image','auto',size).size,size);
    assert.equal(compute('image','dimensions','custom','',8192,64).aspect_ratio,'128:1');
    for(const pair of [[63,64],[64,8193],[100.5,200],['',400],[Infinity,200]])assert.throws(()=>compute('video','dimensions','custom','',...pair));
    for(const value of ['0:4','1:0','-1:2','bogus','1:99999'])assert.throws(()=>compute('image','custom','1K',value));
    h.elements.get('imgResolution').value='1920x1080';h.context.syncGenerationGeometry('image','resolution');assert.equal(h.elements.get('imgRatio').value,'dimensions');assert.equal(h.elements.get('imgWidth').value,1920);
    h.elements.get('imgRatio').value='3:4';h.context.syncGenerationGeometry('image','ratio');assert.equal(h.elements.get('imgResolution').value,'1K');assert.match(h.elements.get('imgGeometry').textContent,/3:4.*768x1024/);
  });
  await test('references encode once in backend order and payload geometry is identical for image editing and video',async()=>{
    const h=harness(),file=(name,type,size=10)=>({name,type,size}),add=async(kind,group,files)=>h.context.addGenerationFiles(kind,group,{files,value:'secret path'});
    await add('image','images',[file('first.png','image/png'),file('<script>.png','image/png')]);await add('image','audios',[file('audio.wav','audio/wav')]);await add('image','files',[file('brief.pdf','application/pdf')]);
    assert.deepEqual(h.fileReads,[]);assert.equal(h.elements.get('imgReferenceList').querySelectorAll('script').length,0);
    assert.match(h.elements.get('imgReferenceList').textContent,/\[图1\].*first.png.*\[图2\]/);assert.match(h.elements.get('imgReferenceList').textContent,/\[音频1\]/);
    const payload=await h.context.generationPayload('image');await h.context.generationPayload('image');
    assert.deepEqual(h.fileReads,['first.png','<script>.png','audio.wav','brief.pdf']);assert.equal(payload.images.length,2);assert.equal(payload.audios.length,1);assert.equal(payload.files.length,1);assert.equal(payload.videos.length,0);
    assert.equal(payload.model,'muse-image');assert.equal(payload.size,payload.resolution);assert.equal(payload.aspect_ratio,'1:1');assert.deepEqual(Object.keys(payload).slice(-4),['images','videos','audios','files']);
    assert.equal(h.storageWrites.length,0);const firstId=h.run('generationState.image.references.images[0].id');h.context.removeGenerationReference('image',firstId);assert.equal(h.revoked.length,1);
    h.context.clearGenerationReferences('image');assert.equal(h.run('referenceEntries("image").length'),0);assert.equal(h.revoked.length,3);assert.equal(h.elements.get('imgReferenceList').textContent,'');
    await add('video','images',[file('frame.webp','image/webp')]);await add('video','audios',[file('voice.mp3','audio/mpeg')]);const video=await h.context.generationPayload('video');assert.equal(video.duration,5);assert.equal(video.aspect_ratio,'16:9');assert.equal(video.size,video.resolution);
  });
  await test('reference prevalidation blocks count, empty/oversize/type/total without reading a single byte',async()=>{
    const h=harness(),file=(name,size,type='image/png')=>({name,size,type});
    for(const files of [[file('huge.png',40*1024*1024+1)],[file('empty.png',0)],[file('bad.exe',1,'application/x-executable')],Array(11).fill(file('small.png',1)),[file('a.png',40*1024*1024),file('b.png',40*1024*1024),file('c.png',21*1024*1024)]]){
      const input={files,value:'private path'};await h.context.addGenerationFiles('image','images',input);assert.equal(input.value,'');assert.equal(h.run('referenceEntries("image").length'),0);assert.match(h.elements.get('imgReferenceMessage').textContent,/失败/);
    }
    assert.equal(h.fileReads.length,0);assert.equal(h.requests.length,0);assert.equal(h.storageWrites.length,0);
    await h.context.addGenerationFiles('image','videos',{files:[file('clip.mp4',1,'video/mp4')],value:''});assert.equal(h.run('referenceEntries("image").length'),0);
    const adding=h.context.addGenerationFiles('video','videos',{files:[file('clip.mp4',1,'video/mp4')],value:''});const timer=[...h.timers].find(([,timer])=>timer.delay===10000);assert.ok(timer);h.timers.delete(timer[0]);timer[1].fn();await adding;assert.equal(h.run('referenceEntries("video").length'),0);assert.equal(h.revoked.at(-1),'blob:fixture/clip.mp4');
  });
  await test('generation POST is single-shot, authenticated, task IDs are encoded, and terminal success releases refs',async()=>{
    const h=harness();h.activate('play');await h.context.addGenerationFiles('image','images',{files:[{name:'a.png',type:'image/png',size:5}],value:''});
    let release;h.setRespond((path,options)=>{assert.equal(path,'/v1/images/tasks');assert.equal(options.headers.Authorization,'Bearer fixture-key');const payload=JSON.parse(options.body);assert.equal(payload.images.length,1);assert.equal(payload.size,payload.resolution);return new Promise(resolve=>release=resolve);});
    const first=h.context.runImage();await flush();await h.context.runImage();assert.equal(h.requests.length,1);release({id:'task/a ?#',status:'queued'});await first;assert.ok(h.elements.get('imgBtn').disabled);await h.context.runImage();assert.equal(h.requests.length,1);
    h.setRespond(async path=>{assert.equal(path,'/v1/images/tasks/task%2Fa%20%3F%23');return {id:'task/a ?#',status:'completed',data:[{url:'/v1/media/result.webp',bytes:100}]};});await h.fireTimer();assert.equal(h.elements.get('imgBtn').disabled,false);assert.match(h.elements.get('imgRes').textContent,/生成成功/);assert.equal(h.elements.get('imgRes').querySelectorAll('img').length,1);assert.equal(h.timers.size,0);assert.equal(h.revoked.length,1);assert.equal(h.run('referenceEntries("image").length'),0);
  });
  await test('video failures and uncertain creation are explicit and cannot trigger automatic duplicate posts',async()=>{
    const h=harness();h.activate('play');h.setRespond(async(path,options)=>options.method==='POST'?{id:'video/1',status:'queued'}:{id:'video/1',status:'failed',error:'upstream failed cookie=NEVER_SHOW'});
    await h.context.runVideo();await h.fireTimer();assert.equal(h.elements.get('vidBtn').disabled,false);assert.match(h.elements.get('vidRes').textContent,/生成失败/);assert.doesNotMatch(h.elements.get('vidRes').textContent,/NEVER_SHOW/);assert.equal(h.timers.size,0);
    const uncertain=harness();uncertain.activate('play');uncertain.setRespond(async()=>{throw Error('offline token=NEVER_SHOW');});await uncertain.context.runVideo();await uncertain.context.runVideo();assert.equal(uncertain.requests.length,1);assert.ok(uncertain.elements.get('vidBtn').disabled);assert.match(uncertain.elements.get('vidRes').textContent,/提交状态未知/);assert.doesNotMatch(uncertain.elements.get('vidRes').textContent,/NEVER_SHOW/);
    await uncertain.context.resumeGeneration('video');assert.equal(uncertain.elements.get('vidBtn').disabled,false);
    const invalid=harness();invalid.elements.get('vidDur').value='0';await invalid.context.runVideo();assert.equal(invalid.requests.length,0);assert.equal(invalid.elements.get('vidBtn').disabled,false);
  });
  await test('generation polling stops when hidden, after five failures and deadline without unlocking resubmission',async()=>{
    const h=harness();h.activate('play');h.setRespond(async()=>({id:'busy',status:'queued'}));await h.context.runImage();assert.equal(h.timers.size,1);h.context.document.hidden=true;h.events.get('visibilitychange')();assert.equal(h.timers.size,0);h.context.document.hidden=false;h.events.get('visibilitychange')();assert.equal(h.timers.size,1);
    h.setRespond(async()=>{throw Error('offline');});for(let i=0;i<5;i++)await h.fireTimer();assert.equal(h.timers.size,0);assert.ok(h.elements.get('imgBtn').disabled);assert.match(h.elements.get('imgRes').textContent,/自动刷新已停止/);const postCount=h.requests.filter(r=>r.options.method==='POST').length;await h.context.runImage();assert.equal(h.requests.filter(r=>r.options.method==='POST').length,postCount);
    h.run('generationState.image.started=Date.now()-16*60*1000;generationState.image.paused=false');await h.run('generationPollers.image.run()');assert.equal(h.timers.size,0);
  });
  await test('quota reset countdown requires exact seconds, calendar dates never fabricate an hour, raw text stays raw',()=>{
    const h=harness(),now=Date.parse('2026-11-14T02:00:00Z');
    const exact=h.context.quotaResetText({weekly_reset_at:now/1000+2*86400+3*3600},now);assert.match(exact,/还剩 2 天 3 小时/);
    assert.match(h.context.quotaResetText({weekly_reset_at:now/1000-1},now),/已到重置时间/);
    const date=h.context.quotaResetText({weekly_reset_date:'2026-11-16',weekly_reset_timezone:'Asia/Shanghai',weekly_reset:'11月16日'},now);assert.match(date,/还有 2 天（日期）/);assert.doesNotMatch(date,/小时|00:00/);
    assert.equal(h.context.quotaResetText({weekly_reset:'Sep 30'},now),'Sep 30');assert.equal(h.context.quotaResetText({weekly_reset:'2026-11-16'},now),'2026-11-16');assert.equal(h.context.quotaResetText({weekly_reset_at:'1794736800',weekly_reset:'raw'},now),'raw');
  });
  await test('refresh all forces one queue request, reloads accounts on completion and reports failed and busy operations',async()=>{
    const h=harness();h.activate('pool');let reloads=0;h.context.loadPool=async()=>reloads++;
    const summary={refreshed:[{id:'a',ok:true,operation:'keepalive'},{id:'a',ok:false,error:'bad cookie=hidden',operation:'quota'}],skipped:[{id:'b',busy:true,error:'busy'}]};
    h.setRespond(async(path,options)=>{if(path==='/admin/maintenance/run'){assert.deepEqual(JSON.parse(options.body),{force_refresh:true});return {__status:202,status:'queued'};}return {...state,last_result:summary,running:false};});
    await h.context.runMaintenance();await h.context.runMaintenance();assert.equal(h.requests.length,1);assert.ok(h.elements.get('btnKeepaliveAll').disabled);await h.fireTimer();assert.equal(reloads,1);assert.equal(h.elements.get('btnKeepaliveAll').disabled,false);assert.match(h.elements.get('poolRefreshMessage').textContent,/成功 1.*失败 1.*忙碌跳过 1/);assert.doesNotMatch(h.elements.get('poolRefreshMessage').textContent,/hidden/);
    h.setRespond(async()=>{throw Error('server rejected token=hidden');});await h.context.runMaintenance();assert.match(h.elements.get('poolRefreshMessage').textContent,/全部刷新失败/);assert.equal(h.elements.get('btnKeepaliveAll').disabled,false);
  });
  await test('logs and task rows show explicit assigned label/id/attempt, never borrow an unrelated account',async()=>{
    const h=harness();h.run('accounts=[{id:"wrong",label:"WrongAccount"}]');h.setRespond(async()=>({entries:[{account_id:'acc-1',account_label:'Label<img src=x>',attempt:2,task_id:'t1',message:'fixture'},{task_id:'t2'}],total:2,offset:0}));
    await h.context.loadLogs();const body=h.elements.get('logsBody');assert.match(body.textContent,/使用账号.*Label<img src=x>.*acc-1/);assert.match(body.textContent,/尝试 2/);assert.match(body.textContent,/未分配账号/);assert.doesNotMatch(body.textContent,/WrongAccount/);assert.equal(body.querySelectorAll('img').length,0);
  });
  await test('task details fetch encoded IDs and only projected safe media with cleanup, prompt escape and account metadata',async()=>{
    const h=harness();const task={id:'task/a ?',status:'completed',kind:'video',prompt:'<img src=x onerror=bad()>',api_prompt:'<script>upstream</script>',account_id:'a1',account_label:'Main',attempts:[{attempt:1,account_id:'a1',account_label:'Main'}],generation:{size:'1920x1080',aspect_ratio:'16:9',resolution:'1920x1080',duration:5,reference_counts:{images:2}},media:{kind:'video',url:'/v1/media/fixture.mp4',exists:true,bytes:20,width:1920,height:1080}};
    h.setRespond(async path=>{assert.equal(path,'/admin/tasks/task%2Fa%20%3F');return {task};});await h.context.openTaskDetail(task.id);const body=h.elements.get('taskDetailBody');assert.equal(body.querySelectorAll('img').length,0);assert.equal(body.querySelectorAll('script').length,0);const player=body.querySelector('video');assert.ok(player);assert.equal(player.getAttribute('preload'),'metadata');assert.equal(player.getAttribute('autoplay'),null);assert.match(body.textContent,/Main.*a1/);assert.match(body.textContent,/1920x1080/);
    h.setRespond(async()=>({task:{...task,media:{...task.media,exists:false,availability:'deleted'}}}));await h.context.openTaskDetail(task.id);assert.equal(body.querySelectorAll('video').length,0);assert.match(body.textContent,/媒体已清理/);
    h.setRespond(async()=>({task:{...task,result:{url:'javascript:evil()'},media:{kind:'image',url:'javascript:evil()',exists:true}}}));await h.context.openTaskDetail(task.id);assert.equal(body.querySelectorAll('img').length,0);assert.doesNotMatch(body.innerHTML,/javascript:/);
    const raw=h.context.taskMediaHTML({kind:'audio',url:'/v1/media/song.mp3',exists:true},true);assert.match(raw,/<audio[^>]*controls[^>]*preload="metadata"/);assert.doesNotMatch(raw,/autoplay/);
    h.context.closeTaskDetail();assert.equal(h.elements.get('taskDetail').hidden,true);assert.equal(body.textContent,'');
  });
  await test('task list never scans the removed media library and details errors are bounded and retained safely',async()=>{
    assert.doesNotMatch(html,/data-t="media"|id="tab-media"|id="mediaGrid"/);assert.doesNotMatch(script,/loadMedia\(|\/admin\/media(?:['"]|\/)/);
    const h=harness();h.activate('tasks');h.setRespond(async()=>({tasks:[{id:'deleted',status:'completed',prompt:'kept',media:{exists:false,availability:'deleted'}},{id:'raw-only',status:'completed',result:{url:'https://untrusted.invalid/raw'}}]}));await h.context.loadTasks();assert.match(h.elements.get('taskBody').textContent,/媒体已清理/);assert.doesNotMatch(h.elements.get('taskBody').innerHTML,/untrusted.invalid/);
    h.setRespond(async()=>{throw Error('not found cookie=hidden');});await h.context.openTaskDetail('missing');assert.match(h.elements.get('taskDetailBody').textContent,/任务详情加载失败/);assert.doesNotMatch(h.elements.get('taskDetailBody').textContent,/hidden/);
    assert.ok(h.requests.every(r=>!r.path.startsWith('/admin/media')));
  });
  await test('pagehide releases local object URLs and new studio/details strings translate in both languages',async()=>{
    for(const language of ['zh','en']){const h=harness(language);await h.context.addGenerationFiles('image','images',{files:[{name:'local.png',type:'image/png',size:2}],value:''});h.events.get('pagehide')();assert.equal(h.revoked.length,1);assert.equal(h.run('referenceEntries("image").length'),0);assert.equal(h.storageWrites.length,0);}
    const h=harness('en');for(const text of ['使用账号','全部刷新','自定义比例','自定义尺寸','分辨率目标','参考素材（可选）','清空参考素材','任务详情','媒体已清理；任务记录仍保留。','详情 / 预览','还剩','天（日期）','提交状态未知，请在任务记录确认结果，勿重复提交。'])assert.notEqual(h.context.t(text),text,text);
  });
  await test('upload cards hide the native picker, expose glass UI hooks and stay fully offline',()=>{
    for(const id of ['imgImageCard','imgAudioCard','imgDocCard','vidImageCard','vidVideoCard','vidAudioCard','vidDocCard'])assert.ok(html.includes('id="'+id+'"'),id+' card missing');
    for(const id of ['imgImageFiles','imgAudioFiles','imgDocumentFiles','vidImageFiles','vidVideoFiles','vidAudioFiles','vidDocumentFiles'])assert.equal([...html.matchAll(new RegExp('id="'+id+'"','g'))].length,1,id+' must stay unique');
    for(const id of ['imgImageCardCount','imgAudioCardCount','imgDocCardCount','vidImageCardCount','vidVideoCardCount','vidAudioCardCount','vidDocCardCount'])assert.ok(html.includes('id="'+id+'"'),id+' count missing');
    assert.doesNotMatch(html,/reference-picker/);
    assert.match(html,/class="upload-input"/);
    assert.match(html,/点击或拖拽文件到此处/);
    assert.match(html,/已选 0 个/);
    for(const rule of [/--glass:rgba\(/,/backdrop-filter:blur\(/,/\.bgfx\{position:fixed/,/linear-gradient\(180deg,#070b14/,/button:hover\{filter:brightness\(1\.08\)/,/--ring:0 0 0 3px/,/\.upload-card\{[^}]*border:1px dashed/,/\.upload-card:hover\{[^}]*box-shadow/,/\.upload-card\.dragover\{/,/\.upload-input\{[^}]*position:absolute[^}]*opacity:0/,/\.detail-view\{[^}]*position:fixed/,/\.reference-list\{[^}]*display:flex/,/@media\(max-width:600px\)/])assert.match(html,rule,'Missing visual rule '+rule);
    assert.doesNotMatch(html,/<link[^>]+href="https?:/);
    assert.doesNotMatch(html,/@import\s+url\(/);
    assert.doesNotMatch(html,/fonts\.googleapis|cdn\.jsdelivr|unpkg\.com|fonts\.gstatic/);
  });
  await test('drag enter highlights the upload card, dragleave clears it and drop queues validated files',async()=>{
    const h=harness(),card=h.elements.get('imgImageCard'),prevented=[];
    const evt=(extra={})=>({preventDefault:()=>prevented.push('preventDefault'),stopPropagation:()=>prevented.push('stopPropagation'),dataTransfer:{files:[],...extra}});
    for(const type of ['dragenter','dragover','dragleave','drop','dragend'])assert.equal(typeof card.listeners[type]?.[0],'function','missing '+type+' listener');
    card.listeners.dragenter[0](evt());
    assert.ok(card.classList.contains('dragover'));
    assert.ok(prevented.length>0);
    const transfer={files:[],dropEffect:'none'};
    card.listeners.dragover[0]({preventDefault(){},dataTransfer:transfer});
    assert.equal(transfer.dropEffect,'copy');
    assert.ok(card.classList.contains('dragover'));
    card.listeners.dragleave[0](evt());
    assert.equal(card.classList.contains('dragover'),false);
    card.listeners.dragenter[0](evt());
    card.listeners.drop[0](evt({files:[{name:'drag.png',type:'image/png',size:12}]}));
    assert.equal(card.classList.contains('dragover'),false);
    await flush();
    assert.equal(h.run('referenceEntries("image").length'),1);
    assert.equal(h.elements.get('imgImageCardCount').textContent,'已选 1 个');
    assert.equal(h.fileReads.length,0);
    card.listeners.drop[0](evt({files:[{name:'bad.exe',type:'application/x-executable',size:4}]}));
    await flush();
    assert.equal(h.run('referenceEntries("image").length'),1);
    assert.match(h.elements.get('imgReferenceMessage').textContent,/失败/);
    const videoCard=h.elements.get('vidImageCard');
    videoCard.listeners.drop[0](evt({files:[{name:'frame.png',type:'image/png',size:3}]}));
    await flush();
    assert.equal(h.run('referenceEntries("video").length'),1);
    assert.equal(h.elements.get('vidImageCardCount').textContent,'已选 1 个');
    assert.equal(h.storageWrites.length,0);
    assert.equal(h.fileReads.length,0);
    assert.doesNotMatch(h.elements.get('imgReferenceList').innerHTML,/onclick=|<script/);
    assert.equal(h.elements.get('imgReferenceList').querySelectorAll('button[data-ref-id]').length,1);
  });
  await test('task detail opens as an independent view and back restores scroll, filters and the log list',async()=>{
    const h=harness(),path='/admin/tasks/'+encodeURIComponent('t/1');
    const task={id:'t/1',status:'completed',kind:'image',prompt:'detail prompt',account_id:'acc-1',account_label:'Main',generation:{size:'1024x1024'},media:{kind:'image',url:'/v1/media/x.png',exists:true,bytes:9}};
    h.setRespond(async requestPath=>{assert.equal(requestPath,path);return {task};});
    h.context.window.scrollY=640;const scrolled=[];h.context.window.scrollTo=(x,y)=>scrolled.push([x,y]);
    h.elements.get('logsTaskId').value='keep-me';h.elements.get('logsAccountId').value='acc-1';h.elements.get('logsLevel').value='error';
    await h.context.openTaskDetail('t/1',{originTab:'logs'});
    assert.equal(h.elements.get('taskDetail').hidden,false);
    assert.equal(h.elements.get('taskListView').hidden,true);
    assert.equal(h.elements.get('taskDetailId').textContent,'t/1');
    assert.match(h.elements.get('taskDetailBody').textContent,/detail prompt/);
    h.context.closeTaskDetail();
    assert.equal(h.elements.get('taskDetail').hidden,true);
    assert.equal(h.elements.get('taskListView').hidden,false);
    assert.equal(h.elements.get('taskDetailBody').textContent,'');
    assert.deepEqual(scrolled,[[0,640]]);
    assert.ok(h.elements.get('tab-logs').classList.contains('active'));
    assert.equal(h.elements.get('logsTaskId').value,'keep-me');
    assert.equal(h.elements.get('logsAccountId').value,'acc-1');
    assert.equal(h.elements.get('logsLevel').value,'error');
    const button={dataset:{taskDetail:'t/1',detailOrigin:'logs'}};
    h.context.taskAction({target:{closest:selector=>selector==='button[data-task-detail]'?button:null}});
    await flush();await flush();
    assert.equal(h.elements.get('taskDetail').hidden,false);
    assert.match(h.elements.get('taskDetailBody').textContent,/detail prompt/);
    assert.equal(h.requests.at(-1).path,path);
    h.context.closeTaskDetail();
    assert.equal(h.elements.get('taskDetail').hidden,true);
    assert.ok(h.elements.get('tab-logs').classList.contains('active'));
    assert.equal(h.elements.get('logsTaskId').value,'keep-me');
    assert.equal(h.requests.filter(request=>request.path===path).length,2);
  });
  await test('task detail hash history, browser back and Esc close once without double unwinding',async()=>{
    const h=harness(),pushes=[],backs=[];
    h.context.history={pushState:(state,title,url)=>pushes.push({state,title,url}),back:()=>backs.push('back')};
    h.setRespond(async()=>({task:{id:'hash-1',status:'completed',kind:'image',prompt:'hash prompt',media:null}}));
    await h.context.openTaskDetail('hash-1');
    assert.equal(pushes.length,1);
    assert.equal(pushes[0].state.muse2apiTaskDetail,'hash-1');
    assert.deepEqual(Object.keys(pushes[0].state),['muse2apiTaskDetail']);
    assert.equal(pushes[0].title,'');
    assert.match(pushes[0].url,/#task=hash-1$/);
    h.events.get('popstate')();
    assert.equal(h.elements.get('taskDetail').hidden,true);
    assert.equal(backs.length,0);
    await h.context.openTaskDetail('hash-1');
    assert.equal(pushes.length,2);
    h.events.get('keydown')({key:'Escape',preventDefault(){}});
    assert.equal(h.elements.get('taskDetail').hidden,true);
    assert.equal(backs.length,1);
    h.events.get('keydown')({key:'Escape',preventDefault(){}});
    h.events.get('popstate')();
    assert.equal(backs.length,1);
    await h.context.openTaskDetail('hash-2');
    assert.equal(h.elements.get('taskDetail').hidden,false);
    h.events.get('keydown')({key:'Enter'});
    assert.equal(h.elements.get('taskDetail').hidden,false);
    h.context.closeTaskDetail();
    assert.equal(h.elements.get('taskDetail').hidden,true);
    assert.equal(backs.length,2);
    assert.equal(h.elements.get('taskListView').hidden,false);
  });
  await test('detail view renders every required field once, escapes payload and never autoplays media',async()=>{
    const h=harness();
    const task={id:"t'\"><img src=x>",status:'failed',kind:'video',prompt:'<img src=x onerror=bad()>',api_prompt:'<script>upstream()</script>',account_id:'a1',account_label:'Main<img src=x>',error:'boom cookie=SECRET',attempts:[{attempt:1,account_id:'a1',account_label:'Main'}],generation:{size:'1920x1080',aspect_ratio:'16:9',duration:5},media:{kind:'video',url:'/v1/media/x.mp4',exists:true,bytes:20,width:1920,height:1080}};
    h.setRespond(async()=>({task}));
    await h.context.openTaskDetail(task.id);
    const body=h.elements.get('taskDetailBody'),sections=body.querySelectorAll('section').length;
    assert.ok(sections>=5,'detail sections: '+sections);
    assert.equal(body.querySelectorAll('img').length,0);
    assert.equal(body.querySelectorAll('script').length,0);
    assert.doesNotMatch(body.textContent,/SECRET/);
    assert.ok(!/<img\b|<script\b/i.test(body.innerHTML),'payload must stay escaped');
    assert.match(body.textContent,/boom/);
    assert.match(body.textContent,/Main/);
    assert.match(body.textContent,/a1/);
    assert.match(body.textContent,/提示词/);
    assert.match(body.textContent,/发送给上游的提示词/);
    assert.match(body.textContent,/生成目标（非实际像素保证）/);
    assert.match(body.textContent,/媒体预览/);
    assert.match(body.textContent,/账号尝试记录/);
    assert.match(body.textContent,/错误详情/);
    assert.match(body.textContent,/生成类型/);
    assert.match(body.textContent,/任务状态/);
    assert.match(body.textContent,/1920x1080/);
    const player=body.querySelector('video');
    assert.ok(player);
    assert.equal(player.getAttribute('preload'),'metadata');
    assert.equal(player.getAttribute('autoplay'),null);
    assert.equal(body.querySelectorAll('button[data-task-logs]').length,1);
    assert.equal(body.querySelectorAll('button[data-task-detail]').length,1);
    await h.context.openTaskDetail(task.id);
    assert.equal(body.querySelectorAll('section').length,sections);
    assert.equal(body.querySelectorAll('video').length,1);
    assert.equal(body.querySelectorAll('button[data-task-logs]').length,1);
    assert.equal(body.querySelectorAll('img').length,0);
    h.setRespond(async()=>({task:{...task,media:{kind:'audio',url:'/v1/media/a.mp3',exists:true,bytes:5}}}));
    await h.context.openTaskDetail(task.id);
    const audio=body.querySelector('audio');
    assert.ok(audio);
    assert.equal(audio.getAttribute('autoplay'),null);
    assert.equal(audio.getAttribute('preload'),'metadata');
    assert.equal(body.querySelectorAll('video').length,0);
    h.context.closeTaskDetail();
    assert.equal(h.elements.get('taskDetail').hidden,true);
    assert.equal(body.textContent,'');
  });
  await test('new upload and detail view copy has English translations in both directions',()=>{
    const zh=harness(),en=harness('en');
    for(const text of ['点击或拖拽文件到此处','拖拽文件到上传卡片即可加入参考素材','查看任务详情','媒体预览','任务状态','生成类型','← 返回'])assert.notEqual(en.context.t(text),text,text+' missing English');
    for(const text of ['点击或拖拽文件到此处','拖拽文件到上传卡片即可加入参考素材','查看任务详情','媒体预览','任务状态','生成类型','← 返回'])assert.equal(zh.context.t(text),text,'Chinese must stay Chinese: '+text);
    assert.equal(en.context.t('已选 0 个'),'Selected: 0');
    assert.equal(en.context.t('已选 3 个'),'Selected: 3');
    assert.equal(zh.context.t('已选 3 个'),'已选 3 个');
    assert.ok(html.includes('title="拖拽文件到上传卡片即可加入参考素材"'));
  });
  await test('destructive task clearing requires an explicit typed confirmation',async()=>{
    const h=harness();
    // `clearTasks` lives outside the extracted page sections; load its real source.
    h.run(script.slice(script.indexOf('async function clearTasks('), script.indexOf('/* ================= 在线测试')));
    h.requests.length=0;
    let prompts=[];
    h.context.prompt=(message)=>{prompts.push(message);return null;};
    await h.context.clearTasks();
    assert.equal(h.requests.length,0,'cancel must not call the API');
    h.context.prompt=()=> 'oops';
    await h.context.clearTasks();
    assert.equal(h.requests.length,0,'unknown text must not call the API');
    h.context.prompt=()=> '清空';
    h.setRespond(async path=>({removed:5,kept:0}));
    h.requests.length=0;
    const clearReq=()=>h.requests.filter(r=>String(r.path).includes('/admin/tasks/clear')).at(-1);
    await h.context.clearTasks();
    assert.deepEqual(JSON.parse(clearReq().options.body),{keep:0,confirm:'CLEAR'});
    h.context.prompt=()=> '20';
    await h.context.clearTasks();
    assert.deepEqual(JSON.parse(clearReq().options.body),{keep:20});
    assert.ok(prompts.length>=1 && /清空/.test(prompts[0]));
  });
  await test('disconnected workspace is never labelled available',()=>{
    const h=harness();
    const ok={id:'a1',label:'ok',enabled:true,ok:true,vm_state:'RUNNING',quota:{found:true,weekly_used_pct:10}};
    const broken={id:'a2',label:'broken',enabled:true,ok:true,vm_state:'DISABLED',quota:{}};
    const unknown={id:'a3',label:'unknown',enabled:true,ok:true,quota:{}};
    assert.match(h.context.accountCard(ok),/● 可用/);
    assert.doesNotMatch(h.context.accountCard(broken),/● 可用/);
    assert.match(h.context.accountCard(broken),/会话有效 · 工作区未连接/);
    assert.match(h.context.accountCard(broken),/VM: DISABLED/);
    assert.match(h.context.accountCard(unknown),/工作区未确认/);
    assert.doesNotMatch(h.context.accountCard(unknown),/● 可用/);
  });
  console.log('ALL PASS ('+passed+' admin automation checks)');
}
