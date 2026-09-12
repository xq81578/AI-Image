const test = require('node:test');
const assert = require('node:assert/strict');
const prompts = require('../batch-prompts.js');
const job = (oldYear,newYear,photoStyle='color') => ({id:'sample',index:0,theme:'校园',oldYear,newYear,span:newYear-oldYear,photoStyle});
test('年份贯穿提示词，短跨度避免几十年老化',()=>{
  const j=job(2024,2026),text=prompts.promptMessages(j,{title:'放学后的教室'})[1].content;
  assert.match(text,/2024/);assert.match(text,/2026/);assert.match(text,/相隔2年/);
  assert.match(text,/保存时间很短/);assert.match(text,/一只真实自然的手/);
  assert.match(text,/没有拿照片的手/);assert.match(text,/不在本次生成中拼接/);
  assert.doesNotMatch(text,/2000年前后|二十年变化预算/);
});
test('长跨度和黑白纸张老化分别控制',()=>{
  const text=prompts.oldRules(job(2001,2026,'bw'));
  assert.match(text,/保存二十年以上/);assert.match(text,/黑白冲印/);assert.match(text,/不均匀褪色/);
  assert.match(text,/2001年/);assert.match(text,/2026年/);
});
test('上一年作为新图年份时不会混用系统今年',()=>{
  const text=prompts.oldRules(job(2013,2025));
  assert.match(text,/相隔12年/);assert.doesNotMatch(text,/2026/);
});
test('拒绝不一致年份和模型缺失结构',()=>{
  assert.throws(()=>prompts.oldRules({...job(2000,2026),span:20}),{code:'VALIDATION'});
  assert.throws(()=>prompts.normalizePack({decadePrompt:'',nowPrompt:'abc'}),{code:'VALIDATION'});
  assert.throws(()=>prompts.normalizeIdea({title:'x'}),{code:'VALIDATION'});
});
