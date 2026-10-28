/* 小说智能体前端：无框架单页应用 */
const S = {
  view: 'dashboard',
  projects: [],
  pid: null,
  overview: null,
  assets: null,
  outline: null,
  plan: [],
  constitution: null,
  generated: [],
  contexts: [],
  tasks: [],
  config: null,
  spent: 0,
  req: {},
  openBatches: new Set(),
  reader: null,
  busy: false,
  lastTaskSig: '',
};

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

const STATUS = {
  pending: ['排队中', ''], running: ['进行中', 'accent'], paused: ['已暂停', 'warn'],
  done: ['完成', 'ok'], failed: ['失败', 'bad'], archive_failed: ['档案失败', 'bad'], canceled: ['已取消', ''],
  uploaded: ['已上传', ''], analyzed: ['已拆书', 'ok'], outlined: ['已出大纲', 'accent'],
  writing: ['写作中', 'accent'], polished: ['已优化', 'ok'], created: ['新建', ''],
  draft: ['草稿', ''], checked: ['已过检', 'ok'], high_aigc: ['AIGC偏高', 'bad'],
  await_check: ['待人工检测', 'warn'], check_failed: ['检测失败', 'warn'],
  rewritten: ['已改写', 'accent'], polished_ch: ['已优化', 'ok'], paused_batch: ['已暂停', 'warn'],
};
const statusChip = (s) => {
  const [label, cls] = STATUS[s] || [s, ''];
  return `<span class="chip ${cls}">${esc(label)}</span>`;
};

const num = (n) => Number(n || 0).toLocaleString('zh-CN');
const wan = (n) => (n >= 10000 ? (n / 10000).toFixed(1) + ' 万' : num(n));

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    // 浏览器仍能显示旧页面，但本地 Python 服务退出时，所有按钮都会统一报
    // “Failed to fetch”。给出可操作提示，并在短时间内合并重复弹窗。
    const now = Date.now();
    const message = '本地服务没有运行，请双击“启动.command”重新启动后刷新页面。';
    if (!S._networkToastAt || now - S._networkToastAt > 5000) {
      S._networkToastAt = now;
      toast(message, 'bad');
    }
    throw new Error(message);
  }
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json().catch(() => ({})) : await res.text();
  if (!res.ok) {
    const msg = (data && data.detail) || (typeof data === 'string' && data) || `请求失败（${res.status}）`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}
const post = (p, body) => api(p, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
});
const put = (p, body) => api(p, {
  method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
});

function toast(msg, kind = '') {
  const now = Date.now();
  if (S._lastToast?.message === msg && now - S._lastToast.at < 4500) return;
  S._lastToast = { message: msg, at: now };
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = '0.3s'; }, 4200);
  setTimeout(() => el.remove(), 4700);
}

function showModal(html) {
  $('#modal').innerHTML = html;
  $('#modalBack').hidden = false;
}
function closeModal() {
  $('#modalBack').hidden = true;
  $('#modal').innerHTML = '';
  S.reader = null;
}
$('#modalBack')?.addEventListener('click', (e) => { if (e.target.id === 'modalBack') closeModal(); });

// ------------------------------------------------------------ 初始化
document.addEventListener('DOMContentLoaded', init);

async function init() {
  bindEvents();
  try {
    const [cfg, projects] = await Promise.all([api('/api/config'), api('/api/projects')]);
    S.config = cfg.config;
    S.spent = cfg.spent;
    S.projects = projects.projects;
    renderProjectSelect();
    updateBalance();
    if (S.projects.length) {
      await selectProject(S.projects[0].id);
    } else {
      renderView();
    }
  } catch (err) {
    toast('初始化失败：' + err.message, 'bad');
  }
  setInterval(liveTick, 3200);
}

function bindEvents() {
  $('#nav').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-view]');
    if (!btn) return;
    switchView(btn.dataset.view);
  });
  $('#projectSelect').addEventListener('change', (e) => {
    const pid = Number(e.target.value);
    if (pid) selectProject(pid);
  });
  $('#newProjectBtn').addEventListener('click', openNewProject);
  $('#continueBtn').addEventListener('click', continueTask);

  document.addEventListener('click', async (e) => {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;
    try {
      await handleAction(act, el);
    } catch (err) {
      toast(err.message, 'bad');
    }
  });
  document.addEventListener('input', (e) => {
    const el = e.target;
    if (el.dataset.pfield) {
      const p = (S.config?.providers || []).find((x) => x.id === el.dataset.pid);
      if (p) {
        let v = el.value;
        if (el.dataset.pfield === 'models') {
          v = v.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
          const card = el.closest('.provider');
          const btn = card?.querySelector('[data-act="test-connection"]');
          const hint = card?.querySelector('[id^="testResult_"]');
          if (btn) btn.disabled = v.length === 0;
          if (hint) {
            hint.textContent = v.length ? `会用「${v[0]}」发一条测试消息` : '先填至少一个模型名，才能测试';
            hint.style.color = '';
          }
        }
        p[el.dataset.pfield] = v;
      }
    }
    if (el.dataset.path) setPath(S.config, el.dataset.path, parseInput(el));
    if (el.id === 'zhuqueHeaders') {
      try { S.config.zhuque.headers = JSON.parse(el.value || '{}'); } catch { /* 保存/测试时提示。 */ }
    }
    if (el.dataset.req) { S.req[el.dataset.req] = el.value; S.reqDirty = true; }
    if (el.id === 'outlineEditor') S.outlineDirty = true;
    if (el.id === 'constitutionEditor') S.constitutionDirty = true;
  });
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (el.dataset.pfield) {
      const p = (S.config?.providers || []).find((x) => x.id === el.dataset.pid);
      if (p) {
        if (el.dataset.pfield === 'models') {
          p.models = el.value.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
        } else {
          p[el.dataset.pfield] = el.value;
        }
      }
    }
    if (el.dataset.path) setPath(S.config, el.dataset.path, parseInput(el));
    if (el.dataset.path === 'zhuque.adapter') renderSettings();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#modalBack').hidden) closeModal();
  });
}

function parseInput(el) {
  if (el.type === 'checkbox') return el.checked;
  if (el.type === 'number') return Number(el.value || 0);
  if (el.dataset.json) {
    try { return JSON.parse(el.value || '{}'); } catch { return el.dataset.raw ? el.value : {}; }
  }
  return el.value;
}

function setPath(obj, path, value) {
  const parts = path.split('.');
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (typeof cur[parts[i]] !== 'object' || cur[parts[i]] === null) cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}

function project() { return S.projects.find((p) => p.id === S.pid) || null; }

function refTitle() {
  return S.overview?.titles?.reference || project()?.name || '参考书';
}

function currentNewTitle() {
  const m = (S.outline?.content || '').match(/^#\s+(.+)$/m);
  if (m) return m[1].replace(/[*_`#]/g, '').trim().replace(/^《(.*)》$/, '$1').trim();
  return S.overview?.titles?.new || '';
}

function renderProjectSelect() {
  const sel = $('#projectSelect');
  sel.innerHTML = '<option value="">未选择项目</option>' + S.projects
    .map((p) => `<option value="${p.id}">${esc(p.name)}${p.new_title ? ` → ${esc(p.new_title)}` : ''}</option>`).join('');
  if (S.pid) sel.value = String(S.pid);
}

async function selectProject(pid) {
  S.pid = pid;
  S.overview = null; S.assets = null; S.outline = null; S.plan = [];
  S.constitution = null; S.generated = []; S.contexts = []; S.tasks = [];
  S.req = defaultReq(); S.reqLoaded = false; S.reader = null;
  $('#projectSelect').value = String(pid);
  await loadBase();
  await loadView(S.view, true);
  updateTopbar();
}

async function loadBase() {
  if (!S.pid) return;
  const [ov, ts] = await Promise.all([
    api(`/api/projects/${S.pid}/overview`),
    api(`/api/tasks?project_id=${S.pid}`),
  ]);
  S.overview = ov;
  S.tasks = ts.tasks;
  const p = project();
  if (p && ov.project) { p.name = ov.project.name; }
}

async function loadView(view, force = false) {
  if (!S.pid) {
    if (view === 'settings') { renderView(); return; }
    renderEmptyProject();
    return;
  }
  try {
    if (view === 'dashboard') {
      await loadBase();
      S.assets = await api(`/api/projects/${S.pid}/assets`);
    } else if (view === 'analyze') {
      await loadBase();
      S.assets = await api(`/api/projects/${S.pid}/assets`);
    } else if (view === 'outline') {
      const data = await api(`/api/projects/${S.pid}/outline`);
      S.outline = data.outline;
      S.plan = data.plan || [];
      S.outlineDirty = false;
      if (!S.reqLoaded && S.outline?.requirement) {
        parseRequirement(S.outline.requirement);
        S.reqLoaded = true;
      }
    } else if (view === 'constitution') {
      const data = await api(`/api/projects/${S.pid}/constitution`);
      S.constitution = data.constitution;
      S.constitutionDirty = false;
      if (!S.plan.length) {
        const o = await api(`/api/projects/${S.pid}/outline`);
        S.outline = o.outline; S.plan = o.plan || [];
      }
    } else if (view === 'write' || view === 'polish') {
      await loadBase();
      const [gen, ol, ctx] = await Promise.all([
        api(`/api/projects/${S.pid}/generated`),
        api(`/api/projects/${S.pid}/outline`),
        api(`/api/projects/${S.pid}/contexts`),
      ]);
      S.generated = gen.chapters;
      S.outline = ol.outline;
      S.plan = ol.plan || [];
      S.contexts = ctx.contexts || [];
    } else if (view === 'tasks') {
      await loadBase();
    } else if (view === 'settings') {
      const cfg = await api('/api/config');
      S.config = cfg.config;
      S.spent = cfg.spent;
    }
    renderView();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

function renderView() {
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${S.view}`));
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === S.view));
  if (!S.pid) {
    if (S.view === 'settings') { renderSettings(); return; }
    renderEmptyProject();
    return;
  }
  if (S.view === 'dashboard') renderDashboard();
  else if (S.view === 'analyze') renderAnalyze();
  else if (S.view === 'outline') renderOutline();
  else if (S.view === 'constitution') renderConstitution();
  else if (S.view === 'write') renderWrite();
  else if (S.view === 'polish') renderPolish();
  else if (S.view === 'tasks') renderTasks();
  else if (S.view === 'settings') renderSettings();
}

function switchView(view) {
  S.view = view;
  renderView();
  loadView(view);
}

// ------------------------------------------------------------ 实时轮询
async function liveTick() {
  if (!S.pid || S.busy) return;
  S.tickCount = (S.tickCount || 0) + 1;
  if (S.config && S.tickCount % 5 === 0 && S.view !== 'settings') {
    try {
      const c = await api('/api/config');
      S.config.balance = c.config.balance;
      S.spent = c.spent;
      updateBalance();
    } catch { /* 静默 */ }
  }
  try {
    const [ov, ts] = await Promise.all([
      api(`/api/projects/${S.pid}/overview`),
      api(`/api/tasks?project_id=${S.pid}`),
    ]);
    const running = ts.tasks.some((t) => t.status === 'running');
    const sig = JSON.stringify(ts.tasks.map((t) => [t.id, t.status, t.progress])) +
      JSON.stringify(ov.stats);
    const changed = sig !== S.lastTaskSig;
    S.lastTaskSig = sig;
    S.overview = ov;
    S.tasks = ts.tasks;
    updateTopbar();
    updateBalance();
    if (!changed) return;
    if (['dashboard', 'tasks', 'analyze'].includes(S.view)) {
      renderView();
      if (S.view === 'analyze') {
        S.assets = await api(`/api/projects/${S.pid}/assets`);
        renderAnalyze();
      }
    } else if (running && ['write', 'polish'].includes(S.view)) {
      const gen = await api(`/api/projects/${S.pid}/generated`);
      S.generated = gen.chapters;
      const ctx = await api(`/api/projects/${S.pid}/contexts`);
      S.contexts = ctx.contexts || [];
      renderView();
    }
    if (!running) {
      // 任务结束：刷新项目和当前视图数据
      try {
        const ps = await api('/api/projects');
        S.projects = ps.projects;
        renderProjectSelect();
      } catch { /* 静默 */ }
      const gen = ['write', 'polish'].includes(S.view)
        ? await api(`/api/projects/${S.pid}/generated`) : null;
      if (gen) S.generated = gen.chapters;
      if (S.view === 'outline' && !S.outlineDirty) {
        const d = await api(`/api/projects/${S.pid}/outline`);
        S.outline = d.outline; S.plan = d.plan || [];
        if (d.outline?.requirement && !S.reqDirty) parseRequirement(d.outline.requirement);
      }
      if (S.view === 'constitution' && !S.constitutionDirty) { const d = await api(`/api/projects/${S.pid}/constitution`); S.constitution = d.constitution; }
      if (S.view === 'analyze') { S.assets = await api(`/api/projects/${S.pid}/assets`); }
      renderView();
    }
  } catch { /* 静默 */ }
}

function updateTopbar() {
  const running = S.tasks.find((t) => t.status === 'running');
  const chip = $('#taskChip');
  if (running) {
    const pct = running.total ? Math.round((running.progress / running.total) * 100) : null;
    chip.hidden = false;
    chip.innerHTML = `<span class="pulse"></span><span class="txt">${esc(running.title)}${pct !== null ? ` · ${pct}%` : ''}</span>`;
  } else {
    chip.hidden = true;
  }
  const paused = S.tasks.some((t) => ['paused', 'failed'].includes(t.status));
  $('#continueBtn').style.opacity = paused ? '1' : '0.55';
}

function updateBalance() {
  if (!S.config) return;
  const { amount = 0, warning_threshold = 3, currency = '元' } = S.config.balance || {};
  $('#sideBalance').textContent = amount > 0 ? `¥ ${Number(amount).toFixed(2)}` : '未设置';
  $('#sideBalanceNote').textContent = S.spent ? `已用 ¥${Number(S.spent).toFixed(2)}` : '';
  $('#thresholdLabel').textContent = warning_threshold;
  if (amount > 0 && amount <= warning_threshold && !S.lowBalanceWarned) {
    S.lowBalanceWarned = true;
    toast(`余额低于预警线（${warning_threshold}${currency}），请及时充值`, 'warn');
  }
  if (amount > warning_threshold) S.lowBalanceWarned = false;
}

// ------------------------------------------------------------ 新建项目
function openNewProject() {
  showModal(`
    <div class="modal-head">
      <div><div class="page-eyebrow">STEP 1 / UPLOAD REFERENCE</div><h2>上传参考书，开始拆书</h2></div>
      <button class="modal-close" data-act="modal-close">&times;</button>
    </div>
    <div class="notice" style="margin-bottom:14px">
      这里上传的是<b>别人写的高分小说</b>，用来拆解手法、节奏和爽点，<b>不是你自己的原创稿</b>。
      拆完之后，系统会另外生成一本全新的书（新书名、新人物、新世界观）。
    </div>
    <label class="field"><span>参考书名（可留空，自动用文件名）</span><input type="text" id="newName" placeholder="例：斗破苍穹"></label>
    <div class="drop" id="dropZone">
      <div class="big">拖入参考书 TXT，或点击选择</div>
      <div class="small">支持 UTF-8 / GBK，自动按“第X章”分章；没有章节号会按长度切块</div>
      <input type="file" id="fileInput" accept=".txt,text/plain" hidden>
      <div id="fileName" class="small" style="margin-top:8px;color:var(--accent-2)"></div>
    </div>
    <div class="hr"></div>
    <label class="field"><span>或者直接粘贴参考书正文</span>
      <textarea id="pasteText" class="editor short" placeholder="把要学习的高分小说正文粘贴到这里……"></textarea>
    </label>
    <div class="modal-foot">
      <button class="btn ghost" data-act="modal-close">取消</button>
      <button class="btn primary" id="createProjectBtn">上传参考书并拆书</button>
    </div>
  `);
  let pickedFile = null;
  const drop = $('#dropZone');
  const input = $('#fileInput');
  drop.addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    pickedFile = input.files[0] || null;
    $('#fileName').textContent = pickedFile ? pickedFile.name : '';
  });
  ['dragover', 'dragenter'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => {
    pickedFile = e.dataTransfer.files[0] || null;
    if (pickedFile) $('#fileName').textContent = pickedFile.name;
  });
  $('#createProjectBtn').addEventListener('click', async () => {
    const name = $('#newName').value.trim();
    const paste = $('#pasteText').value.trim();
    if (!pickedFile && !paste) { toast('请选择文件或粘贴正文', 'warn'); return; }
    const fd = new FormData();
    fd.append('name', name);
    if (pickedFile) fd.append('file', pickedFile);
    else fd.append('text', paste);
    try {
      const btn = $('#createProjectBtn');
      btn.disabled = true; btn.textContent = '正在解析……';
      const res = await api('/api/projects', { method: 'POST', body: fd });
      closeModal();
      const projects = await api('/api/projects');
      S.projects = projects.projects;
      renderProjectSelect();
      await selectProject(res.project_id);
      toast('参考书已上传，点「继续下一批」开始拆书', 'ok');
      switchView('analyze');
    } catch (err) {
      toast(err.message, 'bad');
      const btn = $('#createProjectBtn');
      btn.disabled = false; btn.textContent = '上传参考书并拆书';
    }
  });
}

function renderEmptyProject() {
  const html = `
    <div class="page-head">
      <div>
        <div class="page-eyebrow">NOVEL FORGE / 01</div>
        <h1>先拆别人的高分小说，再写自己的新书</h1>
        <p class="page-desc">上传参考书 → 5 章一批拆细纲 → 学它的手法、节奏、爽点 → 生成全新书名/人物/世界观的原创大纲 → 写一本新小说。参考书只用于拆解学习，不会进入新书正文。</p>
      </div>
    </div>
    <div class="card">
      <div class="empty" style="border:0;padding:30px 10px">
        <strong>还没有项目</strong>
        <div style="margin-bottom:16px">先配置模型，再上传一本参考小说或粘贴正文。</div>
        <div class="btn-row" style="justify-content:center">
          <button class="btn primary" data-act="new-project">新建拆书项目</button>
          <button class="btn ghost" data-act="go" data-view="settings">先配置模型</button>
        </div>
      </div>
    </div>`;
  $$('.view').forEach((v) => { if (v.id === `view-${S.view}`) v.innerHTML = html; });
}

// ------------------------------------------------------------ 动作
async function handleAction(act, el) {
  const pid = S.pid;
  if (act === 'modal-close') return closeModal();
  if (act === 'new-project') return openNewProject();
  if (act === 'go') return switchView(el.dataset.view);
  if (!pid && !['settings-save', 'provider-add', 'provider-del', 'test-model'].includes(act)) return;

  if (act === 'analyze-next' || act === 'analyze-all') {
    if (act === 'analyze-all') {
      const st = S.overview?.stats || {};
      const left = (st.detail_total || 0) - (st.detail_done || 0);
      if (left > 10 && !confirm(`还有 ${left} 批细纲要拆，会连续调用模型，确定自动跑完？`)) return;
    }
    const res = await post(`/api/projects/${pid}/analyze`, {
      mode: act === 'analyze-all' ? 'all' : 'next',
    });
    toast('拆书任务已入队' + (res.task_id ? ` #${res.task_id}` : ''), 'ok');
    return loadView(S.view);
  }
  if (act === 'analyze-batch') {
    const b = Number(el.dataset.batch);
    await post(`/api/projects/${pid}/analyze`, { mode: 'batch', batch: b });
    toast(`第 ${b + 1} 批已重新入队`, 'ok');
    return loadView(S.view);
  }
  if (act === 'analysis-finish') {
    const res = await post(`/api/projects/${pid}/analyze/finish`, {});
    toast(res.message, 'ok');
    await loadBase();
    return switchView('outline');
  }
  if (act === 'toggle-batch') {
    const b = Number(el.dataset.batch);
    if (S.openBatches.has(b)) S.openBatches.delete(b); else S.openBatches.add(b);
    return renderView();
  }
  if (act === 'outline-generate') {
    if (S.overview?.stats?.constitution_locked &&
        !confirm('重新生成大纲会覆盖当前大纲并解锁创作宪法，确定继续？')) return;
    const requirement = composeRequirement();
    if (!requirement) { toast('请至少填写书名或核心看点', 'warn'); return; }
    await post(`/api/projects/${pid}/outline/generate`, { requirement });
    toast('大纲生成任务已入队', 'ok');
    return loadView(S.view);
  }
  if (act === 'outline-save') {
    const content = $('#outlineEditor').value;
    const d = await put(`/api/projects/${pid}/outline`, { content });
    S.plan = d.plan || [];
    S.outlineDirty = false;
    toast('大纲已保存，章节计划已更新', 'ok');
    return renderView();
  }
  if (act === 'plan-repair') {
    if (S.outlineDirty) {
      const content = $('#outlineEditor')?.value;
      if (content != null) await put(`/api/projects/${pid}/outline`, {content});
      S.outlineDirty = false;
    }
    await post(`/api/projects/${pid}/outline/plan`, {});
    toast('正在整理现有大纲的章节计划，无需继续拆书', 'ok');
    return loadView(S.view);
  }
  if (act === 'constitution-generate') {
    if (!confirm('重新提炼会覆盖当前创作宪法，确定继续？')) return;
    const res = await post(`/api/projects/${pid}/constitution/generate`);
    S.constitution = { content: res.content, locked: 0 };
    toast('创作宪法已重新提炼，请确认后锁定', 'ok');
    return renderView();
  }
  if (act === 'constitution-save') {
    const content = $('#constitutionEditor').value;
    const d = await put(`/api/projects/${pid}/constitution`, { content, locked: S.constitution?.locked });
    S.constitution = { ...(S.constitution || {}), content, locked: d.locked ? 1 : 0 };
    S.constitutionDirty = false;
    toast('创作宪法已保存', 'ok');
    return renderView();
  }
  if (act === 'constitution-lock') {
    const checks = $$('.constitution-check');
    if (checks.length && checks.some((c) => !c.checked)) {
      toast('请逐项确认大纲、人物、主题、文风、爽点后再锁定', 'warn');
      return;
    }
    const content = $('#constitutionEditor').value;
    const d = await put(`/api/projects/${pid}/constitution`, { content, locked: true });
    S.constitution = { ...(S.constitution || {}), content, locked: 1 };
    toast('已锁定，可以开始写作', 'ok');
    return loadView(S.view);
  }
  if (act === 'constitution-unlock') {
    const content = $('#constitutionEditor').value;
    await put(`/api/projects/${pid}/constitution`, { content, locked: false });
    S.constitution = { ...(S.constitution || {}), content, locked: 0 };
    toast('已解锁，修改后请重新锁定', 'warn');
    return renderView();
  }
  if (act === 'write-next' || act === 'write-all') {
    if (act === 'write-all') {
      const st = S.overview?.stats || {};
      const left = (st.plan_total || 0) - (st.written || 0);
      if (left > 10 && !confirm(`还有 ${left} 章要写，会连续调用模型，确定自动写完？`)) return;
    }
    await post(`/api/projects/${pid}/write`, { mode: act === 'write-all' ? 'all' : 'next' });
    toast('写作任务已入队', 'ok');
    return loadView(S.view);
  }
  if (act === 'write-batch') {
    const b = Number(el.dataset.batch);
    if (!confirm(`会重新生成第 ${b + 1} 批的全部章节，覆盖现有正文和承接卡，确定继续？`)) return;
    await post(`/api/projects/${pid}/write`, { mode: 'batch', batch: b, force: true });
    toast(`第 ${b + 1} 批重新生成已入队`, 'warn');
    return loadView(S.view);
  }
  if (act === 'open-chapter') {
    return openReader(Number(el.dataset.id));
  }
  if (act === 'check-all') {
    if (!S.config?.zhuque?.enabled) {
      toast('朱雀检测还没启用，请先到设置页配置', 'warn');
      switchView('settings');
      return;
    }
    await post(`/api/projects/${pid}/check`, { idxs: [] });
    toast('朱雀检测任务已入队', 'ok');
    return loadView(S.view);
  }
  if (act === 'polish-all') {
    const start = Number($('#polishStart')?.value || 0);
    const end = Number($('#polishEnd')?.value || 0);
    await post(`/api/projects/${pid}/polish`, {
      start: start || undefined, end: end || undefined,
    });
    toast('优化任务已入队', 'ok');
    return loadView(S.view);
  }
  if (act === 'context-rebuild') {
    const b = Number(el.dataset.batch);
    await post(`/api/projects/${pid}/context`, { batch: b });
    toast('承接卡重建任务已入队', 'ok');
    return;
  }
  if (act === 'task-pause') {
    await post(`/api/tasks/${el.dataset.id}/pause`);
    toast('已暂停，当前批次完成后停止', 'warn');
    return loadView(S.view);
  }
  if (act === 'task-resume') {
    await post(`/api/tasks/${el.dataset.id}/resume`);
    toast('任务继续执行', 'ok');
    return loadView(S.view);
  }
  if (act === 'task-cancel') {
    await post(`/api/tasks/${el.dataset.id}/cancel`);
    toast('任务已取消', 'warn');
    return loadView(S.view);
  }
  if (act === 'save-chapter') {
    const gid = Number(el.dataset.id);
    const content = $('#readerContent').value;
    const title = $('#readerTitle').value;
    const tab = $('#readerTab')?.dataset.tab || 'content';
    const body = { title };
    if (tab === 'polished') body.polished = content;
    else body.content = content;
    await put(`/api/generated/${gid}`, body);
    toast('章节已保存', 'ok');
    closeModal();
    return loadView(S.view);
  }
  if (act === 'rewrite-chapter') {
    const idx = Number(el.dataset.idx);
    const instruction = $('#rewriteInstruction')?.value || '';
    await post(`/api/projects/${pid}/rewrite`, { idx, instruction });
    toast('重写任务已入队，可在写作页查看进度', 'ok');
    closeModal();
    return loadView(S.view);
  }
  if (act === 'recheck-chapter') {
    const idx = Number(el.dataset.idx);
    await post(`/api/projects/${pid}/check`, { idxs: [idx] });
    toast('已提交朱雀检测', 'ok');
    return;
  }
  if (act === 'record-score') {
    const gid = Number(el.dataset.id);
    const score = Number($('#manualScore').value);
    if (!Number.isFinite(score)) { toast('请输入检测分', 'warn'); return; }
    await put(`/api/generated/${gid}/score`, { score });
    toast('检测分已记录', 'ok');
    return openReader(gid);
  }
  if (act === 'reader-tab') {
    const tab = el.dataset.tab;
    $('#readerTab').dataset.tab = tab;
    $$('.seg button').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
    const g = S.reader;
    $('#readerContent').value = tab === 'polished' ? (g.polished || g.content) : g.content;
    return;
  }
  if (act === 'zhuque-test') {
    const headersEl = $('#zhuqueHeaders');
    if (headersEl) {
      try { S.config.zhuque.headers = JSON.parse(headersEl.value || '{}'); }
      catch { toast('请求头 JSON 格式错误', 'bad'); return; }
    }
    const resultEl = $('#zhuqueTestResult');
    resultEl.textContent = '正在提交测试文本并等待检测结果……';
    el.disabled = true;
    try {
      const res = await post('/api/zhuque/test', {settings: S.config.zhuque});
      resultEl.textContent = res.ok ? `检测成功：疑似 AI 占比 ${res.score} 分。启用自动检测并保存全部设置后，每章会自动检测。` : `检测失败：${res.error}`;
      resultEl.style.color = res.ok ? 'var(--ok)' : 'var(--bad)';
    } catch (err) {
      resultEl.textContent = `检测失败：${err.message}`;
    } finally { el.disabled = false; }
    return;
  }
  if (act === 'settings-save') {
    const pricesEl = $('#pricesJson');
    if (pricesEl) {
      try { S.config.balance.prices = JSON.parse(pricesEl.value || '{}'); }
      catch { toast('模型价格 JSON 格式错误', 'bad'); return; }
    }
    const headersEl = $('#zhuqueHeaders');
    if (headersEl) {
      try { S.config.zhuque.headers = JSON.parse(headersEl.value || '{}'); }
      catch { toast('请求头 JSON 格式错误', 'bad'); return; }
    }
    const res = await put('/api/config', { config: S.config });
    S.config = res.config;
    updateBalance();
    toast('设置已保存', 'ok');
    return renderView();
  }
  if (act === 'provider-add') {
    const id = 'p' + Date.now().toString(36);
    S.config.providers = S.config.providers || [];
    S.config.providers.push({
      id, name: '新供应商', type: 'openai', base_url: '', api_key: '', models: [],
    });
    return renderSettings();
  }
  if (act === 'provider-del') {
    const pidr = el.dataset.pid;
    S.config.providers = (S.config.providers || []).filter((p) => p.id !== pidr);
    return renderSettings();
  }
  if (act === 'test-model' || act === 'test-connection') {
    const pidr = el.dataset.pid;
    const model = act === 'test-model' ? el.dataset.model : '';
    const resultEl = document.getElementById(`testResult_${pidr}`);
    if (resultEl) { resultEl.textContent = '正在测试接通……'; resultEl.style.color = 'var(--muted)'; }
    try {
      await put('/api/config', { config: S.config });
      const res = await post('/api/test_model', { provider_id: pidr, model });
      const msg = `接通成功 · ${res.model} · ${res.latency}s`;
      if (resultEl) { resultEl.textContent = msg; resultEl.style.color = 'var(--ok)'; }
      toast(`${msg}（${String(res.text).slice(0, 20)}）`, 'ok');
    } catch (err) {
      if (resultEl) { resultEl.textContent = '接通失败：' + err.message; resultEl.style.color = 'var(--bad)'; }
      toast(err.message, 'bad');
    }
    return;
  }
  if (act === 'export') {
    const fmt = el.dataset.fmt;
    const polished = $('#usePolished')?.checked ? 1 : 0;
    window.location = `/api/projects/${pid}/export?fmt=${fmt}&polished=${polished}`;
    return;
  }
  if (act === 'rename-project') {
    const name = prompt('新的书名', project()?.name || '');
    if (!name) return;
    await put(`/api/projects/${pid}`, { name });
    S.projects = (await api('/api/projects')).projects;
    renderProjectSelect();
    await loadBase();
    renderView();
    return;
  }
  if (act === 'delete-project') {
    if (!confirm('会删除该项目的全部拆书、大纲和正文，无法恢复。确定？')) return;
    await api(`/api/projects/${pid}`, { method: 'DELETE' });
    S.projects = (await api('/api/projects')).projects;
    renderProjectSelect();
    S.pid = null;
    if (S.projects.length) await selectProject(S.projects[0].id);
    else { renderEmptyProject(); }
    return;
  }
}

async function continueTask() {
  if (!S.pid) { toast('先选择一个项目', 'warn'); return; }
  const res = await post(`/api/projects/${S.pid}/continue`);
  if (res.mode === 'running') toast(res.message || '已有任务正在执行', 'warn');
  else if (res.task_id) toast('继续执行任务 #' + res.task_id, 'ok');
  else if (['outline', 'constitution'].includes(res.mode)) {
    toast(res.message, 'ok');
    return switchView(res.mode);
  }
  else {
    const st = S.overview?.stats || {};
    if (st.detail_total && st.detail_done >= st.detail_total && !st.plan_total) {
      toast('拆书已完成。去「原创大纲」填写要求，生成新书大纲', 'warn');
      switchView('outline');
    } else {
      toast(res.message || '当前没有可继续的任务', 'warn');
    }
  }
  await loadView(S.view);
}
// ------------------------------------------------------------ 总览
function renderDashboard() {
  const ov = S.overview;
  if (!ov) return;
  const st = ov.stats;
  const mod = S.assets?.modules;
  const locked = st.constitution_locked;
  const allDetail = st.detail_total > 0 && st.detail_done >= st.detail_total;
  const hasDetail = (st.detail_available || st.detail_done) > 0;
  const steps = [
    {
      t: '拆参考书', d: st.detail_total ? `${st.detail_done} / ${st.detail_total} 批 · 每批 5 章` : '等待上传参考书',
      done: allDetail, active: !allDetail, view: 'analyze', btn: allDetail ? '查看细纲' : '去拆书',
    },
    {
      t: '六维分析', d: mod ? '人物 / 剧情 / 对白 / 文笔 / 世界观 / 读者' : '拆完自动汇总',
      done: !!mod, active: false, view: 'analyze', btn: '查看分析',
    },
    {
      t: '原创大纲（新书）', d: st.plan_total ? `${st.plan_total} 章计划已生成` : '尚未生成',
      done: !!st.plan_total, active: hasDetail && !st.plan_total, view: 'outline', btn: st.plan_total ? '编辑大纲' : '用现有内容生成',
    },
    {
      t: '创作宪法', d: locked ? '已锁定，写作时会全程注入' : (st.outline_status ? '待确认锁定' : '等待大纲'),
      done: locked, active: !!st.plan_total && !locked, view: 'constitution', btn: locked ? '查看' : '去确认',
    },
    {
      t: '正文写作（新书）', d: st.plan_total ? `${st.written} / ${st.plan_total} 章 · ${wan(st.written_words)} 字` : '等待大纲',
      done: st.plan_total > 0 && st.written >= st.plan_total, active: locked && st.written < st.plan_total,
      view: 'write', btn: '去写作',
    },
    {
      t: '优化导出', d: ov.project.status === 'polished' ? '已全书优化，可导出' : (st.written ? '可优化并导出' : '等待正文'),
      done: ov.project.status === 'polished', active: st.written > 0 && ov.project.status !== 'polished',
      view: 'polish', btn: '去优化',
    },
  ];
  const stepsHtml = steps.map((s, i) => `
    <div class="step ${s.done ? 'done' : ''} ${s.active && !s.done ? 'active' : ''}">
      <div class="step-num">${s.done ? '✓' : String(i + 1).padStart(2, '0')}</div>
      <div class="step-body">
        <div class="step-title">${s.t}</div>
        <div class="step-sub">${esc(s.d)}</div>
      </div>
      <button class="btn sm ghost" data-act="go" data-view="${s.view}">${s.btn}</button>
    </div>`).join('');
  const refTitle = ov.titles?.reference || ov.project.name;
  const newTitle = ov.titles?.new || '';
  const warnings = (ov.warnings || []).slice(-4).reverse().map((w) =>
    `<div class="notice">${esc(w.text)}<span class="tiny muted" style="float:right">${esc(w.time)}</span></div>`).join('');
  const needSetup = !S.config?.stage_models?.write;
  const setupNotice = needSetup
    ? `<div class="notice">还没有选择写作模型。先去设置里填 API Key、选好五个环节的模型，再开始拆书和写作。
        <button class="btn sm" data-act="go" data-view="settings" style="margin-left:8px">去设置</button></div>`
    : '';
  const tasks = (S.tasks || []).slice(0, 5).map(taskRowMini).join('') || '<div class="muted tiny">还没有任务</div>';

  $('#view-dashboard').innerHTML = `
    <div class="page-head">
      <div>
        <div class="page-eyebrow">NOVEL FORGE / 参考书 → 新书</div>
        <h1>${esc(refTitle)}</h1>
        <p class="page-desc">参考书 ${esc(ov.project.source_name || '—')} · ${num(st.source_chapters)} 章 · ${wan(st.source_words)} 字 · 每批 ${st.batch_size} 章。参考书只拆手法，正文写的是新书。</p>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px">
          <span class="chip accent">参考书《${esc(refTitle)}》· 只拆不抄</span>
          <span class="muted">→</span>
          <span class="chip ${newTitle ? 'ok' : ''}">${newTitle ? `新书《${esc(newTitle)}》· 原创产出` : '新书：待生成'}</span>
        </div>
      </div>
      <div class="btn-row">
        <button class="btn ghost sm" data-act="rename-project">重命名参考书</button>
        <button class="btn ghost sm danger" data-act="delete-project">删除项目</button>
      </div>
    </div>

    <div class="grid c5" style="margin-bottom:16px">
      ${statCard('源章节', num(st.source_chapters), wan(st.source_words) + ' 字')}
      ${statCard('细纲进度', `${st.detail_done}/${st.detail_total}`, `共 ${st.detail_total} 批`, 'accent')}
      ${statCard('正文章节', `${st.written}`, st.plan_total ? `计划 ${st.plan_total} 章` : '暂无计划')}
      ${statCard('已写字数', wan(st.written_words), '生成正文')}
      ${statCard('AIGC 均分', st.avg_zhuque ?? '—', st.max_zhuque ? `最高 ${st.max_zhuque}` : '未检测')}
    </div>

    ${setupNotice}
    ${warnings}

    <div class="grid c2">
      <div class="card">
        <div class="card-head"><h2>生产流水线</h2><span class="chip accent">断点续跑</span></div>
        <div class="steps">${stepsHtml}</div>
      </div>
      <div>
        <div class="card">
          <div class="card-head"><h2>最近任务</h2><button class="btn sm ghost" data-act="go" data-view="tasks">任务中心</button></div>
          ${tasks}
        </div>
        <div class="card">
          <div class="card-head"><h2>快捷操作</h2></div>
          <div class="btn-row">
            <button class="btn sm" data-act="analyze-next">拆下一批</button>
            <button class="btn sm" data-act="write-next">写下一批</button>
            <button class="btn sm" data-act="polish-all">全书优化</button>
            <button class="btn sm" data-act="check-all">朱雀全检</button>
          </div>
          <p class="tiny muted" style="margin:12px 0 0">
            每批严格 5 章；批与批之间自动生成承接卡，人物状态、伏笔、上一章结尾都会带入下一批。
          </p>
        </div>
      </div>
    </div>`;
}

function statCard(k, v, s, cls = '') {
  return `<div class="stat ${cls}"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${esc(s)}</div></div>`;
}

function taskRowMini(t) {
  const [label, cls] = STATUS[t.status] || [t.status, ''];
  return `<div class="list-row">
    <span class="chip ${cls}">${esc(label)}</span>
    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.title || t.type)}</span>
    <span class="tiny muted mono">${t.total ? Math.round((t.progress / t.total) * 100) + '%' : ''}</span>
  </div>`;
}

// ------------------------------------------------------------ 拆书
function renderAnalyze() {
  const ov = S.overview;
  if (!ov) return;
  const st = ov.stats;
  const details = new Map((S.assets?.details || []).map((d) => [d.batch, d.data]));
  const mod = S.assets?.modules;
  const preflight = S.assets?.preflight;
  const quality = S.assets?.quality_report;
  const pct = st.detail_total ? Math.round((st.detail_done / st.detail_total) * 100) : 0;
  const batches = (ov.batches.detail || []).map((b) => {
    const data = details.get(b.batch_idx);
    const open = S.openBatches.has(b.batch_idx);
    const first = data?.chapters?.[0];
    const last = data?.chapters?.[data.chapters.length - 1];
    const range = data?.chapters?.length
      ? `${first?.title || ''} — ${last?.title || ''}`
      : `第 ${b.batch_idx * st.batch_size + 1}-${(b.batch_idx + 1) * st.batch_size} 章`;
    return `
      <div class="batch ${open ? 'open' : ''}">
        <div class="batch-head" data-act="toggle-batch" data-batch="${b.batch_idx}">
          <span class="batch-title">第 ${b.batch_idx + 1} 批</span>
          ${statusChip(b.status)}
          ${data?.partial_output && data?.analysis_mode !== 'framework' ? '<span class="chip warn">已回收部分摘要</span>' : ''}
          ${data?.analysis_mode === 'framework' ? '<span class="chip warn">仅保留章节框架</span>' : ''}
          <span class="batch-sub" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(range)}</span>
          <span class="tiny muted mono">${data ? data.chapters?.length || 0 : 0} 章</span>
          <button class="btn sm ghost" data-act="analyze-batch" data-batch="${b.batch_idx}">${b.status === 'done' ? '重拆' : '拆这批'}</button>
        </div>
        <div class="batch-body">
          ${b.error ? `<div class="notice ${b.status === 'done' ? 'warn' : 'bad'}">${esc(b.error)}</div>` : ''}
          ${data ? renderDetail(data) : '<div class="muted tiny" style="padding:10px 0">还没有细纲。点右上角「拆这批」。</div>'}
        </div>
      </div>`;
  }).join('') || '<div class="empty">还没有批次。上传项目后会自动按 5 章拆分。</div>';

  $('#view-analyze').innerHTML = `
    <div class="page-head">
      <div>
        <div class="page-eyebrow">NOVEL FORGE / 02 DECONSTRUCT</div>
        <h1>拆参考书 · 5 章一批</h1>
        <p class="page-desc">正在拆解参考书《${esc(ov.titles?.reference || ov.project.name)}》。每批生成细纲（章节节拍、冲突、爽点、钩子），并增量更新全书档案。拆的是别人的书，产出的是你自己的新书。</p>
      </div>
      <div class="btn-row">
        <button class="btn" data-act="analyze-next">继续下一批</button>
        <button class="btn primary" data-act="analyze-all">自动跑完</button>
        <button class="btn" data-act="analysis-finish">本批结束后转大纲</button>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>原文预检</h2><span class="hint">上传时自动检查分章和文本长度</span></div>
      ${preflight ? `
        <div class="grid c4">
          ${statCard('原文长度', num(preflight.source_chars), '字符')}
          ${statCard('平均章节', num(preflight.average_chapter_chars), '字符')}
          ${statCard('短章 / 空章', num(preflight.empty_chapters), '少于 30 字')}
          ${statCard('分章方式', preflight.fallback_split ? '自动分块' : '识别章节标题', `${preflight.batch_count} 个批次`, preflight.fallback_split ? 'warn' : 'ok')}
        </div>
        ${preflight.warnings?.length ? `<div class="notice ${preflight.fallback_split || preflight.empty_chapters ? 'warn' : ''}" style="margin-top:12px">${preflight.warnings.map(esc).join('<br>')}</div>` : '<div class="notice ok" style="margin-top:12px">预检未发现明显分章问题，可以开始拆书。</div>'}
      ` : '<div class="empty">该项目创建较早，没有上传预检记录；拆书质量报告仍可正常查看。</div>'}
    </div>

    <div class="card">
      <div class="card-head"><h2>细纲进度</h2>
        <span class="mono tiny muted">${st.detail_done} / ${st.detail_total} 批 · ${pct}%</span>
      </div>
      <div class="bar ${pct === 100 ? 'ok' : ''}"><i style="width:${pct}%"></i></div>
      <div class="grid c4" style="margin-top:16px">
        ${statCard('源章节', num(st.source_chapters), wan(st.source_words) + ' 字')}
        ${statCard('已完成批次', `${st.detail_done}`, `共 ${st.detail_total} 批`)}
        ${statCard('六维分析', mod ? '已完成' : '待生成', mod ? '可查看下方' : '拆完自动生成', mod ? 'accent' : '')}
        ${statCard('拆书队列', '逐批处理', '上一批落库后再开始下一批')}
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>拆书质量报告</h2><span class="hint">自动检查章节覆盖、失败批次和档案状态</span></div>
      ${quality ? `<div class="grid c4">
        ${statCard('章节覆盖', `${quality.coverage_percent}%`, `${quality.covered_chapters} / ${quality.chapter_count} 章`)}
        ${statCard('未完成批次', `${quality.pending_batches?.length || 0}`, '可继续或重试')}
        ${statCard('无有效细纲', `${quality.invalid_detail_batches?.length || 0}`, '完全没有可用分析', quality.invalid_detail_batches?.length ? 'warn' : 'ok')}
        ${statCard('全书档案', quality.archive_ready ? '已生成' : '待生成', quality.modules_ready ? '六维分析已生成' : '六维分析待生成')}
      </div>
      ${quality.retry_batches?.length ? `<div class="notice warn" style="margin-top:12px">建议重试批次：${quality.retry_batches.map((b) => `第 ${b + 1} 批`).join('、')}。完整细纲会复用，只重新处理失败的档案合并。</div>` : ''}
      ${quality.coverage_gap_batches?.length ? `<div class="tiny muted" style="margin-top:8px">提示：${quality.coverage_gap_batches.map((b) => `第 ${b + 1} 批`).join('、')} 的章节编号或数量与原文分章不完全一致，但已有内容会照常用于档案和六维分析。</div>` : ''}
      ${quality.raw_output_keys?.length ? `<div class="tiny muted" style="margin-top:8px">失败时保留了原始输出：${quality.raw_output_keys.map(esc).join('、')}</div>` : ''}` : '<div class="empty">报告尚未生成。</div>'}
    </div>

    <div class="card">
      <div class="card-head"><h2>批次队列</h2><span class="hint">点批次标题展开细纲</span></div>
      ${batches}
    </div>

    <div class="card">
      <div class="card-head"><h2>六维分析</h2><span class="hint">拆完全部批次后自动汇总</span></div>
      ${mod ? renderModules(mod) : '<div class="empty">还没有六维分析。完成全部细纲后会自动生成。</div>'}
    </div>

    <div class="card">
      <div class="card-head"><h2>全书档案</h2><span class="hint">人物、设定、伏笔、文风的滚动汇总</span></div>
      ${S.assets?.archive
        ? `<details class="raw" open><summary>展开 / 收起档案 JSON</summary><pre>${esc(JSON.stringify(S.assets.archive, null, 2))}</pre></details>`
        : '<div class="empty">还没有档案。拆完第一批后生成。</div>'}
    </div>`;
}

function renderDetail(data) {
  if (!data) return '';
  const chapters = (data.chapters || []).map((ch) => `
    <div class="chapter-line">
      <div class="t">${esc(ch.idx)}. ${esc(ch.title)}</div>
      <div class="d">${esc(ch.summary)}</div>
      ${ch.conflict ? `<div class="kv"><b>冲突</b>${esc(ch.conflict)}</div>` : ''}
      ${ch.payoff ? `<div class="kv"><b>爽/虐点</b>${esc(ch.payoff)}</div>` : ''}
      ${ch.hook ? `<div class="kv"><b>钩子</b>${esc(ch.hook)}</div>` : ''}
    </div>`).join('');
  const n = data.batch_notes || {};
  const notes = [
    ['节奏', n.pacing], ['对白', n.dialogue], ['写法', n.technique],
    ['设定', n.worldbuilding], ['重复套路', n.patterns],
  ].filter(([, v]) => v).map(([k, v]) => `<div class="kv"><b>${k}</b>${esc(v)}</div>`).join('');
  const tks = (data.takeaways || []).map((t) => `<li>${esc(t)}</li>`).join('');
  return `
    ${chapters}
    ${notes ? `<div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">${notes}</div>` : ''}
    ${tks ? `<div style="margin-top:10px"><div class="tiny muted">可借鉴写法</div><ul class="tiny" style="margin:6px 0 0;color:var(--muted)">${tks}</ul></div>` : ''}`;
}

const MODULE_LABELS = {
  characters: '人物', plot: '剧情与节奏', dialogue: '对白',
  style: '写作手法与文笔', world: '世界观与设定', reader: '读者层面',
};
function renderModules(mod) {
  return `<div class="grid c2">${Object.entries(MODULE_LABELS).map(([k, label]) => `
    <div class="card module">
      <h3><span class="dot"></span>${label}</h3>
      <p>${esc(mod[k] || '（暂无）')}</p>
    </div>`).join('')}</div>`;
}

// ------------------------------------------------------------ 原创大纲
function defaultReq() {
  return { title: '', genre: '', hero: '', hook: '', chapters: 40, words: 2600,
    style: '', dialogue: '', pacing: '', world: '', taboo: '', extra: '' };
}

function parseRequirement(text) {
  S.req = S.req || defaultReq();
  const map = {
    '书名': 'title', '类型': 'genre', '题材': 'genre', '主角设定': 'hero', '核心看点': 'hook',
    '总章数': 'chapters', '每章字数': 'words', '文风': 'style', '对白特点': 'dialogue',
    '节奏要求': 'pacing', '世界观要求': 'world', '禁忌': 'taboo', '补充要求': 'extra',
  };
  let matched = false;
  (text || '').split('\n').forEach((line) => {
    const m = line.match(/^【(.+?)】\s*(.*)$/);
    if (!m) return;
    const key = map[m[1].trim()];
    if (!key) return;
    matched = true;
    S.req[key] = ['chapters', 'words'].includes(key) ? Number(m[2]) || S.req[key] : m[2];
  });
  if (!matched) S.req.extra = text;
}

function composeRequirement() {
  const r = S.req;
  const pairs = [
    ['书名', r.title], ['类型', r.genre], ['主角设定', r.hero], ['核心看点', r.hook],
    ['总章数', r.chapters], ['每章字数', r.words], ['文风', r.style], ['对白特点', r.dialogue],
    ['节奏要求', r.pacing], ['世界观要求', r.world], ['禁忌', r.taboo], ['补充要求', r.extra],
  ];
  return pairs.filter(([, v]) => String(v || '').trim())
    .map(([k, v]) => `【${k}】${String(v).trim()}`).join('\n');
}

function reqField(key, label, placeholder, opts = {}) {
  const v = S.req[key] ?? '';
  if (opts.area) {
    return `<label class="field"><span>${label}</span>
      <textarea class="editor short" data-req="${key}" placeholder="${esc(placeholder)}" style="min-height:${opts.h || 74}px">${esc(v)}</textarea></label>`;
  }
  return `<label class="field"><span>${label}</span>
    <input type="${opts.number ? 'number' : 'text'}" data-req="${key}" value="${esc(v)}" placeholder="${esc(placeholder)}"></label>`;
}

function renderOutline() {
  const o = S.outline;
  const has = o && o.content;
  const newTitle = currentNewTitle();
  $('#view-outline').innerHTML = `
    <div class="page-head">
      <div>
        <div class="page-eyebrow">NOVEL FORGE / 03 OUTLINE</div>
        <h1>原创大纲 · 新书</h1>
        <p class="page-desc">参考书《${esc(refTitle())}》只提供结构、节奏、爽点机制和写法。下面生成的是另一本书：新书名、新人物、新世界观、新主线，不复用参考书的专有名词和具体桥段。</p>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px">
          <span class="chip accent">参考书《${esc(refTitle())}》</span>
          <span class="muted">→</span>
          <span class="chip ${newTitle ? 'ok' : ''}">${newTitle ? `新书《${esc(newTitle)}》` : '新书：尚未生成'}</span>
        </div>
      </div>
      <div class="btn-row">
        ${has ? '<button class="btn ghost" data-act="outline-save">保存修改</button>' : ''}
        <button class="btn primary" data-act="outline-generate">${has ? '按新要求重新生成' : '生成原创大纲'}</button>
        ${has ? '<button class="btn" data-act="plan-repair">补提取章节计划</button>' : ''}
      </div>
    </div>

    <div class="notice">可以只拆部分批次就生成大纲，无需拆完整本，也无需等待六维分析。当前有 ${S.overview?.stats?.detail_available || S.overview?.stats?.detail_done || 0} 批可用细纲；生成大纲时，会让正在运行的拆书在当前批次落库后结束。</div>

    <div class="card">
      <div class="card-head">
        <h2>原创要求</h2>
        <span class="hint">这里填的是<b>新书</b>的要求；参考书只用来学手法，不会照搬</span>
      </div>
      <div class="grid c2">
        ${reqField('title', '新书书名', '例：夜行者（不要用参考书的书名）')}
        ${reqField('genre', '类型 / 题材', '例：都市异能 + 悬疑')}
        ${reqField('chapters', '总章数', '40', { number: true })}
        ${reqField('words', '每章目标字数', '2600', { number: true })}
        ${reqField('hero', '新书主角设定（全新人物）', '身份、性格、欲望、执念、弱点', { area: true })}
        ${reqField('hook', '核心看点', '读者追这本书的理由，最爽的三个点', { area: true })}
        ${reqField('style', '文风要求', '例：冷峻克制，短句，少成语，画面感强', { area: true, h: 64 })}
        ${reqField('dialogue', '对白特点', '例：主角话少且噎人，配角市井口语', { area: true, h: 64 })}
        ${reqField('pacing', '节奏要求', '例：三章一个小爽点，十章一个反转，不写日常注水', { area: true, h: 64 })}
        ${reqField('world', '世界观要求', '力量体系、规则、时代背景；没有可留空', { area: true, h: 64 })}
        ${reqField('taboo', '禁忌（绝对不能写）', '例：不写系统流、不写后宫、主角不圣母', { area: true, h: 64 })}
        ${reqField('extra', '补充要求', '其他想法，或直接粘贴完整要求', { area: true, h: 64 })}
      </div>
    </div>

    ${has ? `
      <div class="card">
        <div class="card-head">
          <h2>大纲正文</h2>
          <span class="chip ${o.status === 'edited' ? 'warn' : 'accent'}">${o.status === 'edited' ? '已手动编辑' : '模型生成'}</span>
        </div>
        <textarea class="editor" id="outlineEditor" style="min-height:620px">${esc(o.content)}</textarea>
      </div>
      <div class="card">
        <div class="card-head">
          <h2>章节计划</h2>
          <span class="hint">写作队列按此表每 5 章一批；共 ${S.plan.length} 章</span>
        </div>
        ${S.plan.length && /<!--\s*PLAN/i.test(o.content) && !/PLAN\s*-->/i.test(o.content) ? '<div class="notice">大纲结尾可能被截断，已恢复其中可识别的章节计划，可以按这些计划开始写作；如需更多章节，可补充大纲后保存。</div>' : ''}
        ${S.plan.length ? `
          <div style="max-height:420px;overflow:auto">
            <table class="tbl">
              <thead><tr><th style="width:52px">章</th><th style="width:150px">标题</th><th>核心事件</th><th style="width:160px">爽点 / 冲突</th><th style="width:180px">结尾钩子</th></tr></thead>
              <tbody>${S.plan.map((r) => `<tr>
                <td class="mono">${r.idx}</td><td>${esc(r.title)}</td><td>${esc(r.event)}</td>
                <td>${esc(r.payoff)}</td><td>${esc(r.hook)}</td></tr>`).join('')}</tbody>
            </table>
          </div>` : `<div class="notice">现有大纲还未整理成章节计划，点击「补提取章节计划」即可。直接开始写作时也会自动补提取，无需拆完整本。</div>`}
      </div>` : `
      <div class="empty">
        <strong>还没有原创大纲</strong>
        <div>填好上面的要求，点右上角生成。模型会先聚合细纲和档案，再写一本全新的书。</div>
      </div>`}
  `;
}

// ------------------------------------------------------------ 创作宪法
function renderConstitution() {
  const c = S.constitution;
  const content = c?.content || '';
  const locked = !!c?.locked;
  const checks = [
    '大纲主线和章节计划已确认',
    '主角与配角性格、关系没有偏差',
    '主题与核心看点明确',
    '文风、叙事视角、对白规则可执行',
    '爽点节奏与章末钩子符合预期',
  ];
  $('#view-constitution').innerHTML = `
    <div class="page-head">
      <div>
        <div class="page-eyebrow">NOVEL FORGE / 04 CONSTITUTION</div>
        <h1>创作宪法</h1>
        <p class="page-desc">这是每次写正文时原样注入的硬约束。锁之前逐项确认，锁之后人物性格、主题、写作手法、爽点都不会跑偏。</p>
      </div>
      <div class="btn-row">
        ${locked
          ? '<button class="btn ghost" data-act="constitution-unlock">解锁修改</button>'
          : `<button class="btn ghost" data-act="constitution-generate">重新提炼</button>
             <button class="btn" data-act="constitution-save">仅保存</button>
             <button class="btn primary" data-act="constitution-lock">确认并锁定</button>`}
      </div>
    </div>

    ${!content ? `
      <div class="empty">
        <strong>还没有创作宪法</strong>
        <div style="margin-bottom:16px">先生成大纲，然后让模型提炼；也可以手动粘贴一份。</div>
        <button class="btn primary" data-act="constitution-generate">从大纲提炼</button>
      </div>` : `
      <div class="card">
        <div class="card-head">
          <h2>写前确认清单</h2>
          ${locked ? '<span class="chip ok">已锁定</span>' : '<span class="chip warn">未锁定 · 不能开始写作</span>'}
        </div>
        ${checks.map((t) => `<label class="check"><input type="checkbox" class="constitution-check" ${locked ? 'checked disabled' : ''}>${t}</label>`).join('')}
      </div>
      <div class="card">
        <div class="card-head"><h2>宪法正文</h2><span class="hint">${locked ? '已锁定，解锁后可编辑' : '可直接修改，改完记得锁定'}</span></div>
        <textarea class="editor" id="constitutionEditor" style="min-height:560px" ${locked ? 'readonly' : ''}>${esc(content)}</textarea>
      </div>`}
  `;
}

// ------------------------------------------------------------ 写作
function renderWrite() {
  const ov = S.overview;
  if (!ov) return;
  const st = ov.stats;
  const newTitle = currentNewTitle();
  const locked = st.constitution_locked;
  const genMap = new Map(S.generated.map((g) => [g.idx, g]));
  const size = st.batch_size || 5;
  const plan = S.plan || [];
  const groups = [];
  for (let i = 0; i < plan.length; i += size) groups.push(plan.slice(i, i + size));
  const pct = st.plan_total ? Math.round((st.written / st.plan_total) * 100) : 0;
  const warnings = (ov.warnings || []).slice(-5).reverse().map((w) => `<div class="notice">${esc(w.text)}</div>`).join('');

  const groupsHtml = groups.map((g, b) => {
    const label = `第 ${b + 1} 批 · 第 ${g[0].idx}-${g[g.length - 1].idx} 章`;
    const done = g.every((r) => genMap.get(r.idx));
    const ctx = (S.contexts || []).find((c) => c.after_batch === b);
    const devs = ctx?.data?.deviations || [];
    return `
      <div class="card" style="padding:16px 18px">
        <div class="card-head" style="margin-bottom:10px">
          <div style="display:flex;align-items:center;gap:10px">
            <h3>${label}</h3>
            ${done ? '<span class="chip ok">已完成</span>' : '<span class="chip">待写</span>'}
            ${devs.length ? `<span class="chip bad">${devs.length} 条偏离</span>` : ''}
          </div>
          <div class="btn-row">
            <button class="btn sm ghost" data-act="write-batch" data-batch="${b}">整批重生成</button>
            ${done ? `<button class="btn sm ghost" data-act="context-rebuild" data-batch="${b}">${devs.length ? '重新承接并定位偏离' : '重建承接卡'}</button>` : ''}
          </div>
        </div>
        <div class="chapter-grid">
          ${g.map((r) => {
            const gen = genMap.get(r.idx);
            if (!gen) return `<div class="chapter-card" style="opacity:.5;cursor:default">
              <div class="idx">CH.${String(r.idx).padStart(2, '0')}</div>
              <div class="name">${esc(r.title)}</div>
              <div class="meta"><span class="chip">待写</span></div>
              <div class="excerpt">${esc(r.event)}</div></div>`;
            const score = gen.zhuque_score;
            const sCls = score == null ? '' : (score <= (S.config?.zhuque?.pass_threshold || 20) ? 'ok' : 'bad');
            return `<div class="chapter-card" data-act="open-chapter" data-id="${gen.id}">
              <div class="idx">CH.${String(gen.idx).padStart(2, '0')}</div>
              <div class="name">${esc(gen.title || r.title)}</div>
              <div class="meta">
                ${statusChip(gen.status)}
                <span>${num(gen.words)} 字</span>
                ${score != null ? `<span class="chip ${sCls}">AIGC ${score}</span>` : ''}
              </div>
              <div class="excerpt">${esc(gen.excerpt || '')}</div>
            </div>`;
          }).join('')}
        </div>
        ${devs.length ? `<div class="notice bad" style="margin-top:12px">
          <strong>偏离章节定位</strong>
          <div style="margin-top:8px">${devs.map((d) => {
            const chapter = d.chapter ? `第 ${d.chapter} 章` : '未定位到具体章节（点击重新承接）';
            return `<div style="margin-top:7px"><b>${esc(chapter)} · ${esc(d.level || '中')}</b>：${esc(d.issue)}${d.evidence ? `<br><span class="tiny">证据：${esc(d.evidence)}</span>` : ''}${d.suggestion ? `<br><span class="tiny">拉回：${esc(d.suggestion)}</span>` : ''}</div>`;
          }).join('')}</div>
        </div>` : ''}
        ${ctx ? `<details class="raw" style="margin-top:12px"><summary>查看本批承接卡</summary>
          <pre>${esc(ctx.data?.context_card || '')}</pre>
        </details>` : ''}
      </div>`;
  }).join('') || (S.outline?.content
    ? '<div class="notice">已有大纲，开始写作时会自动整理章节计划；也可到「原创大纲」点击「补提取章节计划」。无需继续拆书。</div>'
    : '<div class="empty">还没有新书大纲。先去「原创大纲」，使用已有拆书结果生成即可。</div>');

  $('#view-write').innerHTML = `
    <div class="page-head">
      <div>
        <div class="page-eyebrow">NOVEL FORGE / 05 WRITING</div>
        <h1>正文写作${newTitle ? ` · 《${esc(newTitle)}》` : ' · 5 章一批'}</h1>
        <p class="page-desc">写的是新书${newTitle ? `《${esc(newTitle)}》` : ''}，参考书《${esc(refTitle())}》只用来对齐手法与节奏。逐章生成，写完立刻过朱雀；一批写完生成承接卡，把人物状态、伏笔、结尾钩子带进下一批。</p>
      </div>
      <div class="btn-row">
        <button class="btn" data-act="write-next" ${locked ? '' : 'disabled'}>继续下一批（5章）</button>
        <button class="btn primary" data-act="write-all" ${locked ? '' : 'disabled'}>自动写完</button>
      </div>
    </div>

    ${!locked ? `<div class="notice bad">创作宪法还没锁定，不能开始写作。<button class="btn sm" data-act="go" data-view="constitution" style="margin-left:10px">去确认锁定</button></div>` : ''}
    ${warnings}

    <div class="card">
      <div class="card-head">
        <h2>写作进度</h2>
        <span class="mono tiny muted">${st.written} / ${st.plan_total || '—'} 章 · ${pct}%</span>
      </div>
      <div class="bar ${pct === 100 ? 'ok' : ''}"><i style="width:${pct}%"></i></div>
      <div class="grid c4" style="margin-top:16px">
        ${statCard('已写章节', `${st.written}`, st.plan_total ? `计划 ${st.plan_total} 章` : '无计划')}
        ${statCard('总字数', wan(st.written_words), `目标 ${num(st.plan_total * (S.req.words || 2600))} 字`)}
        ${statCard('AIGC 均分', st.avg_zhuque ?? '—', '朱雀检测')}
        ${statCard('批次', `${Math.min(Math.floor(st.written / size) + (st.written % size ? 1 : 0), groups.length)} / ${groups.length}`, `每批 ${size} 章`)}
      </div>
    </div>

    ${groupsHtml}

    ${S.generated.length ? `<div class="card">
      <div class="card-head"><h2>整书操作</h2></div>
      <div class="btn-row">
        <button class="btn sm" data-act="check-all">朱雀全书检测</button>
        <button class="btn sm" data-act="polish-all">全书优化</button>
        <button class="btn sm" data-act="go" data-view="polish">导出成稿</button>
      </div>
    </div>` : ''}
  `;
}

// ------------------------------------------------------------ 优化导出
function renderPolish() {
  const ov = S.overview;
  if (!ov) return;
  const rows = S.generated || [];
  const polishedCount = rows.filter((r) => r.has_polished).length;
  $('#view-polish').innerHTML = `
    <div class="page-head">
      <div>
        <div class="page-eyebrow">NOVEL FORGE / 06 POLISH & EXPORT</div>
        <h1>全书优化与导出</h1>
        <p class="page-desc">逐章统一文风、修衔接、强化钩子、去 AI 味；不改剧情。优化稿会单独保存，导出时可选择用哪一版。</p>
      </div>
      <div class="btn-row">
        <button class="btn" data-act="polish-all">开始全书优化</button>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>优化范围</h2><span class="hint">留空表示全部章节</span></div>
      <div class="inline">
        <label class="field"><span>起始章</span><input type="number" id="polishStart" placeholder="1"></label>
        <label class="field"><span>结束章</span><input type="number" id="polishEnd" placeholder="最后一章"></label>
        <button class="btn" data-act="polish-all" style="flex:0 0 auto">优化该范围</button>
      </div>
      <div class="grid c3" style="margin-top:6px">
        ${statCard('已生成章节', `${rows.length}`, '正文')}
        ${statCard('已优化章节', `${polishedCount}`, polishedCount === rows.length ? '全部完成' : '可继续优化')}
        ${statCard('AIGC 均分', ov.stats.avg_zhuque ?? '—', '朱雀检测')}
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>章节列表</h2>
        <div class="btn-row">
          <button class="btn sm ghost" data-act="check-all">朱雀全书检测</button>
        </div>
      </div>
      ${rows.length ? `<div style="max-height:520px;overflow:auto"><table class="tbl">
        <thead><tr><th style="width:50px">章</th><th>标题</th><th style="width:80px">字数</th><th style="width:110px">状态</th><th style="width:90px">AIGC</th><th style="width:90px">优化稿</th></tr></thead>
        <tbody>${rows.map((r) => `<tr data-act="open-chapter" data-id="${r.id}" style="cursor:pointer">
          <td class="mono">${r.idx}</td><td>${esc(r.title)}</td><td>${num(r.words)}</td>
          <td>${statusChip(r.status)}</td><td>${r.zhuque_score ?? '—'}</td>
          <td>${r.has_polished ? '<span class="chip ok">有</span>' : '<span class="muted">无</span>'}</td>
        </tr>`).join('')}</tbody></table></div>` : '<div class="empty">还没有正文。先去写作页生成。</div>'}
    </div>

    <div class="card">
      <div class="card-head"><h2>导出成稿</h2></div>
      <label class="check"><input type="checkbox" id="usePolished" checked>优先使用优化稿（没有优化稿的章节自动用原稿）</label>
      <div class="btn-row" style="margin-top:12px">
        <button class="btn primary" data-act="export" data-fmt="txt">导出 TXT</button>
        <button class="btn" data-act="export" data-fmt="md">导出 Markdown</button>
      </div>
      <p class="tiny muted" style="margin:10px 0 0">导出文件保存在浏览器下载目录，包含书名与全部章节。</p>
    </div>
  `;
}

// ------------------------------------------------------------ 任务中心
function renderTasks() {
  const rows = S.tasks || [];
  $('#view-tasks').innerHTML = `
    <div class="page-head">
      <div>
        <div class="page-eyebrow">NOVEL FORGE / 07 QUEUE</div>
        <h1>任务中心</h1>
        <p class="page-desc">所有批次按顺序执行，同一时间只跑一个任务，避免触发模型限流。暂停后当前章写完即停，随时可继续。</p>
      </div>
      <div class="btn-row"><button class="btn ghost" onclick="location.reload()">刷新</button></div>
    </div>
    <div class="card">
      ${rows.length ? `<table class="tbl">
        <thead><tr><th style="width:44px">#</th><th style="width:190px">任务</th><th style="width:88px">状态</th><th style="width:180px">进度</th><th>信息</th><th style="width:150px">操作</th></tr></thead>
        <tbody>${rows.map((t) => {
          const pct = t.total ? Math.round((t.progress / t.total) * 100) : null;
          const [label, cls] = STATUS[t.status] || [t.status, ''];
          return `<tr>
            <td class="mono muted">${t.id}</td>
            <td>${esc(t.title || t.type)}<div class="tiny muted">${esc(t.created_at || '')}</div></td>
            <td><span class="chip ${cls}">${esc(label)}</span></td>
            <td>${pct !== null ? `<div class="bar" style="margin-bottom:4px"><i style="width:${pct}%"></i></div><span class="tiny muted mono">${t.progress}/${t.total}</span>` : '<span class="muted tiny">—</span>'}</td>
            <td class="tiny ${t.error ? '' : 'muted'}" style="max-width:320px;white-space:normal">${esc(t.error || t.message || '')}</td>
            <td><div class="btn-row">
              ${['running', 'pending'].includes(t.status) ? `<button class="btn sm ghost" data-act="task-pause" data-id="${t.id}">暂停</button>` : ''}
              ${['paused', 'failed'].includes(t.status) ? `<button class="btn sm" data-act="task-resume" data-id="${t.id}">继续</button>` : ''}
              ${['pending', 'running', 'paused'].includes(t.status) ? `<button class="btn sm ghost danger" data-act="task-cancel" data-id="${t.id}">取消</button>` : ''}
            </div></td>
          </tr>`;
        }).join('')}</tbody></table>` : '<div class="empty">还没有任务记录。</div>'}
    </div>`;
}

// ------------------------------------------------------------ 设置
function renderSettings() {
  const cfg = S.config;
  if (!cfg) return;
  const providers = cfg.providers || [];
  const stageDefs = [
    ['analyze', '拆书 / 六维分析'],
    ['outline', '原创大纲 / 创作宪法'],
    ['write', '正文写作'],
    ['check', '承接卡 / 偏离检测'],
    ['polish', '去 AIGC / 全书优化'],
  ];
  const modelOptions = [];
  providers.forEach((p) => (p.models || []).forEach((m) => {
    modelOptions.push({ value: `${p.id}::${m}`, label: `${p.name || p.id} · ${m}` });
  }));
  const zone = cfg.zhuque || {};
  const bal = cfg.balance || {};
  const wr = cfg.writing || {};

  $('#view-settings').innerHTML = `
    <div class="page-head">
      <div>
        <div class="page-eyebrow">NOVEL FORGE / 08 SETTINGS</div>
        <h1>全局设置</h1>
        <p class="page-desc">只区分两种协议：OpenAI 兼容（DeepSeek、通义、Kimi、豆包、自建中转都算）和 Anthropic 原生。平台、地址、模型名全部自己填，不绑定任何厂商；每个环节可以单独指定模型。</p>
      </div>
      <div class="btn-row"><button class="btn primary" data-act="settings-save">保存全部设置</button></div>
    </div>

    <div class="settings-block">
      <div class="settings-title">MODEL PROVIDERS</div>
      ${providers.map(renderProvider).join('') || '<div class="empty" style="padding:24px"><strong>还没有模型供应商</strong><div>点下面「添加供应商」，填接口地址、Key 和模型名即可。</div></div>'}
      <button class="btn ghost" data-act="provider-add">+ 添加供应商</button>
      <p class="tiny muted" style="margin:10px 0 0">提示：填完先点右上角「保存全部设置」，下面的「分环节模型」下拉里才会出现新模型。</p>
    </div>

    <div class="settings-block">
      <div class="settings-title">STAGE MODELS · 分环节模型</div>
      <div class="card">
        <div class="grid c2">
          ${stageDefs.map(([key, label]) => `
            <label class="field"><span>${label}</span>
              <select data-path="stage_models.${key}">
                <option value="">（未设置）</option>
                ${modelOptions.map((o) => `<option value="${esc(o.value)}" ${cfg.stage_models?.[key] === o.value ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}
              </select>
            </label>`).join('')}
        </div>
        <p class="tiny muted" style="margin:0">承接卡与偏离检测留空时，会自动使用「正文写作」的模型；去 AIGC 留空时同样回退到写作模型。</p>
      </div>
    </div>

    <div class="settings-block">
      <div class="settings-title">BALANCE · 余额预警</div>
      <div class="card">
        <div class="grid c3">
          <label class="field"><span>当前余额</span><input type="number" step="0.01" data-path="balance.amount" value="${bal.amount ?? 0}"></label>
          <label class="field"><span>预警阈值（默认 3）</span><input type="number" step="0.01" data-path="balance.warning_threshold" value="${bal.warning_threshold ?? 3}"></label>
          <label class="field"><span>币种</span><input type="text" data-path="balance.currency" value="${esc(bal.currency || '元')}"></label>
        </div>
        <label class="field"><span>模型价格（JSON：元 / 百万 token，用于自动扣减余额）</span>
          <textarea class="editor short mono" id="pricesJson" style="min-height:110px" placeholder='{"claude-sonnet-4-5": {"input": 21, "output": 105}}'>${esc(JSON.stringify(bal.prices || {}, null, 2))}</textarea>
        </label>
        <p class="tiny muted">已累计消耗：¥ ${Number(S.spent || 0).toFixed(2)}。余额为 0 时不自动扣减，只记录消耗。</p>
      </div>
    </div>

    <div class="settings-block">
      <div class="settings-title">ZHUQUE · 朱雀检测</div>
      <div class="card">
        <label class="check"><input type="checkbox" data-path="zhuque.enabled" ${zone.enabled ? 'checked' : ''}>启用朱雀检测（每章生成后自动检测，超标自动降重写）</label>
        <label class="field" style="margin-top:12px"><span>接入方式</span>
          <select data-path="zhuque.adapter">
            <option value="makers" ${zone.adapter === 'makers' ? 'selected' : ''}>腾讯云官方 · Makers 朱雀（只需 API Key）</option>
            <option value="generic" ${!['tencent', 'makers'].includes(zone.adapter) ? 'selected' : ''}>通用检测接口（兼容原有配置）</option>
            <option value="tencent" ${zone.adapter === 'tencent' ? 'selected' : ''}>腾讯云官方 · EdgeOne 朱雀文本检测</option>
          </select>
        </label>
        <div class="grid c2" style="margin-top:12px">
          <label class="field"><span>模式</span>
            <select data-path="zhuque.mode">
              <option value="api" ${zone.mode === 'api' ? 'selected' : ''}>API 自动检测</option>
              <option value="manual" ${zone.mode === 'manual' ? 'selected' : ''}>手动粘贴检测分</option>
            </select>
          </label>
          <label class="field" ${['tencent', 'makers'].includes(zone.adapter) ? 'hidden' : ''}><span>请求方法</span>
            <select data-path="zhuque.method">
              <option value="POST" ${zone.method === 'POST' ? 'selected' : ''}>POST</option>
              <option value="GET" ${zone.method === 'GET' ? 'selected' : ''}>GET</option>
            </select>
          </label>
        </div>
        ${zone.adapter === 'makers' ? '<p class="tiny muted">官方地址：https://ai-gateway.edgeone.link/v1/providers/zhuque-text/classify（已内置，无需填写）</p>' : `<label class="field"><span>${zone.adapter === 'tencent' ? 'EdgeOne 网关地址（控制台分配）' : '检测接口地址'}</span><input type="text" data-path="zhuque.endpoint" value="${esc(zone.endpoint || '')}" placeholder="填写真实服务的 https:// 地址"></label>`}
        ${['tencent', 'makers'].includes(zone.adapter) ? `
          <label class="field"><span>${zone.adapter === 'makers' ? 'Makers Models API Key' : 'EdgeOne 网关 API Key'}</span><input type="password" autocomplete="off" data-path="zhuque.api_key" value="${esc(zone.api_key || '')}" placeholder="粘贴控制台的 API Key"></label>
        ` : ''}
        ${zone.adapter === 'tencent' ? `
          <label class="field"><span>调用方式</span><select data-path="zhuque.work_mode">
            <option value="async" ${zone.work_mode !== 'sync' ? 'selected' : ''}>异步提交并自动查询（推荐）</option>
            <option value="sync" ${zone.work_mode === 'sync' ? 'selected' : ''}>同步等待检测结果</option>
          </select></label>
          <p class="tiny muted">从 <a href="https://console.cloud.tencent.com/edgeone/ai-gateway" target="_blank" rel="noopener noreferrer">EdgeOne 控制台</a>获取网关地址和密钥，需开通朱雀权限。程序会自动处理认证和分值，无需填写下面的通用接口参数。<a href="https://cloud.tencent.com/document/product/1552/135974" target="_blank" rel="noopener noreferrer">官方文档</a></p>
        ` : ''}
        ${zone.adapter === 'makers' ? '<p class="tiny muted">使用 <a href="https://console.cloud.tencent.com/edgeone/makers?tab=models&subTab=apikey" target="_blank" rel="noopener noreferrer">Makers → Models → API Key</a> 的密钥，同步返回结果，无需配置请求头和分值路径。<a href="https://cloud.tencent.com/document/product/1552/137539" target="_blank" rel="noopener noreferrer">官方文档</a></p>' : ''}
        <div ${['tencent', 'makers'].includes(zone.adapter) ? 'hidden' : ''}>
        <label class="field"><span>请求头（JSON）</span>
          <textarea class="editor short mono" id="zhuqueHeaders" style="min-height:80px" placeholder='{"Authorization": "Bearer xxx"}'>${esc(JSON.stringify(zone.headers || {}, null, 2))}</textarea>
        </label>
        <label class="field"><span>请求体模板（<code>{{text}}</code> 会被替换为章节正文）</span>
          <textarea class="editor short mono" data-path="zhuque.body_template" style="min-height:80px">${esc(zone.body_template || '{"text": "{{text}}"}')}</textarea>
        </label>
        <div class="grid c2">
          <label class="field"><span>分值路径（如 data.score）</span><input type="text" data-path="zhuque.score_path" value="${esc(zone.score_path || 'data.score')}"></label>
          <label class="field"><span>分值满分</span><input type="number" data-path="zhuque.score_scale" value="${zone.score_scale ?? 100}"></label>
        </div>
        </div>
        <div class="grid c3">
          <label class="field"><span>合格线（≤ 该分通过）</span><input type="number" data-path="zhuque.pass_threshold" value="${zone.pass_threshold ?? 20}"></label>
          <label class="field"><span>最多重写轮数</span><input type="number" data-path="zhuque.max_rounds" value="${zone.max_rounds ?? 2}"></label>
          <label class="field"><span>超时（秒）</span><input type="number" data-path="zhuque.timeout" value="${zone.timeout ?? 60}"></label>
        </div>
        <div class="btn-row"><button class="btn" data-act="zhuque-test">测试检测（一次调用）</button><span class="tiny muted">仅发送内置测试文本，按服务商规则计费；测试后记得保存设置。</span></div>
        <p id="zhuqueTestResult" class="tiny muted" role="status"></p>
      </div>
    </div>

    <div class="settings-block">
      <div class="settings-title">WRITING · 写作参数</div>
      <div class="card">
        <div class="grid c3">
          <label class="field"><span>每批章节数</span><input type="number" data-path="writing.batch_size" value="${wr.batch_size ?? 5}"></label>
          <label class="field"><span>每章目标字数</span><input type="number" data-path="writing.words_per_chapter" value="${wr.words_per_chapter ?? 2600}"></label>
          <label class="field"><span>带入上一章结尾字数</span><input type="number" data-path="writing.context_tail_chars" value="${wr.context_tail_chars ?? 1800}"></label>
        </div>
        <p class="tiny muted" style="margin:0">默认 5 章一批；修改后新建的批次按新值执行，已生成的批次不受影响。</p>
      </div>
    </div>

    <div class="settings-block">
      <div class="settings-title">DATA · 数据</div>
      <div class="card">
        <div class="list-row"><span style="flex:1">数据库</span><span class="mono tiny muted">data/novel.db</span></div>
        <div class="list-row"><span style="flex:1">配置文件</span><span class="mono tiny muted">config.json</span></div>
        <p class="tiny muted" style="margin:10px 0 0">项目数据保存在本机；调用模型和自动检测时，会发送所需文本到你配置的服务。停掉服务不会丢进度。</p>
      </div>
    </div>
  `;
}

function renderProvider(p) {
  const types = [['openai', 'OpenAI 兼容（含国模 / 自建中转）'], ['anthropic', 'Anthropic 原生']];
  const models = (p.models || []).filter((m) => String(m).trim());
  const first = models[0] || '';
  return `<div class="provider">
    <div class="provider-head">
      <div class="provider-name">${esc(p.name || '未命名供应商')} <span class="chip mono">${esc(p.id)}</span></div>
      <button class="btn sm ghost danger" data-act="provider-del" data-pid="${esc(p.id)}">删除</button>
    </div>
    <div class="grid c3">
      <label class="field"><span>名称（随便起）</span><input type="text" data-pfield="name" data-pid="${esc(p.id)}" value="${esc(p.name || '')}" placeholder="例：我的中转站"></label>
      <label class="field"><span>接口协议</span>
        <select data-pfield="type" data-pid="${esc(p.id)}">
          ${types.map(([v, l]) => `<option value="${v}" ${p.type === v ? 'selected' : ''}>${l}</option>`).join('')}
        </select>
      </label>
      <label class="field"><span>API Key</span><input type="password" data-pfield="api_key" data-pid="${esc(p.id)}" value="${esc(p.api_key || '')}" placeholder="sk-..."></label>
    </div>
    <label class="field"><span>Base URL（填到 /v1 这一层；Anthropic 填到域名即可）</span>
      <input type="text" data-pfield="base_url" data-pid="${esc(p.id)}" value="${esc(p.base_url || '')}" placeholder="https://你的地址/v1">
    </label>
    <label class="field"><span>模型列表（自由填写，逗号分隔；第一个会用于接通测试）</span>
      <input type="text" data-pfield="models" data-pid="${esc(p.id)}" value="${esc(models.join(', '))}" placeholder="model-a, model-b, model-c">
    </label>
    <div class="btn-row" style="align-items:center">
      <button class="btn sm" data-act="test-connection" data-pid="${esc(p.id)}" ${first ? '' : 'disabled'}>测试接通</button>
      <span class="tiny ${first ? 'muted' : ''}" id="testResult_${esc(p.id)}">
        ${first ? `会用「${esc(first)}」发一条测试消息` : '先填至少一个模型名，才能测试'}
      </span>
    </div>
  </div>`;
}

// ------------------------------------------------------------ 阅读 / 编辑章节
async function openReader(gid) {
  try {
    const g = await api(`/api/generated/${gid}`);
    S.reader = g;
    const zone = S.config?.zhuque || {};
    const score = g.zhuque_score;
    const stLabel = (STATUS[g.status] || [g.status])[0];
    const detection = g.detection;
    const detectionNote = detection ? (detection.matches_current
      ? `已核对本次送检正文：${num(detection.text_chars)} 字；${esc(detection.checked_at || '')}。${detection.metric === 'ratio_confidence' ? '分数表示被判为 AI / 疑似 AI 的内容占比，不代表作者身份概率。' : ''}`
      : '正文已修改，上次检测对应旧版本，请重新检测。') : '历史检测未保留送检记录；重新检测后可核对正文版本。';
    showModal(`
      <div class="modal-head">
        <div>
          <div class="page-eyebrow">CH.${String(g.idx).padStart(2, '0')} · ${esc(stLabel)}</div>
          <h2 id="readerHeading">${esc(g.title)}</h2>
          <div class="tiny muted" style="margin-top:4px">
            ${num((g.content || '').replace(/\\s/g, '').length)} 字
            ${score != null ? ` · AIGC ${score}` : ' · 未检测'}
            ${g.polished ? ' · 有优化稿' : ''}
          </div>
        </div>
        <button class="modal-close" data-act="modal-close">&times;</button>
      </div>

      <label class="field"><span>章节标题</span><input type="text" id="readerTitle" value="${esc(g.title)}"></label>

      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <div class="seg">
          <button data-act="reader-tab" data-tab="content" class="active">原稿</button>
          <button data-act="reader-tab" data-tab="polished" ${g.polished ? '' : 'disabled style="opacity:.4"'}>优化稿</button>
        </div>
        <div id="readerTab" data-tab="content" hidden></div>
        <span class="spacer"></span>
        <span class="tiny muted">直接编辑后保存，会立刻影响导出</span>
      </div>
      <textarea class="editor" id="readerContent" style="min-height:420px">${esc(g.content)}</textarea>

      <div class="grid c2" style="margin-top:14px">
        <div class="card" style="padding:14px">
          <div class="tiny muted" style="margin-bottom:6px">朱雀检测</div>
          <div class="inline">
            <input type="number" id="manualScore" placeholder="粘贴/输入检测分" value="${score ?? ''}">
            <button class="btn sm" data-act="record-score" data-id="${g.id}" style="flex:0 0 auto">记录分数</button>
            <button class="btn sm ghost" data-act="recheck-chapter" data-idx="${g.idx}" style="flex:0 0 auto">重新检测</button>
          </div>
          <div class="tiny muted" style="margin-top:6px">合格线 ≤ ${zone.pass_threshold ?? 20}${zone.mode === 'manual' ? '（当前为手动模式）' : ''}</div>
          <div class="tiny muted" style="margin-top:6px">${detectionNote}</div>
        </div>
        <div class="card" style="padding:14px">
          <div class="tiny muted" style="margin-bottom:6px">按指令重写这一章</div>
          <textarea class="editor short" id="rewriteInstruction" style="min-height:58px" placeholder="例：压缩重复解释，保留关键对白和本章事件，加强动作因果"></textarea>
          <div class="tiny muted">重写前读取大纲、本章计划和前五章正文，完成后直接保存，再按设置检测。</div>
          <button class="btn sm" data-act="rewrite-chapter" data-idx="${g.idx}" style="margin-top:8px">提交重写</button>
        </div>
      </div>

      <div class="modal-foot">
        <button class="btn ghost" data-act="modal-close">关闭</button>
        <button class="btn primary" data-act="save-chapter" data-id="${g.id}">保存修改</button>
      </div>
    `);
  } catch (err) {
    toast(err.message, 'bad');
  }
}
