const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';
const form = document.getElementById('download-form');
const urlInput = document.getElementById('url-input');
const destInput = document.getElementById('dest-input');
const formatSelect = document.getElementById('format-select');
const submitBtn = document.getElementById('submit-btn');
const cancelBtn = document.getElementById('cancel-btn');
const formError = document.getElementById('form-error');
const ffmpegBanner = document.getElementById('ffmpeg-warning');
const progressCard = document.getElementById('progress-card');
const trackThumb = document.getElementById('track-thumb');
const trackTitle = document.getElementById('track-title');
const jobStatus = document.getElementById('job-status');
const progressBar = document.getElementById('progress-bar');
const progressMsg = document.getElementById('progress-message');
const summaryEl = document.getElementById('summary');
const historyList = document.getElementById('history-list');

const HISTORY_STORAGE_KEY = 'yt_music_dl_history';

let currentJobId = null;
let eventSource = null;
let pollTimer = null;

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch (_) {
    return [];
  }
}

function saveHistory(items) {
  try {
    localStorage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(items.slice(0, 20)));
  } catch (_) {}
}

function renderHistory() {
  if (!historyList) return;
  const items = loadHistory();
  historyList.innerHTML = '';

  if (items.length === 0) {
    const empty = document.createElement('li');
    empty.className = 'sub';
    empty.textContent = 'Aún no hay descargas en el historial.';
    historyList.appendChild(empty);
    return;
  }

  for (const item of items) {
    const li = document.createElement('li');
    li.className = 'history-item';

    const top = document.createElement('div');
    top.className = 'history-item-top';

    const title = document.createElement('span');
    title.className = 'history-url';
    title.textContent = item.title || item.url;

    const badge = document.createElement('span');
    badge.className = `badge ${item.status || 'pending'}`;
    badge.textContent = item.status || 'pending';

    top.appendChild(title);
    top.appendChild(badge);

    const meta = document.createElement('div');
    meta.className = 'history-meta';
    meta.textContent = `${(item.format || 'mp3').toUpperCase()} • ${item.dest_dir || ''}${item.summary ? ` • ${item.summary}` : ''}`;

    li.appendChild(top);
    li.appendChild(meta);
    historyList.appendChild(li);
  }
}

function upsertHistory(entry) {
  const items = loadHistory();
  const idx = items.findIndex((item) => item.job_id === entry.job_id);
  if (idx >= 0) {
    items[idx] = { ...items[idx], ...entry };
  } else {
    items.unshift(entry);
  }
  saveHistory(items);
  renderHistory();
}

async function init() {
  renderHistory();

  try {
    const health = await fetch('/api/health');
    const hData = await health.json();
    if (!hData.ffmpeg_available) {
      ffmpegBanner.classList.add('visible');
    }
  } catch (_) {}

  try {
    const res = await fetch('/api/default-dir');
    const data = await res.json();
    if (data.default_dir) destInput.value = data.default_dir;
  } catch (_) {}
}

function stopStreams() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function applySnapshot(data) {
  progressCard.classList.add('visible');
  jobStatus.textContent = data.status || 'pending';
  progressBar.value = Number(data.progress || 0);
  progressMsg.textContent = data.message || '';
  if (data.current_track) trackTitle.textContent = data.current_track;
  if (data.thumbnail_url) {
    trackThumb.src = data.thumbnail_url;
    trackThumb.style.display = 'block';
  }

  const terminal = ['completed', 'failed', 'cancelled'].includes(data.status);
  if (terminal) {
    stopStreams();
    cancelBtn.disabled = true;
    submitBtn.disabled = false;
    let summaryText = '';
    if (data.status === 'completed') {
      const dl = data.downloaded_count ?? (data.manifest ? data.manifest.downloaded.length : 0);
      const sk = data.skipped_count ?? (data.manifest ? data.manifest.skipped_archive.length : 0);
      const fl = data.failed_count ?? (data.manifest ? data.manifest.failed.length : 0);
      summaryText = `Descargadas: ${dl} | Saltadas (archivo): ${sk} | Fallidas: ${fl}`;
    } else if (data.status === 'failed') {
      summaryText = `Error: ${data.error || data.message || 'Fallo en la descarga'}`;
    } else {
      summaryText = 'Descarga cancelada.';
    }
    summaryEl.textContent = summaryText;

    if (currentJobId) {
      upsertHistory({
        job_id: currentJobId,
        status: data.status,
        title: data.current_track || undefined,
        summary: summaryText,
      });
    }
  }
}

function startMonitoring(jobId) {
  stopStreams();
  currentJobId = jobId;
  cancelBtn.disabled = false;
  summaryEl.textContent = '';

  eventSource = new EventSource(`/api/jobs/${jobId}/events`);
  const handleEvent = (ev) => {
    try {
      applySnapshot(JSON.parse(ev.data));
    } catch (_) {}
  };
  ['snapshot', 'status', 'progress', 'completed', 'failed', 'cancelled'].forEach((name) => {
    eventSource.addEventListener(name, handleEvent);
  });

  pollTimer = setInterval(async () => {
    try {
      const r = await fetch(`/api/jobs/${jobId}`);
      if (r.ok) {
        const job = await r.json();
        if (job.progress) applySnapshot(job.progress);
      }
    } catch (_) {}
  }, 2000);
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  formError.textContent = '';
  submitBtn.disabled = true;

  const url = urlInput.value.trim();
  const destDir = destInput.value.trim();
  const format = formatSelect.value;

  try {
    const res = await fetch('/api/jobs', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({
        url,
        dest_dir: destDir,
        format,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      formError.textContent = data.error || 'Error al crear el trabajo.';
      submitBtn.disabled = false;
      return;
    }
    upsertHistory({
      job_id: data.job_id,
      url,
      dest_dir: destDir,
      format,
      status: data.status || 'pending',
    });
    startMonitoring(data.job_id);
  } catch (err) {
    formError.textContent = 'Error de conexión con el servidor local.';
    submitBtn.disabled = false;
  }
});

cancelBtn.addEventListener('click', async () => {
  if (!currentJobId) return;
  cancelBtn.disabled = true;
  await fetch(`/api/jobs/${currentJobId}/cancel`, {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  });
});

init();
