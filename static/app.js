const $ = s => document.querySelector(s);
let current = '', refreshTimer;
async function api(path, data) {
  const options = data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Insnet-Action':'1'},body:JSON.stringify(data)};
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw Error(result.error || '请求失败');
  return result;
}
function notice(message, error=false) { $('#notice').textContent=message; $('#notice').classList.toggle('error',error); }
function node(tag, text, className) { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(className)e.className=className; return e; }
function stamp(s) { return s ? String(s).replace('T',' ').slice(0,19) : '尚未同步'; }
function minutes(value) { const n=Number(value)||360; return n%60===0 ? `${n/60} 小时` : `${n} 分钟`; }
async function refresh() {
  clearTimeout(refreshTimer);
  const accounts=await api('/api/accounts');
  $('#login').hidden=true; $('#workspace').hidden=false; $('#refresh').hidden=false;
  if (!accounts.some(a=>a.id===current)) current=accounts[0]?.id || '';
  $('#account').replaceChildren(...accounts.map(a=>{const o=node('option', '@'+a.username);o.value=a.id;return o}));
  $('#account').value=current;
  if (!current) { $('#saved-sync').disabled=true; $('#posts').textContent='先添加账号。'; $('#creators').textContent=''; $('#runs').textContent=''; renderSettings({}); return; }
  $('#saved-sync').disabled=false;
  const [settings,creators,posts,runs]=await Promise.all([
    api('/api/settings?account='+encodeURIComponent(current)),
    api('/api/creators?account='+encodeURIComponent(current)),
    api('/api/posts?account='+encodeURIComponent(current)),
    api('/api/runs')]);
  renderSettings(settings); renderCreators(creators); renderPosts(posts); renderRuns(runs);
  refreshTimer=setTimeout(()=>refresh().catch(err=>notice(err.message,true)),runs.some(run=>run.status==='running')?3000:15000);
}
function renderCreators(creators) {
  const host=$('#creators');host.replaceChildren();
  if (!creators.length) {host.textContent='还没有监控博主。输入 Instagram 用户名即可添加。';return}
  for(const creator of creators){
    const box=node('article',undefined,'creator-card');
    const head=node('div',undefined,'creator-head');
    const avatar=node('div',undefined,'avatar');
    const fallback=node('span',(creator.username||'?').slice(0,1).toUpperCase(),'avatar-fallback');avatar.append(fallback);
    if(creator.avatar_url){const img=node('img');img.src=creator.avatar_url;img.alt='';img.referrerPolicy='no-referrer';img.onerror=()=>img.remove();avatar.append(img)}
    const identity=node('div',undefined,'creator-identity');
    identity.append(node('strong',creator.display_name||'@'+creator.username),node('small','@'+creator.username));
    const count=node('div',undefined,'creator-count');count.append(node('strong',String(creator.archived_count||0)),node('small','已归档帖子'));
    head.append(avatar,identity,count);box.append(head);
    const state=node('div',undefined,'creator-state');
    state.append(node('span',creator.enabled?'自动同步已开启':'自动同步已暂停',creator.enabled?'state-on':'state-off'));
    state.append(node('span',`上次：${stamp(creator.last_sync)}`));
    state.append(node('span',`下次：${creator.enabled?stamp(creator.next_sync_at):'已暂停'}`));
    if(creator.last_error)state.append(node('span','上次错误：'+creator.last_error,'creator-error'));
    box.append(state);
    const controls=node('div',undefined,'creator-controls');
    const enabledLabel=node('label',undefined,'check');const enabled=node('input');enabled.type='checkbox';enabled.checked=!!creator.enabled;enabledLabel.append(enabled,node('span','自动同步'));
    const modeLabel=node('label');modeLabel.append(node('span','同步范围'));const mode=node('select');
    for(const [value,label] of [['recent20','最新 20 条'],['all','全部历史（分批）']]){const option=node('option',label);option.value=value;mode.append(option)}
    mode.value=creator.sync_mode||'recent20';modeLabel.append(mode);
    const intervalLabel=node('label');intervalLabel.append(node('span','同步间隔（分钟）'));const interval=node('input');interval.type='number';interval.min='30';interval.max='10080';interval.step='1';interval.value=creator.interval_minutes||360;intervalLabel.append(interval);
    const maximumLabel=node('label');maximumLabel.append(node('span','每轮最多下载帖子'));const maximum=node('input');maximum.type='number';maximum.min='1';maximum.max='200';maximum.step='1';maximum.value=creator.max_per_run||20;maximumLabel.append(maximum);
    controls.append(enabledLabel,modeLabel,intervalLabel,maximumLabel);box.append(controls);
    const actions=node('div',undefined,'creator-actions');
    const save=node('button','保存设置');const sync=node('button','立即同步','primary');const remove=node('button','移出监控列表','danger-outline');const purge=node('button','删除归档与记录','danger');
    save.onclick=async()=>{try{await api('/api/creator',{account:current,username:creator.username,enabled:enabled.checked,sync_mode:mode.value,interval_minutes:Number(interval.value),max_per_run:Number(maximum.value)});notice('@'+creator.username+' 设置已保存');await refresh()}catch(e){notice(e.message,true)}};
    sync.onclick=async()=>{try{await api('/api/sync',{account:current,kind:'creator',username:creator.username});notice('@'+creator.username+' 同步任务已启动');await refresh()}catch(e){notice(e.message,true)}};
    remove.onclick=()=>deleteCreator(creator.username,'list');
    purge.onclick=()=>deleteCreator(creator.username,'archive');
    actions.append(save,sync,remove,purge);box.append(actions);host.append(box);
  }
}
async function deleteCreator(username,mode){
  const message=mode==='list'
    ? `将 @${username} 从监控列表移除。已下载文件和归档记录会保留；以后重新添加时仍会跳过已归档帖子。继续吗？`
    : `将删除 @${username} 的监控项、专属归档记录和对应文件。与“已保存帖子”或其他博主共享的记录及文件会保留。此操作不能撤销，继续吗？`;
  if(!confirm(message))return;
  try{const result=await api('/api/creator/delete',{account:current,username,mode});
    const suffix=mode==='archive'?`已移除 ${result.removed_posts} 条专属帖子、${result.removed_files} 个文件；保留 ${result.shared_posts} 条共享帖子。`:'';
    notice(`已处理 @${username}。${suffix}`);await refresh();
  }catch(e){notice(e.message,true)}
}
function renderPosts(posts){
  $('#post-count').textContent=`最近 ${posts.length} 条`;
  const host=$('#posts');host.replaceChildren();if(!posts.length){host.textContent='还没有归档记录。';return}
  for(const post of posts){
    const card=node('article',undefined,'post');const viewer=node('div',undefined,'media');const body=node('div',undefined,'post-body');
    const labels=[post.sources.includes('saved')?'已保存':null,post.sources.some(s=>s.startsWith('creator:'))?'监控博主':null].filter(Boolean);
    const headline=node('strong','@'+post.username+' · '+(labels.join(' · ')||'帖子'));
    const date=node('small',stamp(post.published_at)+' · '+post.status+' · '+post.media.length+' 项');
    const caption=node('p',post.caption);const link=node('a','查看原帖');link.href=post.source_url;link.target='_blank';link.rel='noopener noreferrer';
    let index=0;
    function show(){viewer.replaceChildren();const m=post.media[index];if(!m||!m.relative_path){viewer.append(node('span','媒体等待下载'));}else{
      const url=`/media/${post.id}/${encodeURIComponent(m.media_id)}`;
      const e=node(m.kind==='video'?'video':'img');e.src=url;if(m.kind==='video'){e.controls=true;e.preload='metadata'}else e.alt=`第 ${index+1} 张媒体`;viewer.append(e);
    }
      if(post.media.length>1){const nav=node('nav');const prev=node('button','‹');const next=node('button','›');const count=node('span',`${index+1} / ${post.media.length}`);prev.onclick=()=>{index=(index+post.media.length-1)%post.media.length;show()};next.onclick=()=>{index=(index+1)%post.media.length;show()};nav.append(prev,count,next);viewer.append(nav)}
    }
    show();body.append(headline,date,caption,link);card.append(viewer,body);host.append(card);
  }
}
function renderRuns(runs){const host=$('#runs');const expanded=new Set([...host.querySelectorAll('details[open]')].map(el=>el.dataset.runId));host.replaceChildren();if(!runs.length){host.textContent='暂无任务。';return}for(const run of runs){
  const row=node('div',undefined,'run');row.append(node('strong',`@${run.username} · ${run.kind} · ${run.status}`),node('small',`　${stamp(run.started_at)}　下载 ${run.downloaded} / 跳过 ${run.skipped} / 失败 ${run.failed}`));
  if(run.message)row.append(node('pre',run.message,'run-error'));
  const detail=node('details',undefined,'run-logs');detail.dataset.runId=run.id;detail.ontoggle=()=>{if(detail.open)loadRunLogs(run.id,detail)};
  detail.append(node('summary',`查看运行日志（${run.log_count||0} 条）`));row.append(detail);host.append(row);
  if(expanded.has(run.id)||run.status==='failed'||run.status==='partial')detail.open=true;
}}
async function loadRunLogs(runId,container){
  const old=container.querySelector('.log-lines');if(old)old.remove();
  const list=node('div','正在读取日志…','log-lines');container.append(list);
  try{const lines=await api('/api/runlogs?run='+encodeURIComponent(runId));list.replaceChildren();
    if(!lines.length){list.textContent='暂无日志';return}
    for(const item of lines){const line=node('div',`${stamp(item.created_at)} [${item.level}]${item.source?' ['+item.source+']':''} ${item.message}`,'log-line');list.append(line)}
  }catch(err){list.textContent='读取日志失败：'+err.message}
}
function renderSettings(settings){
  const form=$('#settings-form');form.elements.namedItem('media_subdir').value=settings.media_subdir||'Instagram';
  form.elements.namedItem('auto_saved').checked=Boolean(settings.auto_saved);
  form.elements.namedItem('saved_interval_minutes').value=settings.saved_interval_minutes||360;
  $('#save-path').textContent=settings.media_path?`当前容器路径：${settings.media_path}（NAS 根目录由 Compose 映射到 /archive）`:'';
}
$('#login-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/login',{password:e.target.password.value});e.target.reset();await refresh()}catch(err){alert(err.message)}};
$('#account-form').onsubmit=async e=>{e.preventDefault();const form=e.target;try{const file=form.cookies.files[0];if(file.size>256*1024)throw Error('Cookie 文件不能超过 256 KB');const result=await api('/api/accounts',{username:form.username.value,cookies:await file.text()});current=result.id;form.reset();await refresh();notice('账号已添加')}catch(err){notice(err.message,true)}};
$('#creator-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/creators',{account:current,username:e.target.username.value});e.target.reset();await refresh();notice('博主已加入监控列表')}catch(err){notice(err.message,true)}};
$('#settings-form').onsubmit=async e=>{e.preventDefault();try{const f=e.target;const settings=await api('/api/settings',{account:current,media_subdir:f.elements.namedItem('media_subdir').value,auto_saved:f.elements.namedItem('auto_saved').checked,saved_interval_minutes:Number(f.elements.namedItem('saved_interval_minutes').value)});renderSettings(settings);notice('设置已保存')}catch(err){notice(err.message,true)}};
$('#saved-sync').onclick=async()=>{try{await api('/api/sync',{account:current,kind:'saved'});notice('已保存帖子同步任务已启动');await refresh()}catch(err){notice(err.message,true)}};
$('#account').onchange=e=>{current=e.target.value;refresh().catch(err=>notice(err.message,true))};
$('#refresh').onclick=()=>refresh().catch(err=>notice(err.message,true));
api('/api/version').then(version=>{$('#build-version').textContent='版本 '+version.version}).catch(()=>{$('#build-version').textContent='版本未知'});
refresh().catch(()=>{});
