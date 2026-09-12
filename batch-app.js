(function () {
  'use strict';
  const Core = window.TimeEchoCore, Prompts = window.TimeEchoPrompts, Models = window.TimeEchoModels, Video = window.TimeEchoVideo;
  const $ = selector => document.querySelector(selector);
  const esc = Models.escapeHtml;
  const stageNames = ['灵感','提示词','旧照','今照','合成','就绪'];
  const stages = ['idea','prompt','old','now','compose','save'];
  let batch = null, runner = null, selectedId = null, db = null, dbFailure = null;
  let backend = null, directory = null, folderName = '', restored = false, renderTimer = null, detailStamp = '';
  let recentIdeas = [], choosingFolder = false, starting = false, titlesBusy = false;
  let videoController = null, videoReady = false, videoAction = false;
  const imageCache = new Map();
  const jobRows = new Map();
  let rowsBatchId = null;
  const emptyRow = '<div style="padding:42px 20px;text-align:center;color:var(--te-sub)">开始后，所有任务会在这里独立推进。</div>';
  function appError(message, code) { const e = new Error(message); e.code = code; return e; }
  function notice(message, error = false) { $('#batchNotice').textContent = message; $('#batchNotice').classList.toggle('error',error); }
  function settings() {
    return { count:Number($('#count').value),brief:$('#brief').value.trim(),platform:$('#platform').value,yearMode:$('#yearMode').value,provider:$('#providerSelect').value,textLimit:Number($('#textLimit').value),imageLimit:Number($('#imageLimit').value),saveImages:$('#saveImages').checked,config:Models.getConfig() };
  }
  function requestConfig() {
    // 批次模型/参数固定；更正 Key 后可重试，而不会悄悄换模型或年份。
    const snapshot = { ...batch.settings.config }, current = Models.getConfig();
    for (const key of Object.keys(current)) if (/key$/i.test(key)) snapshot[key] = current[key];
    return snapshot;
  }
  function safeSettings(value) {
    return {...value,config:Object.fromEntries(Object.entries(value.config || {}).filter(([key])=>!/key$/i.test(key)))};
  }
  function openDatabase() {
    return new Promise((resolve,reject)=>{
      const request = indexedDB.open('timeEchoBatchCache',1);
      request.onupgradeneeded=()=>{
        const database=request.result;
        database.createObjectStore('meta');
        database.createObjectStore('jobs',{keyPath:'id'});
        database.createObjectStore('handles');
      };
      request.onsuccess=()=>resolve(request.result);
      request.onerror=()=>reject(request.error);
    });
  }
  function storeValue(store,key,value) {
    if(!db) return Promise.reject(appError('浏览器缓存不可用，请允许本地存储后重试。','CACHE'));
    return new Promise((resolve,reject)=>{
      const tx=db.transaction(store,'readwrite');
      if(store==='jobs') tx.objectStore(store).put(value);else tx.objectStore(store).put(value,key);
      tx.oncomplete=()=>resolve();tx.onerror=()=>reject(appError(`缓存写入失败：${tx.error?.message || '空间不足'}`,'CACHE'));tx.onabort=()=>reject(appError('缓存写入中断，已生成结果仍保留在本页。','CACHE'));
    });
  }
  function readValue(store,key) {
    return new Promise((resolve,reject)=>{
      const request=db.transaction(store).objectStore(store).get(key);
      request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
    });
  }
  function pruneCachedJobs(ids) {
    const keep=new Set(ids);
    return new Promise((resolve,reject)=>{
      const tx=db.transaction('jobs','readwrite'),request=tx.objectStore('jobs').openCursor();
      request.onsuccess=()=>{const cursor=request.result;if(cursor){if(!keep.has(cursor.key))cursor.delete();cursor.continue();}};
      tx.oncomplete=resolve;tx.onerror=()=>reject(appError('旧任务缓存清理失败，请重试。','CACHE'));tx.onabort=tx.onerror;
    });
  }
  function releasePreviews(){for(const url of imageCache.values())URL.revokeObjectURL(url);imageCache.clear();}
  function serializable(job) {
    const snapshot=JSON.parse(JSON.stringify(job,(key,value)=>value instanceof Error?{message:value.message,code:value.code,status:value.status}:value));
    if(snapshot.imagesCleared)return snapshot;
    // 调度器先缓存结果再推进状态；磁盘快照直接记录已完成的连续阶段，刷新不会重做。
    let current=0;
    while(current<stages.length&&Object.prototype.hasOwnProperty.call(snapshot.results,stages[current])){
      Object.assign(snapshot.steps[current],{status:'done',error:null,retryAt:0});current++;
    }
    snapshot.current=current;
    return snapshot;
  }
  async function checkpoint(job) { await storeValue('jobs',job.id,serializable(job)); }
  function metaSnapshot(owner=batch) {
    return {id:owner.id,createdAt:owner.createdAt,settings:safeSettings(owner.settings),jobIds:owner.jobs.map(j=>j.id),selectedId,folderName,videoMode:Boolean(owner.videoMode),video:owner.video||null,imagesCleared:Boolean(owner.imagesCleared)};
  }
  async function saveMeta() {
    if(!batch)return;
    await storeValue('meta','latest',metaSnapshot());
  }
  async function api(path,data,binary=false) {
    if(!backend)throw appError('请使用「启动创作工具.vbs」连接本机创作服务。','LOCAL_SERVICE');
    let response;
    try {
      response=await fetch(backend.base+path,{method:data===undefined?'GET':'POST',headers:{'X-Local-Token':backend.token,...(data===undefined?{}:{'Content-Type':'application/json'})},body:data===undefined?undefined:JSON.stringify(data),signal:AbortSignal.timeout(path==='/api/select-folder'?600000:120000)});
    } catch(error) { throw appError(`本机创作服务未响应：${error.message}。请重新打开启动器后重试当前步骤。`,'LOCAL_SERVICE'); }
    if(!response.ok){let value;try{value=await response.json();}catch{}const error=appError(value?.error?.message||value?.error||`本机服务 HTTP ${response.status}`,value?.error?.code||'LOCAL_SERVICE');error.status=response.status;throw error;}
    return binary?response.blob():response.json();
  }
  async function connectLocal() {
    const fragment=new URLSearchParams(location.hash.slice(1)),link=fragment.get('local');
    if(link){
      const match=/^(\d{1,5}):([A-Za-z0-9_-]{20,})$/.exec(link);
      if(match&&Number(match[1])>0&&Number(match[1])<=65535){backend={base:`http://127.0.0.1:${match[1]}`,token:match[2]};try{sessionStorage.setItem('timeEchoLocalSession',JSON.stringify(backend));}catch{}}
      try{history.replaceState(null,'',location.href.split('#')[0]);}catch{}
    }
    if(!backend&&location.protocol==='file:'){
      try{const remembered=JSON.parse(sessionStorage.getItem('timeEchoLocalSession')||'null');if(/^http:\/\/127\.0\.0\.1:\d+$/.test(remembered?.base)&&/^[A-Za-z0-9_-]{20,}$/.test(remembered?.token))backend=remembered;}catch{}
    }
    if(!backend&&location.hostname==='127.0.0.1'){
      try{const response=await fetch('/api/session',{signal:AbortSignal.timeout(2000)});if(response.ok){const value=await response.json();if(value.app==='time-echo'&&value.token)backend={base:location.origin,token:value.token};}}catch{}
    }
    if(backend){
      try{const result=await api('/api/health');if(result.app!=='time-echo')throw new Error('本机服务不匹配');folderName=result.folder||'';return;}catch{backend=null;}
    }
    try{directory=await readValue('handles','directory');if(directory&&await directory.queryPermission({mode:'readwrite'})==='granted')folderName=directory.name;else directory=null;}catch{}
  }
  async function selectFolder() {
    if(choosingFolder)return;choosingFolder=true;
    const button=$('#selectFolder');button.disabled=true;
    try{
      if(backend){const result=await api('/api/select-folder',{});if(!result.cancelled&&result.folder)folderName=result.folder;}
      else if(window.showDirectoryPicker){directory=await window.showDirectoryPicker({id:'time-echo-output',mode:'readwrite'});folderName=directory.name;await storeValue('handles','directory',directory);}
      else{$('#helpDialog').showModal();return;}
      if(folderName){if(batch)await saveMeta();notice('目录已选择，成品视频会自动保存；合图是否保留由开关决定。');}
    }catch(error){if(error.name!=='AbortError')notice(Models.friendlyError(error),true);}
    finally{choosingFolder=false;render();}
  }
  function blobDataUrl(blob) {
    return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(appError('图片读取失败，请重试此步骤。','IMAGE_DOWNLOAD'));reader.readAsDataURL(blob);});
  }
  function loadImage(source) {
    return new Promise((resolve,reject)=>{const image=new Image();image.onload=()=>resolve(image);image.onerror=()=>reject(appError('图片无法解码，原始返回结果已保留，可重试读取。','IMAGE_DOWNLOAD'));image.src=source;});
  }
  async function readImage(source) {
    let url=source;
    if(!/^data:image\//i.test(source)){
      if(!/^https?:\/\//i.test(source))throw appError('模型返回了无效图片地址。','IMAGE_DOWNLOAD');
      let blob;
      if(backend)blob=await api('/api/image',{url:source},true);
      else{
        try{const response=await fetch(source,{signal:AbortSignal.timeout(120000),credentials:'omit'});if(!response.ok)throw new Error(`HTTP ${response.status}`);blob=await response.blob();}
        catch{throw appError('图片已生成，但图床限制了读取。请用本机启动器打开后重试此步骤，不必重新生图。','IMAGE_DOWNLOAD');}
      }
      if(blob.size>40*1024*1024)throw appError('图片超过40MB，无法缓存，请调整图片尺寸。','IMAGE_DOWNLOAD');
      url=await blobDataUrl(blob);
    }
    const image=await loadImage(url);
    if(!image.naturalWidth||image.naturalWidth*image.naturalHeight>40000000)throw appError('图片尺寸异常，已停止合成。','IMAGE_DOWNLOAD');
    // 缓存实际像素，后续参考、拼接与重试均不再依赖图床链接。
    const canvas=document.createElement('canvas');canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;canvas.getContext('2d').drawImage(image,0,0);
    return canvas.toDataURL('image/png');
  }
  function canvasBlob(canvas) { return new Promise((resolve,reject)=>canvas.toBlob(blob=>blob?resolve(blob):reject(appError('合图编码失败，请重试合成。','COMPOSE')),'image/png')); }
  async function compose(job) {
    const [oldImage,nowImage]=await Promise.all([loadImage(job.results.old),loadImage(job.results.now)]);
    const canvas=document.createElement('canvas');canvas.width=1080;canvas.height=1920;const ctx=canvas.getContext('2d');
    function drawFit(image,y){
      const cover=Math.max(1080/image.naturalWidth,960/image.naturalHeight);
      ctx.save();ctx.beginPath();ctx.rect(0,y,1080,960);ctx.clip();ctx.fillStyle='#282a27';ctx.fillRect(0,y,1080,960);
      ctx.filter='blur(28px)';ctx.globalAlpha=.45;ctx.drawImage(image,(1080-image.naturalWidth*cover)/2,y+(960-image.naturalHeight*cover)/2,image.naturalWidth*cover,image.naturalHeight*cover);ctx.filter='none';ctx.globalAlpha=1;
      const fit=Math.min(1080/image.naturalWidth,960/image.naturalHeight);
      ctx.drawImage(image,(1080-image.naturalWidth*fit)/2,y+(960-image.naturalHeight*fit)/2,image.naturalWidth*fit,image.naturalHeight*fit);ctx.restore();
    }
    drawFit(oldImage,0);drawFit(nowImage,960);
    function label(text,y){
      let size=54;ctx.font=`bold ${size}px "Microsoft YaHei",sans-serif`;
      while(ctx.measureText(text).width>980&&size>28){size-=2;ctx.font=`bold ${size}px "Microsoft YaHei",sans-serif`;}
      const width=ctx.measureText(text).width;ctx.fillStyle='rgba(0,0,0,.48)';ctx.fillRect((1080-width)/2-14,y-8,width+28,size+20);
      ctx.textAlign='center';ctx.textBaseline='top';ctx.lineJoin='round';ctx.lineWidth=4;ctx.strokeStyle='#000';ctx.strokeText(text,540,y);ctx.fillStyle='#fff';ctx.fillText(text,540,y);
    }
    label(`${job.oldYear}年`,960-16-64);label(`${job.newYear}年`,960+24);
    return blobDataUrl(await canvasBlob(canvas));
  }
  function fileName(job) {
    const title=(job.theme||'时光对照').replace(/[<>:"/\\|?*\x00-\x1f]/g,'_').slice(0,28);
    return `${batch.settings.provider==='mock'?'演示_':''}${batch.id}_${String(job.index+1).padStart(2,'0')}_${title}_${job.oldYear}-${job.newYear}.png`;
  }
  async function saveImage(job) {
    if(!folderName)throw appError('保存目录尚未选择，请先选择目录，再重试保存。','SAVE');
    const filename=fileName(job);
    if(backend)return api('/api/save',{filename,dataUrl:job.results.compose,requestId:job.id});
    if(!directory||await directory.queryPermission({mode:'readwrite'})!=='granted')throw appError('目录写入权限已失效，请重新选择目录，然后重试保存。','SAVE');
    let target=filename;
    for(let suffix=0;;suffix++){
      target=suffix?filename.replace(/\.png$/,`_${suffix}.png`):filename;
      try{
        const existing=await directory.getFileHandle(target),file=await existing.getFile();
        if(await blobDataUrl(file)===job.results.compose)return{filename:target,path:directory.name+'/'+target};
      }catch(error){if(error.name==='NotFoundError')break;throw error;}
    }
    const file=await directory.getFileHandle(target,{create:true}),writer=await file.createWritable();
    try{await writer.write(Models.dataUrlToBlob(job.results.compose));await writer.close();}catch(error){try{await writer.abort();}catch{}throw appError(`图片尚未保存：${error.message}`,'SAVE');}
    return{filename:target,path:directory.name+'/'+target};
  }
  function videoFileName(owner=batch){return `${owner.settings.provider==='mock'?'演示_':''}${owner.id}_时光对照.mp4`;}
  function videoBusy(){return Boolean(batch?.videoMode&&(batch.video?.status!=='done'||(!batch.settings.saveImages&&!batch.imagesCleared)));}
  function videoRunning(){return Boolean(videoAction||batch?.video?.status==='running');}
  function allFramesReady(){const jobs=batch?.jobs.filter(j=>!j.abandoned)||[];return Boolean(jobs.length&&jobs.every(j=>j.current>=6&&!j.persistenceError)&&!batch.jobs.some(j=>j.steps.some(s=>s.status==='running')));}
  async function cacheVideoFrame(job){
    return api('/api/video/frame',{batchId:batch.id,jobId:job.id,filename:fileName(job),dataUrl:job.results.compose});
  }
  async function clearBatchImages(owner){
    if(owner.settings.saveImages||owner.imagesCleared||owner!==batch)return;
    // 图片和完成标记在同一事务中更新，刷新后不会把已清理的图片当成未生成。
    const snapshots=owner.jobs.map(job=>{
      const copy=serializable(job);for(const key of ['old','now','compose'])delete copy.results[key];
      delete copy.pendingImages;copy.imagesCleared=true;return copy;
    });
    await new Promise((resolve,reject)=>{
      const tx=db.transaction(['jobs','meta'],'readwrite');
      for(const snapshot of snapshots)tx.objectStore('jobs').put(snapshot);
      tx.objectStore('meta').put({...metaSnapshot(owner),imagesCleared:true},'latest');
      tx.oncomplete=resolve;tx.onerror=()=>reject(appError('视频已保存，但图片缓存清理失败，请点击“重新检查视频状态”重试清理。','CACHE'));tx.onabort=tx.onerror;
    });
    if(owner!==batch)return;
    for(const job of owner.jobs){for(const key of ['old','now','compose'])delete job.results[key];delete job.pendingImages;job.imagesCleared=true;}
    owner.imagesCleared=true;releasePreviews();$('#pictureFull').removeAttribute('src');$('#pictureDialog').close();detailStamp='';
    notice('成品视频已保存，原图、合图和临时图片缓存已清理。');scheduleRender();
  }
  function attachVideo(){
    videoController?.dispose();videoController=null;videoReady=false;
    if(!batch?.videoMode)return;
    const owner=batch;
    videoController=Video.createController({batchId:owner.id,initial:owner.video,api,
      onChange:state=>{if(batch===owner){owner.video=state;scheduleRender();}},
      persist:async state=>{if(batch===owner){owner.video=state;await saveMeta();}},
      onDone:async()=>{if(batch===owner){await clearBatchImages(owner);if(owner.settings.saveImages)notice('成品视频和各组合图均已保存。');scheduleRender();}}
    });
  }
  function maybeStartVideo(){
    if(!videoReady||!videoController||videoAction||batch?.video?.status!=='waiting'||batch.video.connectionError||batch.video.error||runner?.getSummary().pausedAll||!allFramesReady())return;
    videoAction=true;
    videoController.start().catch(error=>notice(Models.friendlyError(error),true)).finally(()=>{videoAction=false;scheduleRender();});
  }
  function requireVideoConnection(state){
    if(state?.connectionError)throw appError(state.connectionError.message||String(state.connectionError),'LOCAL_SERVICE');
    if(state?.error&&state.status==='waiting')throw appError(state.error.message||String(state.error),'VIDEO');
    return state;
  }
  async function refreshVideo(){
    if(!videoController||videoAction)return;
    videoAction=true;render();
    try{requireVideoConnection(await videoController.refresh());videoReady=true;}
    catch(error){notice(Models.friendlyError(error),true);}
    finally{videoAction=false;render();}
  }
  async function demoImage(job,old){
    const canvas=document.createElement('canvas');canvas.width=1080;canvas.height=960;const c=canvas.getContext('2d');
    c.fillStyle=old?'#aa9878':'#b6c4b9';c.fillRect(0,0,1080,960);
    c.save();if(old){c.translate(540,430);c.rotate(-.035);c.translate(-540,-430);c.fillStyle='#e7d9ba';c.fillRect(90,45,900,825);c.beginPath();c.rect(120,75,840,765);c.clip();}
    c.fillStyle=old?'#c6c0a1':'#a8b5ad';c.fillRect(0,0,1080,960);c.fillStyle=old?'#b59b77':'#979f92';c.fillRect(200,335,700,365);c.fillStyle=old?'#69665b':'#6c7972';c.beginPath();c.moveTo(160,345);c.lineTo(350,185);c.lineTo(780,185);c.lineTo(940,345);c.fill();c.fillStyle='#575e51';c.fillRect(495,460,140,240);c.fillRect(285,440,115,105);c.fillRect(720,440,115,105);c.fillStyle=old?'#a39874':'#7e9980';c.beginPath();c.arc(140,290,140,0,Math.PI*2);c.fill();c.fillStyle='#77654d';c.fillRect(130,300,25,490);
    if(old){c.fillStyle='#8f6e4c';c.fillRect(370,710,85,12);c.fillRect(378,718,10,65);c.fillRect(438,718,10,65);c.fillStyle='#807660';c.beginPath();c.arc(740,710,27,0,Math.PI*2);c.fill();c.fillRect(718,740,44,70);}
    c.restore();if(old){c.fillStyle='#bb987b';c.beginPath();c.ellipse(77,813,48,125,-.4,0,Math.PI*2);c.fill();}
    c.fillStyle='rgba(0,0,0,.55)';c.fillRect(280,105,520,65);c.fillStyle='#fff';c.textAlign='center';c.font='28px "Microsoft YaHei",sans-serif';c.fillText('本地流程演示 · 非 AI 生成照片',540,148);
    return canvas.toDataURL('image/png');
  }
  async function execute(job,stage){
    const demo=batch.settings.provider==='mock', config=requestConfig();
    if(demo)await new Promise(resolve=>setTimeout(resolve,250+(job.index%3)*220));
    if(stage==='idea'){
      const value=demo?{title:`${job.theme}里的旧日时光`,story:`${job.oldYear}年的热闹，与${job.newYear}年的安静。`,anchorObject:'屋顶、门窗、道路与老树位置',oldLifeDetails:'一起聊天、忙碌生活的人',nowChangeDetails:'人少了，物件收起，地方仍然熟悉',hook:'地方还在，生活的声音少了'}:Prompts.normalizeIdea(Models.extractJson(await Models.callTextModel(Prompts.ideaMessages(job,{...batch.settings,recentIdeas,seed:job.id}),1,config)));
      recentIdeas.push(value.title+'｜'+value.story);recentIdeas=recentIdeas.slice(-40);return value;
    }
    if(stage==='prompt')return demo?{title:job.results.idea.title,decadePrompt:Prompts.oldRules(job),nowPrompt:Prompts.nowRules(job),caption:'曾经热闹的地方，如今只剩安静的风。\n本地流程演示。',hashtags:['时光对照']}:Prompts.normalizePack(Models.extractJson(await Models.callTextModel(Prompts.promptMessages(job,job.results.idea,batch.settings),.8,config)));
    if(stage==='old'||stage==='now'){
      job.pendingImages=job.pendingImages||{};
      if(!job.pendingImages[stage]){
        const old=stage==='old',provider=batch.settings.provider;
        const prompt=(old?job.results.prompt.decadePrompt:job.results.prompt.nowPrompt)+'\n\n'+(old?Prompts.oldRules(job):Prompts.nowRules(job));
        if(demo)job.pendingImages[stage]=await demoImage(job,old);
        else if(provider==='gemini')job.pendingImages[stage]=await Models.imageFromGemini(prompt,old?undefined:job.results.old,config,!old);
        else{
          const imageConfig=Models.getOpenAIImageConfig(provider,config);
          job.pendingImages[stage]=old?await Models.imageFromOpenAI(prompt,imageConfig):await Models.imageEditFromOpenAI(prompt,job.results.old,imageConfig);
        }
        await checkpoint(job);
      }
      const normalized=await readImage(job.pendingImages[stage]);
      delete job.pendingImages[stage];return normalized;
    }
    if(stage==='compose')return compose(job);
    if(stage==='save')return batch.videoMode?cacheVideoFrame(job):saveImage(job);
    throw appError('未知任务步骤。','VALIDATION');
  }
  function attachRunner(){
    runner?.dispose();
    runner=Core.createRunner({jobs:batch.jobs,limits:{text:batch.settings.textLimit,image:batch.settings.imageLimit,local:2},execute,checkpoint,maxRetries:2,retryDelayMs:attempt=>Math.min(30000,3000*Math.pow(2,Math.max(0,attempt-1))),onChange:scheduleRender});
  }
  function scheduleRender(){if(!renderTimer)renderTimer=setTimeout(()=>{renderTimer=null;render();},80);}
  async function startBatch(event){
    event.preventDefault();if(starting||runner?.isBusy()||videoBusy()||videoRunning())return;starting=true;
    let preparedBatchId=null,adopted=false;
    $('#startBatch').disabled=true;
    try{
      if(!folderName)throw appError('请先选择保存目录。','SAVE');
      if(dbFailure)throw appError('浏览器缓存不可用，请允许本地存储后重新打开页面。','CACHE');
      if(!backend)throw appError('自动制作视频需要本机服务，请双击「启动创作工具.vbs」后开始。','LOCAL_SERVICE');
      let environment;
      try{environment=await api('/api/video/environment');}catch(error){if(error.status===404)throw appError('本机服务还是旧版，请重启创作工具的 Python 服务后再开始。','LOCAL_SERVICE');throw error;}
      if(!environment.ready)throw appError(environment.message||'视频环境未就绪，请检查 FFmpeg 和 bgm 目录。','VIDEO_ENVIRONMENT');
      const input=settings();
      if(input.provider!=='mock'){
        if(!Models.getTextModelConfig(input.config).key)throw appError('请先配置文本模型的 API Key。','CONFIG');
        const imageKey=input.provider==='gemini'?input.config.geminiKey:Models.getOpenAIImageConfig(input.provider,input.config).key;
        if(!imageKey)throw appError('请先配置生图模型的 API Key。','CONFIG');
      }
      const id=new Date().toISOString().replace(/[-:]/g,'').replace(/\.\d+Z$/,'')+'_'+crypto.randomUUID().slice(0,6);
      const jobs=Core.createJobs({count:input.count,currentYear:new Date().getFullYear(),yearMode:input.yearMode,idPrefix:id});
      const next={id,createdAt:Date.now(),settings:input,jobs,videoMode:true,imagesCleared:false};
      next.video=await api('/api/video/prepare',{batchId:id,jobIds:jobs.map(job=>job.id),filename:videoFileName(next),saveImages:input.saveImages});
      preparedBatchId=id;
      await Promise.all(jobs.map(checkpoint));
      await storeValue('meta','latest',{...metaSnapshot(next),selectedId:null});
      videoController?.dispose();batch=next;selectedId=null;restored=false;adopted=true;
      releasePreviews();attachRunner();attachVideo();videoReady=true;runner.start();
      notice(input.provider==='mock'?'正在运行本地演示，不消耗模型额度；合图全部完成后会实际制作配乐视频。':'已启动，主题和年份已分配，所有合图完成后自动配乐成片。');
      await pruneCachedJobs(jobs.map(j=>j.id));
    }catch(error){
      let cleanupNote='';
      if(preparedBatchId&&!adopted){
        try{await api('/api/video/discard',{batchId:preparedBatchId});}catch{cleanupNote=' 未开始的本机任务尚未清理。';}
        try{await pruneCachedJobs(batch?.jobs.map(job=>job.id)||[]);}catch{}
      }
      notice(Models.friendlyError(error)+cleanupNote,true);
    }finally{starting=false;render();}
  }
  function stats(){
    const jobs=batch?.jobs||[];
    return{total:jobs.length,saved:jobs.filter(j=>!j.abandoned&&j.steps[5].status==='done').length,failed:jobs.filter(j=>!j.abandoned&&(j.steps.some(s=>s.status==='error')||j.persistenceError)).length,paused:jobs.filter(j=>j.current<6&&!j.abandoned&&j.paused).length,abandoned:jobs.filter(j=>j.abandoned).length,active:jobs.filter(j=>j.steps.some(s=>s.status==='running')).length,pending:jobs.filter(j=>j.current<6&&!j.abandoned).length};
  }
  function stageState(job,k,globalPaused){
    const step=job.steps[k],s=step.status;
    if(s==='done')return k===5?(batch.videoMode?'✓ 就绪':'✓ 已存'):'✓ 完成';
    if(s==='running')return job.paused||globalPaused?'收尾中':k===4?'合成中':k===5?(batch.videoMode?'准备中':'保存中'):'生成中';
    if(job.abandoned)return '已放弃';
    if(s==='error')return '失败';
    if((job.paused||globalPaused)&&job.current===k)return '已暂停';
    if(s==='retry')return '待重试';
    if(s==='queued')return '排队中';
    return '待上步';
  }
  function renderStage(element,job,k,globalPaused){
    const step=job.steps[k],s=step.status;
    const paused=job.paused||globalPaused;
    let note='';
    if(s==='running')note=paused?'本步结束后暂停':Models.formatTime((step.elapsedMs||0)+(step.startedAt?Date.now()-step.startedAt:0));
    else if(s==='retry')note=paused?'暂停重试':`${Math.max(0,Math.ceil(((step.retryAt||Date.now())-Date.now())/1000))}秒 · 第${Math.min(2,step.attempts)}次重试`;
    else if(s==='queued'&&!paused)note=k<4?'等接口名额':'等本地处理';
    else if(s==='error')note=k===5?'合图保留':'可单独重试';
    element.dataset.kind={running:'active',queued:'queue'}[s]||s;
    element.querySelector('.te-chip').textContent=stageState(job,k,globalPaused);
    const noteElement=element.querySelector('small');noteElement.textContent=note;noteElement.hidden=!note;
  }
  function renderJobs(globalPaused){
    const board=$('#batchJobs');
    if(!batch){board.innerHTML=emptyRow;jobRows.clear();rowsBatchId=null;return;}
    if(rowsBatchId!==batch.id){board.replaceChildren();jobRows.clear();rowsBatchId=batch.id;}
    for(const job of batch.jobs){
      let row=jobRows.get(job.id);
      if(!row){
        row=document.createElement('article');row.className='te-job';row.setAttribute('aria-label',`第${job.index+1}组 ${job.theme}`);
        // 任务存续期间保留同一组按钮，进度刷新不能打断鼠标点击或键盘焦点。
        row.innerHTML=`<div class="te-job-info"><div class="te-job-heading"><span class="te-num">${String(job.index+1).padStart(2,'0')}</span><strong>${esc(job.theme)}</strong></div><div class="te-job-meta">${job.oldYear} → ${job.newYear} · ${job.photoStyle==='bw'?'黑白':'彩照'}</div><div class="te-job-title"></div><div class="te-job-tools"><button type="button" class="te-quiet" data-action="detail" data-job="${esc(job.id)}">查看详情</button></div></div>${stageNames.map(name=>`<div class="te-stage"><span class="te-mobile-label">${name}</span><span class="te-chip"></span><small hidden></small></div>`).join('')}<div class="te-job-action"><button type="button" class="te-danger" data-action="retry" data-job="${esc(job.id)}" hidden></button><button type="button" data-action="pause" data-job="${esc(job.id)}">暂停</button><small data-job-result hidden></small></div>`;
        board.append(row);jobRows.set(job.id,row);
      }
      row.dataset.selected=String(selectedId===job.id);
      row.querySelector('.te-job-title').textContent=job.results.idea?.title||'正在构思这一组灵感';
      row.querySelector('[data-action="detail"]').textContent=selectedId===job.id?'收起详情':'查看详情';
      row.querySelectorAll('.te-stage').forEach((element,k)=>renderStage(element,job,k,globalPaused));
      const running=job.steps.some(s=>s.status==='running'),failed=job.steps.some(s=>s.status==='error')||job.persistenceError,terminal=job.abandoned||job.current>=6;
      const retry=row.querySelector('[data-action="retry"]');retry.hidden=terminal||!failed;retry.disabled=running||globalPaused;retry.textContent=`重试${stageNames[Math.min(job.current,5)]}`;
      const pause=row.querySelector('[data-action="pause"]');pause.hidden=terminal;pause.disabled=globalPaused;pause.textContent=job.paused?'继续':'暂停';
      const result=row.querySelector('[data-job-result]');result.hidden=!terminal;result.textContent=job.abandoned?'已排除':batch.videoMode?(batch.imagesCleared?'图片已清理':batch.settings.saveImages?'合图已保存':'素材就绪'):'已保存';
    }
  }
  function renderVideo(globalPaused){
    const state=batch?.video,legacy=batch&&!batch.videoMode,status=state?.status||'waiting';
    const labels={waiting:'等待合图',running:`制作中 · ${Math.floor(state?.progress||0)}%`,done:'成品已保存',error:state?.path?'视频已保存，清理待重试':'视频制作失败',cancelled:'视频制作已停止'};
    $('#videoStatus').textContent=legacy?'旧版图片批次':globalPaused&&status==='waiting'?'等待继续整批':labels[status]||'等待合图';
    const count=batch?batch.jobs.filter(j=>!j.abandoned).length:Number($('#count').value)||0;
    let info=state?`${count} 张合图 · 约 ${count*4} 秒 · 1080×1920 · 配乐：${state.bgm||'等待选曲'}`:`${count} 张合图生成 1 条约 ${count*4} 秒的竖屏视频，BGM 从 bgm 目录随机选择。`;
    if(legacy)info='这是升级前的图片任务，仍可完成或查看图片；新建批次后使用自动视频流程。';
    else if(batch&&count===0)info='所有组已排除，本批不会生成空视频，可以新建批次。';
    else if(status==='done')info+=batch.imagesCleared?'。图片缓存已清理。':batch.settings.saveImages?'。合图已同时保存。':'。正在清理图片缓存。';
    else if(status==='cancelled')info+='。已保留合图，点击重试会重新制作视频。';
    else if(batch?.jobs.some(j=>j.paused||j.steps.some(s=>s.status==='error')))info+='。等待暂停或失败的组处理完毕。';
    $('#videoInfo').textContent=info;
    const progress=status==='done'?100:Math.max(0,Math.min(100,state?.progress||0));
    $('#videoProgress').setAttribute('aria-valuenow',progress);$('#videoProgress>div').style.width=`${progress}%`;
    $('#videoFile').hidden=!state?.path;$('#videoFile').textContent=state?.path?`已保存：${state.path}`:'';
    const error=state?.connectionError||state?.persistenceError||state?.cleanupError||state?.error||'';
    $('#videoError').hidden=!error;$('#videoError').textContent=typeof error==='object'?error.message||String(error):error;
    $('#retryVideo').hidden=!batch?.videoMode||!['error','cancelled'].includes(status);
    $('#retryVideo').textContent=state?.path?'重试缓存清理':'重试视频制作';
    $('#retryVideo').disabled=videoRunning()||globalPaused||!allFramesReady();
    $('#refreshVideo').hidden=!batch?.videoMode||!(!videoReady||error||status==='running'||(status==='done'&&!batch.settings.saveImages&&!batch.imagesCleared));$('#refreshVideo').disabled=videoAction;
    $('#cancelVideo').hidden=status!=='running';$('#cancelVideo').disabled=videoAction;
    $('#videoOpenFolder').hidden=status!=='done';$('#videoOpenFolder').disabled=!backend;
  }
  function render(){
    if(!Core)return;
    const summary=stats(),busy=Boolean(runner?.isBusy()||videoBusy()||videoRunning()),globalPaused=runner?.getSummary().pausedAll||false;
    $('#yearNote').textContent=`当前为 ${new Date().getFullYear()} / ${new Date().getFullYear()-1}`;
    $('#saveImagesHint').textContent=$('#saveImages').checked?'成品目录同时保留带年份的合图 PNG':'仅保留视频，成功后清理图片缓存';
    document.querySelectorAll('[data-batch-setting]').forEach(el=>el.disabled=busy);
    $('#folderPath').textContent=(busy&&batch?.videoMode?batch.video?.outputDir||folderName:folderName)||'请选择成品视频目录';$('#selectFolder').textContent=choosingFolder?'正在选择…':folderName?'更换保存目录':'选择保存目录';$('#selectFolder').disabled=choosingFolder||busy;
    $('#startBatch').disabled=starting||choosingFolder||busy||!folderName||Boolean(dbFailure);$('#startBatch').textContent=starting?'正在检查并准备…':busy?(batch?.video?.status==='running'?'正在制作视频…':globalPaused?(summary.active?'正在暂停本批…':'本批已暂停'):summary.pending?`正在处理 ${summary.total} 组合图`:'请处理视频阶段'):'一键生成视频 →';
    $('#startHint').textContent=dbFailure?'缓存不可用，请检查浏览器存储权限':!backend?'双击「启动创作工具.vbs」使用本机视频制作':summary.pending&&summary.failed?'失败步骤可单独重试；已有结果保留':`${$('#count').value} 张合图 → 1 条约 ${Number($('#count').value)*4} 秒视频`;
    $('#pauseBatch').disabled=!busy||videoRunning()||batch?.video?.status==='done';$('#pauseBatch').textContent=globalPaused?'继续整批':'暂停整批';$('#openFolder').disabled=!folderName||!backend;
    $('#newBatch').hidden=!batch;$('#newBatch').disabled=summary.active>0||videoRunning()||Boolean(batch?.videoMode&&batch.video?.status==='done'&&!batch.settings.saveImages&&!batch.imagesCleared);$('#newBatch').textContent=summary.pending||videoBusy()?'放弃未完成并新建':'清空列表并新建';
    $('#batchCounts').innerHTML=batch?`${batch.videoMode?'已就绪':'已保存'} <strong>${summary.saved} / ${summary.total-summary.abandoned} 张合图</strong>${summary.failed?` · ${summary.failed} 组失败`:''}${summary.paused?` · ${summary.paused} 组暂停`:''}${summary.abandoned?` · ${summary.abandoned} 组排除`:''}`:'尚未开始';
    $('#batchStatus').textContent=!batch?'准备就绪':globalPaused?(summary.active?`正在暂停 · 等待 ${summary.active} 个当前步骤结束`:'整批已暂停'):summary.active?`${summary.active} 组正在处理`:summary.paused?'已暂停，点击“继续”恢复':summary.pending?'等待继续或处理失败步骤':batch.videoMode?'图片阶段已结束，请查看视频进度':'本批已结束';
    const expected=summary.total-summary.abandoned;$('#batchProgress').setAttribute('aria-valuemax',expected||Number($('#count').value));$('#batchProgress').setAttribute('aria-valuenow',summary.saved);$('#batchProgress>div').style.width=`${expected?summary.saved/expected*100:0}%`;
    $('#restoreNotice').hidden=!restored;$('#restoreText').textContent='已恢复上次任务及缓存。已完成的图片不会重新生成；中断步骤请手动重试。';$('#continueRestored').disabled=summary.active>0;
    renderJobs(globalPaused);
    renderVideo(globalPaused);
    renderDetail();
    maybeStartVideo();
  }
  function imageUrl(data){
    if(!data)return '';
    if(!imageCache.has(data))imageCache.set(data,URL.createObjectURL(Models.dataUrlToBlob(data)));
    return imageCache.get(data);
  }
  function renderDetail(force=false){
    const panel=$('#jobDetail'),job=batch?.jobs.find(j=>j.id===selectedId);
    panel.hidden=!job;if(!job){detailStamp='';return;}
    const errors=job.steps.filter(s=>s.error).map(s=>s.error.message||s.error).join('；');
    const stamp=JSON.stringify([job.id,job.current,job.paused,job.abandoned,job.imagesCleared,job.steps.map(s=>s.status),errors,job.results.save?.path,job.results.prompt,job.persistenceError]);
    if(!force&&stamp===detailStamp)return;
    if(!force&&panel.contains(document.activeElement)&&document.activeElement.tagName==='TEXTAREA')return;
    detailStamp=stamp;
    const pack=job.results.prompt,editing=Boolean(pack)&&(job.paused||job.steps.some(s=>s.status==='error'))&&!job.steps.some(s=>s.status==='running');
    panel.innerHTML=`<div class="te-detail-head"><div><h3>第 ${job.index+1} 组 · ${esc(job.results.idea?.title||job.theme)}</h3><p>${job.oldYear} → ${job.newYear}，相隔 ${job.span} 年</p></div><button type="button" data-action="close-detail">收起</button></div><div class="te-detail-status">${job.imagesCleared?'视频已保存，本组图片缓存已清理。':job.results.save?.path?`✓ 合图已保存：${esc(job.results.save.path)}`:job.results.save?'合图已就绪，供本批视频使用。':job.results.compose?'合图已生成，正在准备视频素材。':'各步骤完成后，内容会依次出现在这里。'}</div>${errors?`<p class="te-error-detail">${esc(errors)}</p>`:''}${job.persistenceError?`<p class="te-error-detail">缓存失败，内存中的结果已保留：${esc(job.persistenceError.message||job.persistenceError)}</p>`:''}
      ${job.results.compose?`<button type="button" class="te-quiet" data-action="image" data-kind="compose" data-job="${esc(job.id)}"><img src="${imageUrl(job.results.compose)}" class="te-photo-real" style="max-height:560px" alt="${job.oldYear}与${job.newYear}年的上下合图，点击放大"></button><div class="te-output-tools"><a href="${imageUrl(job.results.compose)}" download="${esc(fileName(job))}">另存合图</a>${!batch.videoMode&&job.steps[5].status==='done'?`<button class="te-quiet" type="button" data-action="save-again" data-job="${esc(job.id)}">再次保存到所选目录</button>`:''}</div>`:''}
      ${[2,3].includes(job.current)&&job.steps[job.current].status==='error'&&job.pendingImages?.[stages[job.current]]?`<p class="te-config-hint">模型已返回图片，但图片未能读取。可先重试读取；若链接失效，可重新生成（会再次调用生图模型）。</p><button type="button" data-action="regenerate-image" data-job="${esc(job.id)}" ${runner.getSummary().pausedAll?'disabled':''}>放弃本次图片结果，重新生成${stageNames[job.current]}</button>`:''}
      <details class="te-advanced-list" ${job.results.compose||job.imagesCleared?'':'open'}><summary>新旧原图与提示词</summary><div class="te-detail-image-grid">${['old','now'].map((stage,i)=>`<div><h3>${i?'现在图':'旧照'} · ${i?job.newYear:job.oldYear}年</h3>${job.results[stage]?`<button type="button" class="te-quiet" data-action="image" data-kind="${stage}" data-job="${esc(job.id)}"><img src="${imageUrl(job.results[stage])}" class="te-photo-real" alt="${i?'现在图':'手持旧照'}，点击放大"></button>`:job.imagesCleared?'<p>图片缓存已清理</p>':'<p>等待图片生成</p>'}${pack?`<label>${i?'现在图':'旧照'}提示词<textarea id="edit-${stage}" ${editing?'':'readonly'}>${esc(i?pack.nowPrompt:pack.decadePrompt)}</textarea></label>`:''}</div>`).join('')}</div>${editing?`<p class="te-config-hint">修改旧照提示词会重做两张图；仅修改现在图提示词会保留旧照。保存修改后点击“继续”。</p><button type="button" data-action="edit-prompts" data-job="${esc(job.id)}">保存提示词修改</button>`:pack&&!job.imagesCleared&&job.current<6?'<p class="te-config-hint">需要调整提示词时，先暂停此组并等待当前请求结束。</p>':''}</details>
      ${job.results.idea?`<details class="te-advanced-list"><summary>灵感详情</summary><p>${esc(job.results.idea.story)}</p><p>场景锚点：${esc(job.results.idea.anchorObject)}</p><p>共情点：${esc(job.results.idea.hook)}</p></details>`:''}
      ${pack?`<div class="te-info-part" style="margin-top:16px"><h3>配文</h3><p style="white-space:pre-wrap">${esc(pack.caption)}</p><p>${esc((pack.hashtags||[]).join(' '))}</p><div class="te-output-tools"><button type="button" class="te-quiet" data-action="copy-caption" data-job="${esc(job.id)}">复制配文</button><button type="button" class="te-quiet" data-action="copy-prompts" data-job="${esc(job.id)}">复制提示词</button><button type="button" class="te-quiet" data-action="titles" data-job="${esc(job.id)}">生成短视频标题</button></div></div>`:''}
      <div class="te-detail-actions"><small>${job.imagesCleared?'已保留年份、提示词和配文；图片已按设置清理。':'失败时保留素材供重试；未开启图片保存时，视频完成后清理图片缓存。'}</small>${job.current<6&&!job.abandoned?`<button class="te-quiet te-danger" type="button" data-action="abandon" data-job="${esc(job.id)}">${batch.videoMode?'放弃此组并排除视频':'放弃此组后续'}</button>`:''}</div>`;
  }
  async function editPrompts(job){
    if(job.steps.some(s=>s.status==='running')||!job.results.prompt)return;
    const old=$('#edit-old').value.trim(),now=$('#edit-now').value.trim();if(!old||!now)throw appError('两段提示词都不能为空。','VALIDATION');
    const from=old!==job.results.prompt.decadePrompt?2:now!==job.results.prompt.nowPrompt?3:-1;if(from<0)return;
    runner.pause(job.id);
    if(from<=job.current&&!runner.resetFrom(job.id,stages[from]))throw appError('当前步骤仍在收尾，请稍后再保存修改。','VALIDATION');
    job.results.prompt.decadePrompt=old;job.results.prompt.nowPrompt=now;
    await checkpoint(job);notice(`提示词已更新，此组已暂停，可继续或重试当前${stageNames[job.current]}步骤。`);renderDetail(true);render();
  }
  async function generateTitles(job){
    if(titlesBusy)return;titlesBusy=true;
    $('#titles').innerHTML='';$('#titleStatus').textContent='正在生成标题…';$('#titlesDialog').showModal();
    try{
      const titles=batch.settings.provider==='mock'?[job.results.idea.title,'同一个地方，两个回不去的年份','清楚的是照片，模糊的是记忆']:Models.extractJson(await Models.callTextModel([{role:'system',content:'你是中文短视频标题作者。只返回JSON。标题具体克制、让人共鸣，不夸大事实。'},{role:'user',content:`为这组${job.oldYear}年与${job.newYear}年对照照片写6个不同标题。故事：${job.results.idea.story}。配文：${job.results.prompt.caption}。返回{"titles":["标题"]}。`}],.9,requestConfig())).titles;
      if(!Array.isArray(titles)||!titles.length)throw appError('标题格式不正确，请重试。','VALIDATION');
      $('#titles').innerHTML=titles.filter(t=>typeof t==='string').map(t=>`<button class="title-card" type="button" data-copy-title="${esc(t)}">${esc(t)}</button>`).join('');$('#titleStatus').textContent='点击标题复制';
    }catch(error){$('#titleStatus').textContent=Models.friendlyError(error);}finally{titlesBusy=false;}
  }
  async function restore(){
    const meta=await readValue('meta','latest');if(!meta?.jobIds?.length)return;
    const jobs=(await Promise.all(meta.jobIds.map(id=>readValue('jobs',id)))).filter(Boolean);if(jobs.length!==meta.jobIds.length)return;
    for(const job of jobs){
      job.paused=job.current<6&&!job.abandoned;
      for(const step of job.steps)if(['running','queued','retry'].includes(step.status)){step.status='error';step.retryAt=0;step.error={message:'页面曾中断。已完成结果保留，请手动重试这个步骤。',code:'INTERRUPTED'};}
    }
    batch={id:meta.id,createdAt:meta.createdAt,settings:{...meta.settings,config:{...Models.defaults,...meta.settings.config}},jobs,videoMode:meta.videoMode===true,video:meta.video||null,imagesCleared:Boolean(meta.imagesCleared)};selectedId=meta.selectedId||jobs[0].id;
    for(const [id,key]of [['count','count'],['brief','brief'],['platform','platform'],['yearMode','yearMode'],['providerSelect','provider'],['textLimit','textLimit'],['imageLimit','imageLimit']])if(meta.settings[key]!==undefined)$('#'+id).value=meta.settings[key];
    $('#saveImages').checked=Boolean(meta.settings.saveImages);
    recentIdeas=jobs.filter(j=>j.results.idea).map(j=>j.results.idea.title+'｜'+j.results.idea.story);
    const imagesPending=jobs.some(j=>j.current<6&&!j.abandoned);
    restored=imagesPending||Boolean(batch.videoMode&&batch.video?.status==='waiting');attachRunner();
    if(restored)runner.pauseAll();runner.start();
    if(batch.videoMode){
      attachVideo();
      try{requireVideoConnection(await videoController.refresh());videoReady=true;if(!imagesPending&&batch.video.status!=='waiting'){runner.resumeAll();restored=false;}}
      catch(error){notice(`已恢复图片缓存，视频状态尚未连接：${error.message}`,true);return;}
    }
    notice(batch.imagesCleared?'已恢复成品记录；本批图片缓存已按设置清理。':restored?'已恢复缓存，可继续未完成任务或单独重试中断步骤。':batch.videoMode?'已恢复本批视频状态和创作记录。':'已恢复旧版图片批次；新建批次后会使用自动视频流程。');
  }
  function openConfig(){
    const busy=runner?.isBusy()||videoBusy();
    $('#configPanel').querySelectorAll('input,select').forEach(el=>el.disabled=Boolean(busy&&!/key$/i.test(el.id)));
    $('#clearConfig').disabled=Boolean(busy);$('#configBatchNote').textContent=busy?'本批模型和参数已固定。可以更正 Key 后重试，其他参数在新批次中设置。':'';$('#configPanel').showModal();
  }
  $('#batchForm').addEventListener('submit',startBatch);
  $('#selectFolder').addEventListener('click',selectFolder);
  $('#count').addEventListener('input',render);
  $('#saveImages').addEventListener('change',render);
  $('#toggleConfig').addEventListener('click',openConfig);
  document.addEventListener('timeecho-config',()=>{if(batch&&runner?.isBusy())$('#providerSelect').value=batch.settings.provider;Models.updateKeyStatus();render();});
  $('#providerSelect').addEventListener('change',()=>{Models.updateKeyStatus();render();});
  $('#pauseBatch').addEventListener('click',()=>{
    const paused=runner.getSummary().pausedAll;
    if(paused){runner.resumeAll();notice('已继续整批，单独暂停的组仍保持暂停。');}else{runner.pauseAll();notice('已暂停后续派发，正在请求的步骤会继续接收结果。');}
    render();
  });
  $('#continueRestored').addEventListener('click',async()=>{
    try{
      if(batch.videoMode){requireVideoConnection(await videoController.refresh());videoReady=true;}else if(!folderName)throw appError('请先选择保存目录。','SAVE');
      runner.resumeAll();for(const job of batch.jobs)if(!job.abandoned&&!job.steps.some(s=>s.status==='error'))runner.resume(job.id);
      restored=false;runner.start();render();
    }catch(error){notice(Models.friendlyError(error),true);}
  });
  $('#newBatch').addEventListener('click',async()=>{
    if(!batch||stats().active||videoRunning())return;videoAction=true;render();
    try{
      if(batch.videoMode){try{await api('/api/video/discard',{batchId:batch.id});}catch(error){if(error.status!==404)throw error;}}
      for(const job of batch.jobs){if(job.current<6)runner.abandon(job.id);await checkpoint(job);}
      runner.dispose();videoController?.dispose();videoController=null;videoReady=false;runner=null;batch=null;selectedId=null;restored=false;await storeValue('meta','latest',null);await pruneCachedJobs([]);releasePreviews();$('#pictureFull').removeAttribute('src');$('#pictureDialog').close();notice('本批临时图片缓存已清理，可以开始新的一批；已保存文件保留。');
    }catch(error){notice(Models.friendlyError(error),true);}finally{videoAction=false;render();}
  });
  $('#openFolder').addEventListener('click',async()=>{try{await api('/api/open-folder',{});}catch(error){notice(Models.friendlyError(error),true);}});
  $('#videoOpenFolder').addEventListener('click',async()=>{try{await api('/api/video/open-folder',{batchId:batch.id});}catch(error){notice(Models.friendlyError(error),true);}});
  $('#refreshVideo').addEventListener('click',refreshVideo);
  $('#retryVideo').addEventListener('click',async()=>{
    if(videoRunning()||!allFramesReady()||runner.getSummary().pausedAll)return;
    videoAction=true;render();
    try{
      requireVideoConnection(await videoController.refresh());
      if(!batch.video.path)for(const id of batch.video.missingJobIds||[]){const job=batch.jobs.find(j=>j.id===id);if(job?.results.compose)await cacheVideoFrame(job);}
      await videoController.start();videoReady=true;
    }catch(error){notice(Models.friendlyError(error),true);}finally{videoAction=false;render();}
  });
  $('#cancelVideo').addEventListener('click',async()=>{
    if(videoAction||!videoController)return;videoAction=true;render();
    try{const state=await videoController.cancel();if(state.status==='cancelled')notice('已停止视频制作，合图保留，之后可单独重试视频。');else if(state.status==='done')notice('视频已完成并保存。');else if(state.connectionError||state.error)notice('停止结果尚未确认，请重新检查视频状态。',true);}
    catch(error){notice(Models.friendlyError(error),true);}finally{videoAction=false;render();}
  });
  $('#batchApp').addEventListener('click',async event=>{
    const button=event.target.closest('[data-action]');if(!button||button.disabled)return;
    const job=batch?.jobs.find(j=>j.id===button.dataset.job),action=button.dataset.action;
    try{
      if(action==='detail'){selectedId=selectedId===job.id?null:job.id;await saveMeta();render();return;}
      if(action==='close-detail'){selectedId=null;render();return;}
      if(!job)return;
      if(action==='pause'){
        const resuming=job.paused,changed=resuming?runner.resume(job.id):runner.pause(job.id);
        if(!changed){notice(`第 ${job.index+1} 组状态已更新，请查看最新进度。`);render();return;}
        if(resuming)notice(`第 ${job.index+1} 组已继续。`);
        else notice(`第 ${job.index+1} 组已请求暂停。${job.steps.some(s=>s.status==='running')?'当前步骤结束后停止，其他组继续。':'后续步骤已停止，其他组继续。'}`);
        render();await checkpoint(job);
      }
      if(action==='retry'){runner.resume(job.id);runner.retry(job.id);runner.start();restored=false;}
      if(action==='abandon'){
        if(batch.videoMode){videoAction=true;render();try{await api('/api/video/skip',{batchId:batch.id,jobId:job.id});}finally{videoAction=false;}}
        runner.abandon(job.id);job.abandoned=true;job.paused=false;await checkpoint(job);notice('此组已放弃，将从本批视频中排除。');
      }
      if(action==='image'){$('#pictureTitle').textContent=`${job.theme} · ${button.dataset.kind==='compose'?'对照合图':button.dataset.kind==='old'?job.oldYear:job.newYear}`;$('#pictureFull').src=imageUrl(job.results[button.dataset.kind]);$('#pictureDialog').showModal();return;}
      if(action==='copy-caption')await Models.copyText(job.results.prompt.caption+'\n'+(job.results.prompt.hashtags||[]).join(' '));
      if(action==='copy-prompts')await Models.copyText(`${job.oldYear}年旧照\n${job.results.prompt.decadePrompt}\n\n${job.newYear}年现在图\n${job.results.prompt.nowPrompt}`);
      if(action==='edit-prompts')await editPrompts(job);
      if(action==='regenerate-image'){
        runner.pause(job.id);const stage=stages[job.current];
        if(!runner.resetFrom(job.id,stage))throw appError('当前步骤仍在收尾，请稍后重试。','VALIDATION');
        await checkpoint(job);runner.resume(job.id);runner.start();notice('正在重新生成当前图片，前序灵感和提示词保持不变。');
      }
      if(action==='titles'){await generateTitles(job);return;}
      if(action==='save-again'){button.disabled=true;const saved=await saveImage(job);if(job.steps[5].status==='done'){job.results.save=saved;await checkpoint(job);}notice('合图已保存到所选目录。');}
      if(action.startsWith('copy'))Models.notify('已复制');render();
    }catch(error){notice(Models.friendlyError(error),true);render();}
  });
  $('#titles').addEventListener('click',async event=>{const button=event.target.closest('[data-copy-title]');if(button){await Models.copyText(button.dataset.copyTitle);Models.notify('标题已复制');}});
  window.addEventListener('beforeunload',event=>{if(stats().active||videoRunning()){event.preventDefault();event.returnValue='';}});
  window.addEventListener('pagehide',()=>{runner?.dispose();videoController?.dispose();releasePreviews();});
  window.TimeEchoApp={readImage};
  (async()=>{
    if(!Core||!Prompts||!Video){notice('程序文件未加载完整，请通过启动器重新打开。',true);return;}
    try{db=await openDatabase();}catch(error){dbFailure=error;notice('无法启用浏览器缓存，请允许本地存储后重试。',true);}
    await connectLocal();if(db)try{await restore();}catch(error){notice(`上次缓存未能恢复：${error.message}`,true);}
    Models.updateKeyStatus();render();
    setInterval(()=>{if(batch&&(stats().active||batch.jobs.some(j=>j.steps.some(s=>s.status==='retry'))))render();},1000);
  })();
})();
