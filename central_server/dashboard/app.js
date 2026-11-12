/**
 * Cognetra 3.0 — Live Dashboard JavaScript
 * Reactive SSE listener + REST API binding for the operations UI
 */

'use strict';

// ── State ──────────────────────────────────────────────────────────────────
const state = {
  incidents: [],
  selectedEventId: null,
  feedEntries: [],
  sseSource: null,
};

// ── Clock ─────────────────────────────────────────────────────────────────
function tickClock() {
  document.getElementById('clock').textContent = new Date().toLocaleTimeString('en-GB');
}
setInterval(tickClock, 1000);
tickClock();

// ── SSE Connection ────────────────────────────────────────────────────────
function connectSSE() {
  const pill = document.getElementById('pill-sse');

  if (state.sseSource) {
    state.sseSource.close();
  }

  pill.innerHTML = '<span class="dot dot-yellow"></span> Connecting…';

  const es = new EventSource('/api/events');
  state.sseSource = es;

  es.addEventListener('open', () => {
    pill.innerHTML = '<span class="dot dot-green"></span> Live';
    addFeedEntry('keepalive', '✓ SSE stream connected');
  });

  es.addEventListener('message', (e) => {
    try {
      const msg = JSON.parse(e.data);
      handleSSEMessage(msg);
    } catch (err) {
      // keepalive comment — ignore
    }
  });

  es.addEventListener('error', () => {
    pill.innerHTML = '<span class="dot dot-red"></span> Reconnecting…';
    addFeedEntry('keepalive', '⚠ SSE disconnected — retrying…');
    es.close();
    setTimeout(connectSSE, 3000);
  });
}

// ── SSE Message Handler ───────────────────────────────────────────────────
function handleSSEMessage(msg) {
  const { type, data, timestamp } = msg;
  const ts = new Date(timestamp * 1000).toLocaleTimeString('en-GB');

  switch (type) {
    case 'INCIDENT_UPDATE': {
      const ev = data.event_id;
      const status = data.status;
      const eventType = data.event_type || 'STANDARD';
      const feedClass = (eventType.includes('NEAR') || eventType.includes('PREDICT')) ? 'near-miss' : 'incident';
      addFeedEntry(feedClass, `[${ts}] 📋 ${ev} → ${status} (${eventType})`);
      refreshIncidentRow(data);
      loadTelemetry();
      break;
    }
    case 'VERIFICATION_COMPLETED': {
      const ev = data.event_id;
      const klass = data.is_near_miss ? 'near-miss' : (data.is_accident ? 'verified' : 'keepalive');
      const icon = data.is_near_miss ? '⚠️' : (data.is_accident ? '🚨' : '✅');
      addFeedEntry(klass, `[${ts}] ${icon} ${ev} → ${data.status} conf=${(data.confidence*100).toFixed(0)}% ${data.severity}`);
      refreshIncidentRow({ event_id: ev, status: data.status });
      if (state.selectedEventId === ev) loadDetail(ev);
      break;
    }
    case 'DISPATCH_SENT': {
      addFeedEntry('dispatch', `[${ts}] 📡 DISPATCHED ${data.event_id} via ${data.dispatch_type} (msg_id=${data.message_id || '—'})`);
      break;
    }
    case 'SYSTEM_RESET': {
      state.incidents = [];
      state.selectedEventId = null;
      renderIncidentTable();
      loadTelemetry();
      document.getElementById('detail-content').innerHTML = '<div class="detail-placeholder"><span>System reset to 0 — waiting for events…</span></div>';
      addFeedEntry('keepalive', `[${ts}] 🗑️ System reset to 0`);
      break;
    }
    default:
      addFeedEntry('keepalive', `[${ts}] ${type}`);
  }
}

// ── REST API Calls ────────────────────────────────────────────────────────
async function loadTelemetry() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    setText('val-db', d.database);
    colorCard('tcard-db', d.database === 'OK' ? 'success' : 'danger');

    setText('val-gemini', d.gemini_api === 'CONFIGURED' ? 'Online' : 'Not Configured');
    colorCard('tcard-gemini', d.gemini_api === 'CONFIGURED' ? 'success' : 'warning');

    setText('val-telegram', d.telegram === 'CONFIGURED' ? 'Online' : 'Not Configured');
    colorCard('tcard-telegram', d.telegram === 'CONFIGURED' ? 'success' : 'warning');

    setText('val-incidents', d.total_incidents);
    setText('val-active', d.active_incidents);
  } catch {
    // Server unreachable
    setText('val-db', 'Offline');
  }
}

async function loadIncidents() {
  const statusFilter = document.getElementById('filter-status').value;
  const url = '/api/incidents?limit=100' + (statusFilter ? `&status=${statusFilter}` : '');

  try {
    const r = await fetch(url);
    const d = await r.json();
    state.incidents = d.incidents || [];
    renderIncidentTable();
  } catch (e) {
    console.error('Failed to load incidents:', e);
  }
}

async function loadDetail(eventId) {
  state.selectedEventId = eventId;
  document.getElementById('detail-title').textContent = `🔍 ${eventId}`;

  try {
    const r = await fetch(`/api/incidents/${eventId}`);
    const d = await r.json();
    renderDetail(d);
  } catch (e) {
    document.getElementById('detail-content').innerHTML =
      `<div class="detail-placeholder"><span>Error loading detail: ${e.message}</span></div>`;
  }
}

// ── Render Functions ──────────────────────────────────────────────────────
function renderIncidentTable() {
  const tbody = document.getElementById('incident-tbody');
  if (!state.incidents.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No incidents yet — waiting for edge client…</td></tr>';
    return;
  }

  tbody.innerHTML = state.incidents.map(inc => {
    const chip = statusChip(inc.status);
    const typeChip = eventTypeChip(inc.event_type);
    const conf = inc.edge_confidence ? `${(inc.edge_confidence * 100).toFixed(0)}%` : '—';
    const time = inc.created_at ? new Date(inc.created_at).toLocaleTimeString('en-GB') : '—';
    const selected = state.selectedEventId === inc.event_id ? 'selected' : '';

    return `
      <tr class="${selected}" onclick="loadDetail('${inc.event_id}')">
        <td>${inc.event_id}</td>
        <td>${typeChip}</td>
        <td>${inc.camera_id || '—'}</td>
        <td>${conf}</td>
        <td>${chip}</td>
        <td>${time}</td>
      </tr>
    `;
  }).join('');
}

function refreshIncidentRow(data) {
  // Update in state.incidents or add new
  const idx = state.incidents.findIndex(i => i.event_id === data.event_id);
  if (idx >= 0) {
    state.incidents[idx] = { ...state.incidents[idx], ...data };
  } else {
    state.incidents.unshift(data);
  }
  renderIncidentTable();
}

function renderDetail(d) {
  const inc = d.incident;
  const vr = d.verification;
  const dp = d.dispatches || [];

  const hasClip1 = inc.clip1_path;
  const hasClip2 = inc.clip2_path;

  let html = `<div class="detail-content">`;

  // Incident info
  html += detailRow('Event ID', `<span class="mono">${inc.event_id}</span>`);
  html += detailRow('Status', statusChip(inc.status));
  html += detailRow('Event Type', eventTypeChip(inc.event_type));
  html += detailRow('Camera', inc.camera_id || '—');
  html += detailRow('Source Video', inc.source_video || '—');
  html += detailRow('Timestamp', inc.candidate_timestamp ? `${inc.candidate_timestamp.toFixed(2)}s` : '—');
  html += detailRow('Edge Confidence', inc.edge_confidence ? `${(inc.edge_confidence * 100).toFixed(1)}%` : '—');
  html += detailRow('Clips', `${inc.clip1_received ? '✅ Clip 1' : '⏳ Clip 1'} &nbsp; ${inc.clip2_received ? '✅ Clip 2' : '⏳ Clip 2'}`);

  if (vr) {
    html += `<hr class="detail-divider" />`;
    html += detailRow('Accident', vr.is_accident ? '🚨 Yes' : '—');
    html += detailRow('Near Miss', vr.is_near_miss ? '⚠️ Yes' : '—');
    html += detailRow('Vehicles', vr.vehicles_involved || '—');
    if (vr.heavy_vehicle_involved) {
      html += detailRow('Mass Hazard', '<span style="color:#DC2626;font-weight:700;">🚛 HEAVY COMMERCIAL VEHICLE (High Momentum p=m·v)</span>');
    }
    if (vr.entrapment_risk) {
      html += detailRow('Entrapment', '<span style="color:#DC2626;font-weight:700;">🆘 HIGH (Rescue Extrication)</span>');
    }
    html += detailRow('AI Confidence', vr.confidence ? `${(vr.confidence * 100).toFixed(1)}%` : '—');
    html += detailRow('Severity', vr.severity || '—');
    html += detailRow('Collision Type', vr.collision_type || '—');
    html += detailRow('Road Condition', vr.road_condition || '—');
    html += detailRow('Obstruction', vr.obstruction_detected ? '⚠️ Yes' : 'No');
    if (vr.rationale) {
      html += `<div class="detail-rationale">📝 ${vr.rationale}</div>`;
    }
  }

  if (dp.length) {
    html += `<hr class="detail-divider" />`;
    dp.forEach(d => {
      html += detailRow('Dispatch', `${d.dispatch_type} via ${d.channel} — ${d.status}`);
    });
  }

  if (hasClip1 || hasClip2) {
    const clip1Url = hasClip1 ? `/clips/${inc.event_id}/clip1.mp4` : null;
    const clip2Url = hasClip2 ? `/clips/${inc.event_id}/clip2.mp4` : null;
    html += `<button class="btn-view-clips" onclick="openClipsModal('${inc.event_id}', '${clip1Url || ''}', '${clip2Url || ''}', \`${vr ? escapeHtml(vr.rationale || '') : ''}\`)">
      🎬 View Evidence Clips
    </button>`;
  }

  html += `</div>`;
  document.getElementById('detail-content').innerHTML = html;
}

// ── Clip Modal ────────────────────────────────────────────────────────────
function openClipsModal(eventId, clip1Url, clip2Url, rationale) {
  document.getElementById('modal-title').textContent = `🎬 Evidence — ${eventId}`;

  const v1 = document.getElementById('clip1-player');
  const v2 = document.getElementById('clip2-player');

  if (clip1Url) { v1.src = clip1Url; v1.style.display = 'block'; }
  else           { v1.src = ''; v1.style.display = 'none'; }

  if (clip2Url)  { v2.src = clip2Url; v2.style.display = 'block'; }
  else           { v2.src = ''; v2.style.display = 'none'; }

  if (rationale) {
    document.getElementById('modal-verification').innerHTML =
      `<strong>AI Analysis:</strong><br>${escapeHtml(rationale)}`;
  } else {
    document.getElementById('modal-verification').innerHTML = '';
  }

  document.getElementById('clip-modal').classList.remove('hidden');
}

function closeModal() {
  document.getElementById('clip-modal').classList.add('hidden');
  document.getElementById('clip1-player').pause();
  document.getElementById('clip2-player').pause();
}

// ── Helpers ───────────────────────────────────────────────────────────────
function statusChip(status) {
  const map = {
    'PENDING_CLIP2':     ['pending',    '⏳ Pending'],
    'RECEIVED_BOTH':     ['pending',    '📦 Received'],
    'VERIFYING':         ['verifying',  '🔍 Verifying'],
    'VERIFIED_ACCIDENT': ['accident',   '🚨 Accident'],
    'VERIFIED_NEAR_MISS':['nearmiss',   '⚠️ Near Miss'],
    'DISPATCHED':        ['dispatched', '✅ Dispatched'],
    'FALSE_POSITIVE':    ['false',      '❌ False Positive'],
    'ERROR':             ['false',      '⚠️ Error'],
  };
  const [cls, label] = map[status] || ['false', status];
  return `<span class="chip chip-${cls}">${label}</span>`;
}

function eventTypeChip(type) {
  if (!type || type === 'STANDARD' || type === 'MOTION_TRIGGER') {
    return `<span class="chip chip-false">STANDARD</span>`;
  }
  if (type === 'PREDICTIVE_TRIGGER') {
    return `<span class="chip chip-predictive">⚡ PREDICTIVE</span>`;
  }
  if (type === 'NEAR_MISS_TRIGGER') {
    return `<span class="chip chip-nearmiss">⚠️ NEAR-MISS</span>`;
  }
  return `<span class="chip chip-false">${type}</span>`;
}

function detailRow(label, value) {
  return `
    <div class="detail-row">
      <span class="detail-label">${label}</span>
      <span class="detail-value">${value}</span>
    </div>
  `;
}

function addFeedEntry(cls, text) {
  const feed = document.getElementById('live-feed');
  const entry = document.createElement('div');
  entry.className = `feed-entry ${cls}`;
  entry.textContent = text;
  feed.insertBefore(entry, feed.firstChild);
  // Trim feed to 200 entries
  while (feed.children.length > 200) {
    feed.removeChild(feed.lastChild);
  }
}

function clearFeed() {
  document.getElementById('live-feed').innerHTML = '';
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function colorCard(id, type) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.borderColor = type === 'success' ? 'var(--success)' :
                          type === 'danger'  ? 'var(--danger)'  :
                          type === 'warning' ? 'var(--warning)' : 'var(--border)';
}

function applyFilter() {
  loadIncidents();
}

function escapeHtml(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
            .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Init ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  connectSSE();
  loadTelemetry();
  loadIncidents();
  // Poll telemetry every 30s
  setInterval(loadTelemetry, 30_000);
  // Poll incidents every 15s
  setInterval(loadIncidents, 15_000);
});
