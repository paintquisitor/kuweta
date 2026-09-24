const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8');
const elements = new Map();
const document = {querySelector: key => {
  if (!elements.has(key)) elements.set(key, {innerHTML: '', textContent: '', checked: false});
  return elements.get(key);
}};
const context = vm.createContext({document, console, setTimeout, clearTimeout});
vm.runInContext(source.slice(0, source.indexOf('function mapSvg(')), context);
vm.runInContext(`
state = {cats: [{id: 'kalinka', name: 'Kalinka', description: 'Kot'}, {id:'kefir', name:'Kefir'}],
  outcomes: {uncertain: 'Niepewne', feces_confirmed: 'Kał potwierdzony ręcznie'},
  visits: [
    {id:'aaa-0', cat_id:'kalinka', entered_at:new Date().toISOString(), exited_at:new Date().toISOString(), source:'camera', box_id:2, outcome:'uncertain', segments:[{id:'aaa-0'},{id:'bbb-0'}]},
    {id:'ccc-0', cat_id:'kefir', entered_at:new Date().toISOString(), source:'camera', box_id:1, outcome:'uncertain'}
  ]};
selectedCat = 'kalinka'; renderCats(); renderCatProfile();
`, context);
let html = elements.get('#cat-profile-content').innerHTML;
assert.ok(html.includes('/api/recordings/aaa/video'));
assert.ok(html.includes('/api/recordings/bbb/video'));
assert.ok(!html.includes('/api/recordings/ccc/video'));
vm.runInContext(`state.visits[0].outcome='feces_confirmed'; state.visits[0].reviewed_at=new Date().toISOString(); renderCats(); renderCatProfile();`, context);
assert.ok(elements.get('#cat-cards').innerHTML.includes('Kał potwierdzony ręcznie'));
assert.ok(elements.get('#cat-profile-content').innerHTML.includes('Twoja ocena'));
// A saved assessment refreshes state without waiting for an SSE event.
let handler, accepted, closed = false;
const dialog = {addEventListener: (_, callback) => {handler = callback;}, close: () => {closed = true;}};
const reviewContext = vm.createContext({
  $: () => dialog, selectedVisit:'aaa-0',
  FormData: class { [Symbol.iterator]() {return [['outcome','feces_confirmed'],['region',''],['feces_region','']][Symbol.iterator]();} },
  post: async (path, data) => {assert.equal(path, '/api/visits/aaa-0/review'); assert.equal(data.outcome,'feces_confirmed');},
  fetch: async path => {assert.equal(path,'/api/state'); return {ok:true,json:async()=>({updated:true})};},
  acceptState: value => {accepted = value;}, toast: () => {}
});
const start = source.indexOf('$("#visit-dialog").addEventListener("submit"');
vm.runInContext(source.slice(start, source.indexOf('$("#visit-dialog").addEventListener("click"', start)), reviewContext);
(async () => {
  await handler({preventDefault(){}, target:{id:'review-form',querySelector:()=>({disabled:false})}});
  assert.ok(closed); assert.equal(accepted.updated,true);
  console.log('PASS: cat filtering, both recording links, updated assessment, refresh after save');
})().catch(error => {console.error(error);process.exitCode=1;});
