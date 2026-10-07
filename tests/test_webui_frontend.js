// 独立验证真实 app.js 函数的提交参数、重试通道和结果排序；无浏览器依赖。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../webui/app.js'), 'utf8');
function load(name, context) {
  const start = source.indexOf('  function ' + name + '(');
  assert(start >= 0, name);
  const end = source.indexOf('\n  }', start) + 4;
  vm.runInNewContext(source.slice(start, end), context);
  return context[name];
}

let cases = 0;
const payloadContext = {
  currentSource: 'file', cleanPath: x => x.trim(), $: () => ({value:' test.mp4 '}),
  currentRule: () => { throw new Error('纯文字稿不应读取抽帧设置'); }
};
const payload = load('buildPayload', payloadContext)('transcript');
assert.equal(payload.url, 'test.mp4');
assert.equal(payload.want_frames, false);
assert.equal(payload.keyword, undefined);
cases++;

let submitted;
const retryContext = {
  taskRows: {old: {params: {mode:'single', urls:['video.mp4'], want_transcript:false, want_frames:true}}},
  submitPayload: p => { submitted = p; }, toast: () => { throw new Error('unexpected toast'); }
};
load('retryJob', retryContext)('old');
assert.equal(submitted.url, 'video.mp4');
assert.equal(retryContext.taskRows.old.params.url, undefined);
cases++;

// 当前 UI 是否勾选关键词不参与重试通道分配，完全按原请求判断。
let posts = 0;
const submitContext = {
  laneJobs: {transcript:'busy'}, updateButtons() {}, submittingAction:null,
  requiredChannels() { throw new Error('不能读取当前侧栏'); },
  apiPost() { posts++; return Promise.resolve({ok:false}); }, fieldErr() {}
};
const submit = load('submitPayload', submitContext);
submit({want_transcript:false, want_frames:true}, 'frames');
assert.equal(posts, 1, '纯抽帧重试不占用正在使用的文字稿通道');
submit({want_transcript:true, want_frames:true, keyword:'video'}, 'both');
assert.equal(posts, 1, '含关键词重试必须等待文字稿通道');
cases++;

const resultsContext = {resultsFeed:[], renderResults() {}};
const feed = load('feedResults', resultsContext);
feed('old', {created_at:'2026-10-07T10:00:00', results:[{ok:true}]});
feed('new', {created_at:'2026-10-07T11:00:00', results:[{ok:true}]});
assert.equal(resultsContext.resultsFeed[0].jobId, 'new');
cases++;
class Element {
  constructor(tag, cls, text) { this.tag = tag; this.className = cls; this.textContent = text || ''; this.childNodes = []; this.dataset = {}; }
  appendChild(node) { this.childNodes.push(node); return node; }
  addEventListener() {}
  setAttribute(key, value) { this[key] = value; }
}
function descendants(node) { return [node, ...node.childNodes.flatMap(descendants)]; }
const rowContext = {
  el: (tag, cls, text) => new Element(tag, cls, text),
  document: {createTextNode: text => new Element('text', null, text)},
  encodeURIComponent, copyText() {}, retryJob() {}, openReader() {}, openFrames() {}
};
load('makeExportPdfButton', rowContext);
const render = load('renderResultRow', rowContext);
const stale = {ok:true, run_dir:'runs/video', transcript_txt:'runs/video/transcript.txt',
  frames_dir:'runs/video/frames', artifacts:{transcript:{status:'succeeded'},frames:{status:'succeeded'}},
  availability:{transcript:false,frames:false,pdf:false,reason:'文件已移动或不存在'}};
const nodes = descendants(render(stale,'old'));
for (const label of ['阅读文字稿','浏览关键帧','导出PDF']) {
  assert(nodes.some(n=>n.textContent===label && n.disabled), label + ' must be disabled');
}
assert(!nodes.some(n=>n.tag==='a' && n.href), '失效文字稿没有可点的预览链接');
assert(nodes.some(n=>n.textContent==='重新生成' && !n.disabled));
assert(nodes.some(n=>n.textContent==='runs/video'), '原路径仍可见');
cases++;
const restored = {...stale, availability:{transcript:true,transcript_path:'runs/video/original.txt',frames:true,pdf:true}};
const restoredNodes = descendants(render(restored,'old'));
assert(restoredNodes.some(n=>n.tag==='a' && n.href.includes('original.txt')));
assert(restoredNodes.some(n=>n.textContent==='导出PDF' && !n.disabled));
cases++;

(async () => {
  let polling = 0, feeds = 0;
  const historyContext = {
    fetch: () => Promise.resolve({json: () => Promise.resolve({ok:true,jobs:[{
      job_id:'cancel',status:'cancelling',params:{want_transcript:true,want_frames:true},progress:{}}]})}),
    taskRows:{},laneByJob:{},laneJobs:{},jobOrder:[],jobCache:{},renderedJobs:{},
    renderLane() {},renderTaskList() {},updateButtons() {},feedResults() { feeds++; },
    activeJobIds() { return Object.values(historyContext.laneJobs); },startPolling() { polling++; }
  };
  load('loadHistory', historyContext)();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(historyContext.laneJobs.transcript,'cancel');
  assert.equal(historyContext.laneJobs.frames,'cancel');
  assert.equal(polling,1,'取消中刷新必须继续轮询');
  assert.equal(feeds,0,'取消中任务不能当成已完成结果');
  cases++;
  console.log(`${cases} frontend regression cases passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
