(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TimeEchoPrompts = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function invalid(message) { const error = new Error(message); error.code = 'VALIDATION'; return error; }
  function check(job) {
    if (!Number.isInteger(job.oldYear) || !Number.isInteger(job.newYear) || job.oldYear < 2000 || job.oldYear >= job.newYear || job.span !== job.newYear - job.oldYear) throw invalid('任务年份不一致，请重新创建任务。');
  }
  function aging(job) {
    check(job);
    if (job.span <= 3) return '保存时间很短：接近原始打印质感，仅少量指印或边角使用痕迹；不可严重泛黄、深度褪色、大片污渍或布满折痕。';
    if (job.span <= 9) return '保存数年：轻微且不均匀的褪色、少量细划痕或边角磨损，纸面整体仍完整；不要伪造成保存了几十年的老照片。';
    if (job.span <= 19) return '保存十余年：自然褪色、局部泛黄、细划痕、少量污渍及轻微边角磨损；这些痕迹分布不均匀，主体仍可辨认。';
    return '保存二十年以上：明显但自然的不均匀褪色、局部泛黄或染污、细划痕、指印与边角磨损，可有轻微卷曲或一两道折痕；从中合理组合，不要全部堆满，不要让污损盖住主体。';
  }
  function changes(job) {
    check(job);
    if (job.span <= 3) return '时间跨度很短，主要表现人的离开、生活物件收起、门店关门或场所用途的变化。不要突然出现几十年的植被生长、房屋坍塌、严重锈蚀和大面积破损。';
    if (job.span <= 9) return '允许人物活动减少、家具器物更新或撤走、少量自然老化与局部植被变化；整体空间仍连续，不强行改成废墟。';
    return '时间要改变生活，不仅是减少人物或换高清画质。保留辨认地点所需的主要轮廓、道路方向与机位，允许装修、门窗材料、招牌、设施和空间用途随年限更新或消失。至少安排三项跨类别的可见变化，例如红纸或招牌拆走留下色差、旧窗口改作封闭门板、长凳撤去留下修补痕迹；也可翻新成陌生的新用途。今照应让人看出曾经的生活不再发生，而非同一天清场后的照片。不强制每个场景都有荒草、破窗和坍塌。';
  }
  const noText = '画面内不生成年份、时间标签、字幕、可读文字、Logo或水印；时间文字在最后由本地程序叠加。';
  const composition = '为最终上下对照合图生成一幅独立照片，构图目标约8:9，主体完整、边缘留有余量，不在本次生成中拼接。靠近画面上/下边缘的中央区域避免重要人脸，以便最后叠时间文字。';
  function oldRules(job) {
    check(job);
    const style = job.photoStyle === 'bw' ? '这张原始照片为少量出现的黑白冲印照片，使用自然灰阶；纸张局部老化可以略暖，黑白不等于全图棕黄。' : '这张原始照片为彩色照片，保留符合时代的自然色彩；允许褪色和局部偏色，禁止自动变成黑白。';
    return `【旧照最终约束：照片内${job.oldYear}年，翻拍于${job.newYear}年，相隔${job.span}年】
必须有一只真实自然的手拿着一张纸质照片，由${job.newYear}年的普通手机近距离翻拍。手只抓边角，不遮主体；相纸及照片内场景占据大部分画面，背景桌面不抢主体。外层手、纸边、自然反光相对清楚。
纸质照片内部描绘${job.oldYear}年的真实生活：服装、家具、工具、交通物件、电子设备、建筑细节与拍摄年代相符。原始成像质感依据拍摄年，不能把近年的照片内部故意做成上世纪的画质。
${style}
${aging(job)} 老化进入纸面与照片内容区域，但不把现代翻拍的手和背景一起变旧。避免统一棕黄滤镜、电影光影、精致摆拍、海报、高清修复感。
旧日生活虽然简单、可能贫困或单调，但有人在使用这个地方，有具体的人情和日常活动。
${composition}
${noText}`;
  }
  function nowRules(job) {
    check(job);
    return `【现在图最终约束：${job.newYear}年，距离照片内${job.oldYear}年过去${job.span}年】
必须引用本组旧照作为同一地点的参考。只提取纸质照片内部的现实场景，不参考翻拍外层的手、桌面、纸边、折痕、反光或相纸角度。
输出${job.newYear}年站在同一地点直接用普通手机拍下的一张正常照片：没有拿照片的手、没有相纸边框、没有翻拍桌面。保持照片内部地点的机位、主要透视、建筑轮廓与道路几何可辨识。门窗洞口可用来认地点，但窗框、门板、玻璃、装修、红纸招贴、招牌、桌椅及其他设施都属于可随时间改变的细节，不能全部冻结。先明确哪些拆走、哪些替换或翻修、留下什么痕迹，再生成今照。
${changes(job)}
画面更清楚，但仍像普通人随手拍。孤独和空落来自曾经的人气减少与生活痕迹变化，而非一律变冷、变脏或变成废墟。用品、服装、车辆等符合${job.newYear}年。
${composition}
${noText}`;
  }
  function system(job) {
    check(job);
    return `你是中文自媒体视觉策划和图片提示词作者。这一组的旧年份为${job.oldYear}年，新年份为${job.newYear}年，相隔${job.span}年。年份已经确定，不得自行改写，也不得套用固定的十年前、二十年前或其他年份。
主题可以来自农村、小镇、校园、老街、城市日常、亲情、爱情、宠物和老物件等普通人的生活，依据本组主题自由创造具体场景。不要固定轮换同一套地点和活动。
旧日简单却有生活温度，新图清楚却有安静空落的感觉。情绪克制、真实、不靠夸大破败和空洞鸡汤。
只返回符合要求的JSON，不要Markdown或额外说明。`;
  }
  function ideaMessages(job, options = {}) {
    return [{ role: 'system', content: system(job) }, { role: 'user', content: `只生成1个完整且独立的图片选题。
本组随机主题：${job.theme}
用户补充方向：${options.brief || '无'}
发布平台：${options.platform || '小红书'}
创意变化种子：${options.seed || job.id}
同批任务编号：${job.index + 1}。同类主题也请换生活事件、空间功能、情感触发物和永久锚点，不要仅换标题。
最近已有选题，避免重复或轻微改写：${(options.recentIdeas || []).slice(-40).join('；') || '暂无'}
年份：${job.oldYear} → ${job.newYear}，相隔${job.span}年。
${changes(job)}
给出2至4个永久场景锚点，保证新旧图能看出是同一地点。移动的物件、人物和动物不能当作必须永久原样保留的锚点。
JSON：{"title":"具体选题标题","story":"新旧对照故事","anchorObject":"永久场景锚点","oldLifeDetails":"旧年份有生活气息的具体活动","nowChangeDetails":"符合跨度的场景与生活变化","hook":"让普通人共鸣的具体细节"}` }];
  }
  function promptMessages(job, idea, options = {}) {
    return [{ role: 'system', content: system(job) }, { role: 'user', content: `将这条选题扩写为一组生图提示词和配文：
${JSON.stringify(idea)}
发布平台：${options.platform || '小红书'}
${oldRules(job)}
${nowRules(job)}
两段提示词应分别完整包含各自的年份、主体、环境、拍摄方式、质感、构图和排除项。现在图提示词写成对旧照内部现实地点的编辑指令，避免重新设计一个相似地点。
JSON：{"title":"选题标题","decadePrompt":"旧照片完整提示词","nowPrompt":"现在图完整提示词","caption":"2至4行克制的自媒体配文","hashtags":["标签"]}` }];
  }
  function required(data, names) {
    if (!data || Array.isArray(data) || typeof data !== 'object') throw invalid('模型返回的结构不正确，请重试此步骤。');
    for (const name of names) if (typeof data[name] !== 'string' || !data[name].trim()) throw invalid(`模型返回缺少 ${name}，请重试此步骤。`);
    return data;
  }
  function normalizeIdea(data) {
    const candidate = Array.isArray(data?.ideas) ? data.ideas[0] : data;
    const result = { ...candidate, nowChangeDetails: candidate?.nowChangeDetails || candidate?.nowDecayDetails };
    required(result, ['title','story','anchorObject','oldLifeDetails','nowChangeDetails','hook']);
    return Object.fromEntries(['title','story','anchorObject','oldLifeDetails','nowChangeDetails','hook'].map(k=>[k,result[k].trim()]));
  }
  function normalizePack(data) {
    required(data,['decadePrompt','nowPrompt','caption']);
    return {title:typeof data.title==='string'?data.title:'',decadePrompt:data.decadePrompt.trim(),nowPrompt:data.nowPrompt.trim(),caption:data.caption.trim(),hashtags:Array.isArray(data.hashtags)?data.hashtags.filter(s=>typeof s==='string'):[]};
  }
  return { aging, changes, oldRules, nowRules, ideaMessages, promptMessages, normalizeIdea, normalizePack };
});
