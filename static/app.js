'use strict';

const state = {
  channel: null,      // {name, url, thumbnail, subscribers, counts}
  sources: [],        // [{source, count, seconds, filterable, fetched_at}]
  playlists: [],
  selected: new Set(),
  selectedPlaylists: new Set(),
  jobId: null,
  socket: null,
  cursor: 0,
  outDir: '',
  preview: null,
  maxStep: 1,         // 여기까지는 자유롭게 오갈 수 있다
};

const LAST_JOB_KEY = 'ytsub.lastJobId';
const TOTAL_STEPS = 4;
const $ = (id) => document.getElementById(id);
const SOURCE_LABEL = { videos: '일반 영상', shorts: '쇼츠', streams: '라이브 다시보기' };

function showError(message) {
  const box = $('error');
  box.textContent = message;
  box.classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function clearError() { $('error').classList.add('hidden'); }

function goto(step) {
  clearError();
  state.maxStep = Math.max(state.maxStep, step);
  for (let i = 1; i <= TOTAL_STEPS; i++) $('screen' + i).classList.add('hidden');
  $('screen' + step).classList.remove('hidden');
  renderSteps(step);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// 단계 바는 지금 어디까지 왔는지 보여주기만 한다.
// 다 끝난 작업에서 이전 단계로 되돌아가지는 게 어색해서 이동 기능은 뺐다.
// 되돌아가는 길은 각 화면의 "← 다른 채널 고르기" 버튼이 맡는다.
function renderSteps(current) {
  document.querySelectorAll('#steps .step').forEach((el) => {
    const n = Number(el.dataset.step);
    el.className = 'step px-3 py-1 rounded-full ' + (
      n === current ? 'bg-slate-200 text-slate-700 font-medium'
      : n < current ? 'text-slate-500'
      : 'text-slate-300');
  });
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail;
    try { detail = (await res.json()).detail; } catch (e) { detail = res.statusText; }
    if (detail && detail.message) throw Object.assign(new Error(detail.message), { detail });
    throw new Error(typeof detail === 'string' ? detail : '요청에 실패했습니다.');
  }
  return res.json();
}

function formatDuration(seconds) {
  if (seconds == null) return '—';
  const h = Math.floor(seconds / 3600), m = Math.round((seconds % 3600) / 60);
  if (h) return `${h}시간 ${m}분`;
  return `${Math.max(1, m)}분`;
}
function formatClock(seconds) {
  const m = Math.floor(seconds / 60), s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}
function formatCount(n) { return (n ?? 0).toLocaleString('ko-KR'); }

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

// --- 이전 작업 복구 ---

// 새로고침하거나 서버가 재시작돼도 작업을 잃지 않도록,
// 마지막 job_id를 브라우저에 남겨두고 페이지를 열 때 서버에 상태를 물어본다.
function rememberJob(jobId) {
  state.jobId = jobId;
  try { localStorage.setItem(LAST_JOB_KEY, jobId); } catch (e) { /* 무시 */ }
}
function forgetJob() {
  try { localStorage.removeItem(LAST_JOB_KEY); } catch (e) { /* 무시 */ }
}

async function checkPreviousJob() {
  let saved = null;
  try { saved = localStorage.getItem(LAST_JOB_KEY); } catch (e) { /* 무시 */ }
  try {
    const data = await api(saved ? `/api/jobs/${saved}` : '/api/jobs/latest');
    const job = saved ? data : data.job;
    if (job && job.job_id) renderResumeBar(job);
  } catch (e) {
    forgetJob();   // 서버에 없는 작업이면 기억할 이유가 없다
  }
}

function renderResumeBar(job) {
  const bar = $('resumeBar');
  const running = job.status === 'running';
  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  const label = running ? '진행 중인 작업이 있습니다' : '마치지 못한 작업이 있습니다';

  bar.className = 'mb-6 px-4 py-3 rounded-lg border flex items-center gap-4 flex-wrap ' +
    (running ? 'bg-blue-50 border-blue-200 text-blue-900'
             : 'bg-amber-50 border-amber-200 text-amber-900');
  bar.innerHTML = `
    <span class="text-sm"><b>${label}</b>
      — ${escapeHtml(job.channel_name)} · ${formatCount(job.done)}/${formatCount(job.total)} (${pct}%)</span>
    <span class="flex-1"></span>
    ${running ? '<button id="resumeView" class="px-3 py-1.5 rounded bg-blue-600 text-white text-xs font-medium">이어보기</button>' : ''}
    ${job.resumable ? '<button id="resumeRun" class="px-3 py-1.5 rounded bg-amber-600 text-white text-xs font-medium">이어서 실행</button>' : ''}
    ${!running ? '<button id="resumeResult" class="px-3 py-1.5 rounded border border-current text-xs font-medium">결과 보기</button>' : ''}
    <button id="resumeClose" class="text-xs underline opacity-70">닫기</button>`;
  bar.classList.remove('hidden');

  const view = $('resumeView');
  if (view) view.addEventListener('click', () => { attachJob(job.job_id); hideResumeBar(); });
  const result = $('resumeResult');
  if (result) result.addEventListener('click', () => {
    state.jobId = job.job_id;
    state.maxStep = TOTAL_STEPS;
    showResults();
    hideResumeBar();
  });
  const run = $('resumeRun');
  if (run) run.addEventListener('click', async () => {
    run.disabled = true; run.textContent = '시작 중…';
    try {
      await api(`/api/jobs/${job.job_id}/resume`, { method: 'POST' });
      attachJob(job.job_id);
      hideResumeBar();
    } catch (err) {
      showError(err.message);
      run.disabled = false; run.textContent = '이어서 실행';
    }
  });
  $('resumeClose').addEventListener('click', hideResumeBar);
}

function hideResumeBar() { $('resumeBar').classList.add('hidden'); }

// --- [1] 검색 + 자동완성 ---

const SUGGEST_DEBOUNCE_MS = 350;
let suggestTimer = null;
let suggestAbort = null;
let suggestItems = [];
let suggestIndex = -1;

$('searchBtn').addEventListener('click', () => { closeSuggest(); search(); });
$('q').addEventListener('input', onType);
$('q').addEventListener('keydown', onKeyDown);
$('q').addEventListener('blur', () => setTimeout(closeSuggest, 150));

function looksLikeUrl(text) {
  return /^https?:\/\//i.test(text) || /youtube\.com|youtu\.be/i.test(text);
}

function onType() {
  const query = $('q').value.trim();
  clearTimeout(suggestTimer);
  if (query.length < 2 || looksLikeUrl(query)) { closeSuggest(); return; }
  suggestTimer = setTimeout(() => fetchSuggestions(query), SUGGEST_DEBOUNCE_MS);
}

async function fetchSuggestions(query) {
  // 앞선 요청이 늦게 도착해 뒤엎지 않도록 취소한다
  if (suggestAbort) suggestAbort.abort();
  suggestAbort = new AbortController();
  renderSuggest(null, '찾는 중…');
  try {
    const res = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
      signal: suggestAbort.signal,
    });
    if (!res.ok) { closeSuggest(); return; }
    const data = await res.json();
    if ($('q').value.trim() !== query) return;   // 그새 더 입력했다면 버린다
    renderSuggest(data.candidates.slice(0, 8));
  } catch (err) {
    if (err.name !== 'AbortError') closeSuggest();
  }
}

function renderSuggest(candidates, placeholder) {
  const box = $('suggest');
  if (placeholder) {
    box.innerHTML = `<div class="px-4 py-3 text-sm text-slate-400">${placeholder}</div>`;
    box.classList.remove('hidden');
    return;
  }
  suggestItems = candidates || [];
  suggestIndex = -1;
  if (!suggestItems.length) { closeSuggest(); return; }

  box.innerHTML = suggestItems.map((c, i) => `
    <button data-i="${i}" class="sg w-full flex items-center gap-3 px-4 py-2.5 text-left hover:bg-slate-50">
      <img src="${c.thumbnail || ''}" alt="" class="w-8 h-8 rounded-full bg-slate-200 object-cover shrink-0">
      <span class="min-w-0 flex-1">
        <span class="block text-sm truncate">${escapeHtml(c.name)}</span>
        <span class="block text-xs text-slate-400 truncate">${
          c.subscribers != null ? '구독자 ' + formatCount(c.subscribers) + '명'
          : c.sample_title ? '예: ' + escapeHtml(c.sample_title) : ''}</span>
      </span>
    </button>`).join('');
  box.classList.remove('hidden');
  box.querySelectorAll('.sg').forEach((btn) => {
    btn.addEventListener('mousedown', (e) => {
      e.preventDefault();
      pickSuggestion(Number(btn.dataset.i));
    });
  });
}

function closeSuggest() {
  $('suggest').classList.add('hidden');
  suggestItems = [];
  suggestIndex = -1;
}

function highlight() {
  $('suggest').querySelectorAll('.sg').forEach((btn, i) => {
    btn.classList.toggle('bg-slate-100', i === suggestIndex);
  });
}

function onKeyDown(e) {
  const open = !$('suggest').classList.contains('hidden') && suggestItems.length;
  if (e.key === 'Escape') { closeSuggest(); return; }
  if (!open) {
    if (e.key === 'Enter') { closeSuggest(); search(); }
    return;
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    suggestIndex = (suggestIndex + 1) % suggestItems.length;
    highlight();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    suggestIndex = (suggestIndex - 1 + suggestItems.length) % suggestItems.length;
    highlight();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (suggestIndex >= 0) pickSuggestion(suggestIndex);
    else { closeSuggest(); search(); }
  }
}

function pickSuggestion(index) {
  const candidate = suggestItems[index];
  if (!candidate) return;
  $('q').value = candidate.name;
  closeSuggest();
  selectChannel(candidate);
}

async function search() {
  const query = $('q').value.trim();
  if (!query) return;
  const btn = $('searchBtn');
  btn.disabled = true; btn.textContent = '검색 중…';
  try {
    const data = await api('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });
    if (!data.candidates.length) { showError('검색된 채널이 없습니다.'); return; }
    // 후보가 여럿이면 드롭다운으로 고르게 한다 — 별도 선택 화면은 두지 않는다
    if (data.is_url || data.candidates.length === 1) selectChannel(data.candidates[0]);
    else renderSuggest(data.candidates.slice(0, 8));
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false; btn.textContent = '검색';
  }
}

// --- [2] 수집 범위 ---

$('refreshBtn').addEventListener('click', () => loadSources(true));

async function selectChannel(candidate) {
  state.channel = candidate;
  goto(2);
  await loadSources(false);
  loadPlaylists();
}

function renderChannelHead() {
  const counts = state.channel.counts;
  const total = counts
    ? `<div class="text-sm mt-1">전체 영상 <b>${formatCount(counts.total)}개</b>
         <span class="text-slate-400">(${counts.sources.map(
           (s) => `${SOURCE_LABEL[s.source]} ${formatCount(s.count)}`).join(' · ')})</span></div>`
    : '<div class="text-sm text-slate-400 mt-1">영상 개수를 세는 중…</div>';

  $('chanHead').innerHTML = `
    <div class="flex items-center gap-4">
      <img src="${state.channel.thumbnail || ''}" class="w-12 h-12 rounded-full bg-slate-200 object-cover">
      <div>
        <div class="font-semibold">${escapeHtml(state.channel.name)}</div>
        <div class="text-xs text-slate-500">구독자 ${formatCount(state.channel.subscribers)}명</div>
        ${total}
      </div>
    </div>`;
}

async function loadSources(force) {
  renderChannelHead();
  $('sources').innerHTML =
    '<div class="text-sm text-slate-400 py-4">영상 개수를 세는 중… (약 20초)</div>';
  $('startBtn').disabled = true;

  try {
    const data = await api('/api/channel/sources', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel_url: state.channel.url, force: !!force }),
    });
    applySources(data);
  } catch (err) {
    showError(err.message);
    $('sources').innerHTML = '';
  }
}

function applySources(data) {
  state.channel.name = data.channel_name;
  state.channel.url = data.channel_url;
  state.channel.thumbnail = data.thumbnail;
  state.channel.subscribers = data.subscribers;
  state.channel.counts = data;
  state.sources = data.sources;
  renderChannelHead();
  renderSources();
}

function renderSources() {
  const box = $('sources');
  box.innerHTML = '';
  state.sources.forEach((s) => {
    const row = document.createElement('label');
    row.className = 'flex items-center gap-3 py-2 cursor-pointer';
    row.innerHTML = `
      <input type="checkbox" class="src w-4 h-4 rounded border-slate-300" value="${s.source}"
             ${s.count ? '' : 'disabled'}>
      <span class="w-36 text-sm">${SOURCE_LABEL[s.source]}</span>
      <span class="w-24 text-sm tabular-nums text-right">${formatCount(s.count)}개</span>
      <span class="text-xs text-slate-500">예상 ${formatDuration(s.seconds)}</span>
      ${s.filterable ? '' : '<span class="text-xs text-amber-600 ml-2">필터 불가</span>'}`;
    box.appendChild(row);
  });
  const stamp = state.sources.find((s) => s.fetched_at);
  if (stamp) {
    const note = document.createElement('div');
    note.className = 'text-xs text-slate-400 mt-3';
    note.textContent = `목록 기준 ${stamp.fetched_at.replace('T', ' ')} — 새 영상이 올라왔다면 새로고침하세요.`;
    box.appendChild(note);
  }
  box.querySelectorAll('.src').forEach((cb) => cb.addEventListener('change', updateSelection));
  updateSelection();
}

async function loadPlaylists() {
  const box = $('playlistBox');
  box.innerHTML = '<div class="text-xs text-slate-400">재생목록 불러오는 중…</div>';
  try {
    const data = await api(`/api/channel/playlists?url=${encodeURIComponent(state.channel.url)}`);
    state.playlists = data.playlists;
    if (!data.playlists.length) { box.innerHTML = ''; return; }
    box.innerHTML = `
      <details>
        <summary class="text-sm cursor-pointer select-none">재생목록 선택 <span class="text-slate-400">(${data.playlists.length}개)</span></summary>
        <div class="mt-3 max-h-48 overflow-y-auto space-y-1 pl-1">
          ${data.playlists.map((p) => `
            <label class="flex items-center gap-2 text-sm py-1 cursor-pointer">
              <input type="checkbox" class="pl w-4 h-4 rounded border-slate-300" value="${p.playlist_id}">
              <span class="truncate">${escapeHtml(p.title)}</span>
            </label>`).join('')}
        </div>
        <p class="text-xs text-slate-400 mt-2">재생목록은 위 항목과 겹칠 수 있습니다. 겹치는 영상은 한 번만 처리됩니다.</p>
      </details>`;
    box.querySelectorAll('.pl').forEach((cb) => cb.addEventListener('change', updateSelection));
  } catch (e) {
    box.innerHTML = '';
  }
}

$('sort').addEventListener('change', updateSelection);
$('limit').addEventListener('input', updateSelection);

function updateSelection() {
  state.selected = new Set([...document.querySelectorAll('.src:checked')].map((c) => c.value));
  state.selectedPlaylists = new Set([...document.querySelectorAll('.pl:checked')].map((c) => c.value));

  let filterable = 0, unfilterable = 0;
  state.sources.forEach((s) => {
    if (!state.selected.has(s.source)) return;
    if (s.filterable) filterable += s.count; else unfilterable += s.count;
  });

  // 쇼츠는 유튜브가 업로드일·길이를 주지 않아 필터를 적용할 수 없다.
  // 쇼츠만 고른 경우엔 아예 손대지 못하게 막고, 다른 종류와 섞였으면
  // 필터를 살려두되 무엇에 적용되는지 분명히 알린다.
  const onlyUnfilterable = unfilterable > 0 && filterable === 0;
  const warn = $('filterWarn');
  if (unfilterable > 0) {
    warn.textContent = onlyUnfilterable
      ? '쇼츠는 유튜브가 업로드일·길이를 제공하지 않아 필터를 쓸 수 없습니다.'
      : '쇼츠는 유튜브가 업로드일·길이를 제공하지 않아 필터가 적용되지 않습니다. 쇼츠는 조건과 관계없이 모두 수집됩니다.';
    warn.classList.remove('hidden');
  } else {
    warn.classList.add('hidden');
  }
  document.querySelectorAll('.filter').forEach((el) => {
    el.disabled = onlyUnfilterable;
    el.classList.toggle('bg-slate-100', onlyUnfilterable);
    el.classList.toggle('pointer-events-none', onlyUnfilterable);  // 포커스도 막는다
    el.classList.toggle('opacity-50', onlyUnfilterable);
  });

  renderSortNote(unfilterable);

  const nothing = !state.selected.size && !state.selectedPlaylists.size;
  $('startBtn').disabled = nothing;
  if (nothing) {
    state.preview = null;
    $('totalBox').innerHTML =
      '<span class="text-slate-400 font-normal">영상 종류를 하나 이상 선택하세요.</span>';
    return;
  }
  schedulePreview();
}

// 개수는 화면이 직접 더하지 않는다. 작업을 만들 때 쓰는 함수에 그대로 물어본다.
// 따로 계산하면 재생목록·필터·중복 제거가 빠져 실제와 어긋난다.
const PREVIEW_DEBOUNCE_MS = 400;
let previewTimer = null;
let previewAbort = null;

function schedulePreview() {
  clearTimeout(previewTimer);
  markPreviewPending();
  previewTimer = setTimeout(fetchPreview, PREVIEW_DEBOUNCE_MS);
}

function markPreviewPending() {
  // 숫자를 지우지 않는다. 사라졌다 나타나면 더 불안해 보인다.
  const note = '<span class="font-normal text-slate-400 text-xs">계산 중…</span>';
  $('totalBox').innerHTML = state.preview
    ? renderPreview(state.preview) + ' ' + note
    : '<span class="text-slate-400 font-normal">개수를 세는 중…</span>';
}

async function fetchPreview() {
  if (previewAbort) previewAbort.abort();
  previewAbort = new AbortController();
  const body = collectionBody();
  try {
    const res = await fetch('/api/channel/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: previewAbort.signal,
    });
    if (!res.ok) throw new Error('preview failed');
    const data = await res.json();
    if (!data.ok) {
      $('totalBox').innerHTML =
        `<span class="text-amber-700 font-normal">${escapeHtml(data.message)}</span>`;
      return;
    }
    state.preview = data;
    $('totalBox').innerHTML = renderPreview(data);
    $('startBtn').disabled = data.remaining === 0 && data.count === 0;
  } catch (err) {
    if (err.name === 'AbortError') return;    // 더 최신 요청이 뒤따른다
    $('totalBox').innerHTML =
      '<span class="text-amber-700 font-normal">개수를 계산하지 못했습니다.</span>';
  }
}

function renderPreview(d) {
  const parts = [`선택 <b>${formatCount(d.count)}개</b>`];
  if (d.limited) parts.push(`<span class="font-normal text-slate-400">(상위 ${formatCount(d.count)}개로 제한)</span>`);
  if (d.already) {
    parts.push(`<span class="font-normal text-slate-500">— 이미 받은 ${formatCount(d.already)}개를 빼면 <b>${formatCount(d.remaining)}개</b></span>`);
  }
  parts.push(`<span class="font-normal">· 예상 ${formatDuration(d.seconds)}</span>`);
  return parts.join(' ');
}

function renderSortNote(shortsCount) {
  const sort = $('sort').value;
  const note = $('sortNote');
  if (sort.startsWith('views')) {
    note.textContent = '조회수가 없는 영상(일반 영상의 약 20%)은 목록 맨 뒤로 밀립니다.';
  } else if (sort.startsWith('date') && shortsCount) {
    note.textContent = `쇼츠 ${formatCount(shortsCount)}개는 업로드일 정보가 없어 맨 뒤로 밀립니다.`;
  } else {
    note.textContent = '';
  }
}

// --- 날짜 입력 ---

// YYYYMMDD, 그리고 - . / 로 구분한 형태를 받아준다 (2026-1-1 처럼 한 자리도 허용)
function parseDateInput(text) {
  const raw = (text || '').trim();
  if (!raw) return null;

  let y, m, d;
  const parts = raw.split(/[^0-9]+/).filter(Boolean);
  if (parts.length === 3) {
    [y, m, d] = parts;
  } else {
    const digits = raw.replace(/[^0-9]/g, '');
    if (digits.length !== 8) return null;
    [y, m, d] = [digits.slice(0, 4), digits.slice(4, 6), digits.slice(6, 8)];
  }
  if (y.length !== 4) return null;

  const pad = (v) => String(Number(v)).padStart(2, '0');
  [m, d] = [pad(m), pad(d)];
  const date = new Date(`${y}-${m}-${d}T00:00:00`);
  if (Number.isNaN(date.getTime())
      || date.getMonth() + 1 !== Number(m) || date.getDate() !== Number(d)) {
    return null;
  }
  return `${y}-${m}-${d}`;
}

function dateValue(id) {
  const raw = $(id).value.trim();
  return raw ? parseDateInput(raw) : '';
}

function markDateValidity(el) {
  const ok = !el.value.trim() || parseDateInput(el.value) !== null;
  el.classList.toggle('border-red-400', !ok);
  el.classList.toggle('border-slate-300', ok);
  return ok;
}

document.querySelectorAll('.datefield').forEach((el) => {
  el.addEventListener('input', () => markDateValidity(el));
  el.addEventListener('blur', () => {
    const normalized = parseDateInput(el.value);
    if (normalized) el.value = normalized;   // 20260101 → 2026-01-01
    markDateValidity(el);
    updateSelection();
  });
});

// 달력 버튼 — 숨겨둔 date 입력의 네이티브 피커를 띄우고 값을 되돌려 받는다
document.querySelectorAll('.calbtn').forEach((btn) => {
  const target = $(btn.dataset.for);
  const picker = document.querySelector(`[data-picker="${btn.dataset.for}"]`);
  btn.addEventListener('click', () => {
    picker.value = parseDateInput(target.value) || '';
    if (picker.showPicker) picker.showPicker();
    else picker.click();
  });
  picker.addEventListener('change', () => {
    target.value = picker.value;
    markDateValidity(target);
    updateSelection();
  });
});

// --- [3] 실행 ---

$('startBtn').addEventListener('click', startJob);

// 미리보기와 실제 작업이 완전히 같은 조건을 쓰도록 한 곳에서 만든다
function collectionBody() {
  return {
    channel_url: state.channel.url,
    channel_name: state.channel.name,
    sources: [...state.selected],
    playlist_ids: [...state.selectedPlaylists],
    filters: {
      date_from: dateValue('dateFrom') || '',
      date_to: dateValue('dateTo') || '',
      min_minutes: Number($('minMin').value) || 0,
      max_minutes: Number($('maxMin').value) || 0,
    },
    options: { langs: $('langs').value, timestamps: $('ts').checked },
    sort: $('sort').value,
    limit: Number($('limit').value) || 0,
  };
}

async function startJob() {
  const bad = [...document.querySelectorAll('.datefield')].filter((el) => !markDateValidity(el));
  if (bad.length) {
    showError('날짜 형식이 올바르지 않습니다. YYYY-MM-DD 형태로 입력하세요.');
    return;
  }
  const btn = $('startBtn');
  btn.disabled = true; btn.textContent = '준비 중…';
  const body = collectionBody();
  try {
    const data = await api('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    attachJob(data.job_id);
  } catch (err) {
    if (err.detail && err.detail.job_id) {
      attachJob(err.detail.job_id);
      showError('이미 실행 중인 작업이 있어 그 작업을 보여드립니다.');
    } else {
      showError(err.message);
    }
  } finally {
    btn.disabled = false; btn.textContent = '수집 시작';
  }
}

function attachJob(jobId) {
  rememberJob(jobId);
  state.cursor = 0;
  state.maxStep = Math.max(state.maxStep, 3);
  $('console').innerHTML = '';
  goto(3);
  openSocket();
}

const LEVEL_COLOR = { ok: 'text-emerald-400', fail: 'text-red-400', skip: 'text-zinc-500', info: 'text-zinc-300' };

function openSocket() {
  if (state.socket) { try { state.socket.close(); } catch (e) {} }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/api/jobs/${state.jobId}/ws?since=${state.cursor}`;
  const ws = new WebSocket(url);
  state.socket = ws;

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'logs') {
      state.cursor = msg.cursor;
      appendLogs(msg.entries);
    } else if (msg.type === 'state') {
      renderProgress(msg.state);
    } else if (msg.type === 'finished') {
      renderProgress(msg.state);
      showResults();
    } else if (msg.type === 'error') {
      showError(msg.message);
    }
  };
  // 연결이 끊기면 마지막 커서부터 다시 붙어 로그가 끊기지 않게 한다
  ws.onclose = () => {
    if (state.jobId && !$('screen3').classList.contains('hidden')) {
      setTimeout(openSocket, 1500);
    }
  };
}

function appendLogs(entries) {
  const box = $('console');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  entries.forEach((e) => {
    const line = document.createElement('div');
    line.className = LEVEL_COLOR[e.level] || 'text-zinc-300';
    line.textContent = e.message;
    box.appendChild(line);
  });
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderProgress(s) {
  const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
  $('progNum').textContent = `${formatCount(s.done)} / ${formatCount(s.total)}`;
  $('progPct').textContent = `(${pct}%)`;
  $('bar').style.width = pct + '%';
  $('elapsed').textContent = formatClock(s.elapsed);
  $('eta').textContent = s.eta == null ? '—' : formatDuration(s.eta);

  // 진행이 오래 멈춰 있으면 알려만 준다 — 상태를 함부로 바꾸지 않는다
  const stall = $('stallNote');
  if (s.status === 'running' && s.stalled_minutes >= 3) {
    stall.textContent = `${s.stalled_minutes}분째 진행이 없습니다. 자막이 큰 영상이거나 네트워크가 느릴 수 있습니다.`;
    stall.classList.remove('hidden');
  } else {
    stall.classList.add('hidden');
  }
}

$('stopBtn').addEventListener('click', async () => {
  const btn = $('stopBtn');
  btn.disabled = true; btn.textContent = '중지 중…';
  try { await api(`/api/jobs/${state.jobId}/stop`, { method: 'POST' }); }
  catch (err) { showError(err.message); }
  finally { btn.disabled = false; btn.textContent = '중지'; }
});

// --- [4] 완료 ---

$('scopeAll').addEventListener('change', showResults);

function scope() { return $('scopeAll').checked ? 'channel' : 'job'; }

async function showResults() {
  goto(4);
  try {
    const [stateData, videoData] = await Promise.all([
      api(`/api/jobs/${state.jobId}?scope=${scope()}`),
      api(`/api/jobs/${state.jobId}/videos?scope=${scope()}`),
    ]);
    state.outDir = stateData.out_dir || '';
    $('doneFolder').textContent = state.outDir || '(경로를 알 수 없습니다)';
    $('openFolderBtn').disabled = !state.outDir;
    renderSummary(stateData.summary);
    renderVideos(videoData.videos);
  } catch (err) {
    showError(err.message);
  }
}

function renderSummary(s) {
  const mb = (s.total_chars * 3 / 1024 / 1024).toFixed(1);  // UTF-8 한글 ≈ 3바이트
  $('summary').innerHTML =
    `성공 ${formatCount(s.success)}개 · 실패 ${formatCount(s.failed)}개 · 약 ${mb}MB`;

  $('reasons').innerHTML = s.reasons.map((r) => `
    <div class="flex items-start gap-3 text-sm">
      <span class="px-2 py-0.5 rounded text-xs shrink-0 ${r.retryable ? 'bg-amber-100 text-amber-800' : 'bg-slate-100 text-slate-600'}">
        ${formatCount(r.count)}개</span>
      <span class="text-slate-600">${escapeHtml(r.message)}
        <span class="text-xs text-slate-400">${r.retryable ? '(재시도 가능)' : '(재시도 불가)'}</span></span>
    </div>`).join('');

  $('retryBox').innerHTML = s.retryable
    ? `<button id="retryBtn" class="px-4 py-2 rounded-lg border border-amber-300 text-amber-700 text-sm font-medium hover:bg-amber-50">
         ${formatCount(s.retryable)}건 재시도</button>`
    : '';
  const retry = $('retryBtn');
  if (retry) retry.addEventListener('click', doRetry);
}

async function doRetry() {
  const btn = $('retryBtn');
  btn.disabled = true; btn.textContent = '재시도 시작 중…';
  try {
    await api(`/api/jobs/${state.jobId}/retry`, { method: 'POST' });
    state.cursor = 0;
    goto(3);
    openSocket();
  } catch (err) {
    showError(err.message);
    btn.disabled = false;
  }
}

const STATUS_STYLE = {
  OK: 'bg-emerald-100 text-emerald-800',
  PRIVATE_VIDEO: 'bg-slate-100 text-slate-600',
  NO_SUBTITLES: 'bg-slate-100 text-slate-600',
};

function renderVideos(videos) {
  $('videoRows').innerHTML = videos.map((v) => `
    <tr class="border-t border-slate-100 hover:bg-slate-50 ${v.has_text ? 'cursor-pointer' : ''}"
        data-vid="${v.video_id}" data-has="${v.has_text ? '1' : ''}">
      <td class="px-6 py-2.5"><div class="truncate max-w-md">${escapeHtml(v.title)}</div>
        ${v.message ? `<div class="text-xs text-slate-400 truncate max-w-md">${escapeHtml(v.message)}</div>` : ''}</td>
      <td class="px-3 py-2.5 text-xs text-slate-500 tabular-nums">${v.upload_date}</td>
      <td class="px-3 py-2.5"><span class="px-2 py-0.5 rounded text-xs ${STATUS_STYLE[v.status] || 'bg-amber-100 text-amber-800'}">${v.status}</span></td>
      <td class="px-6 py-2.5 text-right tabular-nums text-slate-600">${v.char_count ? formatCount(v.char_count) : '—'}</td>
    </tr>`).join('');

  $('videoRows').querySelectorAll('tr').forEach((row) => {
    if (!row.dataset.has) return;
    row.addEventListener('click', () => openPreview(row.dataset.vid));
  });
}

$('openFolderBtn').addEventListener('click', async () => {
  if (!state.outDir) return;
  const btn = $('openFolderBtn');
  btn.disabled = true;
  try {
    await api('/api/open-folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: state.outDir }),
    });
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
  }
});

// --- 다운로드 / 미리보기 ---

// window.location으로 넘기면 오류가 났을 때 JSON 원문이 브라우저에 그대로 찍힌다.
// 직접 받아서 성공했을 때만 저장하고, 실패는 오류 배너로 보여준다.
document.querySelectorAll('.dl').forEach((btn) => {
  btn.addEventListener('click', async () => {
    const original = btn.textContent;
    btn.disabled = true; btn.textContent = '준비 중…';
    try {
      const url = `/api/jobs/${state.jobId}/download?type=${btn.dataset.type}&scope=${scope()}`;
      const res = await fetch(url);
      if (!res.ok) {
        let message = '다운로드에 실패했습니다.';
        try { message = (await res.json()).detail || message; } catch (e) { /* 무시 */ }
        throw new Error(typeof message === 'string' ? message : '다운로드에 실패했습니다.');
      }
      saveBlob(await res.blob(), filenameFrom(res, btn.dataset.type));
    } catch (err) {
      showError(err.message);
    } finally {
      btn.disabled = false; btn.textContent = original;
    }
  });
});

function filenameFrom(res, type) {
  const header = res.headers.get('Content-Disposition') || '';
  const star = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (star) return decodeURIComponent(star[1]);
  const plain = header.match(/filename="([^"]+)"/i);
  if (plain && plain[1]) return plain[1];
  return `transcripts.${type === 'merged' ? 'txt' : type}`;
}

function saveBlob(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function openPreview(videoId) {
  try {
    const data = await api(`/api/jobs/${state.jobId}/videos/${videoId}`);
    $('modalTitle').textContent = data.title;
    $('modalText').textContent = data.text;
    $('modal').classList.remove('hidden');
    $('modalDl').onclick = () => {
      const name = data.title.replace(/[\\/:*?"<>|]/g, '_').slice(0, 100) + '.txt';
      saveBlob(new Blob([data.text], { type: 'text/plain;charset=utf-8' }), name);
    };
  } catch (err) {
    showError(err.message);
  }
}

$('modalClose').addEventListener('click', () => $('modal').classList.add('hidden'));
$('modal').addEventListener('click', (e) => {
  if (e.target === $('modal')) $('modal').classList.add('hidden');
});

// --- 이동 ---

document.querySelectorAll('.back').forEach((btn) => {
  btn.addEventListener('click', () => {
    const target = Number(btn.dataset.to);
    if (target === 1) resetForNewChannel();
    goto(target);
  });
});

function resetForNewChannel() {
  if (state.socket) { try { state.socket.close(); } catch (e) {} }
  state.socket = null;
  state.jobId = null;
  state.cursor = 0;
  state.channel = null;
  state.sources = [];
  state.playlists = [];
  state.selected = new Set();
  state.selectedPlaylists = new Set();
  state.maxStep = 1;
  $('console').innerHTML = '';
  $('scopeAll').checked = false;
  $('limit').value = '';
  $('sort').value = 'channel';
  document.querySelectorAll('.datefield').forEach((el) => { el.value = ''; });
  $('minMin').value = '';
  $('maxMin').value = '';
}

renderSteps(1);
checkPreviousJob();

// --- 수집 / 검색 모드 전환 ---

function setMode(mode) {
  clearError();
  $('collectMode').classList.toggle('hidden', mode !== 'collect');
  $('findMode').classList.toggle('hidden', mode !== 'find');
  document.querySelectorAll('.mode').forEach((btn) => {
    const on = btn.dataset.mode === mode;
    btn.className = 'mode px-4 py-2 text-sm font-medium border-b-2 ' + (on
      ? 'border-slate-900 text-slate-900'
      : 'border-transparent text-slate-400 hover:text-slate-700');
  });
  if (mode === 'find') loadFindStatus();
}

document.querySelectorAll('.mode').forEach((btn) => {
  btn.addEventListener('click', () => setMode(btn.dataset.mode));
});

// --- 업로드 날짜 프리셋 ---

// 오늘로부터 N일 전을 시작일로 채운다. 직접 입력·달력은 그대로 살아 있고,
// 값을 고치면 아래 하이라이트가 풀린다.
document.querySelectorAll('.preset').forEach((btn) => {
  btn.addEventListener('click', () => {
    const days = Number(btn.dataset.days);
    if (days > 0) {
      const from = new Date();
      from.setDate(from.getDate() - days);
      $('dateFrom').value = from.toISOString().slice(0, 10);
    } else {
      $('dateFrom').value = '';
    }
    $('dateTo').value = '';
    document.querySelectorAll('.datefield').forEach(markDateValidity);
    highlightPreset(btn.dataset.days);
    updateSelection();
  });
});

function highlightPreset(days) {
  document.querySelectorAll('.preset').forEach((b) => {
    const on = days != null && b.dataset.days === String(days);
    b.classList.toggle('bg-slate-900', on);
    b.classList.toggle('text-white', on);
    b.classList.toggle('border-slate-900', on);
  });
}

document.querySelectorAll('.datefield').forEach((el) => {
  el.addEventListener('input', () => highlightPreset(null));
});

// --- 저장 위치 ---

async function loadRoot() {
  try {
    const data = await api('/api/settings');
    $('rootPath').textContent = data.output_root;
  } catch (e) { /* 기본값 유지 */ }
}

$('pickFolderBtn').addEventListener('click', async () => {
  const btn = $('pickFolderBtn');
  btn.disabled = true; btn.textContent = '창에서 고르는 중…';
  try {
    const picked = await api('/api/pick-folder', { method: 'POST' });
    if (!picked.cancelled) {
      const saved = await api('/api/settings/root', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: picked.path }),
      });
      $('rootPath').textContent = saved.output_root;
    }
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false; btn.textContent = '폴더 선택';
  }
});

// --- 자막 검색 ---

let findRows = [];

async function loadFindStatus() {
  try {
    const root = $('froot').value;
    const s = await api(`/api/search/status?root=${encodeURIComponent(root)}`);
    $('findStatus').textContent = `색인 ${formatCount(s.total)}개`;

    // 폴더가 하나뿐이면 고를 것이 없으므로 드롭다운을 숨긴다
    const rootSelect = $('froot');
    rootSelect.classList.toggle('hidden', (s.roots || []).length < 2);
    if ((s.roots || []).length >= 2) {
      const keep = rootSelect.value;
      rootSelect.innerHTML = '<option value="">전체 폴더</option>' +
        s.roots.map((r) => `<option value="${escapeHtml(r)}">${escapeHtml(shortPath(r))}</option>`).join('');
      rootSelect.value = keep;
    }

    // 폴더를 좁히면 채널 목록도 그 폴더 안의 채널로 바뀐다
    const select = $('fchannel');
    const current = select.value;
    select.innerHTML = '<option value="">전체 채널</option>' +
      s.channels.map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
    select.value = s.channels.includes(current) ? current : '';
  } catch (e) {
    $('findStatus').textContent = '색인 없음';
  }
}

// 경로가 길어 드롭다운을 넘치므로 뒤쪽 두 칸만 보여준다
function shortPath(path) {
  const parts = path.split('/').filter(Boolean);
  return parts.length <= 2 ? path : '…/' + parts.slice(-2).join('/');
}

$('froot').addEventListener('change', () => {
  loadFindStatus();
  if ($('fq').value.trim()) runFind();
});

$('addRootBtn').addEventListener('click', async () => {
  const btn = $('addRootBtn');
  btn.disabled = true; btn.textContent = '창에서 고르는 중…';
  try {
    const picked = await api('/api/pick-folder', { method: 'POST' });
    if (!picked.cancelled) {
      await api('/api/settings/add-root', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: picked.path }),
      });
      await loadFindStatus();
    }
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false; btn.textContent = '폴더 추가';
  }
});

$('reindexBtn').addEventListener('click', async () => {
  const el = $('findStatus');
  el.textContent = '색인 갱신 중…';
  try {
    const r = await api('/api/search/index', { method: 'POST' });
    el.textContent = `색인 ${formatCount(r.total)}개 (추가 ${r.added} · 변경 ${r.updated})`;
    loadFindStatus();
  } catch (err) { showError(err.message); }
});

$('fbtn').addEventListener('click', runFind);
$('fq').addEventListener('keydown', (e) => { if (e.key === 'Enter') runFind(); });
$('fchannel').addEventListener('change', () => { if ($('fq').value.trim()) runFind(); });

async function runFind() {
  const q = $('fq').value.trim();
  if (!q) return;
  const btn = $('fbtn');
  btn.disabled = true; btn.textContent = '검색 중…';
  try {
    const url = `/api/search?q=${encodeURIComponent(q)}`
      + `&channel=${encodeURIComponent($('fchannel').value)}`
      + `&root=${encodeURIComponent($('froot').value)}&limit=50`;
    const data = await api(url);
    findRows = data.results;
    renderFind(data);
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false; btn.textContent = '검색';
  }
}

function renderFind(data) {
  const note = $('findNote');
  if (data.note) { note.textContent = data.note; note.classList.remove('hidden'); }
  else note.classList.add('hidden');

  const box = $('findResults');
  if (!data.results.length) {
    box.innerHTML = '<div class="text-sm text-slate-400 py-8 text-center">결과가 없습니다.</div>';
    $('findActions').classList.add('hidden');
    return;
  }
  $('findActions').classList.remove('hidden');
  $('fcheckAll').checked = false;

  box.innerHTML = data.results.map((r, i) => `
    <div class="bg-white rounded-lg border border-slate-200 p-4 flex gap-3">
      <input type="checkbox" class="fpick mt-1 w-4 h-4 rounded border-slate-300 shrink-0" data-i="${i}">
      <div class="min-w-0 flex-1">
        <div class="flex items-baseline gap-2 flex-wrap">
          <button class="fopen font-medium text-sm text-left hover:underline" data-i="${i}">${escapeHtml(r.title)}</button>
          <span class="text-xs text-slate-400">${escapeHtml(r.channel)} · ${r.upload_date || '—'} · ${formatCount(r.char_count)}자</span>
        </div>
        <div class="text-xs text-slate-600 mt-1 leading-relaxed">${highlightSnippet(r.snippet)}</div>
      </div>
    </div>`).join('');

  box.querySelectorAll('.fpick').forEach((cb) => cb.addEventListener('change', updateFindCount));
  box.querySelectorAll('.fopen').forEach((btn) => {
    btn.addEventListener('click', () => openFindPreview(findRows[Number(btn.dataset.i)]));
  });
  updateFindCount();
}

// 서버가 «»로 감싼 일치 부분을 강조로 바꾼다 (HTML은 먼저 이스케이프)
function highlightSnippet(text) {
  return escapeHtml(text || '')
    .replace(/«/g, '<mark class="bg-amber-200 rounded px-0.5">')
    .replace(/»/g, '</mark>');
}

$('fcheckAll').addEventListener('change', () => {
  const on = $('fcheckAll').checked;
  document.querySelectorAll('.fpick').forEach((cb) => { cb.checked = on; });
  updateFindCount();
});

function selectedPaths() {
  return [...document.querySelectorAll('.fpick:checked')]
    .map((cb) => findRows[Number(cb.dataset.i)].path);
}

function updateFindCount() {
  const n = selectedPaths().length;
  $('fcount').textContent = n ? `${formatCount(n)}개 선택됨` : '내보낼 자막을 선택하세요';
  document.querySelectorAll('.fexp').forEach((b) => { b.disabled = !n; b.classList.toggle('opacity-40', !n); });
}

document.querySelectorAll('.fexp').forEach((btn) => {
  btn.addEventListener('click', async () => {
    const paths = selectedPaths();
    if (!paths.length) return;
    const original = btn.textContent;
    btn.disabled = true; btn.textContent = '준비 중…';
    try {
      const res = await fetch('/api/search/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths, type: btn.dataset.type }),
      });
      if (!res.ok) {
        let message = '내보내기에 실패했습니다.';
        try { message = (await res.json()).detail || message; } catch (e) { /* 무시 */ }
        throw new Error(typeof message === 'string' ? message : '내보내기에 실패했습니다.');
      }
      saveBlob(await res.blob(), filenameFrom(res, btn.dataset.type === 'zip' ? 'zip' : 'merged'));
    } catch (err) {
      showError(err.message);
    } finally {
      btn.disabled = false; btn.textContent = original;
    }
  });
});

async function openFindPreview(row) {
  try {
    const res = await fetch('/api/search/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths: [row.path], type: 'merged' }),
    });
    if (!res.ok) throw new Error('자막을 불러오지 못했습니다.');
    const text = await res.text();
    $('modalTitle').textContent = row.title;
    $('modalText').textContent = text;
    $('modal').classList.remove('hidden');
    $('modalDl').onclick = () => {
      const name = row.title.replace(/[\\/:*?"<>|]/g, '_').slice(0, 100) + '.txt';
      saveBlob(new Blob([text], { type: 'text/plain;charset=utf-8' }), name);
    };
  } catch (err) {
    showError(err.message);
  }
}

setMode('collect');
loadRoot();
