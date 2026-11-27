const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';
const form = document.getElementById('download-form');
const urlInput = document.getElementById('url-input');
const pasteBtn = document.getElementById('paste-btn');
const destInput = document.getElementById('dest-input');
const pickFolderBtn = document.getElementById('pick-folder-btn');
const recentDirsChips = document.getElementById('recent-dirs-chips');
const formatQualitySelect = document.getElementById('format-quality-select');
const submitBtn = document.getElementById('submit-btn');
const cancelBtn = document.getElementById('cancel-btn');
const openFolderBtn = document.getElementById('open-folder-btn');
const downloadedCountEl = document.getElementById('downloaded-count');
const backendStatusEl = document.getElementById('backend-status');
const backendStatusText = document.getElementById('backend-status-text');
const formError = document.getElementById('form-error');
const ffmpegBanner = document.getElementById('ffmpeg-warning');
const ffmpegWarningText = document.getElementById('ffmpeg-warning-text');
const healthRemediationSteps = document.getElementById('health-remediation-steps');

const previewCard = document.getElementById('preview-card');
const previewLoading = document.getElementById('preview-loading');
const previewThumb = document.getElementById('preview-thumb');
const previewType = document.getElementById('preview-type');
const previewCount = document.getElementById('preview-count');
const previewTitle = document.getElementById('preview-title');
const previewAuthor = document.getElementById('preview-author');
const trackSelectorDetails = document.getElementById('track-selector-details');
const previewTracksList = document.getElementById('preview-tracks-list');
const selectAllTracksBtn = document.getElementById('select-all-tracks-btn');
const selectNoneTracksBtn = document.getElementById('select-none-tracks-btn');
const selectedTracksSummary = document.getElementById('selected-tracks-summary');

const progressCard = document.getElementById('progress-card');
const trackThumb = document.getElementById('track-thumb');
const trackTitle = document.getElementById('track-title');
const jobStatus = document.getElementById('job-status');
const processingEngineEl = document.getElementById('processing-engine');
const progressBar = document.getElementById('progress-bar');
const progressPercent = document.getElementById('progress-percent');
const progressMsg = document.getElementById('progress-message');
const liveTrackQueue = document.getElementById('live-track-queue');
const summaryEl = document.getElementById('summary');
const downloadZipBtn = document.getElementById('download-zip-btn');

const historySearch = document.getElementById('history-search');
const historyPlaylistFilter = document.getElementById('history-playlist-filter');
const historyList = document.getElementById('history-list');

const playerThumb = document.getElementById('player-thumb');
const playerTitle = document.getElementById('player-title');
const playerArtist = document.getElementById('player-artist');
const playerPrevBtn = document.getElementById('player-prev-btn');
const playerPlayBtn = document.getElementById('player-play-btn');
const playerNextBtn = document.getElementById('player-next-btn');
const playerSeek = document.getElementById('player-seek');
const playerTime = document.getElementById('player-time');

const globalAudio = document.createElement('audio');
globalAudio.preload = 'metadata';

const HISTORY_STORAGE_KEY = 'yt_music_dl_history_v2';
const RECENT_DIRS_KEY = 'yt_music_dl_recent_dirs_v1';
const DEFAULT_COVER_DATA_URI =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">' +
      '<rect width="64" height="64" rx="10" fill="#121620"/>' +
      '<path d="M27 42V20l18-3v20" fill="none" stroke="#8b5cf6" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>' +
      '<circle cx="23" cy="43" r="4.5" fill="none" stroke="#06b6d4" stroke-width="3"/>' +
      '<circle cx="41" cy="39" r="4.5" fill="none" stroke="#06b6d4" stroke-width="3"/>' +
      '</svg>'
  );

function attachCoverErrorHandler(img) {
  if (!img) return;
  const applyDefaultCover = () => {
    if (img.dataset.defaultCoverApplied === '1') return;
    img.dataset.defaultCoverApplied = '1';
    img.classList.add('is-default-cover');
    img.src = DEFAULT_COVER_DATA_URI;
  };
  img.addEventListener('error', applyDefaultCover);
  if (!img.getAttribute('src')) {
    applyDefaultCover();
  } else if (img.complete && img.naturalWidth === 0) {
    applyDefaultCover();
  }
}

function setImageSource(img, url) {
  if (!img) return;
  if (url) {
    img.dataset.defaultCoverApplied = '';
    img.classList.remove('is-default-cover');
    img.src = url;
  } else {
    img.dataset.defaultCoverApplied = '1';
    img.classList.add('is-default-cover');
    img.src = DEFAULT_COVER_DATA_URI;
  }
}

attachCoverErrorHandler(previewThumb);
attachCoverErrorHandler(trackThumb);
attachCoverErrorHandler(playerThumb);

let currentJobId = null;
let currentJobMeta = null;
let currentPreview = null;
let ffmpegAvailable = true;
let ytdlpAvailable = true;
let downloadDirWritable = true;
let healthDetails = {};
let lastHealthData = {};
let eventSource = null;
let pollTimer = null;
let previewTimer = null;
let previewAbortController = null;
let healthTimer = null;
let pollErrorCount = 0;

function setBackendStatus(state, label) {
  if (backendStatusEl) {
    backendStatusEl.className = `status-pill is-${state}`;
    backendStatusEl.setAttribute('aria-label', `Estado de conexión con el servidor local: ${label}`);
  }
  if (backendStatusText) {
    backendStatusText.textContent = label;
  }
}

function clearFormError() {
  if (!formError) return;
  formError.replaceChildren();
  formError.classList.remove('has-error');
}

function showFormError(message, options = {}) {
  if (!formError) return;
  const { onRetry } = options;
  formError.replaceChildren();
  if (!message) {
    formError.classList.remove('has-error');
    return;
  }
  formError.classList.add('has-error');
  const textSpan = document.createElement('span');
  textSpan.className = 'error-msg-text';
  textSpan.textContent = message;
  formError.appendChild(textSpan);

  const actionsDiv = document.createElement('div');
  actionsDiv.className = 'error-msg-actions';

  if (typeof onRetry === 'function') {
    const retryBtn = document.createElement('button');
    retryBtn.type = 'button';
    retryBtn.className = 'btn-secondary btn-xs';
    retryBtn.textContent = 'Reintentar';
    retryBtn.addEventListener('click', () => {
      clearFormError();
      onRetry();
    });
    actionsDiv.appendChild(retryBtn);
  }

  const dismissBtn = document.createElement('button');
  dismissBtn.type = 'button';
  dismissBtn.className = 'btn-secondary btn-xs';
  dismissBtn.textContent = 'Cerrar';
  dismissBtn.setAttribute('aria-label', 'Cerrar mensaje de error');
  dismissBtn.addEventListener('click', clearFormError);
  actionsDiv.appendChild(dismissBtn);

  formError.appendChild(actionsDiv);
}

async function checkBackendHealth(options = {}) {
  const { showErrorOnFail = false } = options;
  try {
    const destVal = destInput ? destInput.value.trim() : '';
    const res = destVal
      ? await fetch(`/api/health?dest_dir=${encodeURIComponent(destVal)}`)
      : await fetch('/api/health');
    const hData = await res.json().catch(() => ({}));
    ffmpegAvailable = Boolean(hData.ffmpeg_available);
    ytdlpAvailable = hData.yt_dlp_available !== undefined ? Boolean(hData.yt_dlp_available) : true;
    downloadDirWritable =
      hData.download_dir_writable !== undefined ? Boolean(hData.download_dir_writable) : true;
    healthDetails = hData.details || {};
    lastHealthData = hData;
    updateFfmpegWarning();
    if (res.ok) {
      setBackendStatus('online', 'Conectado');
      return true;
    }
    if (res.status === 503) {
      if (!ytdlpAvailable) {
        setBackendStatus('degraded', 'Falta yt-dlp');
      } else if (!downloadDirWritable) {
        setBackendStatus('degraded', 'Sin permiso de escritura');
      } else {
        setBackendStatus('degraded', 'Conectado (sin ffmpeg)');
      }
      return true;
    }
    setBackendStatus('offline', 'Error del servidor');
    if (showErrorOnFail) {
      showFormError('El servidor local devolvió un error al comprobar su estado.', {
        onRetry: () => checkBackendHealth({ showErrorOnFail: true }),
      });
    }
    return false;
  } catch (_) {
    setBackendStatus('offline', 'Sin conexión');
    if (showErrorOnFail) {
      showFormError('No hay conexión con el servidor local.', {
        onRetry: () => checkBackendHealth({ showErrorOnFail: true }),
      });
    }
    return false;
  }
}

let queueTracksState = [];
let playableQueue = [];
let currentPlayingIndex = -1;

function parseFormatQuality() {
  const raw = (formatQualitySelect && formatQualitySelect.value) || 'mp3:192';
  const [format, quality] = raw.split(':');
  return { format: format || 'mp3', quality: quality || '192' };
}

function getProcessingEngineInfo(format) {
  if (format === 'm4a') {
    return {
      label: 'Motor: yt-dlp nativo (audio original)',
      isNative: true,
    };
  }
  if (!ffmpegAvailable) {
    return {
      label: 'Motor alternativo: yt-dlp nativo (sin ffmpeg)',
      isNative: true,
    };
  }
  return {
    label: 'Motor: ffmpeg / recodificación',
    isNative: false,
  };
}

function updateProcessingEngineBadge(format) {
  if (!processingEngineEl) return;
  const targetFormat = format || parseFormatQuality().format;
  const info = getProcessingEngineInfo(targetFormat);
  processingEngineEl.textContent = info.label;
  processingEngineEl.classList.toggle('is-native', info.isNative);
}

function syncFfmpegFormatOptions() {
  if (!formatQualitySelect) return;
  for (const opt of Array.from(formatQualitySelect.options)) {
    if (opt.value.startsWith('mp3:')) {
      opt.disabled = !ffmpegAvailable;
    }
  }
  if (!ffmpegAvailable && formatQualitySelect.value.startsWith('mp3:')) {
    const fallbackOpt = Array.from(formatQualitySelect.options).find((opt) => !opt.disabled);
    if (fallbackOpt) {
      formatQualitySelect.value = fallbackOpt.value;
    }
  }
}

function updateFfmpegWarning() {
  syncFfmpegFormatOptions();
  if (!ffmpegBanner) return;
  const { format } = parseFormatQuality();
  updateProcessingEngineBadge(format);

  const missingMessages = [];
  const steps = [];

  if (!ffmpegAvailable) {
    const missingBins = [];
    if (healthDetails.ffmpeg === false) missingBins.push('ffmpeg');
    if (healthDetails.ffprobe === false) missingBins.push('ffprobe');
    const binLabel = missingBins.length > 0 ? missingBins.join(' y ') : 'ffmpeg y ffprobe';
    missingMessages.push(
      `Falta ${binLabel} en el PATH. Las opciones de conversión a MP3 están bloqueadas; puedes descargar en M4A · Audio original o instalar ffmpeg.`
    );
    steps.push('Windows: ejecuta "winget install Gyan.FFmpeg" o agrega la carpeta bin de ffmpeg al PATH del sistema.');
    steps.push('macOS: ejecuta "brew install ffmpeg" | Linux (Debian/Ubuntu): "sudo apt install ffmpeg".');
    steps.push('Reinicia la aplicación después de instalar ffmpeg para desbloquear la conversión a MP3.');
  }

  if (!ytdlpAvailable) {
    missingMessages.push('yt-dlp no está disponible en el entorno Python.');
    steps.push('Instala o actualiza yt-dlp con "pip install -U yt-dlp" y reinicia el servidor.');
  }

  if (!downloadDirWritable) {
    const dirPath = lastHealthData.download_dir || (destInput ? destInput.value.trim() : '');
    missingMessages.push(
      `Sin permisos de escritura en la carpeta de descargas${dirPath ? ` (${dirPath})` : ''}.`
    );
    steps.push('Otorga permisos de escritura a la carpeta o selecciona otra carpeta de destino válida.');
  }

  if (missingMessages.length > 0) {
    if (ffmpegWarningText) {
      ffmpegWarningText.textContent = missingMessages.join(' ');
    }
    if (healthRemediationSteps) {
      healthRemediationSteps.replaceChildren();
      for (const stepText of steps) {
        const li = document.createElement('li');
        li.textContent = stepText;
        healthRemediationSteps.appendChild(li);
      }
      healthRemediationSteps.hidden = steps.length === 0;
    }
    ffmpegBanner.classList.add('visible');
  } else {
    if (healthRemediationSteps) {
      healthRemediationSteps.replaceChildren();
      healthRemediationSteps.hidden = true;
    }
    ffmpegBanner.classList.remove('visible');
  }
}

function formatDuration(seconds) {
  const num = Number(seconds);
  if (!Number.isFinite(num) || num <= 0) return '';
  const mins = Math.floor(num / 60);
  const secs = Math.floor(num % 60);
  return `${mins}:${String(secs).padStart(2, '0')}`;
}

function loadRecentDirs() {
  try {
    const raw = localStorage.getItem(RECENT_DIRS_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch (_) {
    return [];
  }
}

function saveRecentDir(dir) {
  if (!dir) return;
  const list = loadRecentDirs().filter((d) => d !== dir);
  list.unshift(dir);
  try {
    localStorage.setItem(RECENT_DIRS_KEY, JSON.stringify(list.slice(0, 5)));
  } catch (_) {}
  renderRecentDirChips();
}

function renderRecentDirChips() {
  if (!recentDirsChips) return;
  const dirs = loadRecentDirs();
  recentDirsChips.replaceChildren();
  for (const dir of dirs) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip-btn';
    btn.title = dir;
    btn.textContent = dir.split(/[\\/]/).filter(Boolean).pop() || dir;
    btn.addEventListener('click', () => {
      destInput.value = dir;
    });
    recentDirsChips.appendChild(btn);
  }
}

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
    localStorage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(items.slice(0, 50)));
  } catch (_) {}
}

function updateDownloadedCounter(items) {
  if (!downloadedCountEl) return;
  const completedCount = items.filter((item) => item.status === 'completed').length;
  downloadedCountEl.textContent = String(completedCount);
}

function parseTrackNumber(fileName, trackIndex) {
  const match = String(fileName || '').match(/^(\d{1,3})\s*-\s*/);
  if (match) return `#${match[1].padStart(2, '0')}`;
  return `#${String(trackIndex + 1).padStart(2, '0')}`;
}

function refreshPlaylistFilterOptions(items) {
  if (!historyPlaylistFilter) return;
  const currentVal = historyPlaylistFilter.value;
  const playlists = Array.from(
    new Set(items.map((it) => it.playlist_title).filter((v) => typeof v === 'string' && v.trim()))
  );
  historyPlaylistFilter.replaceChildren();
  const defaultOpt = document.createElement('option');
  defaultOpt.value = '';
  defaultOpt.textContent = 'Todas las playlists';
  historyPlaylistFilter.appendChild(defaultOpt);
  for (const pl of playlists) {
    const opt = document.createElement('option');
    opt.value = pl;
    opt.textContent = pl;
    historyPlaylistFilter.appendChild(opt);
  }
  if (playlists.includes(currentVal)) {
    historyPlaylistFilter.value = currentVal;
  }
}

function renderHistory() {
  if (!historyList) return;
  const allItems = loadHistory();
  updateDownloadedCounter(allItems);
  refreshPlaylistFilterOptions(allItems);

  const query = ((historySearch && historySearch.value) || '').trim().toLowerCase();
  const plFilter = (historyPlaylistFilter && historyPlaylistFilter.value) || '';

  const filtered = allItems.filter((item) => {
    if (plFilter && item.playlist_title !== plFilter) return false;
    if (query) {
      const hay = `${item.title || ''} ${item.author || ''} ${item.playlist_title || ''}`.toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  });

  playableQueue = filtered.filter((item) => item.audio_url && item.status === 'completed');
  updatePlayerButtons();

  historyList.replaceChildren();
  if (filtered.length === 0) {
    const empty = document.createElement('li');
    empty.className = 'sub';
    empty.textContent = allItems.length === 0
      ? 'Aún no hay descargas en el historial.'
      : 'No hay pistas que coincidan con el filtro.';
    historyList.appendChild(empty);
    return;
  }

  for (const item of filtered) {
    const li = document.createElement('li');
    const isPlayingThis =
      currentPlayingIndex >= 0 &&
      playableQueue[currentPlayingIndex] &&
      playableQueue[currentPlayingIndex].id === item.id;
    li.className = `history-item${isPlayingThis ? ' active-playing' : ''}`;

    const row = document.createElement('div');
    row.className = 'history-card-row';

    const img = document.createElement('img');
    img.className = 'history-thumb';
    img.alt = item.title || 'Portada';
    attachCoverErrorHandler(img);
    setImageSource(img, item.thumbnail_url || '');
    row.appendChild(img);

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
    const qualityLabel = item.quality === 'best' ? 'Audio original' : `${item.quality || '192'} kbps`;
    const artistPart = item.author ? `${item.author} • ` : '';
    const plPart = item.playlist_title ? `${item.playlist_title} • ` : '';
    meta.textContent = `${plPart}${artistPart}${(item.format || 'mp3').toUpperCase()} (${qualityLabel})${item.summary ? ` • ${item.summary}` : ''}`;

    details.appendChild(top);
    details.appendChild(meta);
    row.appendChild(details);

    if (item.audio_url && item.status === 'completed') {
      const itemActions = document.createElement('div');
      itemActions.className = 'history-item-actions';

      const playBtn = document.createElement('button');
      playBtn.type = 'button';
      playBtn.className = 'btn-secondary btn-xs';
      playBtn.textContent = isPlayingThis && !globalAudio.paused ? 'Pausar' : 'Reproducir';
      playBtn.addEventListener('click', () => {
        const idx = playableQueue.findIndex((p) => p.id === item.id);
        if (idx >= 0) {
          if (idx === currentPlayingIndex && !globalAudio.paused) {
            globalAudio.pause();
          } else {
            playTrackAtIndex(idx);
          }
        }
      });
      itemActions.appendChild(playBtn);

      const dlLink = document.createElement('a');
      dlLink.className = 'btn-secondary btn-xs';
      dlLink.href = `${item.audio_url}?download=1`;
      dlLink.download = '';
      dlLink.textContent = 'Descargar';
      itemActions.appendChild(dlLink);

      row.appendChild(itemActions);
    }

    li.appendChild(row);
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

function playTrackAtIndex(idx) {
  if (idx < 0 || idx >= playableQueue.length) return;
  currentPlayingIndex = idx;
  const track = playableQueue[idx];
  globalAudio.src = track.audio_url;
  globalAudio.play().catch(() => {});
  if (playerTitle) playerTitle.textContent = track.title || 'Sin título';
  if (playerArtist) playerArtist.textContent = track.author || track.playlist_title || 'Descarga local';
  if (playerThumb) {
    setImageSource(playerThumb, track.thumbnail_url || '');
    playerThumb.style.display = 'block';
  }
  updatePlayerButtons();
  renderHistory();
}

function updatePlayerButtons() {
  const hasQueue = playableQueue.length > 0;
  if (playerPlayBtn) {
    playerPlayBtn.disabled = !hasQueue;
    playerPlayBtn.textContent = !globalAudio.paused && currentPlayingIndex >= 0 ? 'Pausar' : 'Reproducir';
  }
  if (playerPrevBtn) playerPrevBtn.disabled = !hasQueue || currentPlayingIndex <= 0;
  if (playerNextBtn) playerNextBtn.disabled = !hasQueue || currentPlayingIndex >= playableQueue.length - 1;
  if (playerSeek) playerSeek.disabled = currentPlayingIndex < 0;
}

if (playerPlayBtn) {
  playerPlayBtn.addEventListener('click', () => {
    if (currentPlayingIndex < 0 && playableQueue.length > 0) {
      playTrackAtIndex(0);
    } else if (globalAudio.paused) {
      globalAudio.play().catch(() => {});
    } else {
      globalAudio.pause();
    }
  });
}

if (playerPrevBtn) {
  playerPrevBtn.addEventListener('click', () => {
    if (currentPlayingIndex > 0) playTrackAtIndex(currentPlayingIndex - 1);
  });
}

if (playerNextBtn) {
  playerNextBtn.addEventListener('click', () => {
    if (currentPlayingIndex < playableQueue.length - 1) playTrackAtIndex(currentPlayingIndex + 1);
  });
}

if (playerSeek) {
  playerSeek.addEventListener('input', () => {
    if (Number.isFinite(globalAudio.duration) && globalAudio.duration > 0) {
      globalAudio.currentTime = (Number(playerSeek.value) / 100) * globalAudio.duration;
    }
  });
}

globalAudio.addEventListener('play', updatePlayerButtons);
globalAudio.addEventListener('pause', () => {
  updatePlayerButtons();
  renderHistory();
});
globalAudio.addEventListener('timeupdate', () => {
  const dur = globalAudio.duration || 0;
  const cur = globalAudio.currentTime || 0;
  if (playerSeek && dur > 0) {
    playerSeek.value = String((cur / dur) * 100);
  }
  if (playerTime) {
    playerTime.textContent = `${formatDuration(cur) || '0:00'} / ${formatDuration(dur) || '0:00'}`;
  }
});
globalAudio.addEventListener('ended', () => {
  if (currentPlayingIndex >= 0 && currentPlayingIndex < playableQueue.length - 1) {
    playTrackAtIndex(currentPlayingIndex + 1);
  } else {
    updatePlayerButtons();
    renderHistory();
  }
});

function updateSelectedTracksSummary() {
  if (!previewTracksList || !selectedTracksSummary) return;
  const boxes = Array.from(previewTracksList.querySelectorAll('input[type="checkbox"]'));
  const checked = boxes.filter((b) => b.checked).length;
  selectedTracksSummary.textContent = `${checked} de ${boxes.length} seleccionadas`;
}

function renderPreviewTracks(tracks) {
  if (!trackSelectorDetails || !previewTracksList) return;
  previewTracksList.replaceChildren();
  if (!Array.isArray(tracks) || tracks.length === 0) {
    trackSelectorDetails.classList.remove('visible');
    return;
  }

  for (const track of tracks) {
    const li = document.createElement('li');
    li.className = 'preview-track-item';

    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = true;
    cb.value = String(track.index);
    cb.dataset.title = track.title || '';
    cb.addEventListener('change', updateSelectedTracksSummary);

    label.appendChild(cb);

    const thumbUrl = track.thumbnail || track.thumbnail_url;
    if (thumbUrl) {
      const img = document.createElement('img');
      img.className = 'preview-track-thumb';
      img.alt = '';
      attachCoverErrorHandler(img);
      setImageSource(img, thumbUrl);
      label.appendChild(img);
    }

    const span = document.createElement('span');
    span.className = 'preview-track-title';
    span.textContent = `${String(track.index).padStart(2, '0')} - ${track.title || 'Sin título'}`;
    label.appendChild(span);

    const dur = formatDuration(track.duration);
    if (dur) {
      const durEl = document.createElement('span');
      durEl.className = 'sub';
      durEl.textContent = dur;
      label.appendChild(durEl);
    }

    li.appendChild(label);
    previewTracksList.appendChild(li);
  }

  trackSelectorDetails.classList.add('visible');
  updateSelectedTracksSummary();
}

function getSelectedTrackIndices() {
  if (!previewTracksList) return null;
  const boxes = Array.from(previewTracksList.querySelectorAll('input[type="checkbox"]'));
  if (boxes.length === 0) return null;
  const checked = boxes.filter((b) => b.checked).map((b) => Number(b.value));
  return checked;
}

if (selectAllTracksBtn) {
  selectAllTracksBtn.addEventListener('click', () => {
    previewTracksList.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
      cb.checked = true;
    });
    updateSelectedTracksSummary();
  });
}

if (selectNoneTracksBtn) {
  selectNoneTracksBtn.addEventListener('click', () => {
    previewTracksList.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
      cb.checked = false;
    });
    updateSelectedTracksSummary();
  });
}

function renderLiveTrackQueue() {
  if (!liveTrackQueue) return;
  liveTrackQueue.replaceChildren();
  for (const item of queueTracksState) {
    const li = document.createElement('li');
    li.className = 'live-queue-item';

    const title = document.createElement('span');
    title.className = 'live-queue-title';
    title.textContent = item.title;

    const actions = document.createElement('div');
    actions.className = 'live-queue-actions';

    const badge = document.createElement('span');
    badge.className = `badge ${item.badgeClass || ''}`;
    badge.textContent = item.label;
    actions.appendChild(badge);

    if (item.status === 'failed' && item.retryPayload) {
      const retryBtn = document.createElement('button');
      retryBtn.type = 'button';
      retryBtn.className = 'btn-secondary btn-xs';
      retryBtn.textContent = 'Reintentar';
      retryBtn.addEventListener('click', () => {
        submitDownloadJob(item.retryPayload);
      });
      actions.appendChild(retryBtn);
    }

    li.appendChild(title);
    li.appendChild(actions);
    liveTrackQueue.appendChild(li);
  }
}

function updateLiveQueueForProgress(currentTrackName) {
  if (!currentTrackName || queueTracksState.length === 0) return;
  let found = false;
  queueTracksState = queueTracksState.map((item) => {
    if (item.title === currentTrackName || currentTrackName.includes(item.rawTitle)) {
      found = true;
      return { ...item, status: 'downloading', label: 'Descargando', badgeClass: 'badge-accent' };
    }
    if (!found && item.status === 'downloading') {
      return { ...item, status: 'completed', label: 'Completada', badgeClass: 'completed' };
    }
    return item;
  });
  renderLiveTrackQueue();
}

function finalizeLiveQueueFromManifest(manifest, retryBasePayload) {
  const tracks = (manifest && manifest.tracks) || [];
  if (tracks.length === 0) return;
  queueTracksState = tracks.map((t) => {
    if (t.status === 'downloaded') {
      return { title: t.title, rawTitle: t.title, status: 'completed', label: 'Completada', badgeClass: 'completed' };
    }
    if (t.status === 'skipped') {
      return { title: t.title, rawTitle: t.title, status: 'skipped', label: 'Ya descargada/omitida', badgeClass: '' };
    }
    return {
      title: t.title,
      rawTitle: t.title,
      status: 'failed',
      label: 'Error',
      badgeClass: 'failed',
      retryPayload: retryBasePayload,
    };
  });
  renderLiveTrackQueue();
}

async function fetchPreview(url) {
  if (previewAbortController) {
    previewAbortController.abort();
    previewAbortController = null;
  }
  if (!url || !/^https?:\/\//i.test(url)) {
    if (previewLoading) previewLoading.hidden = true;
    previewCard.classList.remove('visible', 'is-loading');
    previewCard.removeAttribute('aria-busy');
    currentPreview = null;
    renderPreviewTracks([]);
    return;
  }
  previewAbortController = new AbortController();
  const { signal } = previewAbortController;
  if (previewLoading) previewLoading.hidden = false;
  previewCard.classList.add('visible', 'is-loading');
  previewCard.setAttribute('aria-busy', 'true');
  try {
    const cacheBust = Date.now();
    const res = await fetch(`/api/preview?_t=${cacheBust}`, {
      method: 'POST',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-cache',
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({ url, _t: cacheBust }),
      signal,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      previewCard.classList.remove('visible', 'is-loading');
      currentPreview = null;
      renderPreviewTracks([]);
      showFormError(err.error || 'No se pudo obtener la vista previa de la URL.', {
        onRetry: () => fetchPreview(url),
      });
      return;
    }
    const data = await res.json();
    clearFormError();
    currentPreview = data;
    previewTitle.textContent = data.title || 'Sin título';
    previewAuthor.textContent = data.author || data.channel || data.uploader || '';
    previewType.textContent = data.is_playlist ? 'Playlist' : 'Canción';
    const count = Number(data.track_count || 1);
    previewCount.textContent = count === 1 ? '1 pista' : `${count} pistas`;
    setImageSource(previewThumb, data.thumbnail || data.thumbnail_url || '');
    previewThumb.style.display = 'block';
    renderPreviewTracks(data.tracks || []);
    previewCard.classList.remove('is-loading');
    previewCard.classList.add('visible');
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    previewCard.classList.remove('visible', 'is-loading');
    setBackendStatus('offline', 'Sin conexión');
    showFormError('No se pudo conectar con el servidor para obtener la vista previa.', {
      onRetry: () => fetchPreview(url),
    });
  } finally {
    if (previewLoading) previewLoading.hidden = true;
    previewCard.removeAttribute('aria-busy');
  }
}

async function init() {
  renderRecentDirChips();
  renderHistory();

  if (backendStatusEl) {
    backendStatusEl.addEventListener('click', () => {
      setBackendStatus('checking', 'Comprobando...');
      checkBackendHealth({ showErrorOnFail: true });
    });
  }

  await checkBackendHealth({ showErrorOnFail: true });
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = setInterval(() => {
    checkBackendHealth();
  }, 30000);

  try {
    const res = await fetch('/api/default-dir');
    if (res.ok) {
      const data = await res.json();
      if (data.default_dir) {
        destInput.value = data.default_dir;
        if (loadRecentDirs().length === 0) saveRecentDir(data.default_dir);
      }
    }
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
  const { format: selectedFormat, quality: selectedQuality } = parseFormatQuality();
  const format = (jobDetails && jobDetails.audio_format) || selectedFormat;
  const quality = (jobDetails && jobDetails.quality) || selectedQuality;
  const author = (currentJobMeta && currentJobMeta.author) || '';
  const playlistTitle = (currentJobMeta && currentJobMeta.playlist_title) || '';
  const thumb = data.thumbnail_url || (currentJobMeta && currentJobMeta.thumbnail_url) || '';

  if (manifest) {
    finalizeLiveQueueFromManifest(manifest, currentJobMeta && currentJobMeta.requestPayload);
  }

  if (data.status === 'completed' && promoted.length > 0) {
    if (downloadZipBtn) {
      downloadZipBtn.href = `/api/jobs/${jobId}/zip`;
      downloadZipBtn.hidden = false;
    }
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
        playlist_title: playlistTitle,
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
    title: data.current_track || (currentJobMeta && currentJobMeta.title) || undefined,
    author,
    playlist_title: playlistTitle,
    format,
    quality,
    thumbnail_url: thumb,
    summary: summaryEl.textContent,
  });
}

function applySnapshot(data) {
  progressCard.classList.add('visible');
  const activeFormat = (currentJobMeta && currentJobMeta.requestPayload && currentJobMeta.requestPayload.format) || parseFormatQuality().format;
  updateProcessingEngineBadge(activeFormat);
  const status = data.status || 'pending';
  jobStatus.textContent = status;
  jobStatus.className = `badge ${status}`;
  const pct = Math.round(Number(data.progress || 0));
  progressBar.value = pct;
  if (progressPercent) progressPercent.textContent = `${pct}%`;
  progressMsg.textContent = data.message || '';
  if (data.current_track) {
    trackTitle.textContent = data.current_track;
    updateLiveQueueForProgress(data.current_track);
  }
  if (data.thumbnail_url) {
    setImageSource(trackThumb, data.thumbnail_url);
    trackThumb.style.display = 'block';
  } else if (!trackThumb.getAttribute('src')) {
    setImageSource(trackThumb, '');
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
      if (queueTracksState.length > 0 && currentJobMeta) {
        queueTracksState = queueTracksState.map((item) => ({
          ...item,
          status: 'failed',
          label: 'Error',
          badgeClass: 'failed',
          retryPayload: currentJobMeta.requestPayload,
        }));
        renderLiveTrackQueue();
      }
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
  if (downloadZipBtn) {
    downloadZipBtn.hidden = true;
    downloadZipBtn.href = '#';
  }

  eventSource = new EventSource(`/api/jobs/${jobId}/events`);
  const handleEvent = (ev) => {
    try {
      applySnapshot(JSON.parse(ev.data));
    } catch (_) {}
  };
  ['snapshot', 'status', 'progress', 'completed', 'failed', 'cancelled'].forEach((name) => {
    eventSource.addEventListener(name, handleEvent);
  });

  pollErrorCount = 0;
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch(`/api/jobs/${jobId}`);
      if (r.ok) {
        pollErrorCount = 0;
        const job = await r.json();
        if (job.progress) applySnapshot(job.progress);
      } else {
        pollErrorCount += 1;
      }
    } catch (_) {
      pollErrorCount += 1;
      if (pollErrorCount >= 3) {
        setBackendStatus('offline', 'Sin conexión');
        showFormError('Se perdió la conexión con el servidor local durante la descarga.', {
          onRetry: () => checkBackendHealth({ showErrorOnFail: true }),
        });
      }
    }
  }, 1500);
}

async function submitDownloadJob(requestPayload) {
  clearFormError();
  if (form) form.setAttribute('aria-busy', 'true');
  setButtonLoading(submitBtn, true, 'Iniciando...');

  try {
    const res = await fetch('/api/jobs', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify(requestPayload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setButtonLoading(submitBtn, false);
      if (form) form.removeAttribute('aria-busy');
      showFormError(data.error || 'Error al crear el trabajo.', {
        onRetry: () => submitDownloadJob(requestPayload),
      });
      return;
    }

    setButtonLoading(submitBtn, false);
    submitBtn.disabled = true;
    if (form) form.removeAttribute('aria-busy');
    setBackendStatus('online', 'Conectado');

    saveRecentDir(requestPayload.dest_dir);

    const previewTracks = (currentPreview && currentPreview.tracks) || [];
    const selectedSet = Array.isArray(requestPayload.selected_tracks)
      ? new Set(requestPayload.selected_tracks)
      : null;

    if (previewTracks.length > 0) {
      queueTracksState = previewTracks
        .filter((t) => !selectedSet || selectedSet.has(t.index))
        .map((t) => ({
          title: `${String(t.index).padStart(2, '0')} - ${t.title}`,
          rawTitle: t.title,
          status: 'queued',
          label: 'En cola',
          badgeClass: '',
        }));
    } else {
      queueTracksState = [
        {
          title: (currentPreview && currentPreview.title) || requestPayload.url,
          rawTitle: (currentPreview && currentPreview.title) || requestPayload.url,
          status: 'queued',
          label: 'En cola',
          badgeClass: '',
        },
      ];
    }
    renderLiveTrackQueue();

    currentJobMeta = {
      title: (currentPreview && currentPreview.title) || requestPayload.url,
      author: (currentPreview && currentPreview.author) || '',
      playlist_title: (currentPreview && currentPreview.is_playlist) ? currentPreview.title : '',
      thumbnail_url: (currentPreview && (currentPreview.thumbnail || currentPreview.thumbnail_url)) || '',
      requestPayload,
    };

    upsertHistory({
      id: data.job_id,
      job_id: data.job_id,
      url: requestPayload.url,
      dest_dir: requestPayload.dest_dir,
      format: requestPayload.format,
      quality: requestPayload.quality,
      title: currentJobMeta.title,
      author: currentJobMeta.author,
      playlist_title: currentJobMeta.playlist_title,
      thumbnail_url: currentJobMeta.thumbnail_url,
      status: data.status || 'pending',
    });
    startMonitoring(data.job_id);
  } catch (_) {
    setButtonLoading(submitBtn, false);
    if (form) form.removeAttribute('aria-busy');
    setBackendStatus('offline', 'Sin conexión');
    showFormError('Error de conexión con el servidor local.', {
      onRetry: () => submitDownloadJob(requestPayload),
    });
  }
}

if (pasteBtn) {
  pasteBtn.addEventListener('click', async () => {
    clearFormError();
    try {
      const text = await navigator.clipboard.readText();
      if (text && text.trim()) {
        urlInput.value = text.trim();
        fetchPreview(urlInput.value);
      }
    } catch (_) {
      urlInput.focus();
      showFormError('No se pudo leer el portapapeles; pega la URL manualmente.');
    }
  });
}

const originalButtonNodes = new WeakMap();

function setButtonLoading(btn, isLoading, loadingText) {
  if (!btn) return;
  if (isLoading) {
    if (!originalButtonNodes.has(btn)) {
      originalButtonNodes.set(btn, Array.from(btn.childNodes));
    }
    const spinner = document.createElement('span');
    spinner.className = 'spinner spinner-sm';
    spinner.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.textContent = loadingText;
    btn.replaceChildren(spinner, label);
    btn.disabled = true;
    btn.classList.add('is-loading');
    btn.setAttribute('aria-busy', 'true');
  } else {
    btn.disabled = false;
    btn.classList.remove('is-loading');
    btn.setAttribute('aria-busy', 'false');
    const saved = originalButtonNodes.get(btn);
    if (saved) {
      btn.replaceChildren(...saved);
    }
  }
}

if (pickFolderBtn) {
  pickFolderBtn.addEventListener('click', async () => {
    clearFormError();
    setButtonLoading(pickFolderBtn, true, 'Abriendo...');
    try {
      const res = await fetch('/api/pick-folder', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrfToken,
        },
        body: JSON.stringify({ initial_dir: destInput.value.trim() }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.selected_dir) {
        destInput.value = data.selected_dir;
        saveRecentDir(data.selected_dir);
      } else if (!res.ok) {
        showFormError(data.error || 'Selector de carpetas no disponible.');
      }
    } catch (_) {
      showFormError('No se pudo abrir el selector de carpetas.', {
        onRetry: () => pickFolderBtn.click(),
      });
    } finally {
      setButtonLoading(pickFolderBtn, false);
    }
  });
}

if (formatQualitySelect) {
  formatQualitySelect.addEventListener('change', updateFfmpegWarning);
}

if (destInput) {
  destInput.addEventListener('change', () => {
    checkBackendHealth();
  });
}

if (historySearch) {
  historySearch.addEventListener('input', renderHistory);
}
if (historyPlaylistFilter) {
  historyPlaylistFilter.addEventListener('change', renderHistory);
}

urlInput.addEventListener('input', () => {
  clearTimeout(previewTimer);
  const val = urlInput.value.trim();
  previewTimer = setTimeout(() => fetchPreview(val), 450);
});

openFolderBtn.addEventListener('click', async () => {
  clearFormError();
  setButtonLoading(openFolderBtn, true, 'Abriendo...');
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
      showFormError(err.error || 'No se pudo abrir la carpeta.');
    }
  } catch (_) {
    showFormError('No se pudo comunicar con el servidor.', {
      onRetry: () => openFolderBtn.click(),
    });
  } finally {
    setButtonLoading(openFolderBtn, false);
  }
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = urlInput.value.trim();
  const destDir = destInput.value.trim();
  const { format, quality } = parseFormatQuality();
  const selectedTracks = getSelectedTrackIndices();

  if (Array.isArray(selectedTracks) && selectedTracks.length === 0) {
    showFormError('Selecciona al menos una canción de la playlist.');
    return;
  }

  const payload = {
    url,
    dest_dir: destDir,
    format,
    quality,
  };
  if (Array.isArray(selectedTracks) && selectedTracks.length > 0) {
    payload.selected_tracks = selectedTracks;
  }
  await submitDownloadJob(payload);
});

cancelBtn.addEventListener('click', async () => {
  if (!currentJobId) return;
  clearFormError();
  setButtonLoading(cancelBtn, true, 'Cancelando...');
  try {
    const res = await fetch(`/api/jobs/${currentJobId}/cancel`, {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      setButtonLoading(cancelBtn, false);
      showFormError(err.error || 'No se pudo cancelar la descarga.');
      return;
    }
    setButtonLoading(cancelBtn, false);
    cancelBtn.disabled = true;
  } catch (_) {
    setButtonLoading(cancelBtn, false);
    showFormError('Error de conexión al intentar cancelar la descarga.');
  }
});

init();
