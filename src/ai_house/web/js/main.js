// Shell (v2 暖灯小屋): Bearer玄关钥匙登录、五栏导航、主题灯光、路由与 15s 无感刷新。
import {$, el, put, api, icon, toast, go, leaveOk, setDirty, setLogoutHandler, setAuthTiers, getToken, setToken, clearToken,
  ava, currentActor, setCurrentActor, hasTier, isPrivateOnly, isOnboarding, setOnboarding, store, LoginNeeded} from './core.js';
import {RESIDENTS, byActor, setResidentsFromState, setResidentsFromRoster} from './residents.js';

const PAGES = {home: 'home', house: 'house', people: 'people', days: 'days', keep: 'keep', ops: 'ops', setup: 'setup'};
const PEOPLE_TABS = [['now', '此刻', 'sun'], ['wardrobe', '衣柜', 'box'], ['profile', '档案', 'book'], ['chat', '对话', 'msg']];
const THEMES = [['light', '浅色', 'sun'], ['dark', '深色', 'moon'], ['auto', '自动', 'sun']];

export function route(){
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, query] = raw.split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  return {page: parts[0] || 'home', parts, q: Object.fromEntries(new URLSearchParams(query || ''))};
}

function applyTheme(t){
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}
applyTheme(store.get('theme', 'auto'));
function setTheme(t){
  store.set('theme', t);
  applyTheme(t);
  const item = THEMES.find(x => x[0] === t) || THEMES[2];
  toast('灯光：' + item[1]);
  drawNav();
}
function cycleTheme(){
  const cur = store.get('theme', 'auto');
  const next = THEMES[(THEMES.findIndex(t => t[0] === cur) + 1) % THEMES.length];
  setTheme(next[0]);
}
const themeNow = () => THEMES.find(t => t[0] === store.get('theme', 'auto')) || THEMES[2];

function navBtn([, label, ic, hash], on){
  return el('a', {
    class: 'nav' + (on ? ' on' : ''),
    href: hash,
    'aria-current': on ? 'page' : false,
    onclick: e => { e.preventDefault(); closeAll(); go(hash); },
  }, icon(ic), el('span', {}, label));
}

function peopleTarget(actor, r){
  const curTab = r.page === 'people' && r.parts[2] ? r.parts[2] : (isPrivateOnly() ? 'chat' : 'now');
  return `#/people/${actor}/${curTab}`;
}

function drawNav(){
  const r = route(), page = PAGES[r.page] ? r.page : 'home';
  const curTheme = store.get('theme', 'auto');
  const [, tLabel, tIcon] = themeNow();
  const curRes = (page === 'people' && r.parts[1] && byActor(r.parts[1])) ? r.parts[1] : currentActor();
  const isHome = page === 'home' || page === 'house';
  const privOnly = isPrivateOnly();
  const onboarding = isOnboarding();

  const whos = !onboarding ? el('div', {class: 'whos', 'aria-label': '快速切换住客'},
    RESIDENTS.map(res => el('a', {
      href: peopleTarget(res.actor, r),
      title: res.name,
      'aria-label': res.name,
      onclick: e => { e.preventDefault(); setCurrentActor(res.actor); closeAll(); go(peopleTarget(res.actor, r)); },
    }, ava(res, 'sm', page === 'people' && curRes === res.actor ? 'on' : ''))),
    hasTier('owner') && !privOnly ? el('a', {
      href: '#/setup?add=1',
      class: 'ava-add',
      title: '邀请新住客',
      'aria-label': '邀请新住客',
      onclick: e => { e.preventDefault(); closeAll(); go('#/setup?add=1'); },
    }, icon('plus')) : null) : null;

  const themeSeg = el('div', {class: 'seg tseg', role: 'group', 'aria-label': '主题灯光'},
    THEMES.map(([k, lb]) => el('button', {
      type: 'button',
      class: curTheme === k ? 'on' : '',
      onclick: () => setTheme(k),
    }, lb)));

  const brandTarget = onboarding ? '#/setup' : (privOnly ? `#/people/${curRes}/chat` : '#/home');
  put($('#side'),
    el('a', {class: 'brand', href: brandTarget,
      onclick: e => { e.preventDefault(); closeAll(); go(brandTarget); }},
      el('span', {class: 'lamp'}, icon('home')), 'AI 小屋'),
    onboarding ? [
      navBtn(['setup', '迎接第一位住客', 'sparkle', '#/setup'], true),
      el('p', {class: 'tiny muted', style: 'padding:8px 12px;line-height:1.6;margin:4px 0'}, '创建首位住客或载入示例后，将解锁整栋小屋、衣柜、日记、记忆与群聊。'),
    ] : [
      !privOnly ? navBtn(['home', '小屋', 'home', '#/home'], isHome) : null,
      navBtn(['people', '住客', 'users', `#/people/${curRes}/${privOnly ? 'chat' : 'now'}`], page === 'people'),
      whos,
      !privOnly ? navBtn(['days', '日子', 'calendar', '#/days'], page === 'days') : null,
      !privOnly ? navBtn(['keep', '珍藏', 'bookOpen', '#/keep'], page === 'keep') : null,
      !privOnly ? navBtn(['ops', '管家', 'gear', '#/ops'], page === 'ops') : null,
    ],
    el('div', {class: 'side-foot foot'},
      themeSeg,
      el('button', {type: 'button', class: 'nav', onclick: logout}, icon('lock'), '锁门')));

  put($('#tabbar'), onboarding
    ? [
        el('button', {type: 'button', class: 'on', 'aria-label': '迎接住客', onclick: () => go('#/setup')}, icon('sparkle'), el('span', {}, '迎接住客')),
        el('button', {type: 'button', 'aria-label': '灯光 / 锁门', onclick: () => openSheet('admin')}, icon('gear'), el('span', {}, '外观')),
      ]
    : privOnly
      ? [
          ['people', '住客私聊', 'users', null, 'people'],
          ['ops', '灯光 / 锁门', 'gear', null, 'admin'],
        ].map(([k, label, ic, , sheet]) =>
          el('button', {type: 'button', class: k === 'people' ? 'on' : '', 'aria-label': label,
            onclick: () => openSheet(sheet)}, icon(ic), el('span', {}, label)))
      : [
          ['home', '小屋', 'home', '#/home'],
          ['people', '住客', 'users', null, 'people'],
          ['days', '日子', 'calendar', '#/days'],
          ['keep', '珍藏', 'bookOpen', '#/keep'],
          ['admin', '管家', 'gear', null, 'admin'],
        ].map(([k, label, ic, hash, sheet]) =>
          el('button', {
            type: 'button',
            class: ((k === 'home' && isHome) || (k === 'admin' && page === 'ops') || page === k) ? 'on' : '',
            'aria-label': label,
            onclick: () => sheet ? openSheet(sheet) : (closeAll(), go(hash)),
          }, icon(ic), el('span', {}, label))));

  put($('#mtop'),
    el('a', {class: 'brand', href: brandTarget, onclick: e => { e.preventDefault(); closeAll(); go(brandTarget); }},
      el('span', {class: 'lamp'}, icon('home')), 'AI 小屋'),
    el('span', {class: 'sp'}),
    el('button', {type: 'button', class: 'btn icon ghost', 'aria-label': '刷新', onclick: () => render()}, icon('refresh')),
    el('button', {type: 'button', class: 'btn icon ghost', 'aria-label': '切换灯光：' + tLabel, onclick: cycleTheme}, icon(tIcon)));
}

function openSheet(which){
  const sheet = $('#sheet');
  if (!sheet.classList.contains('hidden') && sheet.dataset.which === which){ closeAll(); return; }
  const r = route(), page = PAGES[r.page] ? r.page : 'home';
  const curRes = (page === 'people' && r.parts[1] && byActor(r.parts[1])) ? r.parts[1] : currentActor();
  const curTab = page === 'people' && r.parts[2] ? r.parts[2] : (isPrivateOnly() ? 'chat' : 'now');
  const curTheme = store.get('theme', 'auto');
  sheet.dataset.which = which;
  if (which === 'people'){
    put(sheet, el('div', {class: 'grab'}),
      el('div', {class: 'nav-sec'}, '选一位住客'),
      el('div', {class: 'whos big'}, RESIDENTS.map(res =>
        el('a', {
          href: `#/people/${res.actor}/${curTab}`,
          title: res.name,
          'aria-label': res.name,
          onclick: e => { e.preventDefault(); setCurrentActor(res.actor); closeAll(); go(`#/people/${res.actor}/${curTab}`); },
        }, ava(res, 'lg', res.actor === curRes ? 'on' : '')))),
      el('div', {class: 'nav-sec'}, '住客页面'),
      PEOPLE_TABS.map(([k, lb, ic]) => navBtn([k, lb, ic, `#/people/${curRes}/${k}`], page === 'people' && curTab === k)));
  } else {
    put(sheet, el('div', {class: 'grab'}),
      !isPrivateOnly() ? [
        el('div', {class: 'nav-sec'}, '管家'),
        navBtn(['ops', '控制与 MCP', 'gear', '#/ops'], page === 'ops'),
        navBtn(['house', '走进像素小屋', 'home', '#/house'], page === 'house'),
      ] : null,
      el('div', {class: 'nav-sec'}, '外观与玄关'),
      el('div', {class: 'seg tseg', role: 'group', 'aria-label': '主题灯光', style: 'display:flex;margin:6px 0 10px'},
        THEMES.map(([k, lb]) => el('button', {
          type: 'button', class: curTheme === k ? 'on' : '', style: 'flex:1;justify-content:center',
          onclick: () => { setTheme(k); closeAll(); },
        }, lb))),
      el('button', {type: 'button', class: 'nav', onclick: () => { closeAll(); logout(); }}, icon('lock'), '锁门'));
  }
  sheet.classList.remove('hidden');
  $('#shade').classList.add('open');
}
function closeAll(){
  $('#shade').classList.remove('open');
  $('#sheet').classList.add('hidden');
  delete $('#sheet').dataset.which;
}
$('#shade').addEventListener('click', closeAll);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAll(); });

/* ---------- Rendering & 15s Refresh ---------- */
let current = null, timer = null, seq = 0, lastHash = location.hash;
const ctx = {
  setTheme,
  logout,
  redrawNav: drawNav,
};

function leavePage(){
  clearInterval(timer);
  timer = null;
  if (current?.cleanup) try { current.cleanup(); } catch { /* ignore */ }
  current = null;
  setDirty(null);
}

export async function render(){
  let r = route();
  if (isOnboarding() && r.page !== 'setup'){
    location.hash = '#/setup';
    return;
  }
  if (!isOnboarding() && isPrivateOnly() && r.page !== 'people'){
    location.hash = `#/people/${currentActor()}/chat`;
    return;
  }
  if (r.page === 'people' && r.parts[1] && byActor(r.parts[1])){
    setCurrentActor(r.parts[1]);
  }
  leavePage();
  drawNav();
  const view = $('#view'), my = ++seq;
  const name = PAGES[r.page] || 'home';
  put(view, el('div', {class: 'pending', role: 'status'}, el('div', {class: 'skel'}, el('i'), el('i'), el('i')), el('span', {class: 'small muted'}, '读取中…')));
  try {
    const mod = await import(`./pages/${name}.js`);
    if (my !== seq) return;
    const host = el('div');
    current = (await mod.render(host, r, ctx)) || {};
    if (my !== seq) return;
    put(view, host);
    requestAnimationFrame(() => view.querySelectorAll('.hscroll').forEach(s => s.dispatchEvent(new Event('scroll'))));
    if (current.refresh){
      timer = setInterval(async () => {
        if (document.hidden || my !== seq || !current?.refresh) return;
        try { await current.refresh(); } catch { /* ignore transient background error */ }
      }, 15000);
    }
  } catch (e){
    if (e instanceof LoginNeeded) return;
    if (my === seq){
      put(view,
        el('div', {class: 'err'}, icon('alert'), ' 读取失败：' + (e?.message || e)),
        el('button', {type: 'button', class: 'btn sm mt', onclick: () => render()}, icon('refresh'), '重试'));
    }
  }
  if (my === seq) window.scrollTo(0, 0);
}

window.addEventListener('hashchange', async () => {
  if (!(await leaveOk())){
    history.replaceState(null, '', lastHash);
    return;
  }
  lastHash = location.hash;
  closeAll();
  render();
});
window.addEventListener('aihc:render', () => render());

/* ---------- Login (玄关钥匙 -> Bearer token) ---------- */
function showLogin(msg = ''){
  clearInterval(timer);
  setDirty(null);
  $('#app').classList.add('hidden');
  const pw = el('input', {type: 'password', id: 'key', placeholder: '请输入玄关钥匙 (AI_HOUSE_OWNER_TOKEN)', autocomplete: 'current-password', 'aria-label': '玄关钥匙'});
  const eye = el('button', {type: 'button', class: 'btn icon ghost', 'aria-label': '显示或隐藏钥匙',
    onclick: () => { pw.type = pw.type === 'password' ? 'text' : 'password'; eye.replaceChildren(icon(pw.type === 'password' ? 'eyeOff' : 'eye')); }}, icon('eyeOff'));
  const err = el('div', {class: 'lerr', role: 'alert'}, msg);
  const btn = el('button', {class: 'btn primary go', type: 'submit'}, '开门进去', icon('arrowR'));
  const form = el('form', {
    onsubmit: async e => {
      e.preventDefault();
      err.textContent = '';
      const cand = pw.value.trim();
      if (!cand){ err.textContent = '请先输入钥匙（可在 .env 中查看 AI_HOUSE_OWNER_TOKEN）'; pw.focus(); return; }
      btn.disabled = true;
      try {
        const authDoc = await api('/api/auth', undefined, {quiet: true, token: cand});
        setToken(cand);
        pw.value = '';
        await boot(authDoc);
      } catch (x){
        btn.disabled = false;
        err.textContent = x.status === 401 ? '钥匙不对，请核对 .env 中的 AI_HOUSE_OWNER_TOKEN' : (x.message || '连不上控制台');
        pw.focus();
      }
    },
  }, el('div', {class: 'pwrow'}, icon('key'), pw, eye), btn);

  put($('#login'),
    el('div', {class: 'lbox'},
      el('div', {class: 'logo'}, icon('home')),
      el('h1', {}, '欢迎回 AI 小屋'),
      el('p', {}, '输入玄关钥匙（.env 中的 AI_HOUSE_OWNER_TOKEN），走进大家一起生活的小屋。'),
      form,
      err,
      el('p', {class: 'tiny muted', style: 'margin:10px 0 0'}, '钥匙仅保存在当前浏览器标签页会话中，通过 Bearer 请求头鉴权。')),
    el('button', {type: 'button', class: 'btn ltheme', onclick: () => { cycleTheme(); showLogin(err.textContent); }}, icon(themeNow()[2]), themeNow()[1]));
  $('#login').classList.remove('hidden');
  pw.focus();
}

function logout(){
  clearToken();
  showLogin('已锁门退出，欢迎随时回来。');
}
setLogoutHandler(() => showLogin());

async function boot(preAuth = null){
  const authDoc = preAuth || await api('/api/auth', undefined, {quiet: true});
  setAuthTiers(authDoc);
  if (hasTier('read')){
    const st = await api('/api/state', undefined, {quiet: true});
    setOnboarding(!!st?.needs_onboarding);
    setResidentsFromState(st);
  } else {
    const r = await api('/api/chat/roster', undefined, {quiet: true});
    if (typeof r?.needs_onboarding === 'boolean') setOnboarding(r.needs_onboarding);
    setResidentsFromRoster(r.residents || []);
  }
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  if (isOnboarding()){
    if (!location.hash.startsWith('#/setup')){
      history.replaceState(null, '', '#/setup');
      lastHash = location.hash;
    }
  } else if (isPrivateOnly()){
    location.hash = `#/people/${currentActor()}/chat`;
  } else if (location.hash.startsWith('#/setup') && !location.hash.includes('?')){
    history.replaceState(null, '', '#/home');
    lastHash = location.hash;
  }
  await render();
}

boot().catch(() => showLogin());
