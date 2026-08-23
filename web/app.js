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

function shortHash(h, n = 10) {
  return h ? h.slice(0, n) + '…' : '';
}

// ── QR popup, shared by the header "open on phone" button and each
// downloaded video's per-item scan button ─────────────────────────────────

function toggleQr(anchorEl, url) {
  let popup = document.getElementById('shared-qr-popup');
  if (!popup) {
    popup = document.createElement('div');
    popup.id = 'shared-qr-popup';
    popup.className = 'qr-popup hidden';
    document.body.appendChild(popup);
  }
  if (!popup.classList.contains('hidden') && popup.dataset.url === url) {
    popup.classList.add('hidden');
    return;
  }
  popup.dataset.url = url;
  popup.innerHTML =
    '<img src="/api/qr?data=' + encodeURIComponent(url) + '" alt="QR code for ' + url + '">' +
    '<div class="qr-url">' + url + '</div>';
  const rect = anchorEl.getBoundingClientRect();
  popup.style.top = (rect.bottom + window.scrollY + 6) + 'px';
  popup.style.left = (rect.left + window.scrollX) + 'px';
  popup.classList.remove('hidden');
}

document.addEventListener('click', e => {
  const popup = document.getElementById('shared-qr-popup');
  if (!popup || popup.classList.contains('hidden')) return;
  if (popup.contains(e.target) || e.target.closest('.qr-btn, #phone-qr-toggle')) return;
  popup.classList.add('hidden');
});

// The server's idea of "your phone's own address" beats the browser's:
// location.origin only reflects whatever address *this* browser used to
// load the page, which is 127.0.0.1 the instant someone opens it via
// localhost -- exactly the bug a LAN-bound server needs to avoid handing
// a phone a QR code that points right back at the desktop machine.
let lanUrlBase = null;
fetch('/api/lan-url').then(r => r.json()).then(d => { lanUrlBase = d.url; }).catch(() => {});

function qrBaseUrl() {
  return lanUrlBase || (location.origin + '/');
}

document.getElementById('phone-qr-toggle').addEventListener('click', e => {
  toggleQr(e.currentTarget, qrBaseUrl());
});

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

function playInline(jobId) {
  const container = document.getElementById('player-container');
  container.innerHTML = '';
  const video = document.createElement('video');
  video.controls = true;
  video.autoplay = true;
  video.src = '/api/stream/' + jobId;
  container.appendChild(video);
  container.scrollIntoView({behavior: 'smooth'});
}

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
      resultCell.textContent = '';
      const pathSpan = document.createElement('span');
      pathSpan.textContent = job.path + ' ';
      const playBtn = document.createElement('button');
      playBtn.type = 'button';
      playBtn.className = 'play-btn';
      playBtn.textContent = '▶ Play';
      playBtn.addEventListener('click', () => playInline(jobId));
      const qrBtn = document.createElement('button');
      qrBtn.type = 'button';
      qrBtn.className = 'play-btn qr-btn';
      qrBtn.textContent = '📱';
      qrBtn.title = 'scan to open this video on your phone';
      qrBtn.addEventListener('click', () => toggleQr(qrBtn, qrBaseUrl() + 'api/stream/' + jobId));
      resultCell.appendChild(pathSpan);
      resultCell.appendChild(playBtn);
      resultCell.appendChild(qrBtn);
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
