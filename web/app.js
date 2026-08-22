'use strict';

// ── small fetch helpers ─────────────────────────────────────────────────

async function apiGet(path) {
  const res = await fetch(path);
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return res.json();
}

function relayList(inputEl) {
  return inputEl.value.split(',').map(s => s.trim()).filter(Boolean);
}

function relayQuery(relays) {
  return relays.map(r => 'relay=' + encodeURIComponent(r)).join('&');
}

function shortHash(h, n = 16) {
  return h ? h.slice(0, n) + '…' : '';
}

// ── tabs ─────────────────────────────────────────────────────────────────

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.add('active');
  });
});

// ── identity ─────────────────────────────────────────────────────────────

async function loadIdentity() {
  const { pubkey } = await apiGet('/api/whoami');
  document.getElementById('identity').innerHTML = 'you are <code>' + shortHash(pubkey, 20) + '</code>';
  document.getElementById('identity-pubkey').textContent = pubkey;
}

// ── discover ─────────────────────────────────────────────────────────────

async function refreshDiscover() {
  const relays = relayList(document.getElementById('discover-relays'));
  const { results } = await apiGet('/api/discover?' + relayQuery(relays));
  const tbody = document.querySelector('#discover-table tbody');
  tbody.innerHTML = '';
  if (!results || !results.length) {
    tbody.innerHTML = '<tr><td colspan="6">nothing found — relay(s) unreachable, or nothing published yet</td></tr>';
    return;
  }
  for (const r of results) {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + (r.title || '') + '</td>' +
      '<td><code>' + shortHash(r.content_hash) + '</code></td>' +
      '<td>' + (r.host || '') + '</td>' +
      '<td>' + (r.tunnel || '—') + '</td>' +
      '<td><code>' + shortHash(r.signer_pubkey, 12) + '</code></td>' +
      '<td></td>';
    const actions = tr.lastElementChild;

    const dlBtn = document.createElement('button');
    dlBtn.textContent = 'Download';
    dlBtn.addEventListener('click', () => {
      document.querySelector('.tab-btn[data-tab="downloads"]').click();
      document.getElementById('download-hash').value = r.content_hash;
      document.getElementById('download-relays').value = relays.join(', ');
    });
    actions.appendChild(dlBtn);

    const likeBtn = document.createElement('button');
    likeBtn.textContent = 'Like';
    likeBtn.addEventListener('click', async () => {
      await apiPost('/api/like', { content_hash: r.content_hash, relay: relays[0] });
      likeBtn.textContent = 'Liked';
      likeBtn.disabled = true;
    });
    actions.appendChild(likeBtn);

    const subBtn = document.createElement('button');
    subBtn.textContent = 'Subscribe';
    subBtn.addEventListener('click', async () => {
      await apiPost('/api/subscribe', { target_pubkey: r.signer_pubkey, relay: relays[0] });
      subBtn.textContent = 'Subscribed';
      subBtn.disabled = true;
    });
    actions.appendChild(subBtn);

    tbody.appendChild(tr);
  }
}

document.getElementById('discover-form').addEventListener('submit', e => {
  e.preventDefault();
  refreshDiscover();
});

// ── host ─────────────────────────────────────────────────────────────────

document.getElementById('host-tunnel-enabled').addEventListener('change', e => {
  document.getElementById('host-tunnel-addr-row').classList.toggle('hidden', !e.target.checked);
});

document.getElementById('host-form').addEventListener('submit', async e => {
  e.preventDefault();
  const tunnelEnabled = document.getElementById('host-tunnel-enabled').checked;
  const body = {
    archive_dir: document.getElementById('host-archive-dir').value,
    file_name: document.getElementById('host-file-name').value || null,
    port: Number(document.getElementById('host-port').value),
    price: Number(document.getElementById('host-price').value),
    relay: relayList(document.getElementById('host-relays')),
    advertise_host: document.getElementById('host-advertise').value,
    tunnel: tunnelEnabled ? document.getElementById('host-tunnel-addr').value : null,
  };
  const result = document.getElementById('host-result');
  result.textContent = 'starting…';
  const resp = await apiPost('/api/host', body);
  if (resp.error) {
    result.textContent = 'error: ' + resp.error;
    return;
  }
  result.textContent = 'hosting started (id ' + resp.host_id + ')';
  refreshHosts();
});

async function refreshHosts() {
  const { hosts } = await apiGet('/api/hosts');
  const tbody = document.querySelector('#hosts-table tbody');
  tbody.innerHTML = '';
  for (const h of hosts || []) {
    const tr = document.createElement('tr');
    const statusText = h.status === 'error' ? 'error: ' + h.error : h.status;
    const statusClass = h.status === 'error' ? 'status-error' : (h.status === 'running' ? 'status-done' : 'status-running');
    tr.innerHTML =
      '<td>' + (h.name || '(starting…)') + '</td>' +
      '<td>' + h.port + '</td>' +
      '<td>' + (h.price ? h.price + ' sat' : 'free') + '</td>' +
      '<td>' + (h.tunnel || '—') + '</td>' +
      '<td class="' + statusClass + '">' + statusText + '</td>';
    tbody.appendChild(tr);
  }
}

// ── downloads ────────────────────────────────────────────────────────────

const activeJobRows = {};

document.getElementById('download-form').addEventListener('submit', async e => {
  e.preventDefault();
  const body = {
    content_hash: document.getElementById('download-hash').value,
    relay: relayList(document.getElementById('download-relays')),
    out_path: document.getElementById('download-out').value || null,
    lightning: document.getElementById('download-lightning').checked,
  };
  const resp = await apiPost('/api/download', body);
  if (resp.error) {
    alert('error: ' + resp.error);
    return;
  }
  addJobRow(resp.job_id, body.content_hash);
  pollJob(resp.job_id);
});

function addJobRow(jobId, contentHash) {
  const tbody = document.querySelector('#jobs-table tbody');
  const tr = document.createElement('tr');
  tr.innerHTML =
    '<td><code>' + jobId + '</code></td>' +
    '<td><code>' + shortHash(contentHash) + '</code></td>' +
    '<td><div class="progress-bar"><div class="progress-fill" style="width:0%"></div></div></td>' +
    '<td class="status-running">running</td>' +
    '<td>—</td>';
  document.querySelector('#jobs-table tbody').appendChild(tr);
  activeJobRows[jobId] = tr;
}

function pollJob(jobId) {
  const tr = activeJobRows[jobId];
  const timer = setInterval(async () => {
    const job = await apiGet('/api/download/' + jobId);
    if (job.error && !job.status) {
      clearInterval(timer);
      return;
    }
    const fill = tr.querySelector('.progress-fill');
    const statusCell = tr.children[3];
    const resultCell = tr.children[4];
    if (job.n_chunks) {
      const pct = Math.round(100 * (job.idx + 1) / job.n_chunks);
      fill.style.width = pct + '%';
    }
    if (job.status === 'done') {
      fill.style.width = '100%';
      statusCell.textContent = 'done';
      statusCell.className = 'status-done';
      resultCell.textContent = job.path;
      clearInterval(timer);
    } else if (job.status === 'error') {
      statusCell.textContent = 'error';
      statusCell.className = 'status-error';
      resultCell.textContent = job.error;
      clearInterval(timer);
    }
  }, 500);
}

// ── identity / reputation tab ────────────────────────────────────────────

document.getElementById('reputation-form').addEventListener('submit', async e => {
  e.preventDefault();
  const pubkey = document.getElementById('reputation-pubkey').value.trim();
  const result = document.getElementById('reputation-result');
  if (!pubkey) { result.textContent = ''; return; }
  const data = await apiGet('/api/reputation/' + encodeURIComponent(pubkey));
  result.textContent = 'score ' + data.score.toFixed(2) + ' — ' + data.why;
});

// ── init ─────────────────────────────────────────────────────────────────

loadIdentity();
refreshDiscover();
refreshHosts();
setInterval(refreshHosts, 3000);
