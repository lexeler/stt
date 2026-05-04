/* Citrus Lab — upload + polling + restore */

const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const jobsEl = document.getElementById('jobs');
const jobTpl = document.getElementById('jobTemplate');

const cards = new Map(); // job_id -> card element

['dragenter', 'dragover'].forEach(ev =>
  dropZone.addEventListener(ev, e => {
    e.preventDefault();
    e.stopPropagation();
    dropZone.classList.add('dragover');
  })
);

['dragleave', 'drop'].forEach(ev =>
  dropZone.addEventListener(ev, e => {
    e.preventDefault();
    e.stopPropagation();
    dropZone.classList.remove('dragover');
  })
);

dropZone.addEventListener('drop', e => {
  const f = e.dataTransfer?.files?.[0];
  if (f) startUpload(f);
});

fileInput.addEventListener('change', e => {
  const f = e.target.files?.[0];
  if (f) startUpload(f);
  e.target.value = '';
});

function fmtSize(bytes) {
  const u = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
  let f = bytes;
  for (const unit of u) {
    if (f < 1024 || unit === u[u.length - 1])
      return f >= 10 ? `${f.toFixed(1)} ${unit}` : `${Math.round(f)} ${unit}`;
    f /= 1024;
  }
}

function ensureCard(jobId, filename, sizeText) {
  let card = cards.get(jobId);
  if (card) return card;
  card = jobTpl.content.firstElementChild.cloneNode(true);
  card.dataset.jobId = jobId;
  card.querySelector('.job-name').textContent = filename;
  card.querySelector('.meta-size').textContent = sizeText;
  card.querySelector('.meta-duration').textContent = '— сек';
  card.querySelector('.meta-eta').textContent = 'оценка...';
  attachCardHandlers(card, jobId);
  jobsEl.prepend(card);
  cards.set(jobId, card);
  return card;
}

function attachCardHandlers(card, jobId) {
  const cancelBtn = card.querySelector('.btn-cancel');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', async () => {
      cancelBtn.disabled = true;
      cancelBtn.textContent = 'Отменяется…';
      try { await fetch(`/jobs/${jobId}/cancel`, {method: 'POST'}); } catch {}
    });
  }
  const closeBtn = card.querySelector('.btn-close');
  if (closeBtn) {
    closeBtn.addEventListener('click', async () => {
      try { await fetch(`/jobs/${jobId}`, {method: 'DELETE'}); } catch {}
      card.remove();
      cards.delete(jobId);
    });
  }
  const previewBtn = card.querySelector('.btn-preview');
  if (previewBtn) {
    previewBtn.addEventListener('click', async () => {
      const block = card.querySelector('.job-preview');
      const pre = card.querySelector('.preview-text');
      const label = previewBtn.querySelector('.btn-label');
      if (!block.hidden) {
        block.hidden = true;
        label.textContent = 'Посмотреть';
        return;
      }
      if (!pre.textContent) {
        label.textContent = 'Загрузка…';
        try {
          const r = await fetch(`/jobs/${jobId}/text/${previewBtn.dataset.kind}`);
          if (!r.ok) throw new Error('http ' + r.status);
          pre.textContent = await r.text();
        } catch (e) {
          pre.textContent = 'Ошибка загрузки текста: ' + e.message;
        }
      }
      block.hidden = false;
      label.textContent = 'Скрыть';
    });
  }
}

const STATUS_LABELS = {
  uploading: 'Загрузка',
  queued: 'В очереди',
  running: 'Распознаётся',
  done: 'Готово',
  failed: 'Ошибка',
  cancelled: 'Отменено',
};

function applyJobState(card, j) {
  card.dataset.status = j.status;
  const pill = card.querySelector('.job-status-pill');
  pill.dataset.status = j.status;
  pill.textContent = STATUS_LABELS[j.status] || j.status;

  card.querySelector('.stage-text').textContent = j.stage;
  const pct = Math.round((j.progress || 0) * 100);
  card.querySelector('.progress-bar').style.width = pct + '%';
  card.querySelector('.pct').textContent = pct + '%';

  if (j.duration_human) card.querySelector('.meta-duration').textContent = j.duration_human;
  if (j.eta_human) card.querySelector('.meta-eta').textContent = j.eta_human;

  // cancel button: only for queued/running
  const cancelBtn = card.querySelector('.btn-cancel');
  if (cancelBtn) {
    if (j.status === 'queued' || j.status === 'running') {
      cancelBtn.style.display = '';
      cancelBtn.disabled = false;
      cancelBtn.textContent = 'Отменить';
    } else {
      cancelBtn.style.display = 'none';
    }
  }

  // close (×) button: only for terminal states
  const closeBtn = card.querySelector('.btn-close');
  if (closeBtn) {
    closeBtn.style.display = (j.status === 'done' || j.status === 'failed' || j.status === 'cancelled') ? '' : 'none';
  }

  // error
  const errBox = card.querySelector('.job-error');
  if (j.status === 'failed' && j.error) {
    errBox.textContent = j.error;
    errBox.hidden = false;
  } else {
    errBox.hidden = true;
  }

  // download buttons
  const actions = card.querySelector('.job-actions');
  if (j.status === 'done') {
    actions.querySelector('[data-kind="clean"]').href = `/jobs/${j.id}/download/clean`;
    actions.querySelector('[data-kind="timestamps"]').href = `/jobs/${j.id}/download/timestamps`;
    actions.classList.add('visible');
  } else {
    actions.classList.remove('visible');
  }
}

function startUploadProgress(card) {
  // shows uploading state with progress bar, before server has the file
  card.dataset.status = 'uploading';
  const pill = card.querySelector('.job-status-pill');
  pill.dataset.status = 'uploading';
  pill.textContent = STATUS_LABELS.uploading;
  card.querySelector('.stage-text').textContent = 'Загрузка файла';
}

async function startUpload(file) {
  const tempId = 'tmp-' + Date.now();
  const card = ensureCard(tempId, file.name, fmtSize(file.size));
  startUploadProgress(card);

  const fd = new FormData();
  fd.append('file', file);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload');

  xhr.upload.onprogress = e => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 95);
      card.querySelector('.progress-bar').style.width = pct + '%';
      card.querySelector('.pct').textContent = pct + '%';
    }
  };

  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      const j = JSON.parse(xhr.responseText);
      // re-key the card from temp id to real job_id
      cards.delete(tempId);
      cards.set(j.id, card);
      card.dataset.jobId = j.id;
      attachCardHandlers(card, j.id);
      applyJobState(card, j);
      pollJob(j.id);
    } else {
      let msg = 'Ошибка загрузки';
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
      card.dataset.status = 'failed';
      card.querySelector('.job-status-pill').dataset.status = 'failed';
      card.querySelector('.job-status-pill').textContent = STATUS_LABELS.failed;
      card.querySelector('.stage-text').textContent = 'Ошибка';
      const err = card.querySelector('.job-error');
      err.textContent = msg;
      err.hidden = false;
    }
  };

  xhr.onerror = () => {
    card.dataset.status = 'failed';
    card.querySelector('.job-status-pill').dataset.status = 'failed';
    card.querySelector('.job-status-pill').textContent = STATUS_LABELS.failed;
    const err = card.querySelector('.job-error');
    err.textContent = 'Сетевая ошибка';
    err.hidden = false;
  };

  xhr.send(fd);
}

async function pollJob(jobId) {
  const tick = async () => {
    const card = cards.get(jobId);
    if (!card) return;
    try {
      const r = await fetch(`/jobs/${jobId}`);
      if (r.status === 404) return;
      if (!r.ok) throw new Error('status ' + r.status);
      const j = await r.json();
      applyJobState(card, j);
      if (j.status === 'queued' || j.status === 'running') {
        setTimeout(tick, 1000);
      }
    } catch (e) {
      setTimeout(tick, 2000);
    }
  };
  tick();
}

async function loadExistingJobs() {
  try {
    const r = await fetch('/api/jobs');
    if (!r.ok) return;
    const data = await r.json();
    for (const j of data.jobs) {
      const card = ensureCard(j.id, j.filename, j.size_human);
      applyJobState(card, j);
      if (j.status === 'queued' || j.status === 'running') {
        pollJob(j.id);
      }
    }
  } catch (e) {
    console.warn('Failed to load existing jobs', e);
  }
}

loadExistingJobs();
