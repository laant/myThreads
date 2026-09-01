/* myThreads — 저장한 Threads 글 자동 분류 뷰어 */
const S = {
  cats: [], tags: [], posts: [], total: 0, unclassified: 0,
  cat: 'all', view: 'card', q: '', tag: null, sort: 'newest',
  tagsOpen: false,          // 태그를 다 펼쳐 놓았는가
};
const TAG_HEAD = 40;        // 평소에 보여줄 태그 수

const $ = (s) => document.querySelector(s);
const esc = (s) => (s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmtDate = (ts) => {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, '0')}.${String(d.getDate()).padStart(2, '0')}`;
};
const imgOf = (m) => (m.local ? '/media/' + m.local.replace(/^media\//, '') : m.url);
const api = async (url, opt) => (await fetch(url, opt)).json();

/* ── 초기화 ─────────────────────────────── */
async function boot() {
  const d = await api('/api/bootstrap');
  S.cats = d.categories; S.total = d.total; S.unclassified = d.unclassified;
  $('#stat').textContent = `저장 ${d.total}건 · 미분류 ${d.unclassified}건 · 자동수집 ${(d.sync_times || []).join(', ')}`;
  const warn = [];
  if (!d.logged_in) warn.push('⚠ 로그인 세션 없음 — 터미널에서 `make login`');
  if (!d.llm_ready) warn.push(`⚠ ${d.llm_key_env} 미설정 (${d.provider}) — .env 확인`);
  if (warn.length) $('#joblog').textContent = warn.join('\n');
  renderCats(); showJob(d.job);
  await Promise.all([loadTags(), load()]);
  if (!polling && d.job && ['queued', 'running'].includes(d.job.status)) poll();
}

function renderCats() {
  const rows = [
    { slug: 'all', name: '전체', color: 'var(--dim)', count: S.total, description: '' },
    ...S.cats,
  ];
  if (S.unclassified) rows.push({ slug: 'unclassified', name: '미분류', color: '#9ca3af', count: S.unclassified, description: '아직 분류되지 않은 글' });
  $('#cats').innerHTML = rows.map((c) => `
    <button class="cat ${S.cat === c.slug ? 'active' : ''}" data-slug="${c.slug}">
      <span class="dot" style="background:${c.color || 'var(--dim)'}"></span>
      <span>${esc(c.name)}</span><span class="n">${c.count ?? ''}</span>
    </button>`).join('');
  $('#cats').querySelectorAll('.cat').forEach((b) => b.onclick = () => {
    S.cat = b.dataset.slug;
    const c = S.cats.find((x) => x.slug === S.cat);
    if (c && c.view) S.view = c.view;
    // 태그는 카테고리 안에서 다시 세므로, 옮겨온 태그 필터는 풀어준다
    S.tag = null;
    S.tagsOpen = false;
    renderCats(); loadTags(); load();
  });
}

/* 태그는 '지금 보고 있는 카테고리' 기준으로 다시 받아온다 —
   그래야 눌렀을 때 숫자대로 나오고, 그 안에 없는 태그가 보이지 않는다 */
async function loadTags() {
  const p = S.cat === 'all' ? '' : '?category=' + encodeURIComponent(S.cat);
  try {
    S.tags = (await api('/api/tags' + p)).tags || [];
  } catch (e) {
    S.tags = [];
  }
  renderTags();
}

function renderTags() {
  const box = $('#tags');
  if (!S.tags.length) {
    box.innerHTML = '<span class="tagnote">분류 후 생성됩니다</span>';
    return;
  }
  const hidden = S.tags.length - TAG_HEAD;
  const shown = S.tagsOpen ? S.tags : S.tags.slice(0, TAG_HEAD);
  box.innerHTML = shown.map((t) =>
    `<button class="tag ${S.tag === t.name ? 'active' : ''}" data-t="${esc(t.name)}">${esc(t.name)} ${t.count}</button>`).join('')
    + (hidden > 0
      ? `<button class="tag more" id="tag-more">${S.tagsOpen ? '접기' : `더 보기 ${hidden}개`}</button>`
      : '');
  box.querySelectorAll('.tag').forEach((b) => b.onclick = () => {
    if (b.id === 'tag-more') { S.tagsOpen = !S.tagsOpen; return renderTags(); }
    S.tag = S.tag === b.dataset.t ? null : b.dataset.t;
    renderTags(); load();
  });
}

/* ── 목록 로드 & 렌더 ────────────────────── */
async function load() {
  const p = new URLSearchParams();
  if (S.cat !== 'all') p.set('category', S.cat);
  if (S.q) p.set('q', S.q);
  if (S.tag) p.set('tag', S.tag);
  p.set('sort', S.sort);
  const d = await api('/api/posts?' + p.toString());
  S.posts = d.posts;
  renderHead(); renderView();
}

/* 이 카테고리가 주분류인 글 / 보조로만 걸린 '관련 글' 로 나눈다 */
const splitPosts = () => [S.posts.filter((p) => !p.related), S.posts.filter((p) => p.related)];

function renderHead() {
  const c = S.cats.find((x) => x.slug === S.cat);
  const name = S.cat === 'all' ? '전체' : (S.cat === 'unclassified' ? '미분류' : (c ? c.name : S.cat));
  const [own, rel] = splitPosts();
  $('#cathead').innerHTML =
    `<h1>${esc(name)} <span style="color:var(--dim);font-weight:400">${own.length}</span>
       ${rel.length ? `<span class="relcount">+ 관련 ${rel.length}</span>` : ''}</h1>
     ${c && c.description ? `<p>${esc(c.description)}</p>` : ''}`;
  $('#viewtoggle').querySelectorAll('button').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === S.view));
}

function renderView() {
  if (!S.posts.length) {
    $('#content').innerHTML = `<div class="empty">표시할 글이 없습니다.<br>
      왼쪽 아래 <b>지금 동기화</b>를 눌러 저장된 글을 가져오세요.</div>`;
    return;
  }
  const draw = (list) => (S.view === 'table' ? renderTable(list)
    : S.view === 'board' ? renderBoard(list) : renderCards(list));
  const [own, rel] = splitPosts();
  let html = own.length ? draw(own) : '';
  if (rel.length) {
    html += `<div class="relhead">
        <b>관련 글 ${rel.length}건</b>
        <span>주분류는 다르지만 이 주제와도 맞닿아 있는 글입니다</span>
      </div>` + draw(rel);
  }
  $('#content').innerHTML = html;
  bindOpen(S.view === 'table' ? 'tbody tr' : (S.view === 'board' ? '.bcard' : '.card'));
  if (S.view === 'board') watchBoard();
}

/* ── 보드(벽돌쌓기) 배치 ────────────────────
   그리드 행 높이를 1px로 두고, 카드가 제 높이만큼 행을 차지하게 한다.
   그러면 브라우저가 왼쪽 위부터 가로로 채우면서 빈 곳을 메운다. */
const BOARD_GAP = 12;

function layoutBoard() {
  document.querySelectorAll('.board .bcard').forEach((el) => {
    const h = el.getBoundingClientRect().height;
    if (h) el.style.gridRowEnd = 'span ' + Math.ceil(h + BOARD_GAP);
  });
}

let boardPending = false;
function relayoutBoard() {          // 이미지가 우르르 들어와도 한 번만 다시 그린다
  if (boardPending) return;
  boardPending = true;
  requestAnimationFrame(() => { boardPending = false; layoutBoard(); });
}

function watchBoard() {
  layoutBoard();
  // 이미지는 나중에(스크롤해서) 로드되며 높이가 바뀐다 → 그때마다 다시 배치
  document.querySelectorAll('.board img').forEach((img) => {
    if (img.complete) return;
    img.addEventListener('load', relayoutBoard, { once: true });
    img.addEventListener('error', relayoutBoard, { once: true });
  });
}

window.addEventListener('resize', () => { if (S.view === 'board') relayoutBoard(); });

function renderCards(posts) {
  return '<div class="cards">' + posts.map((p) => {
    const imgs = p.media.filter((m) => imgOf(m)).slice(0, 3);
    const thumbs = imgs.length
      ? `<div class="thumbs">${imgs.map((m) => `<img loading="lazy" src="${esc(imgOf(m))}" alt="">`).join('')}</div>` : '';
    return `<article class="card" data-id="${p.id}">
      ${thumbs}
      <div class="body">
        <div class="meta">
          ${p.cat_name ? `<span class="badge" style="background:${p.cat_color || 'var(--dim)'}">${esc(p.cat_name)}</span>` : ''}
          <span>@${esc(p.author || '')}</span><span>·</span><span>${fmtDate(p.posted_at)}</span>
        </div>
        ${p.summary ? `<div class="summary">${esc(p.summary)}</div>` : ''}
        <div class="excerpt">${esc(p.full_text || p.body || '')}</div>
        ${p.thread_text ? `<div class="cont">↳ 작성자가 이어서 쓴 글 포함</div>` : ''}
        <div class="cardtags">${(p.tags || []).map((t) => `<span>${esc(t)}</span>`).join('')}</div>
      </div>
    </article>`;
  }).join('') + '</div>';
}

function renderTable(posts) {
  const rows = posts.map((p) => {
    const img = p.media.find((m) => imgOf(m));
    return `<tr data-id="${p.id}">
      <td class="nowrap">${p.cat_name ? `<span class="badge" style="background:${p.cat_color}">${esc(p.cat_name)}</span>` : '-'}</td>
      <td class="t-sum"><div>${esc(p.summary || (p.body || '').slice(0, 60))}</div>
        <div class="sub">${esc((p.full_text || '').replace(/\n+/g, ' ').slice(0, 160))}</div></td>
      <td class="nowrap">@${esc(p.author || '')}</td>
      <td class="nowrap">${fmtDate(p.posted_at)}</td>
      <td class="t-img">${img ? `<img loading="lazy" src="${esc(imgOf(img))}">` : ''}</td>
      <td class="nowrap">${(p.tags || []).slice(0, 3).map(esc).join(', ')}</td>
    </tr>`;
  }).join('');
  return `<div class="tablewrap"><table><thead><tr>
      <th>분류</th><th>요약 / 본문</th><th>작성자</th><th>날짜</th><th>이미지</th><th>태그</th>
    </tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderBoard(posts) {
  return '<div class="board">' + posts.map((p) => {
    const img = p.media.find((m) => imgOf(m));
    return `<div class="bcard" data-id="${p.id}">
      ${img ? `<img loading="lazy" src="${esc(imgOf(img))}" alt="">` : ''}
      <div class="bcap">${esc(p.summary || (p.body || '').slice(0, 80))}
        <div class="m">@${esc(p.author || '')} · ${fmtDate(p.posted_at)}</div></div>
    </div>`;
  }).join('') + '</div>';
}

function bindOpen(sel) {
  document.querySelectorAll(sel).forEach((el) => el.onclick = () => openPost(el.dataset.id));
}

/* ── 상세 모달 ──────────────────────────── */
function openPost(id) {
  const p = S.posts.find((x) => x.id === id);
  if (!p) return;
  const segs = (p.segments || []).filter((s) => (s.text || '').trim());
  const body = segs.length
    ? segs.map((s, i) => `<div class="seg ${s.kind}">${i > 0 ? '<div class="seg-label">↳ 작성자가 이어서 쓴 글</div>' : ''}${esc(s.text)}</div>`).join('')
    : `<div class="seg">${esc(p.full_text || p.body || '')}</div>`;
  const imgs = p.media.filter((m) => imgOf(m))
    .map((m) => `<img class="full" loading="lazy" src="${esc(imgOf(m))}" alt="${esc(m.alt || '')}">`).join('');
  const opts = S.cats.map((c) =>
    `<option value="${c.id}" ${c.slug === p.cat_slug ? 'selected' : ''}>${esc(c.name)}</option>`).join('');

  $('#modalbox').innerHTML = `
    <div class="modal-head">
      <select id="m-cat">${opts}</select>
      <span class="nowrap">@${esc(p.author || '')} · ${fmtDate(p.posted_at)}</span>
      ${p.confidence ? `<span class="nowrap">확신도 ${Math.round(p.confidence * 100)}%</span>` : ''}
      ${p.url ? `<a class="btn ghost" href="${esc(p.url)}" target="_blank" rel="noopener">원문 열기 ↗</a>` : ''}
      <button class="x" id="m-x">✕</button>
    </div>
    <div class="modal-body">
      ${p.summary ? `<div class="summary">${esc(p.summary)}</div>` : ''}
      ${body}
      ${imgs}
      <div class="cardtags">${(p.tags || []).map((t) => `<span>${esc(t)}</span>`).join('')}</div>
    </div>
    <div class="modal-foot">
      <span class="hint">내 컴퓨터에 받아둔 사본만 지웁니다 —
        Threads 계정의 '저장됨' 목록은 그대로입니다.</span>
      <button class="btn danger" id="m-del">로컬에서 삭제</button>
    </div>`;
  $('#modal').classList.remove('hidden');
  $('#m-x').onclick = closeModal;
  $('.modal-bg').onclick = closeModal;
  $('#m-del').onclick = () => deletePost(p);
  $('#m-cat').onchange = async (e) => {
    await api(`/api/posts/${p.id}/category`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category_id: Number(e.target.value), lock: true }),
    });
    await boot(); closeModal();
  };
}
const closeModal = () => $('#modal').classList.add('hidden');
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

/* 로컬에 받아둔 사본만 삭제 — Threads 계정의 '저장됨' 목록은 건드리지 않는다 */
async function deletePost(p) {
  const peek = (p.summary || p.body || p.full_text || '').replace(/\s+/g, ' ').slice(0, 60);
  if (!confirm(`이 글을 로컬에서 지웁니다.\n\n@${p.author || ''}\n${peek}\n\n`
    + '· 본문·이어쓴 글·분류·내려받은 이미지가 삭제됩니다\n'
    + "· Threads 계정의 '저장됨' 목록은 그대로입니다\n"
    + '· 다음 동기화에서 다시 가져오지 않습니다\n\n삭제할까요?')) return;

  const btn = $('#m-del');
  if (btn) { btn.disabled = true; btn.textContent = '삭제 중…'; }
  let r = null;
  try {
    r = await api(`/api/posts/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
  } catch (e) { /* 아래에서 실패로 처리 */ }

  if (!r || !r.ok) {
    toast('삭제하지 못했습니다. ' + ((r && (r.detail || r.error)) || ''));
    if (btn) { btn.disabled = false; btn.textContent = '로컬에서 삭제'; }
    return;
  }
  closeModal();
  S.posts = S.posts.filter((x) => x.id !== p.id);   // 목록에서 즉시 치운다
  renderHead(); renderView();
  toast(`삭제했습니다 — 이미지 ${r.media_removed}개 정리. `
    + "다시 받으려면 '전체 다시 훑기' 전에 복원이 필요합니다.");
  boot();                                            // 개수·카테고리 갱신
}

/* ── 툴바 동작 ──────────────────────────── */
$('#viewtoggle').querySelectorAll('button').forEach((b) => b.onclick = async () => {
  S.view = b.dataset.view;
  const c = S.cats.find((x) => x.slug === S.cat);
  if (c) { // 카테고리별 기본 보기로 기억
    c.view = S.view;
    api(`/api/categories/${c.id}/view`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ view: S.view }),
    });
  } else {
    localStorage.setItem('mt-view', S.view);
  }
  renderHead(); renderView();
});

$('#sort').onchange = (e) => {
  S.sort = e.target.value;
  localStorage.setItem('mt-sort', S.sort);
  load();
};

let qt;
$('#q').oninput = (e) => { clearTimeout(qt); qt = setTimeout(() => { S.q = e.target.value.trim(); load(); }, 250); };

$('#btn-theme').onclick = () => {
  const cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', cur);
  localStorage.setItem('mt-theme', cur);
};

$('#btn-sync').onclick = () => runJob('/api/sync', '새로 저장한 글 확인 중…');
$('#btn-syncfull').onclick = () => {
  if (!confirm('저장됨 목록을 처음부터 끝까지 훑습니다.\n글이 많으면 몇 분 걸립니다. 계속할까요?')) return;
  runJob('/api/sync?full=1', '저장됨 목록 전체 훑는 중…');
};
$('#btn-reclassify').onclick = () => {
  if (!confirm('카테고리 체계를 다시 만들고 전체를 재분류합니다.\n(수동으로 확정한 글은 유지됩니다) 계속할까요?')) return;
  runJob('/api/reclassify', '재분류 중…');
};

async function runJob(url, label) {
  setButtons(true);
  $('#progress').classList.remove('hidden');
  $('#progress-text').textContent = label;
  $('#progress-bar').classList.add('indet');
  $('#progress-cancel').style.display = 'none';
  const r = await api(url, { method: 'POST' });
  if (!r.ok) toast(r.message || '실행하지 못했습니다.');
  lastTotal = null;
  poll();
}

/* ── 진행 표시 · 자동 새로고침 ─────────────── */
const setButtons = (busy) => ['#btn-sync', '#btn-syncfull', '#btn-reclassify']
  .forEach((id) => { $(id).disabled = busy; });
const PROGRESS_RE = /(\d+)\s*\/\s*(\d+)/;   // "상세 수집 137/366건"
let polling = false;
let lastTotal = null;

function toast(msg, ms = 6000) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function showProgress(job) {
  const box = $('#progress');
  if (!job) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  const label = { sync: '동기화', sync_full: '전체 훑기', classify: '분류',
                  reclassify: '재분류' }[job.kind] || job.kind;
  const msg = job.message || (job.status === 'queued' ? '대기 중…' : '준비 중…');
  $('#progress-text').textContent = `${label} — ${msg}`;
  const bar = $('#progress-bar');
  const m = PROGRESS_RE.exec(msg || '');
  if (m) {
    bar.classList.remove('indet');
    bar.style.width = Math.round((+m[1] / Math.max(+m[2], 1)) * 100) + '%';
  } else {
    bar.classList.add('indet');       // 진행률을 모를 땐 흐르는 막대
    bar.style.width = '';
  }
  const c = $('#progress-cancel');
  c.style.display = '';
  c.onclick = async () => {
    c.disabled = true; c.textContent = '중단 요청함…';
    await api(`/api/jobs/${job.id}/cancel`, { method: 'POST' });
  };
}

async function poll() {
  let d;
  try {
    d = await api('/api/status');
  } catch (e) {
    setTimeout(poll, 5000);          // 일시적인 통신 오류로 루프가 죽지 않도록
    return;
  }

  // 컨테이너를 새로 빌드해 화면 코드가 바뀌었으면 통째로 새로고침
  if (window.APP_VERSION && d.version && d.version !== window.APP_VERSION) {
    location.reload();
    return;
  }

  const j = d.job;
  showJob(j);
  const live = j && (j.status === 'queued' || j.status === 'running');

  if (live) {
    polling = true;
    setButtons(true);
    showProgress(j);
    if (lastTotal !== null && d.total !== lastTotal) await load();  // 새 글이 들어왔을 때만
    lastTotal = d.total;
    setTimeout(poll, 4000);
    return;
  }

  // ── 작업 종료 → 화면 전체 갱신 ──
  const wasRunning = polling;
  polling = false;
  lastTotal = d.total;
  showProgress(null);
  setButtons(false);
  await boot();
  if (wasRunning && j) {
    let done = '';
    try {
      const s = JSON.parse(j.stats || '{}');
      done = Object.entries(s).map(([k, v]) => `${k} ${v}`).join(' · ');
    } catch (e) {}
    const head = { done: '✅ 완료', error: '⛔ 실패', canceled: '⏹ 중단됨' }[j.status] || '';
    toast(`${head} — ${done || j.message || ''}`.trim());
  }
}

function showJob(j) {
  if (!j) return;
  const t = j.finished_at || j.heartbeat_at || j.started_at;
  const when = t ? new Date(t * 1000).toLocaleString('ko-KR', { hour12: false }) : '';
  const icon = { queued: '⏳', running: '🔄', done: '✅', error: '⛔', canceled: '⏹' }[j.status] || '';
  let extra = '';
  try {
    const s = JSON.parse(j.stats || '{}');
    extra = Object.entries(s).map(([k, v]) => `${k} ${v}`).join(' · ');
  } catch (e) {}
  $('#joblog').innerHTML =
    `${esc(icon)} ${esc(j.kind)} ${esc(when)}<br>${esc(j.message || '')}` +
    (extra ? `<br>${esc(extra)}` : '');
}

document.documentElement.setAttribute('data-theme', localStorage.getItem('mt-theme') || 'light');
S.view = localStorage.getItem('mt-view') || 'card';
S.sort = localStorage.getItem('mt-sort') || 'newest';
$('#sort').value = S.sort;
boot();
