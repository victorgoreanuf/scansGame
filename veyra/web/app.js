const $ = id => document.getElementById(id);
let waveData = {};
let sse = null;

// ── Toast Notifications ─────────────────────────────────────────────────────

function toast(msg, type = 'info') {
  const container = $('toasts');
  const el = document.createElement('div');
  el.className = 'toast toast-' + type;
  el.textContent = msg;
  container.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 300);
  }, 3500);
}

// ── Helpers ──────────────────────────────────────────────────────────────────

const GRADS = [
  'linear-gradient(135deg,#6366f1,#818cf8)','linear-gradient(135deg,#8b5cf6,#a78bfa)',
  'linear-gradient(135deg,#ec4899,#f472b6)','linear-gradient(135deg,#ef4444,#f87171)',
  'linear-gradient(135deg,#f59e0b,#fbbf24)','linear-gradient(135deg,#10b981,#34d399)',
  'linear-gradient(135deg,#06b6d4,#22d3ee)','linear-gradient(135deg,#3b82f6,#60a5fa)',
  'linear-gradient(135deg,#f97316,#fb923c)','linear-gradient(135deg,#14b8a6,#2dd4bf)',
];
function hash(s){let h=0;for(let i=0;i<s.length;i++)h=((h<<5)-h)+s.charCodeAt(i);return Math.abs(h);}
function ini(name){return name.split(' ').map(w=>w[0]).join('').toUpperCase().slice(0,2);}
function grad(name){return GRADS[hash(name)%GRADS.length];}
function fmt(n){if(n>=1e6)return(n/1e6).toFixed(1).replace(/\.0$/,'')+'M';if(n>=1e3)return(n/1e3).toFixed(1).replace(/\.0$/,'')+'K';return n.toString();}

function parseGoal(str) {
  str = (str||'').trim().toLowerCase().replace(/,/g,'');
  if (!str || str==='0') return 0;
  const m = str.match(/^(\d+(?:\.\d+)?)\s*([km])?$/);
  if (!m) return parseInt(str)||0;
  let n = parseFloat(m[1]);
  if (m[2]==='k') n*=1000;
  if (m[2]==='m') n*=1000000;
  return Math.round(n);
}

function fmtGoal(n) {
  if (n <= 0) return '0';
  if (n >= 1e6 && n % 1e6 === 0) return (n/1e6)+'m';
  if (n >= 1e3 && n % 1e3 === 0) return (n/1e3)+'k';
  return n.toLocaleString();
}

function toggleSettings(){} // backward compat

function toggleWave(gridId, on) {
  $(gridId).querySelectorAll('.mc').forEach(card => {
    card.querySelector('.switch input').checked = on;
    card.classList.toggle('off', !on);
  });
}

function setGoal(btn, val) {
  const card = btn.closest('.mc');
  card.querySelector('.f-goal').value = fmtGoal(parseInt(val));
  card.querySelectorAll('.chip').forEach(c => c.classList.remove('on'));
  btn.classList.add('on');
}

// ── Auth ─────────────────────────────────────────────────────────────────────

async function checkSession() {
  try {
    const res = await fetch('/api/session');
    const data = await res.json();
    if (data.ok) { showApp(data); return; }
  } catch(e) {}
  showLogin();
}

function showLogin() {
  $('loginScreen').classList.remove('hidden');
  $('mainApp').style.display = 'none';
  const savedEmail = localStorage.getItem('veyra_email');
  if (savedEmail) $('loginEmail').value = savedEmail;
}

function showApp(data) {
  $('loginScreen').classList.add('hidden');
  $('mainApp').style.display = '';

  if (data.waves) {
    waveData = {};
    for (const [k,v] of Object.entries(data.waves)) waveData[k] = v;
    renderWave('1', waveData['1']||[], 'w1grid', 'w1badge', 'w1empty', 0);
    renderWave('2', waveData['2']||[], 'w2grid', 'w2badge', 'w2empty', 3000000);
  }

  $('dot').classList.add('on');
  $('statusText').textContent = 'Connected';

  if (data.running) {
    $('startBtn').disabled = true;
    $('stopBtn').disabled = false;
  }
  if (data.stats) {
    $('stKilled').textContent = fmtGoal(data.stats.killed);
    $('stDmg').textContent = fmtGoal(data.stats.damage);
    $('stStam').textContent = fmtGoal(data.stats.stamina);
    $('stLooted').textContent = fmtGoal(data.stats.looted || 0);
  }
  updatePvPStatus(data.pvp_running, data.pvp_stats);
  updateStatStatus(data.stat_running, data.stat_stats);
  updateQuestStatus(data.quest_running, data.quest_stats);

  startSSE();
  startPolling();
  fetchProfiles();
}

async function login(e) {
  e.preventDefault();
  const btn = $('loginBtn');
  const err = $('loginError');
  btn.disabled = true; btn.textContent = 'Connecting...'; err.textContent = '';
  try {
    const email = $('loginEmail').value;
    const password = $('loginPassword').value;
    const res = await fetch('/api/connect', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({email, password}),
    });
    const data = await res.json();
    if (data.ok) {
      localStorage.setItem('veyra_email', email);
      showApp(data);
    } else {
      err.textContent = data.error || 'Login failed';
    }
  } catch(ex) {
    err.textContent = ex.message;
  }
  btn.disabled = false; btn.textContent = 'Connect';
}

async function logout() {
  await fetch('/api/logout', {method:'POST'});
  if (sse) { sse.close(); sse = null; }
  if (statusPoll) { clearInterval(statusPoll); statusPoll = null; }
  showLogin();
}

async function refreshWaves() {
  const btn = $('refreshBtn');
  btn.disabled = true; btn.classList.add('loading');
  startSSE();
  try {
    const res = await fetch('/api/refresh', {method:'POST'});
    const data = await res.json();
    if (data.ok) {
      waveData = {};
      for (const [k,v] of Object.entries(data.waves)) waveData[k] = v;
      renderWave('1', waveData['1']||[], 'w1grid', 'w1badge', 'w1empty', 0);
      renderWave('2', waveData['2']||[], 'w2grid', 'w2badge', 'w2empty', 3000000);
      toast('Waves refreshed', 'success');
    } else if (data.error === 'Not authenticated') {
      showLogin();
    } else {
      toast(data.error || 'Refresh failed', 'error');
    }
  } catch(e) { toast(e.message, 'error'); }
  btn.disabled = false; btn.classList.remove('loading');
}

function renderWave(waveNum, groups, gridId, badgeId, emptyId, defaultGoal) {
  const grid = $(gridId);
  const empty = $(emptyId);
  if (empty) empty.remove();
  grid.querySelectorAll('.mc').forEach(el => el.remove());

  const total = groups.reduce((s,g) => s+g.count, 0);
  $(badgeId).textContent = groups.length+' types \u00b7 '+total+' alive';

  const presets = defaultGoal === 0
    ? [{l:'1x Hit',v:'0'},{l:'70K',v:'70000'},{l:'500K',v:'500000'},{l:'1M',v:'1000000'},{l:'3M',v:'3000000'}]
    : [{l:'1x Hit',v:'0'},{l:'70K',v:'70000'},{l:'500K',v:'500000'},{l:'1M',v:'1000000'},{l:'3M',v:'3000000'},{l:'5M',v:'5000000'},{l:'10M',v:'10000000'}];

  const defStr = fmtGoal(defaultGoal);

  groups.forEach((g, idx) => {
    const card = document.createElement('div');
    card.className = 'mc';
    card.dataset.name = g.name;
    card.dataset.wave = waveNum;
    card.dataset.ids = JSON.stringify(g.ids);
    card.dataset.instances = JSON.stringify(g.instances);
    card.style.animationDelay = (idx * 40) + 'ms';

    const hasImg = g.image && g.image.length > 5;
    const chipHtml = presets.map(p =>
      `<button type="button" class="chip${p.v===String(defaultGoal)?' on':''}" onclick="setGoal(this,'${p.v}')">${p.l}</button>`
    ).join('');

    card.innerHTML = `
      <div class="mc-top">
        <div class="mc-avatar" style="background:${grad(g.name)}">
          ${hasImg ? '<img src="'+g.image+'" onerror="this.remove()">' : ''}
          <span>${ini(g.name)}</span>
        </div>
        <div class="mc-info">
          <div class="mc-name">${g.name}</div>
          <div class="mc-meta">${g.new_count||0} new${g.joined_count ? ' \u00b7 <span class="jn">'+g.joined_count+' joined</span>' : ''} &middot; HP: <span class="hp">${fmt(g.max_hp)}</span>${g.total_your_dmg ? ' &middot; DMG: <span class="yd">'+fmt(g.total_your_dmg)+'</span>' : ''}</div>
        </div>
        <label class="switch">
          <input type="checkbox" checked onchange="this.closest('.mc').classList.toggle('off',!this.checked)">
          <span class="track"></span>
        </label>
      </div>
      <div class="mc-controls">
        <div class="mc-selects">
          <div class="mc-field">
            <label>Priority</label>
            <select class="f-priority">${Array.from({length:10},(_,i)=>'<option value="'+(i+1)+'"'+(i===idx?' selected':'')+'>'+('#'+(i+1))+'</option>').join('')}</select>
          </div>
          <div class="mc-field">
            <label>Stamina</label>
            <select class="f-stamina">
              <option value="1 Stamina">1</option>
              <option value="10 Stamina" selected>10</option>
              <option value="50 Stamina">50</option>
              <option value="100 Stamina">100</option>
              <option value="200 Stamina">200</option>
            </select>
          </div>
        </div>
        <div class="mc-goal">
          <label>Damage Goal <span class="hint">type a number, use k/m (e.g. 70k, 3m) &middot; 0 = hit once</span></label>
          <div class="goal-row">
            <input type="text" class="f-goal" value="${defStr}" onfocus="this.select()" oninput="this.closest('.mc').querySelectorAll('.chip').forEach(c=>c.classList.remove('on'))">
            <div class="chips">${chipHtml}</div>
          </div>
        </div>
      </div>`;
    grid.appendChild(card);
  });
}

// ── Start / Stop ────────────────────────────────────────────────────────────

async function start() {
  const cards = document.querySelectorAll('.mc');
  const targets = [];
  cards.forEach(card => {
    if (!card.querySelector('.switch input').checked) return;
    const damage_goal = parseGoal(card.querySelector('.f-goal').value);
    targets.push({
      name: card.dataset.name,
      wave: parseInt(card.dataset.wave),
      instances: JSON.parse(card.dataset.instances),
      ids: JSON.parse(card.dataset.ids),
      priority: parseInt(card.querySelector('.f-priority').value),
      stamina: card.querySelector('.f-stamina').value,
      damage_goal,
    });
  });
  if (!targets.length) { toast('Enable at least one monster type', 'warning'); return; }

  $('startBtn').disabled = true;
  $('stopBtn').disabled = false;
  startSSE();
  const res = await fetch('/api/start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({targets})});
  const data = await res.json();
  if (!data.ok) { toast(data.error, 'error'); $('startBtn').disabled=false; $('stopBtn').disabled=true; return; }
}

async function stop() {
  await fetch('/api/stop',{method:'POST'});
  $('startBtn').disabled=false; $('stopBtn').disabled=true;
}

// ── SSE / Polling ───────────────────────────────────────────────────────────

function startSSE() {
  if (sse) sse.close();
  sse = new EventSource('/api/logs');
  sse.onmessage = e => {
    const entry = JSON.parse(e.data);
    const div = document.createElement('div');
    div.className = 'l';
    const m = entry.msg;
    if (m.startsWith('===')||m.startsWith('[')) div.classList.add('l-h');
    else if (m.includes('WON')) div.classList.add('l-ok');
    else if (m.includes('Goal reached')||m.includes('successful')||m.includes('Ready')||m.includes('COMPLETE')||m.includes('died')) div.classList.add('l-ok');
    else if (m.includes('LOST')||m.includes('Error')||m.includes('failed')||m.includes('STAMINA')) div.classList.add('l-err');
    else if (m.includes('Skipping')||m.includes('Skipped')||m.includes('Already dead')) div.classList.add('l-skip');
    else if (m.includes('%]')) div.classList.add('l-p');
    else if (m.startsWith('[PvP]')) div.classList.add('l-pvp');
    else if (m.startsWith('[Quest]')) div.classList.add('l-quest');
    div.textContent = m;
    $('logBox').appendChild(div);
    while ($('logBox').childElementCount > 200) {
      $('logBox').removeChild($('logBox').firstChild);
    }
    $('logBox').scrollTop = $('logBox').scrollHeight;
  };
}
function clearLog(){$('logBox').innerHTML='';}

let statusPoll = null;
function fmtTime(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sc = s%60;
  return (h>0?h+':':'') + m.toString().padStart(h>0?2:1,'0') + ':' + sc.toString().padStart(2,'0');
}

function startPolling() {
  if (statusPoll) clearInterval(statusPoll);
  statusPoll = setInterval(async() => {
    try {
      const s = await (await fetch('/api/status')).json();
      if(s.stats) {
        $('stKilled').textContent = fmtGoal(s.stats.killed);
        $('stDmg').textContent = fmtGoal(s.stats.damage);
        $('stStam').textContent = fmtGoal(s.stats.stamina);
        $('stLooted').textContent = fmtGoal(s.stats.looted || 0);
        if(s.stats.start_time > 0) {
          if (s.running) {
            const elap = Math.floor(Date.now()/1000 - s.stats.start_time);
            $('stTime').textContent = fmtTime(elap);
          }
        } else {
          $('stTime').textContent = '00:00';
        }
      }
      if(!s.running) {
        $('startBtn').disabled = false;
        $('stopBtn').disabled = true;
      }
      updatePvPStatus(s.pvp_running, s.pvp_stats);
      updateStatStatus(s.stat_running, s.stat_stats);
      updateQuestStatus(s.quest_running, s.quest_stats);
    } catch(e){}
  }, 1000);
}

// ── PvP ─────────────────────────────────────────────────────────────────────

async function startPvP() {
  $('pvpStartBtn').disabled = true;
  startSSE();
  try {
    const res = await fetch('/api/pvp/start', {method:'POST'});
    const data = await res.json();
    if (data.ok) {
      $('pvpStartBtn').style.display = 'none';
      $('pvpStopBtn').style.display = '';
      $('pvpBadge').textContent = 'FIGHTING';
      $('pvpBadge').className = 'pvp-badge on';
    } else {
      toast(data.error || 'Failed to start PvP', 'error');
      $('pvpStartBtn').disabled = false;
    }
  } catch(e) {
    toast('PvP error: ' + e.message, 'error');
    $('pvpStartBtn').disabled = false;
  }
}

async function stopPvP() {
  await fetch('/api/pvp/stop', {method:'POST'});
  $('pvpStartBtn').style.display = '';
  $('pvpStartBtn').disabled = false;
  $('pvpStopBtn').style.display = 'none';
  $('pvpBadge').textContent = 'OFF';
  $('pvpBadge').className = 'pvp-badge off';
}

function updatePvPStatus(pvpRunning, pvpStats) {
  if (pvpRunning) {
    $('pvpStartBtn').style.display = 'none';
    $('pvpStopBtn').style.display = '';
    $('pvpBadge').textContent = 'FIGHTING';
    $('pvpBadge').className = 'pvp-badge on';
  } else {
    $('pvpStartBtn').style.display = '';
    $('pvpStartBtn').disabled = false;
    $('pvpStopBtn').style.display = 'none';
    $('pvpBadge').textContent = 'OFF';
    $('pvpBadge').className = 'pvp-badge off';
  }
  if (pvpStats && pvpStats.matches > 0) {
    $('pvpStat').textContent = pvpStats.matches + ' played \u00b7 ' + pvpStats.wins + 'W/' + pvpStats.losses + 'L' + (pvpStats.tokens > 0 ? ' \u00b7 ' + pvpStats.tokens + ' tokens' : '');
  } else if (pvpStats && pvpStats.tokens > 0) {
    $('pvpStat').textContent = pvpStats.tokens + ' tokens';
  }
}

// ── Stat Allocator ──────────────────────────────────────────────────────────

async function startStats() {
  const target = $('statTarget').value;
  $('statStartBtn').disabled = true;
  startSSE();
  try {
    const res = await fetch('/api/stats/start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target})});
    const data = await res.json();
    if (data.ok) {
      $('statStartBtn').style.display = 'none';
      $('statStopBtn').style.display = '';
      $('statBadge').textContent = target.toUpperCase();
      $('statBadge').className = 'pvp-badge on';
      $('statTarget').disabled = true;
    } else {
      toast(data.error || 'Failed to start stat allocator', 'error');
      $('statStartBtn').disabled = false;
    }
  } catch(e) {
    toast('Stat allocator error: ' + e.message, 'error');
    $('statStartBtn').disabled = false;
  }
}

async function stopStats() {
  await fetch('/api/stats/stop', {method:'POST'});
  $('statStartBtn').style.display = '';
  $('statStartBtn').disabled = false;
  $('statStopBtn').style.display = 'none';
  $('statBadge').textContent = 'OFF';
  $('statBadge').className = 'pvp-badge off';
  $('statTarget').disabled = false;
}

function updateStatStatus(statRunning, statStats) {
  if (statRunning) {
    $('statStartBtn').style.display = 'none';
    $('statStopBtn').style.display = '';
    $('statBadge').textContent = (statStats && statStats.target ? statStats.target.toUpperCase() : 'ON');
    $('statBadge').className = 'pvp-badge on';
    $('statTarget').disabled = true;
    if (statStats && statStats.target) $('statTarget').value = statStats.target;
  } else {
    $('statStartBtn').style.display = '';
    $('statStartBtn').disabled = false;
    $('statStopBtn').style.display = 'none';
    $('statBadge').textContent = 'OFF';
    $('statBadge').className = 'pvp-badge off';
    $('statTarget').disabled = false;
  }
  if (statStats && (statStats.attack > 0 || statStats.defense > 0 || statStats.stamina > 0)) {
    $('statDisplay').textContent = 'ATK:' + statStats.attack + ' DEF:' + statStats.defense + ' STA:' + statStats.stamina + ' | Unspent:' + statStats.unspent + (statStats.allocated > 0 ? ' | Allocated:' + statStats.allocated : '');
  }
}

// ── Quests ──────────────────────────────────────────────────────────────────

async function startQuest() {
  $('questStartBtn').disabled = true;
  startSSE();

  // Collect enabled farming targets for fallback mode
  const cards = document.querySelectorAll('.mc');
  const targets = [];
  cards.forEach(card => {
    if (!card.querySelector('.switch input').checked) return;
    targets.push({
      name: card.dataset.name,
      wave: parseInt(card.dataset.wave),
      ids: JSON.parse(card.dataset.ids),
      priority: parseInt(card.querySelector('.f-priority').value),
      stamina: card.querySelector('.f-stamina').value,
      damage_goal: parseGoal(card.querySelector('.f-goal').value),
    });
  });

  try {
    const res = await fetch('/api/quest/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({targets}),
    });
    const data = await res.json();
    if (data.ok) {
      $('questStartBtn').style.display = 'none';
      $('questStopBtn').style.display = '';
      $('questBadge').textContent = 'RUNNING';
      $('questBadge').className = 'pvp-badge on';
      // Disable wave farmer start while quest is running
      $('startBtn').disabled = true;
    } else {
      toast(data.error || 'Failed to start quests', 'error');
      $('questStartBtn').disabled = false;
    }
  } catch(e) {
    toast('Quest error: ' + e.message, 'error');
    $('questStartBtn').disabled = false;
  }
}

async function stopQuest() {
  await fetch('/api/quest/stop', {method: 'POST'});
  $('questStartBtn').style.display = '';
  $('questStartBtn').disabled = false;
  $('questStopBtn').style.display = 'none';
  $('questBadge').textContent = 'OFF';
  $('questBadge').className = 'pvp-badge off';
  $('startBtn').disabled = false;
}

function updateQuestStatus(questRunning, questStats) {
  if (questRunning) {
    $('questStartBtn').style.display = 'none';
    $('questStopBtn').style.display = '';
    $('questBadge').className = 'pvp-badge on';
    $('startBtn').disabled = true;

    if (questStats && questStats.fallback) {
      $('questBadge').textContent = 'FARMING';
    } else {
      $('questBadge').textContent = 'RUNNING';
    }
  } else {
    $('questStartBtn').style.display = '';
    $('questStartBtn').disabled = false;
    $('questStopBtn').style.display = 'none';
    $('questBadge').textContent = 'OFF';
    $('questBadge').className = 'pvp-badge off';
  }

  const display = $('questDisplay');
  if (!questStats) { display.textContent = ''; return; }

  let parts = [];
  if (questStats.current) {
    parts.push(questStats.current);
  }
  if (questStats.progress) {
    parts.push(questStats.progress);
  }
  if (questStats.completed > 0) {
    parts.push(questStats.completed + ' completed');
  }
  if (questStats.fallback) {
    parts.push('(wave farming)');
  }
  display.textContent = parts.join(' \u00b7 ');
}

// ── Profiles ────────────────────────────────────────────────────────────────

let backendProfiles = {};

async function fetchProfiles() {
  try {
    const res = await fetch('/api/profiles');
    backendProfiles = await res.json();
    const s = $('profileSelect');
    const val = s.value;
    s.innerHTML = '<option value="">Select profile...</option>';
    for (const name of Object.keys(backendProfiles)) {
      const opt = document.createElement('option');
      opt.value = name; opt.textContent = name;
      s.appendChild(opt);
    }
    if (backendProfiles[val]) s.value = val;
  } catch(e) { console.error('Error fetching profiles', e); }
}

async function saveProfile() {
  const name = prompt('Profile name:');
  if (!name || !name.trim()) return;
  const cards = document.querySelectorAll('.mc');
  const conf = {};
  cards.forEach(card => {
    conf[card.dataset.name] = {
      enabled: card.querySelector('.switch input').checked,
      priority: card.querySelector('.f-priority').value,
      stamina: card.querySelector('.f-stamina').value,
      goal: String(parseGoal(card.querySelector('.f-goal').value))
    };
  });
  const res = await fetch('/api/profiles', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: name.trim(), profile: conf})
  });
  if (res.ok) {
    await fetchProfiles();
    $('profileSelect').value = name.trim();
    toast('Profile "' + name.trim() + '" saved', 'success');
  }
}

function loadProfile() {
  const name = $('profileSelect').value;
  if (!name) return toast('Select a profile first', 'warning');
  const conf = backendProfiles[name];
  if (!conf) return;
  const cards = document.querySelectorAll('.mc');
  let loaded = 0;
  cards.forEach(card => {
    const c = conf[card.dataset.name];
    if (c) {
      const sw = card.querySelector('.switch input');
      if (sw.checked !== c.enabled) {
        sw.checked = c.enabled;
        card.classList.toggle('off', !c.enabled);
      }
      card.querySelector('.f-priority').value = c.priority;
      card.querySelector('.f-stamina').value = c.stamina;
      card.querySelector('.f-goal').value = fmtGoal(Number(c.goal) || 0);
      card.querySelectorAll('.chip').forEach(ch => ch.classList.remove('on'));
      loaded++;
    }
  });
  if (loaded > 0) toast('Profile "' + name + '" loaded', 'success');
  else toast('No matching monsters in visible waves', 'warning');
}

async function deleteProfile() {
  const name = $('profileSelect').value;
  if (!name) return toast('Select a profile first', 'warning');
  if (!confirm('Delete profile "' + name + '"?')) return;
  const res = await fetch('/api/profiles', {
    method: 'DELETE', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name})
  });
  if (res.ok) {
    await fetchProfiles();
    toast('Profile deleted', 'info');
  }
}

// ── Init ────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  $('pvpBadge').className = 'pvp-badge off';
  $('statBadge').className = 'pvp-badge off';
  $('questBadge').className = 'pvp-badge off';
  checkSession();
});
