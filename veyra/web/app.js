const $ = id => document.getElementById(id);
let waveData = {};
let currentWave = null;
let sse = null;

const WAVE_DEFAULT_GOALS = { 1: 0, 2: 3000000, 3: 5000000, 4: 10000000 };

// Per-monster goal overrides: MONSTER_DEFAULT_GOALS[waveNum][name] wins over
// WAVE_DEFAULT_GOALS[waveNum]. Use whole numbers (e.g. 3_500_000 for 3.5m).
const MONSTER_DEFAULT_GOALS = {
  2: {
    'Lizardman Shadowclaw': 3500000,
  },
};

function monsterDefaultGoal(waveNum, name, fallback) {
  const wave = MONSTER_DEFAULT_GOALS[waveNum];
  return (wave && wave[name] != null) ? wave[name] : fallback;
}

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
  if (n >= 1e6) {
    const m = n / 1e6;
    // Render up to one decimal: 3.5m, 10m, 2.7m
    if (Math.abs(m * 10 - Math.round(m * 10)) < 0.001) {
      return (Math.round(m * 10) / 10) + 'm';
    }
  }
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

function toggleCurrentWave(on) {
  const grid = document.querySelector('.monster-grid[data-wave="' + currentWave + '"]');
  if (!grid) return;
  grid.querySelectorAll('.mc').forEach(card => {
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
    renderAllWaves();
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
      renderAllWaves();
      toast('Waves refreshed', 'success');
    } else if (data.error === 'Not authenticated') {
      showLogin();
    } else {
      toast(data.error || 'Refresh failed', 'error');
    }
  } catch(e) { toast(e.message, 'error'); }
  btn.disabled = false; btn.classList.remove('loading');
}

function renderWaveGrid(grid, waveNum, groups, defaultGoal) {
  grid.innerHTML = '';

  if (!groups.length) {
    grid.innerHTML = `<div class="empty"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg><div>No monsters in this wave</div></div>`;
    return;
  }

  const presets = defaultGoal === 0
    ? [{l:'1x Hit',v:'0'},{l:'70K',v:'70000'},{l:'500K',v:'500000'},{l:'1M',v:'1000000'},{l:'3M',v:'3000000'}]
    : [{l:'1x Hit',v:'0'},{l:'70K',v:'70000'},{l:'500K',v:'500000'},{l:'1M',v:'1000000'},{l:'3M',v:'3000000'},{l:'5M',v:'5000000'},{l:'10M',v:'10000000'}];

  const waveNumInt = parseInt(waveNum);

  groups.forEach((g, idx) => {
    const monsterGoal = monsterDefaultGoal(waveNumInt, g.name, defaultGoal);
    const defStr = fmtGoal(monsterGoal);

    const card = document.createElement('div');
    card.className = 'mc off';
    card.dataset.name = g.name;
    card.dataset.wave = waveNum;
    card.dataset.ids = JSON.stringify(g.ids);
    card.dataset.instances = JSON.stringify(g.instances);
    card.style.animationDelay = (idx * 40) + 'ms';

    const hasImg = g.image && g.image.length > 5;
    const chipHtml = presets.map(p =>
      `<button type="button" class="chip${p.v===String(monsterGoal)?' on':''}" onclick="setGoal(this,'${p.v}')">${p.l}</button>`
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
          <input type="checkbox" onchange="this.closest('.mc').classList.toggle('off',!this.checked)">
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

function renderAllWaves() {
  const pills = $('wavePills');
  const container = $('wavesContainer');
  const empty = $('wavesEmpty');
  if (empty) empty.remove();

  pills.innerHTML = '';
  container.querySelectorAll('.monster-grid').forEach(el => el.remove());

  const waveKeys = Object.keys(waveData).sort((a, b) => parseInt(a) - parseInt(b));
  if (!waveKeys.length) {
    container.innerHTML = `<div class="empty"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg><div>Connect to load monsters</div></div>`;
    return;
  }

  if (!currentWave || !waveData[currentWave]) currentWave = parseInt(waveKeys[0]);

  waveKeys.forEach(wk => {
    const wn = parseInt(wk);
    const groups = waveData[wk] || [];
    const total = groups.reduce((s, g) => s + g.count, 0);

    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'wave-pill' + (wn === currentWave ? ' active' : '');
    pill.dataset.wave = wn;
    pill.onclick = () => selectWave(wn);
    pill.innerHTML = `Wave ${wn} <span class="wave-pill-count">${total}</span>`;
    pills.appendChild(pill);

    const grid = document.createElement('div');
    grid.className = 'monster-grid';
    grid.dataset.wave = wn;
    if (wn !== currentWave) grid.hidden = true;
    container.appendChild(grid);
    renderWaveGrid(grid, String(wn), groups, WAVE_DEFAULT_GOALS[wn] || 0);
  });

  updateWaveCountRow();
}

function selectWave(wn) {
  currentWave = wn;
  document.querySelectorAll('.wave-pill').forEach(p => {
    p.classList.toggle('active', parseInt(p.dataset.wave) === wn);
  });
  document.querySelectorAll('.monster-grid[data-wave]').forEach(g => {
    g.hidden = parseInt(g.dataset.wave) !== wn;
  });
  updateWaveCountRow();
}

function updateWaveCountRow() {
  const row = $('waveCountRow');
  if (!row) return;
  const groups = waveData[currentWave] || [];
  const total = groups.reduce((s, g) => s + g.count, 0);
  row.textContent = groups.length + ' types \u00b7 ' + total + ' alive';
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

function addStatGoal(stat, target) {
  const container = $('statGoals');
  const idx = container.children.length + 1;
  const row = document.createElement('div');
  row.className = 'stat-goal-row';
  row.innerHTML = `
    <span class="stat-goal-num">${idx}</span>
    <select class="stat-select sg-stat">
      <option value="attack"${stat==='attack'?' selected':''}>Attack</option>
      <option value="defense"${stat==='defense'?' selected':''}>Defense</option>
      <option value="stamina"${stat==='stamina'?' selected':''}>Stamina</option>
    </select>
    <input type="number" class="stat-goal-input sg-target" value="${target||''}" placeholder="Target" min="1">
    <span class="stat-goal-progress"></span>
    <button class="stat-goal-rm" onclick="this.closest('.stat-goal-row').remove();renumberGoals()">&times;</button>
  `;
  container.appendChild(row);
}

function renumberGoals() {
  $('statGoals').querySelectorAll('.stat-goal-row').forEach((row, i) => {
    row.querySelector('.stat-goal-num').textContent = i + 1;
  });
}

function getStatGoals() {
  const goals = [];
  $('statGoals').querySelectorAll('.stat-goal-row').forEach(row => {
    const stat = row.querySelector('.sg-stat').value;
    const target = parseInt(row.querySelector('.sg-target').value);
    if (stat && target > 0) goals.push({stat, target});
  });
  return goals;
}

function setStatUIDisabled(disabled) {
  $('statGoals').querySelectorAll('select, input, .stat-goal-rm').forEach(el => el.disabled = disabled);
  $('statAddGoalBtn').disabled = disabled;
  $('statDefaultStat').disabled = disabled;
}

async function startStats() {
  const goals = getStatGoals();
  const default_stat = $('statDefaultStat').value;
  $('statStartBtn').disabled = true;
  startSSE();
  try {
    const res = await fetch('/api/stats/start', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({goals, default_stat})
    });
    const data = await res.json();
    if (data.ok) {
      $('statStartBtn').style.display = 'none';
      $('statStopBtn').style.display = '';
      $('statBadge').textContent = 'RUNNING';
      $('statBadge').className = 'pvp-badge on';
      setStatUIDisabled(true);
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
  setStatUIDisabled(false);
}

function updateStatStatus(statRunning, statStats) {
  if (statRunning) {
    $('statStartBtn').style.display = 'none';
    $('statStopBtn').style.display = '';
    $('statBadge').textContent = 'RUNNING';
    $('statBadge').className = 'pvp-badge on';
    setStatUIDisabled(true);

    // Restore goals in UI if empty (session restore)
    if (statStats && statStats.goals && statStats.goals.length > 0 && $('statGoals').children.length === 0) {
      statStats.goals.forEach(g => addStatGoal(g.stat, g.target));
      setStatUIDisabled(true);
    }
    if (statStats && statStats.default_stat) {
      $('statDefaultStat').value = statStats.default_stat;
    }

    // Update goal row states
    if (statStats) {
      $('statGoals').querySelectorAll('.stat-goal-row').forEach((row, i) => {
        const goal = statStats.goals && statStats.goals[i];
        if (!goal) return;
        const current = statStats[goal.stat] || 0;
        const done = current >= goal.target;
        row.classList.toggle('active', !done && i === statStats.active_goal_index);
        row.classList.toggle('done', done);
        const progress = row.querySelector('.stat-goal-progress');
        progress.textContent = current + ' / ' + goal.target;
      });
    }
  } else {
    $('statStartBtn').style.display = '';
    $('statStartBtn').disabled = false;
    $('statStopBtn').style.display = 'none';
    $('statBadge').textContent = 'OFF';
    $('statBadge').className = 'pvp-badge off';
    setStatUIDisabled(false);
    $('statGoals').querySelectorAll('.stat-goal-row').forEach(row => {
      row.classList.remove('active', 'done');
      row.querySelector('.stat-goal-progress').textContent = '';
    });
  }
  if (statStats && (statStats.attack > 0 || statStats.defense > 0 || statStats.stamina > 0)) {
    const parts = [
      'ATK ' + statStats.attack,
      'DEF ' + statStats.defense,
      'STA ' + statStats.stamina,
      'Unspent ' + statStats.unspent,
    ];
    if (statStats.allocated > 0) parts.push('+' + statStats.allocated + ' allocated');
    $('statDisplay').textContent = parts.join('  \u00b7  ');
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

// ── Tabs ────────────────────────────────────────────────────────────────────

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('.tab-page').forEach(p => {
    p.hidden = p.id !== 'tab-' + name;
  });
  // Close any open event detail when switching away from Events tab
  if (name !== 'events') closeEvent();
  // Action bar is farming-specific
  $('actionBar').hidden = name !== 'farming';
  // Auto-scroll the log to bottom when entering the Logs tab
  if (name === 'logs') {
    const box = $('logBox');
    if (box) box.scrollTop = box.scrollHeight;
  }
}

// ── Events ──────────────────────────────────────────────────────────────────
//
// Add a new event by pushing an entry to EVENTS. Keep past events in the
// list with status='ended' so the UI (and shared helpers) stay available
// when the event returns.
//
// Shape:
//   {
//     id:       unique string id (used in URLs / detail container)
//     title:    main card label
//     subtitle: short status line shown under the title
//     image:    optional URL or data-URI for the card art (falls back to gradient)
//     badge:    'LIVE' | 'ENDED' | custom label  (color derived from status)
//     status:   'live' | 'ended'  (ended events render greyed + non-clickable)
//     render:   (containerEl) => void — builds the detail view when opened
//   }

const EVENTS = [
  {
    id: 'emberfall-vaelith',
    title: 'Emberfall: Vaelith',
    subtitle: 'Live event',
    image: '/static/events/emberfall-vaelith.png',
    badge: 'LIVE',
    status: 'live',
    render(container) { renderEmberfallDetail(container); },
  },
];

// ── Collection Farmer (Emberfall event detail) ──────────────────────────────

let collectionPlans = null;          // cached /api/collections/plan result
let collectionPollTimer = null;      // setTimeout handle
let collectionPollCadence = 0;       // current cadence in ms (0 = none)
let lastCollectionStatus = null;
const COLL_POLL_ACTIVE_MS = 2000;
const COLL_POLL_IDLE_MS = 30000;

async function renderEmberfallDetail(container) {
  container.innerHTML = `
    <div class="event-detail-header emberfall-header">
      <h2>Emberfall: Vaelith</h2>
      <p>Auto-farm the bulk ingredients for the event collections.
      Boss drops (Star-Split Glass Heart, Living Black Index, Emberwing Plume,
      Lucid Memory Shard, Hollow Star Core Dust) stay up to you.</p>
    </div>

    <div class="section-header" style="margin-top:8px">
      <div class="section-title"><span>Collection Farmer</span></div>
    </div>
    <div class="coll-grid" id="collGrid">
      <div class="empty" style="padding:40px 16px;grid-column:1/-1">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
        <div>Loading plan…</div>
      </div>
    </div>

    <div class="section-header" style="margin-top:28px">
      <div class="section-title"><span>Achievement Farmer</span></div>
    </div>
    <div class="ach-card" id="achCard">
      <div class="ach-header">
        <div class="ach-title-block">
          <div class="ach-title">Event Mob Achievements</div>
          <div class="ach-sub">Farms <em>Deal N damage to M Monster</em> achievements on event wave 101, in page order. Mobs not on the wave are skipped.</div>
        </div>
        <div class="coll-controls">
          <select class="coll-stamina" id="achStamina">
            <option value="10 Stamina" selected>10 Stam</option>
            <option value="50 Stamina">50 Stam</option>
            <option value="100 Stamina">100 Stam</option>
            <option value="200 Stamina">200 Stam</option>
          </select>
          <button class="btn btn-sm btn-ghost" id="achRefreshBtn" onclick="refreshAchievementsPreview()">Scan</button>
          <button class="btn btn-sm btn-accent" id="achStartBtn" onclick="startAchievements()">Run</button>
          <button class="btn btn-sm btn-red" id="achStopBtn" hidden onclick="stopAchievements()">Stop</button>
        </div>
      </div>
      <div class="ach-meta" id="achMeta">—</div>
      <div class="ach-list" id="achList">
        <div class="empty" style="padding:24px 16px">
          <div>Click <b>Scan</b> to load achievements from the game.</div>
        </div>
      </div>
    </div>
    `;

  try {
    if (!collectionPlans) {
      const res = await fetch('/api/collections/plan');
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'Failed to load plan');
      collectionPlans = data.collections;
    }
    renderCollectionCards();
    await refreshCollectionStatus();
    startCollectionPolling();
  } catch (e) {
    $('collGrid').innerHTML = `<div class="empty" style="padding:40px 16px;grid-column:1/-1">
      <div>Failed to load collections: ${e.message}</div></div>`;
  }

  // Kick off achievement status polling in parallel (idempotent).
  refreshAchievementStatus();
  startAchievementPolling();
}

function renderCollectionCards() {
  const grid = $('collGrid');
  if (!grid || !collectionPlans) return;
  grid.innerHTML = '';
  for (const plan of collectionPlans) {
    const card = document.createElement('div');
    card.className = 'coll-card';
    card.dataset.collId = plan.id;
    card.innerHTML = `
      <div class="coll-header">
        <div class="coll-title">${plan.name}</div>
        <div class="coll-reward">Reward · ${plan.reward}</div>
      </div>
      <div class="coll-items"></div>
      <div class="coll-footer">
        <div class="coll-stats" data-role="stats">—</div>
        <div class="coll-controls">
          <select class="coll-stamina">
            <option value="10 Stamina" selected>10 Stam</option>
            <option value="50 Stamina">50 Stam</option>
            <option value="100 Stamina">100 Stam</option>
            <option value="200 Stamina">200 Stam</option>
          </select>
          <button class="btn btn-sm btn-accent coll-start">Start</button>
          <button class="btn btn-sm btn-red coll-stop" hidden>Stop</button>
        </div>
      </div>`;

    const itemsEl = card.querySelector('.coll-items');
    for (const item of plan.items) {
      const row = document.createElement('div');
      row.className = 'coll-item';
      row.dataset.itemName = item.name;
      row.innerHTML = `
        <div class="coll-item-name" title="Source: ${item.source_monster || '—'}">${item.name}</div>
        <div class="coll-item-right">
          <div class="coll-item-count"><span data-role="have">0</span> / <span data-role="need">${fmtGoal(item.need)}</span></div>
          <div class="coll-item-bar"><div class="coll-item-fill" style="width:0%"></div></div>
        </div>`;
      itemsEl.appendChild(row);
    }

    card.querySelector('.coll-start').onclick = () => startCollection(plan.id, card);
    card.querySelector('.coll-stop').onclick = () => stopCollection();
    grid.appendChild(card);
  }
}

function applyCollectionStatus(st) {
  lastCollectionStatus = st;
  if (!collectionPlans) return;

  const running = !!st.running;
  const activeId = running ? st.collection_id : 0;
  const progress = st.progress || {};

  for (const plan of collectionPlans) {
    const card = document.querySelector(`.coll-card[data-coll-id="${plan.id}"]`);
    if (!card) continue;

    const isActive = activeId === plan.id;
    card.classList.toggle('active', isActive);

    for (const item of plan.items) {
      const row = card.querySelector(`.coll-item[data-item-name="${CSS.escape(item.name)}"]`);
      if (!row) continue;
      const p = progress[item.name];
      const have = p ? p.have : 0;
      const need = item.need;  // always use the plan target, not game.need
      const pct = need > 0 ? Math.min(100, Math.round(have / need * 100)) : 0;
      row.querySelector('[data-role="have"]').textContent = fmtGoal(have);
      row.querySelector('[data-role="need"]').textContent = fmtGoal(need);
      row.querySelector('.coll-item-fill').style.width = pct + '%';
      row.classList.toggle('done', have >= need);
      row.classList.toggle('current', isActive && st.current_item === item.name);
    }

    const statsEl = card.querySelector('[data-role="stats"]');
    const startBtn = card.querySelector('.coll-start');
    const stopBtn = card.querySelector('.coll-stop');
    const staminaSel = card.querySelector('.coll-stamina');

    // Totals across plan items
    let haveTot = 0, needTot = 0;
    for (const item of plan.items) {
      const p = progress[item.name];
      haveTot += p ? Math.min(p.have, item.need) : 0;
      needTot += item.need;
    }
    const overallPct = needTot > 0 ? Math.round(haveTot / needTot * 100) : 0;

    let statsText = `${fmtGoal(haveTot)} / ${fmtGoal(needTot)} total · ${overallPct}%`;
    if (isActive) {
      const s = st.stats || {};
      if (s.killed) statsText += ` · ${s.killed} kills this session`;
      if (st.current_item) statsText += ` · on ${st.current_item}`;
    }
    statsEl.textContent = statsText;

    if (running && !isActive) {
      // Another collection is running — disable start
      startBtn.disabled = true;
      startBtn.hidden = false;
      stopBtn.hidden = true;
      staminaSel.disabled = true;
    } else if (isActive) {
      startBtn.hidden = true;
      stopBtn.hidden = false;
      staminaSel.disabled = true;
    } else {
      startBtn.disabled = false;
      startBtn.hidden = false;
      stopBtn.hidden = true;
      staminaSel.disabled = false;
    }
  }
}

async function refreshCollectionStatus() {
  try {
    const res = await fetch('/api/collections/status');
    const data = await res.json();
    if (data.ok) applyCollectionStatus(data);
  } catch (e) {
    console.error('collection status', e);
  }
  // Re-schedule at the cadence matching current state (adaptive poll).
  const wantCadence = (lastCollectionStatus && lastCollectionStatus.running)
    ? COLL_POLL_ACTIVE_MS : COLL_POLL_IDLE_MS;
  if (collectionPollCadence > 0) {
    schedulePoll(wantCadence);
  }
}

function schedulePoll(ms) {
  if (collectionPollTimer) clearTimeout(collectionPollTimer);
  collectionPollCadence = ms;
  collectionPollTimer = setTimeout(refreshCollectionStatus, ms);
}

function startCollectionPolling() {
  // Kick off the adaptive loop. refreshCollectionStatus itself re-schedules.
  schedulePoll(COLL_POLL_IDLE_MS);
}

function stopCollectionPolling() {
  if (collectionPollTimer) clearTimeout(collectionPollTimer);
  collectionPollTimer = null;
  collectionPollCadence = 0;
}

async function startCollection(id, card) {
  const stamina = card.querySelector('.coll-stamina').value || '200 Stamina';
  const startBtn = card.querySelector('.coll-start');
  startBtn.disabled = true;
  try {
    const res = await fetch('/api/collections/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({collection_id: id, stamina}),
    });
    const data = await res.json();
    if (!data.ok) {
      toast(data.error || 'Failed to start', 'warning');
      startBtn.disabled = false;
      return;
    }
    toast('Farming started', 'success');
    schedulePoll(COLL_POLL_ACTIVE_MS);
    await refreshCollectionStatus();
  } catch (e) {
    toast('Network error: ' + e.message, 'warning');
    startBtn.disabled = false;
  }
}

async function stopCollection() {
  try {
    await fetch('/api/collections/stop', {method: 'POST'});
    toast('Stopping…', 'info');
    schedulePoll(COLL_POLL_ACTIVE_MS);  // stay fast a few cycles to watch shutdown
    await refreshCollectionStatus();
  } catch (e) {
    toast('Network error: ' + e.message, 'warning');
  }
}

// ── Achievement Farmer ─────────────────────────────────────────────────────

const ACH_POLL_ACTIVE_MS = 2500;
const ACH_POLL_IDLE_MS = 30000;
let achPollTimer = null;
let achPollCadence = 0;
let lastAchStatus = null;

function renderAchList(items, activeSet, currentMonster) {
  const list = $('achList');
  if (!list) return;
  if (!items || items.length === 0) {
    list.innerHTML = '<div class="empty" style="padding:24px 16px"><div>No matching achievements yet.</div></div>';
    return;
  }
  const active = activeSet || new Set();
  list.innerHTML = items.map(a => {
    const farmable = active.has(a.title);
    const done = a.kills_current >= a.kills_required;
    const pct = a.percent ?? (a.kills_required ? Math.min(100, Math.round(a.kills_current / a.kills_required * 100)) : 0);
    const state = done ? 'done' : (farmable ? (a.monster === currentMonster ? 'current' : 'farmable') : 'skipped');
    const stateLabel = done ? 'DONE' : (farmable ? (a.monster === currentMonster ? 'NOW' : 'QUEUED') : 'SKIP');
    return `
      <div class="ach-row ${state}">
        <div class="ach-row-main">
          <div class="ach-row-title">${a.title || a.monster}</div>
          <div class="ach-row-sub">${a.monster} · ${fmtGoal(a.damage_required)} dmg each · target ${fmtGoal(a.kills_required)}</div>
        </div>
        <div class="ach-row-meta">
          <span class="ach-state-pill ${state}">${stateLabel}</span>
          <div class="ach-row-count">${fmtGoal(a.kills_current)} / ${fmtGoal(a.kills_required)} <span class="ach-pct">${pct}%</span></div>
          <div class="coll-item-bar"><div class="coll-item-fill" style="width:${pct}%"></div></div>
        </div>
      </div>`;
  }).join('');
}

function applyAchStatus(st) {
  lastAchStatus = st;
  const running = !!st.running;
  const startBtn = $('achStartBtn');
  const stopBtn = $('achStopBtn');
  const staminaSel = $('achStamina');
  if (startBtn && stopBtn) {
    startBtn.hidden = running;
    stopBtn.hidden = !running;
    startBtn.disabled = false;
    if (staminaSel) staminaSel.disabled = running;
  }

  const meta = $('achMeta');
  if (meta) {
    const stats = st.stats || {};
    const parts = [];
    parts.push(running ? `Running — wave ${st.wave}` : (st.wave ? `Idle — last scan wave ${st.wave}` : 'Idle'));
    if (st.wave_monsters && st.wave_monsters.length) {
      parts.push(`Wave mobs: ${st.wave_monsters.join(', ')}`);
    }
    if (running && st.current_monster) parts.push(`Now: ${st.current_monster}`);
    if (stats.killed) parts.push(`${stats.killed} kills this session`);
    if (stats.damage) parts.push(`${fmtGoal(stats.damage)} dmg dealt`);
    meta.textContent = parts.join(' · ');
  }

  const items = st.achievements || [];
  const activeSet = new Set((st.active || []).map(a => a.title));
  renderAchList(items, activeSet, st.current_monster || '');
}

async function refreshAchievementsPreview() {
  const btn = $('achRefreshBtn');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/achievements/preview?wave=101');
    const data = await res.json();
    if (!data.ok) {
      toast(data.error || 'Scan failed', 'warning');
      return;
    }
    // Merge preview into a status-shaped object so the same renderer works.
    applyAchStatus({
      running: (lastAchStatus && lastAchStatus.running) || false,
      wave: data.wave,
      wave_monsters: data.wave_monsters,
      achievements: data.achievements,
      active: data.active,
      current_monster: (lastAchStatus && lastAchStatus.current_monster) || '',
      stats: (lastAchStatus && lastAchStatus.stats) || {},
    });
    toast(`${data.active.length} farmable on wave ${data.wave}`, 'success');
  } catch (e) {
    toast('Network error: ' + e.message, 'warning');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function refreshAchievementStatus() {
  try {
    const res = await fetch('/api/achievements/status');
    const data = await res.json();
    if (data.ok) applyAchStatus(data);
  } catch (e) {
    console.error('achievement status', e);
  }
  const wantMs = (lastAchStatus && lastAchStatus.running) ? ACH_POLL_ACTIVE_MS : ACH_POLL_IDLE_MS;
  if (achPollCadence > 0) scheduleAchPoll(wantMs);
}

function scheduleAchPoll(ms) {
  if (achPollTimer) clearTimeout(achPollTimer);
  achPollCadence = ms;
  achPollTimer = setTimeout(refreshAchievementStatus, ms);
}

function startAchievementPolling() {
  scheduleAchPoll(ACH_POLL_IDLE_MS);
}

function stopAchievementPolling() {
  if (achPollTimer) clearTimeout(achPollTimer);
  achPollTimer = null;
  achPollCadence = 0;
}

async function startAchievements() {
  const stamina = $('achStamina')?.value || '10 Stamina';
  const btn = $('achStartBtn');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/achievements/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({wave: 101, stamina}),
    });
    const data = await res.json();
    if (!data.ok) {
      toast(data.error || 'Failed to start', 'warning');
      if (btn) btn.disabled = false;
      return;
    }
    toast('Achievement farmer started', 'success');
    scheduleAchPoll(ACH_POLL_ACTIVE_MS);
    await refreshAchievementStatus();
  } catch (e) {
    toast('Network error: ' + e.message, 'warning');
    if (btn) btn.disabled = false;
  }
}

async function stopAchievements() {
  try {
    await fetch('/api/achievements/stop', {method: 'POST'});
    toast('Stopping…', 'info');
    scheduleAchPoll(ACH_POLL_ACTIVE_MS);
    await refreshAchievementStatus();
  } catch (e) {
    toast('Network error: ' + e.message, 'warning');
  }
}

function eventBadgeClass(ev) {
  return ev.status === 'live' ? 'event-badge live' : 'event-badge ended';
}

function renderEvents() {
  const grid = $('eventsGrid');
  grid.innerHTML = '';

  if (!EVENTS.length) {
    grid.innerHTML = `
      <div class="events-empty">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>
        <div>No active events right now</div>
        <div class="events-empty-hint">New events appear here when they go live.</div>
      </div>`;
    return;
  }

  EVENTS.forEach((ev, idx) => {
    const card = document.createElement('div');
    card.className = 'event-card' + (ev.status === 'ended' ? ' ended' : '');
    card.style.animationDelay = (idx * 60) + 'ms';
    if (ev.status === 'live') {
      card.onclick = () => openEvent(ev.id);
    }
    const badgeLabel = ev.badge || (ev.status === 'live' ? 'LIVE' : 'ENDED');
    card.innerHTML = `
      <div class="event-card-art">
        <span class="${eventBadgeClass(ev)}">${badgeLabel}</span>
        <div class="event-card-art-fallback" style="background:${grad(ev.title)}">${ini(ev.title)}</div>
        ${ev.image ? `<img src="${ev.image}" alt="${ev.title}" onerror="this.remove()">` : ''}
      </div>
      <div class="event-card-body">
        <div class="event-card-title">${ev.title}</div>
        <div class="event-card-subtitle">${ev.subtitle || ''}</div>
        <button class="btn btn-sm ${ev.status === 'live' ? 'btn-accent' : 'btn-ghost'} event-card-btn"
                ${ev.status === 'ended' ? 'disabled' : ''}>
          ${ev.status === 'live' ? 'Enter ' + ev.title : 'Event Ended'}
        </button>
      </div>`;
    grid.appendChild(card);
  });
}

function openEvent(id) {
  const ev = EVENTS.find(e => e.id === id);
  if (!ev || ev.status !== 'live') return;
  const detail = $('eventDetailContent');
  detail.innerHTML = '';
  try {
    ev.render(detail);
  } catch (e) {
    detail.innerHTML = '<div class="empty">Failed to load event.</div>';
    console.error(e);
  }
  $('eventsGridView').hidden = true;
  $('eventsDetailView').hidden = false;
}

function closeEvent() {
  const gridView = $('eventsGridView');
  const detailView = $('eventsDetailView');
  if (gridView && detailView) {
    gridView.hidden = false;
    detailView.hidden = true;
  }
  stopCollectionPolling();
  stopAchievementPolling();
}

// ── Init ────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  $('pvpBadge').className = 'pvp-badge off';
  $('statBadge').className = 'pvp-badge off';
  $('questBadge').className = 'pvp-badge off';
  renderEvents();
  checkSession();
});
