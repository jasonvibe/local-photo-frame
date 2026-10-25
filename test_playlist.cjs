const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const html=fs.readFileSync(__dirname+'/templates/gallery.html','utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/settings\(\);refresh\(\);refreshTimer=setInterval\(refresh,\d+\);setInterval\(settings,30000\);/,'');
function setup(){
 let names=['A.jpg','B.jpg'],pending=[];
 const frame={children:[],replaceChildren(...nodes){this.children=nodes},querySelectorAll(){return[]},animate(){return{cancel(){},finished:Promise.resolve()}},cloneNode(){return{style:{},remove(){}}}};
 const element=()=>({style:{},append(){},remove(){}});
 const context=vm.createContext({console,Map,Set,Promise,JSON,Math,innerWidth:1920,innerHeight:1280,AbortSignal,
  document:{getElementById:id=>id==='frame'?frame:null,createDocumentFragment:element,createElement:element,body:{append(){}}},
  window:{addEventListener(){}},setInterval(){return 1},clearInterval(){},setTimeout,clearTimeout,
  fetch:async(url)=>({ok:true,json:async()=>url==='/api/playlist'?names.map(name=>({name,r:.67})):names.slice()}),
  Image:class{set src(url){if(url.includes('slow'))pending.push(()=>{this.naturalWidth=800;this.naturalHeight=1200;this.onload?.()});else {this.naturalWidth=800;this.naturalHeight=1200;queueMicrotask(()=>this.onload?.())}}decode(){return Promise.resolve()}}
 });
 vm.runInContext(script,context);
 vm.runInContext(`meta.set('A.jpg',{name:'A.jpg',r:.67});meta.set('B.jpg',{name:'B.jpg',r:.67});slides=[[meta.get('A.jpg'),meta.get('B.jpg')]];key=JSON.stringify(['A.jpg','B.jpg']);`,context);
 return{run:s=>vm.runInContext(s,context),names:n=>{names=n},finish:()=>pending.splice(0).forEach(f=>f()),frame};
}
const settle=async()=>{for(let i=0;i<25;i++)await Promise.resolve()};
test('deletion is applied while a new image is still loading; stale load cannot resurrect it',async()=>{
 const c=setup();c.names(['A.jpg','B.jpg','slow.jpg']);c.run('refresh()');await settle();
 c.names(['B.jpg']);c.run('refresh()');await settle();
 assert.equal(c.run("meta.has('A.jpg')"),false,'deleted photo remains in metadata while loader is busy');
 assert.equal(c.run("slides.flat().some(x=>x.name==='A.jpg')"),false,'deleted photo remains in playback queue');
 c.finish();await settle();assert.equal(c.run("meta.has('slow.jpg')"),false,'stale load resurrected a deleted photo');
});
test('deleting all images empties the queue and frame',async()=>{
 const c=setup();c.names([]);await c.run('refresh()');await settle();
 assert.equal(c.run('slides.length'),0);assert.equal(c.frame.children.length,0);
});
test('new photos join the queue without a page reload',async()=>{
 const c=setup();c.names(['A.jpg','B.jpg','C.jpg']);await c.run('refresh()');await settle();
 assert.equal(c.run("slides.flat().some(x=>x.name==='C.jpg')"),true);
});
