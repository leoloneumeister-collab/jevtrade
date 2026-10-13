const $=id=>document.getElementById(id);
function paintBars(root){root.querySelectorAll('.fill[data-w]').forEach(e=>{e.style.width=e.dataset.w+'%';e.style.background=e.dataset.c})}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=x=>x==null?'n/a':(x>0?'+':'')+x.toFixed(2)+'%';
const lab={ 'LEAN UP':['UP','Lean up'],'LEAN DOWN':['DOWN','Lean down'],'SIT OUT':['OUT','Sit out'] };
async function api(path){const r=await fetch(path,{headers:{'X-Jevbot':'1'}});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function busy(on){['go','bt','sc','xt'].forEach(i=>$(i).disabled=on)}
function bar(name,p,color){return `<div class="bar"><span>${name}</span><div class="track"><div class="fill" data-w="${(p*100).toFixed(0)}" data-c="${color}"></div></div><span>${(p*100).toFixed(0)}%</span></div>`}
function chart(s){
  const v=s.map(x=>x[1]),mn=Math.min(...v),mx=Math.max(...v),W=640,H=170,p=6;
  const pts=v.map((y,i)=>`${(p+i*(W-2*p)/(v.length-1)).toFixed(1)},${(H-p-(y-mn)/(mx-mn||1)*(H-2*p)).toFixed(1)}`).join(' ');
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Closing price, last ${v.length} days"><polyline fill="none" stroke="var(--accent)" stroke-width="2" points="${pts}"/></svg>
  <div class="mute axis"><span>${s[0][0]}</span><span>low ${mn.toFixed(2)} / high ${mx.toFixed(2)}</span><span>${s[s.length-1][0]}</span></div>`}
function render(d){
  const [cls,txt]=lab[d.lean],j=d.jev,f=d.features;
  const keys=[['ret_5d_pct','5 day move %'],['ret_20d_pct','20 day move %'],['rsi14','RSI (14)'],['vs_sma50_pct','vs 50 day avg %'],['vol_20d_pct','daily swing %'],['pos_in_52w_range','52 week range (0-1)']];
  $('out').innerHTML=`<div class="card">
   <div class="mute">${esc(d.symbol)} closed at ${esc(d.close)} on ${esc(d.asof)} · ${esc(d.h)} day look ahead · data: ${esc(d.source)}${d.src==='file'?' (your CSV, historical)':''}${d.demo?' (FAKE)':''}</div>
   <div class="lean ${cls}">${txt}</div><div>${esc(d.why)}</div>${d.warning?`<div class="warn mt10">${esc(d.warning)}</div>`:''}
   <div class="bars">${bar('Up',j.p_up,'var(--up)')}${bar('Flat',j.p_flat,'var(--flat)')}${bar('Down',j.p_down,'var(--down)')}</div>
   <div class="grid">
     <div class="kv"><b>${(j.uptrend*100).toFixed(0)}%</b><span>chance it is in an uptrend</span></div>
     <div class="kv"><b>${(j.overextended*100).toFixed(0)}%</b><span>chance it is overextended</span></div>
     <div class="kv"><b>${esc(j.vol_regime)}</b><span>how jumpy (${(j.vol_conf*100).toFixed(0)}% sure)</span></div>
     <div class="kv"><b>${j.entry_score.toFixed(1)} / 3</b><span>setup quality (${(j.entry_conf*100).toFixed(0)}% sure)</span></div>
   </div>
   <div class="mt14">${chart(d.series)}</div>
   <details class="mt10"><summary class="mute">What Jev was shown</summary>
     <div class="grid mt8">${keys.map(([k,n])=>`<div class="kv"><b>${f[k]??'n/a'}</b><span>${n}</span></div>`).join('')}</div>
     <p class="mute small">Model ${esc(j.model)} · answered in ${j.latency_ms} ms</p></details></div>`;paintBars($('out'))}
function statCells(s){return `<div class="grid">
   <div class="kv"><b>${s.days}</b><span>days judged</span></div>
   <div class="kv"><b>${(s.baseline_up_rate*100).toFixed(0)}%</b><span>of days the price rose anyway</span></div>
   <div class="kv"><b>${s.independent_long}</b><span>independent "lean up" calls (${s.long_signals} counting overlaps)</span></div>
   <div class="kv"><b>${s.long_hit_rate==null?'n/a':(s.long_hit_rate*100).toFixed(0)+'%'}</b><span>of those went up</span></div>
   <div class="kv"><b>${pct(s.avg_return_after_long_pct)}</b><span>avg move after "lean up"</span></div>
   <div class="kv"><b>${pct(s.avg_return_after_long_net_pct)}</b><span>same, minus ${s.cost_pct}% costs</span></div>
   <div class="kv"><b>${pct(s.avg_return_all_days_pct)}</b><span>avg move on any day</span></div>
   <div class="kv"><b>${s.sat_out}</b><span>days it sat out</span></div>
   ${s.rank_corr==null?'':`<div class="kv"><b>${pct(s.top20_avg_pct)}</b><span>avg move after Jev's most bullish 20% of days</span></div>
   <div class="kv"><b>${pct(s.bottom20_avg_pct)}</b><span>after its least bullish 20% (rank link ${s.rank_corr})</span></div>`}</div>`}
function stats(s,title){
  const cal=s.calibration.map(b=>`<tr><td>${esc(b.range)}</td><td>${b.n}</td><td>${(b.was_up*100).toFixed(0)}%</td></tr>`).join('');
  return `<h2>${esc(title)}</h2>${statCells(s)}
   <p class="verdict">${esc(s.verdict)}</p>
   ${cal?`<table><tr><th>Jev said chance of rise</th><th>days</th><th>actually rose</th></tr>${cal}</table>`:''}`}
async function ask(){
  busy(true);$('btout').innerHTML='';$('out').innerHTML='<div class="card mute">Asking Jev...</div>';
  try{const d=await api(`/api/analyze?symbol=${$('sym').value}&h=${$('h').value}&src=${$('src').value}`);render(d);loadSb()}
  catch(e){$('out').innerHTML=`<div class="card err">${esc(e.message)}</div>`}finally{busy(false)}}
async function bt(){
  busy(true);$('btout').innerHTML=`<div class="card mute">Running about ${$('n').value} questions through Jev. Takes under a minute.</div>`;
  try{const d=await api(`/api/backtest?symbol=${$('sym').value}&h=${$('h').value}&n=${$('n').value}&cost=${encodeURIComponent($('cost').value)}&src=${$('src').value}`);
    $('btout').innerHTML=`<div class="card">${stats(d.stats,'Jev, past test: '+d.symbol+(d.demo?' (FAKE prices)':d.src==='file'?' (your CSV file)':''))}
      <h2 class="mt16">The 3-line momentum rule, same days</h2>${statCells(d.rule_stats)}
      <p class="verdict">${esc(d.comparison)}</p><p class="mute small2">${esc(d.caveat)}${d.errors?` ${d.errors} questions failed and were skipped.`:''}</p></div>`}
  catch(e){$('btout').innerHTML=`<div class="card err">${esc(e.message)}</div>`}finally{busy(false)}}
async function loadSb(){
  try{const d=await api('/api/scoreboard');
    $('sb').innerHTML=d.made?`<p>${d.made} reads saved, ${d.pending} still waiting for their look-ahead period to pass.</p>${d.stats?stats(d.stats,'Results so far'):''}`:'No reads saved yet. Ask Jev about a real ticker (demo off) to start the scoreboard.'}
  catch(e){$('sb').innerHTML=`<span class="err">${esc(e.message)}</span>`}}
async function scan(){
  busy(true);$('scout').innerHTML='<span class="mute">Scanning...</span>';
  try{const d=await api(`/api/scan?symbols=${encodeURIComponent($('wl').value)}&h=${$('h').value}&src=${$('src').value}`);
    const rows=d.rows.map(r=>r.error?`<tr><td>${esc(r.symbol)}</td><td colspan="4" class="err">${esc(r.error)}</td></tr>`:
      `<tr><td><b>${esc(r.symbol)}</b></td><td class="${lab[r.lean][0]}">${lab[r.lean][1]}</td><td>${(r.p_up*100).toFixed(0)}%</td><td>${(r.p_down*100).toFixed(0)}%</td><td>${esc(r.close)} <span class="mute">${esc(r.vol_regime)}</span></td></tr>`).join('');
    $('scout').innerHTML=`<table><tr><th>Ticker</th><th>Jev's lean</th><th>Up</th><th>Down</th><th>Last close</th></tr>${rows}</table>${d.truncated?'<p class="mute">List cut to 25 tickers.</p>':''}`;loadSb()}
  catch(e){$('scout').innerHTML=`<span class="err">${esc(e.message)}</span>`}finally{busy(false)}}
$('sc').onclick=scan;
$('go').onclick=ask;$('bt').onclick=bt;$('sym').addEventListener('keydown',e=>{if(e.key==='Enter')ask()});
api('/api/status').then(s=>{if(!s.has_key){const el=$('status');el.hidden=false;el.textContent='TYPESAFE_API_KEY is not set on this computer, so Jev cannot be asked. Set it and restart the app.'}}).catch(()=>{});
loadSb();

async function fileHint(){
  const el=$('filehint');
  if($('src').value!=='file'){el.hidden=true;return}
  try{const d=await api('/api/files');el.hidden=false;
    el.textContent=d.symbols.length?'CSV files for: '+d.symbols.join(', '):'No CSV files yet. Run: jevbot import-csv FILE --symbol AAPL'}
  catch(e){el.hidden=false;el.textContent=e.message}}
$('src').onchange=fileHint;

const f3=v=>v==null?'n/a':(v>0?'+':'')+Number(v).toFixed(3);
function xTable(r){
  const row=(n,s)=>`<tr><td><b>${n}</b></td><td>${f3(s.ic_mean)}</td><td>${f3(s.ic_t)}</td><td>${pct(s.spread_mean_pct)}</td><td>${pct(s.spread_net_pct)}</td><td>${s.independent_dates}</td></tr>`;
  return `<table><tr><th></th><th>Rank link (IC)</th><th>t-stat</th><th>Top 20% minus bottom 20%</th><th>After costs</th><th>Indep. days</th></tr>${row('Jev',r.jev)}${row('Momentum rule',r.momentum)}</table>`}
function xBars(name,q){
  const m=Math.max(...q.map(Math.abs),0.01);
  return `<p class="mute small mt10">${esc(name)}: average move over the next ${'days'} by rank (Q1 least bullish ... Q5 most bullish)</p><div class="bars">`+
    q.map((v,i)=>`<div class="bar"><span>Q${i+1}</span><div class="track"><div class="fill" data-w="${(Math.abs(v)/m*100).toFixed(0)}" data-c="${v>=0?'var(--up)':'var(--down)'}"></div></div><span>${pct(v)}</span></div>`).join('')+'</div>'}
async function cross(){
  busy(true);$('xout').innerHTML='<span class="mute">Asking Jev about every stock on every day. Takes a few minutes for a long test.</span>';
  try{const r=await api(`/api/cross?symbols=${encodeURIComponent($('xs').value)}&h=${$('h').value}&n=${$('xn').value}&cost=${encodeURIComponent($('cost').value)}&src=${$('src').value}`);
    $('xout').innerHTML=`<div class="mute small">${r.n_stocks} stocks, ${r.n_dates} days (${esc(r.first_date)} to ${esc(r.last_date)}), ${r.calls} Jev questions, ${r.errors} failed${r.demo?' (FAKE prices)':''}</div>
      ${xTable(r)}${xBars('Jev',r.jev.quintiles)}${xBars('Momentum rule',r.momentum.quintiles)}
      <p class="verdict">${esc(r.verdict)}</p><p class="mute small2">${esc(r.caveat)}${r.skipped.length?' Skipped: '+esc(r.skipped.join('; ')):''}</p>`;
    paintBars($('xout'))}
  catch(e){$('xout').innerHTML=`<span class="err">${esc(e.message)}</span>`}finally{busy(false)}}
$('xt').onclick=cross;
