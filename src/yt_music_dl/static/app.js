const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';
const form = document.getElementById('download-form');
const urlInput = document.getElementById('url-input');
const destInput = document.getElementById('dest-input');
const formatSelect = document.getElementById('format-select');
const qualitySelect = document.getElementById('quality-select');
const submitBtn = document.getElementById('submit-btn');
const cancelBtn = document.getElementById('cancel-btn');
const openFolderBtn = document.getElementById('open-folder-btn');
const downloadedCountEl = document.getElementById('downloaded-count');
const formError = document.getElementById('form-error');
const ffmpegBanner = document.getElementById('ffmpeg-warning');

const previewCard = document.getElementById('preview-card');
const previewThumb = document.getElementById('preview-thumb');
const previewType = document.getElementById('preview-type');
const previewCount = document.getElementById('preview-count');
const previewTitle = document.getElementById('preview-title');
const previewAuthor = document.getElementById('preview-author');

const progressCard = document.getElementById('progress-card');
const trackThumb = document.getElementById('track-thumb');
const trackTitle = document.getElementById('track-title');
const jobStatus = document.getElementById('job-status');
const progressBar = document.getElementById('progress-bar');
const progressPercent = document.getElementById('progress-percent');
const progressMsg = document.getElementById('progress-message');
const summaryEl = document.getElementById('summary');
const historyList = document.getElementById('history-list');

const HISTORY_STORAGE_KEY = 'yt_music_dl_history_v2';

let currentJobId = null;
let currentPreview = null;
let eventSource = null;
let pollTimer = null;
let previewTimer = null;

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
    localStorage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(items.slice(0, 30)));
  } catch (_) {}
}

function updateDownloadedCounter(items) {
  if (!downloadedCountEl) return;
  const completedCount = items.filter((item) => item.status === 'completed').length;
  downloadedCountEl.textContent = String(completedCount);
}

function parseTrackNumber(fileName, fallbackIdx) {
  const match = String(fileName || '').match(/^(\d{1,3})\s*-\s*/);
  if (match) return `#${match[1].padStart(2, '0')}`;
  return `#${String(fallbackIdx + 1).padStart(2, '0')}`;
}

function renderHistory() {
  if (!historyList) return;
  const items = loadHistory();
  updateDownloadedCounter(items);
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

    const row = document.createElement('div');
    row.className = 'history-card-row';

    if (item.thumbnail_url) {
      const img = document.createElement('img');
      img.className = 'history-thumb';
      img.src = item.thumbnail_url;
      img.alt = item.title || 'Portada';
      row.appendChild(img);
    }

    const details = document.createElement('div');
    details.className = 'history-details';

    const top = document.createElement('div');
    top.className = 'history-item-top';

    const title = document.createElement('span');
    title.className = 'history-title';
    const trackPrefix = item.track_number ? `${item.track_number} ` : '';
    title.textContent = `${trackPrefix}${item.title || item.url}`;

    const badge = document.createElement('span');
    badge.className = `badge ${item.status || 'pending'}`;
    badge.textContent = item.status || 'pending';

    top.appendChild(title);
    top.appendChild(badge);

    const meta = document.createElement('div');
    meta.className = 'history-meta';
    const qualityLabel = item.quality === 'best' ? 'Mejor calidad' : `${item.quality || '192'} kbps`;
    const artistPart = item.author ? `${item.author} • ` : '';
    meta.textContent = `${artistPart}${(item.format || 'mp3').toUpperCase()} (${qualityLabel})${item.summary ? ` • ${item.summary}` : ''}`;

    details.appendChild(top);
    details.appendChild(meta);
    row.appendChild(details);
    li.appendChild(row);

    if (item.audio_url && item.status === 'completed') {
      const audio = document.createElement('audio');
      audio.className = 'history-audio';
      audio.controls = true;
      audio.preload = 'none';
      audio.src = item.audio_url;
      li.appendChild(audio);
    }

    historyList.appendChild(li);
  }
}

function upsertHistory(entry) {
  const items = loadHistory();
  const idx = items.findIndex((item) => item.id === entry.id || (item.job_id && item.job_id === entry.job_id && !entry.id));
  if (idx >= 0) {
    items[idx] = { ...items[idx], ...entry };
  } else {
    items.unshift(entry);
  }
  saveHistory(items);
  renderHistory();
}

function syncFormatAndQuality(source) {
  if (!formatSelect || !qualitySelect) return;
  if (source === 'format') {
    if (formatSelect.value === 'm4a') {
      qualitySelect.value = 'best';
    } else if (qualitySelect.value === 'best') {
      qualitySelect.value = '320';
    }
  } else if (source === 'quality') {
    if (qualitySelect.value === 'best') {
      formatSelect.value = 'm4a';
    } else {
      formatSelect.value = 'mp3';
    }
  }
}

async function fetchPreview(url) {
  if (!url || !/^https?:\/\//i.test(url)) {
    previewCard.classList.remove('visible');
    currentPreview = null;
    return;
  }
  try {
    const res = await fetch('/api/preview', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({ url }),
    });
    if (!res.ok) {
      previewCard.classList.remove('visible');
      currentPreview = null;
      return;
    }
    const data = await res.json();
    currentPreview = data;
    previewTitle.textContent = data.title || 'Sin título';
    previewAuthor.textContent = data.author || data.channel || data.uploader || '';
    previewType.textContent = data.is_playlist ? 'Playlist' : 'Canción';
    const count = Number(data.track_count || 1);
    previewCount.textContent = count === 1 ? '1 pista' : `${count} pistas`;
    if (data.thumbnail || data.thumbnail_url) {
      previewThumb.src = data.thumbnail || data.thumbnail_url;
      previewThumb.style.display = 'block';
    } else {
      previewThumb.style.display = 'none';
    }
    previewCard.classList.add('visible');
  } catch (_) {
    previewCard.classList.remove('visible');
  }
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

async function finalizeHistoryFromJob(jobId, data) {
  let jobDetails = null;
  try {
    const resp = await fetch(`/api/jobs/${jobId}`);
    if (resp.ok) jobDetails = await resp.json();
  } catch (_) {}

  const promoted = (jobDetails && jobDetails.promoted_files) || data.promoted_files || [];
  const manifest = (jobDetails && jobDetails.manifest) || data.manifest;
  const tracks = (manifest && manifest.tracks) || [];
  const format = (jobDetails && jobDetails.audio_format) || formatSelect.value;
  const quality = (jobDetails && jobDetails.quality) || qualitySelect.value;
  const author = (currentPreview && (currentPreview.author || currentPreview.title)) || '';
  const thumb = data.thumbnail_url || (currentPreview && (currentPreview.thumbnail || currentPreview.thumbnail_url)) || '';

  if (data.status === 'completed' && promoted.length > 0) {
    const items = loadHistory().filter((it) => it.job_id !== jobId);
    promoted.forEach((filePath, idx) => {
      const fileName = String(filePath).split(/[\\/]/).pop() || `Pista ${idx + 1}`;
      const cleanTitle = fileName.replace(/\.[^.]+$/, '');
      const trackMeta = tracks[idx];
      items.unshift({
        id: `${jobId}-${idx}`,
        job_id: jobId,
        status: 'completed',
        title: (trackMeta && trackMeta.title) || cleanTitle,
        track_number: parseTrackNumber(fileName, idx),
        author,
        format,
        quality,
        thumbnail_url: thumb,
        audio_url: `/api/jobs/${jobId}/audio/${idx}`,
      });
    });
    saveHistory(items);
    renderHistory();
    return;
  }

  upsertHistory({
    id: jobId,
    job_id: jobId,
    status: data.status,
    title: data.current_track || (currentPreview && currentPreview.title) || undefined,
    author,
    format,
    quality,
    thumbnail_url: thumb,
    summary: summaryEl.textContent,
  });
}

function applySnapshot(data) {
  progressCard.classList.add('visible');
  const status = data.status || 'pending';
  jobStatus.textContent = status;
  jobStatus.className = `badge ${status}`;
  const pct = Math.round(Number(data.progress || 0));
  progressBar.value = pct;
  if (progressPercent) progressPercent.textContent = `${pct}%`;
  progressMsg.textContent = data.message || '';
  if (data.current_track) trackTitle.textContent = data.current_track;
  if (data.thumbnail_url) {
    trackThumb.src = data.thumbnail_url;
    trackThumb.style.display = 'block';
  }

  const terminal = ['completed', 'failed', 'cancelled'].includes(status);
  if (terminal) {
    stopStreams();
    cancelBtn.disabled = true;
    submitBtn.disabled = false;
    let summaryText = '';
    if (status === 'completed') {
      const dl = data.downloaded_count ?? (data.manifest ? data.manifest.downloaded.length : 0);
      const sk = data.skipped_count ?? (data.manifest ? data.manifest.skipped_archive.length : 0);
      const fl = data.failed_count ?? (data.manifest ? data.manifest.failed.length : 0);
      summaryText = `Descargadas: ${dl} | Saltadas: ${sk} | Fallidas: ${fl}`;
    } else if (status === 'failed') {
      summaryText = `Error: ${data.error || data.message || 'Fallo en la descarga'}`;
    } else {
      summaryText = 'Descarga cancelada.';
    }
    summaryEl.textContent = summaryText;

    if (currentJobId) {
      const finishedJobId = currentJobId;
      currentJobId = null;
      finalizeHistoryFromJob(finishedJobId, data);
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
  }, 1500);
}

urlInput.addEventListener('input', () => {
  clearTimeout(previewTimer);
  const val = urlInput.value.trim();
  previewTimer = setTimeout(() => fetchPreview(val), 450);
});

formatSelect.addEventListener('change', () => syncFormatAndQuality('format'));
qualitySelect.addEventListener('change', () => syncFormatAndQuality('quality'));

openFolderBtn.addEventListener('click', async () => {
  formError.textContent = '';
  try {
    const res = await fetch('/api/open-folder', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({ dest_dir: destInput.value.trim() }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      formError.textContent = err.error || 'No se pudo abrir la carpeta.';
    }
  } catch (_) {
    formError.textContent = 'No se pudo comunicar con el servidor.';
  }
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  formError.textContent = '';
  submitBtn.disabled = true;

  const url = urlInput.value.trim();
  const destDir = destInput.value.trim();
  const format = formatSelect.value;
  const quality = qualitySelect.value;

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
        quality,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      formError.textContent = data.error || 'Error al crear el trabajo.';
      submitBtn.disabled = false;
      return;
    }
    upsertHistory({
      id: data.job_id,
      job_id: data.job_id,
      url,
      dest_dir: destDir,
      format,
      quality,
      title: (currentPreview && currentPreview.title) || url,
      author: (currentPreview && currentPreview.author) || '',
      thumbnail_url: (currentPreview && currentPreview.thumbnail) || '',
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
