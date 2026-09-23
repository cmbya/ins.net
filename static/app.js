const $ = s => document.querySelector(s);
let current = '', items = [];
async function api(path, data) {
  const options = data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Insnet-Action':'1'},body:JSON.stringify(data)};
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw Error(result.error || '请求失败');
  return result;
}
function notice(message, error=false) { $('#notice').textContent=message; $('#notice').classList.toggle('error',error); }
function node(tag, text, className) { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(className)e.className=className; return e; }
function stamp(s) { return s ? String(s).replace('T',' ').slice(0,19) : '日期未知'; }
async function refresh() {
  const accounts = await api('/api/accounts');
  $('#login').hidden=true; $('#workspace').hidden=false; $('#refresh').hidden=false;
  if (!accounts.some(a=>a.id===current)) current=accounts[0]?.id || '';
  $('#account').replaceChildren(...accounts.map(a=>{const o=node('option', '@'+a.username);o.value=a.id;return o}));
  $('#account').value=current;
  $('#actions').hidden=!current;
  if (!current) { $('#posts').textContent='先添加账号。'; $('#creators').textContent=''; $('#runs').textContent=''; return; }
  const [creators, posts, runs]=await Promise.all([
    api('/api/creators?account='+encodeURIComponent(current)),
    api('/api/posts?account='+encodeURIComponent(current)), api('/api/runs')]);
  renderCreators(creators); renderPosts(posts); renderRuns(runs);
}
function renderCreators(creators) {
  const host=$('#creators');host.replaceChildren();
  if (!creators.length) {host.textContent='暂无博主。可先导入关注列表。';return}
  for(const creator of creators){
    const box=node('div',undefined,'creator');
    const first=node('label');const enabled=node('input');enabled.type='checkbox';enabled.checked=!!creator.enabled;first.append(enabled,node('span','@'+creator.username));
    const second=node('label');const full=node('input');full.type='checkbox';full.checked=!!creator.full_sync;second.append(full,node('small','每次扫描全部历史（更慢）'));
    async function change(){try{await api('/api/creator',{account:current,username:creator.username,enabled:enabled.checked,full_sync:full.checked});notice('设置已保存')}catch(e){notice(e.message,true)}}
    enabled.onchange=change;full.onchange=change;box.append(first,second);host.append(box);
  }
}
function renderPosts(posts){
  $('#post-count').textContent=`最近 ${posts.length} 条`;
  const host=$('#posts');host.replaceChildren();if(!posts.length){host.textContent='还没有归档记录。';return}
  for(const post of posts){
    const card=node('article',undefined,'post');const viewer=node('div',undefined,'media');const body=node('div',undefined,'post-body');
    const labels=[post.sources.includes('saved')?'已保存':null,post.source_url.includes('/reel/')?'Reels':null,post.sources.some(s=>s.startsWith('creator:'))?'关注博主':null].filter(Boolean);
    const headline=node('strong','@'+post.username+' · '+labels.join(' · '));
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
function renderRuns(runs){const host=$('#runs');host.replaceChildren();if(!runs.length){host.textContent='暂无任务。';return}for(const run of runs){
  const row=node('div',undefined,'run');row.append(node('strong',`@${run.username} · ${run.kind} · ${run.status}`),node('small',`　${stamp(run.started_at)}　下载 ${run.downloaded} / 跳过 ${run.skipped} / 失败 ${run.failed}`));
  if(run.message)row.append(node('div',run.message,'error'));host.append(row);
}}
$('#login-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/login',{password:e.target.password.value});e.target.reset();await refresh()}catch(err){alert(err.message)}};
$('#account-form').onsubmit=async e=>{e.preventDefault();const form=e.target;try{const file=form.cookies.files[0];if(file.size>256*1024)throw Error('Cookie 文件不能超过 256 KB');const result=await api('/api/accounts',{username:form.username.value,cookies:await file.text()});current=result.id;form.reset();await refresh();notice('账号已添加')}catch(err){notice(err.message,true)}};
$('#creator-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/creators',{account:current,username:e.target.username.value});e.target.reset();await refresh();notice('已添加博主')}catch(err){notice(err.message,true)}};
$('#account').onchange=e=>{current=e.target.value;refresh().catch(err=>notice(err.message,true))};
$('#refresh').onclick=()=>refresh().catch(err=>notice(err.message,true));
document.querySelectorAll('[data-kind]').forEach(b=>b.onclick=async()=>{try{await api('/api/sync',{account:current,kind:b.dataset.kind});notice('任务已启动，可稍后刷新查看进度');await refresh()}catch(err){notice(err.message,true)}});
refresh().catch(()=>{});
