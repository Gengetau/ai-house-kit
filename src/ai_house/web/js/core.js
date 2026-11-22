// Shared DOM helpers, Bearer API client, native <dialog> modal, accessible tabs, and JST formatters.
import {icon, glyph} from './icons.js';
export {icon, glyph};
import {RESIDENTS, anyKey, toneStyle} from './residents.js';

export const $ = s => document.querySelector(s);

export function el(tag, attrs, ...kids){
  const e = document.createElement(tag);
  let value;
  for (const [k, v] of Object.entries(attrs || {})){
    if (v === false || v == null) continue;
    if (k === 'value') value = v;
    else if (k === 'class') e.className = v;
    else if (k === 'style') e.setAttribute('style', v);
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (k in e && typeof v !== 'string') e[k] = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat(4)){
    if (k != null && k !== false && k !== '') e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  if (value !== undefined) e.value = String(value);
  return e;
}

export const put = (node, ...kids) => {
  node.replaceChildren(...kids.flat(4).filter(k => k != null && k !== false));
  return node;
};

/* ---------- Token & API ---------- */
const TOKEN_KEY = 'ai_house_token';
export function getToken(){
  try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}
export function setToken(t){
  try {
    const clean = String(t || '').trim();
    if (clean) sessionStorage.setItem(TOKEN_KEY, clean);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* ignore private storage failure */ }
}
export function clearToken(){ setToken(''); }

export class LoginNeeded extends Error {}
let onLogout = () => {};
export const setLogoutHandler = f => { onLogout = f; };

let writing = 0;
export const writesInFlight = () => writing;

export async function api(path, body, opts = {}){
  if (body === undefined) return request(path, body, opts);
  writing++;
  try { return await request(path, body, opts); } finally { writing--; }
}

async function request(path, body, opts){
  const headers = {};
  const tok = opts.token !== undefined ? opts.token : getToken();
  if (tok) headers['Authorization'] = `Bearer ${tok}`;
  const init = body === undefined ? {headers} : {method: 'POST', headers};
  if (body !== undefined){
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let late = null;
  if (opts.timeout){
    const ctl = new AbortController();
    init.signal = ctl.signal;
    late = setTimeout(() => ctl.abort(), opts.timeout);
  }
  let r;
  try {
    r = await fetch(path, init);
  } catch {
    clearTimeout(late);
    throw new Error(init.signal?.aborted ? `读取超时（${Math.round(opts.timeout / 1000)} 秒没回应）` : '连不上 AI 小屋服务端');
  }
  if (r.status === 401 && !opts.quiet){
    clearToken();
    onLogout();
    throw new LoginNeeded('login');
  }
  const ct = r.headers.get('content-type') || '';
  let d;
  try {
    d = ct.includes('json') ? await r.json() : await r.text();
  } catch (e){
    if (init.signal?.aborted) throw new Error(`读取超时（${Math.round(opts.timeout / 1000)} 秒没回应）`);
    throw e;
  } finally {
    clearTimeout(late);
  }
  if (!r.ok){
    const err = new Error((d && d.error) || ('HTTP ' + r.status));
    err.status = r.status;
    err.data = d;
    err.isForbidden = r.status === 403;
    throw err;
  }
  return d;
}

/* ---------- Toast & Native <dialog> ---------- */
export function toast(text, bad){
  const t = $('#toast');
  if (!t) return;
  t.textContent = text;
  t.classList.toggle('bad', !!bad);
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), bad ? 5000 : 3000);
}

let dialogSeq = 0;
export const dialogOpen = () => !!$('#modal')?.open;

// Native <dialog> modal using showModal(), backdrop click dismiss, and close returnValue read/reset.
export function dialog({title, body, buttons = [['取消', null], ['确定', true, 'primary']], wide = false}){
  const m = $('#modal');
  if (!m) return Promise.resolve(null);
  if (m.open){
    m._resolve?.(null);
    m.close('');
  }
  const id = 'dlg-' + (++dialogSeq);
  const ordered = buttons.length === 2 && buttons[1][1] === null && buttons[0][1] !== null
    ? [buttons[1], buttons[0]] : buttons;

  return new Promise(res => {
    let settled = false;
    let customVal;
    const finish = val => {
      if (settled) return;
      settled = true;
      m._resolve = null;
      m._closeWith = null;
      m.removeEventListener('close', onClose);
      m.removeEventListener('click', onBackdrop);
      m.replaceChildren();
      res(val);
    };
    const onClose = () => {
      const rv = m.returnValue;
      m.returnValue = '';
      if (customVal !== undefined){
        const v = customVal;
        customVal = undefined;
        finish(v);
        return;
      }
      finish(rv === 'true' ? true : rv === 'cancel' ? null : (rv || null));
    };
    const onBackdrop = e => {
      if (e.target === m){
        customVal = null;
        m.close('');
      }
    };
    const closeWith = v => {
      const resolved = typeof v === 'function' ? v() : v;
      customVal = resolved;
      if (m.open) m.close(resolved === true ? 'true' : resolved == null ? 'cancel' : 'ok');
      else finish(resolved);
    };
    m._resolve = finish;
    m._closeWith = closeWith;

    const head = el('div', {class: 'dlg-head'},
      title ? el('h3', {id}, title) : el('h3', {id, class: 'sr'}, '对话框'),
      el('button', {type: 'button', class: 'dlg-close', 'aria-label': '关闭对话框', onclick: () => closeWith(null)}, icon('x')));
    const bodyWrap = el('div', {class: 'dlg-body'}, body);
    const acts = ordered.length ? el('div', {class: 'acts'},
      ordered.map(([t, v, cls]) => el('button', {
        class: 'btn ' + (cls || ''),
        type: 'button',
        onclick: () => closeWith(v),
      }, t))) : null;
    const box = el('div', {class: 'box' + (wide ? ' wide' : '')}, head, bodyWrap, acts);
    m.classList.toggle('wide', !!wide);
    m.setAttribute('aria-labelledby', id);
    m.returnValue = '';
    m.replaceChildren(box);
    m.addEventListener('close', onClose);
    m.addEventListener('click', onBackdrop);
    m.showModal();
  });
}

export const confirmBox = (title, text, ok = '确定', cls = 'primary') =>
  dialog({
    title,
    body: el('p', {class: 'small', style: 'color:var(--ink-2);line-height:1.7;margin:0'}, text),
    buttons: [['取消', null], [ok, true, cls]],
  });

/* ---------- Time & Formatting (JST) ---------- */
export const TZ = 'Asia/Tokyo';
const jstDayStr = d => d.toLocaleDateString('en-CA', {timeZone: TZ});
export const jstDate = ts => jstDayStr(new Date(ts * 1000));
export const jstHour = () => Number(new Date().toLocaleString('en-US', {hour: 'numeric', hour12: false, timeZone: TZ})) % 24;

export function fmtTime(ts, withDate = false){
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false, timeZone: TZ});
  const today = jstDayStr(new Date()), day = jstDayStr(d);
  if (!withDate && day === today) return '今天 ' + hm;
  if (!withDate && day === jstDayStr(new Date(Date.now() - 86400000))) return '昨天 ' + hm;
  return d.toLocaleDateString('zh-CN', {year: day.slice(0, 4) === today.slice(0, 4) ? undefined : 'numeric', month: 'numeric', day: 'numeric', timeZone: TZ}) + ' ' + hm;
}
export const dur = s => s < 60 ? '不到 1 分钟' : s < 3600 ? Math.floor(s / 60) + ' 分钟' : s < 86400 ? (s / 3600).toFixed(1).replace(/\.0$/, '') + ' 小时' : Math.floor(s / 86400) + ' 天';
export const ago = ts => !ts ? '还没有' : (Date.now() / 1000 - ts < 60 ? '刚刚' : dur(Date.now() / 1000 - ts) + '前');
export const num = n => n == null ? '—' : Number(n).toLocaleString('zh-CN');

/* ---------- Viewer Preferences ---------- */
export const store = {
  get(k, d){ try { const v = localStorage.getItem('aihk.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v){ try { localStorage.setItem('aihk.' + k, JSON.stringify(v)); } catch { /* ignore */ } },
};

/* ---------- Active Resident & Tier Capabilities ---------- */
export function currentActor(){
  const cur = store.get('currentResident', '');
  const hit = cur && anyKey(cur);
  if (hit) return hit.actor;
  return (RESIDENTS[0] && RESIDENTS[0].actor) || '';
}
export function setCurrentActor(key){
  const r = anyKey(key);
  if (!r) return currentActor();
  store.set('currentResident', r.actor);
  return r.actor;
}
export function pickActor(route, fallback){
  const qWho = route?.q?.who || route?.q?.actor || '';
  const hit = anyKey(qWho);
  if (hit) return setCurrentActor(hit.actor);
  return fallback ? (anyKey(fallback)?.actor || currentActor()) : currentActor();
}

let onboardingState = false;
export const isOnboarding = () => onboardingState;
export const setOnboarding = v => { onboardingState = !!v; };

let tiersState = {checked: false, read: true, write: true, private: true, owner: true, authEnabled: false};
export function setAuthTiers(doc){
  const t = (doc && doc.tiers) || {};
  tiersState = {
    checked: true,
    read: t.read !== false,
    write: t.write !== false,
    private: t.private !== false,
    owner: t.owner !== false,
    authEnabled: !!doc?.auth_enabled,
  };
  if (doc && typeof doc.needs_onboarding === 'boolean') onboardingState = doc.needs_onboarding;
}
export const hasTier = t => !!tiersState[t];
export const isReadonly = () => tiersState.checked && !tiersState.write;
export const isPrivateOnly = () => tiersState.checked && tiersState.private && !tiersState.read;

export function ownerOnly(what = '这一项', hint = '当前钥匙没有此权限，请使用房主令牌登录后操作。'){
  return el('div', {class: 'owner-only', role: 'region', 'aria-label': what + '需要更高权限'},
    el('div', {style: 'margin-bottom:8px;color:var(--accent-ink)'}, icon('lock')),
    el('b', {style: 'display:block;font-size:15.5px;color:var(--ink);margin-bottom:4px'}, `「${what}」需要对应权限`),
    hint ? el('p', {class: 'small muted', style: 'margin:0 auto;max-width:440px'}, hint) : null);
}

/* ---------- Accessible Form Validation (:user-invalid + ARIA Sync) ---------- */
let formAnnouncer = null, announceTimer = 0, announcedField = null;
function ensureAnnouncer(){
  if (!formAnnouncer){
    formAnnouncer = el('div', {class: 'visually-hidden', 'aria-live': 'polite'});
    document.body.append(formAnnouncer);
  }
  return formAnnouncer;
}
function linkFieldError(ctrl, shown){
  const errorId = ctrl.dataset?.error;
  if (!errorId) return;
  const ids = (ctrl.getAttribute('aria-describedby') || '').split(/\s+/).filter(id => id && id !== errorId);
  if (shown) ids.push(errorId);
  if (ids.length) ctrl.setAttribute('aria-describedby', ids.join(' '));
  else ctrl.removeAttribute('aria-describedby');
}
function spokenText(node){
  if (!node) return '';
  const copy = node.cloneNode(true);
  copy.querySelectorAll('[aria-hidden="true"]').forEach(n => n.remove());
  copy.querySelectorAll('[role="img"][aria-label]').forEach(n => n.replaceWith(`${n.getAttribute('aria-label')}: `));
  return copy.textContent.replace(/\s+/g, ' ').trim();
}
function announceFieldError(ctrl){
  const errEl = ctrl.dataset?.error ? document.getElementById(ctrl.dataset.error) : null;
  if (!errEl) return;
  const lbl = ctrl.labels?.[0] ? `${spokenText(ctrl.labels[0])}: ` : '';
  const box = ensureAnnouncer();
  clearTimeout(announceTimer);
  box.textContent = '';
  announcedField = ctrl;
  announceTimer = setTimeout(() => { box.textContent = lbl + spokenText(errEl); }, 500);
}

export function wireFormValidation(formEl){
  if (!formEl) return {validateAll: () => true, syncField: () => {}};
  const supportsUserInvalid = typeof CSS !== 'undefined' && CSS.supports && CSS.supports('selector(:user-invalid)');
  const flagged = new WeakSet();
  const announced = new WeakSet();
  const dirtyFallback = new WeakMap();

  const isFieldInvalid = ctrl => {
    if (supportsUserInvalid) return ctrl.matches(':user-invalid');
    const st = dirtyFallback.get(ctrl);
    return !!(st && st.interacted && st.blurred && !ctrl.validity.valid);
  };

  const syncAria = (ctrl, announce = false, forceCheck = false) => {
    if (!ctrl?.matches?.('input, select, textarea')) return;
    if (ctrl.disabled) return;
    if (!supportsUserInvalid && forceCheck){
      dirtyFallback.set(ctrl, {interacted: true, blurred: true});
    }
    const invalid = forceCheck ? !ctrl.validity.valid : isFieldInvalid(ctrl);
    if (!supportsUserInvalid){
      const st = dirtyFallback.get(ctrl);
      ctrl.classList.toggle('user-invalid-fallback', !!(st?.blurred && !ctrl.validity.valid));
    }
    if (invalid){
      if (announce && !announced.has(ctrl)){
        announceFieldError(ctrl);
        announced.add(ctrl);
      }
      ctrl.setAttribute('aria-invalid', 'true');
      linkFieldError(ctrl, true);
      flagged.add(ctrl);
    } else if (flagged.has(ctrl) || ctrl.getAttribute('aria-invalid') !== 'true'){
      ctrl.setAttribute('aria-invalid', 'false');
      linkFieldError(ctrl, false);
      flagged.delete(ctrl);
      announced.delete(ctrl);
      if (announcedField === ctrl){
        clearTimeout(announceTimer);
        if (formAnnouncer) formAnnouncer.textContent = '';
      }
    }
  };

  formEl.querySelectorAll('input, select, textarea').forEach(ctrl => syncAria(ctrl));
  formEl.addEventListener('blur', e => {
    if (!supportsUserInvalid && e.target?.matches?.('input, select, textarea')){
      const st = dirtyFallback.get(e.target) || {interacted: false, blurred: false};
      st.blurred = true;
      dirtyFallback.set(e.target, st);
    }
    syncAria(e.target, true);
  }, true);
  formEl.addEventListener('input', e => {
    if (!supportsUserInvalid && e.target?.matches?.('input, select, textarea')){
      const st = dirtyFallback.get(e.target) || {interacted: false, blurred: false};
      st.interacted = true;
      dirtyFallback.set(e.target, st);
    }
    if (e.target?.hasAttribute?.('aria-invalid')) syncAria(e.target);
  });
  formEl.addEventListener('change', e => syncAria(e.target, true), true);
  formEl.addEventListener('invalid', e => {
    announced.add(e.target);
    syncAria(e.target, false, true);
  }, true);
  formEl.addEventListener('reset', () => {
    setTimeout(() => {
      formEl.querySelectorAll('input, select, textarea').forEach(ctrl => {
        dirtyFallback.delete(ctrl);
        ctrl.classList.remove('user-invalid-fallback');
        ctrl.setCustomValidity?.('');
        syncAria(ctrl);
      });
    });
  }, true);

  return {
    syncField: (ctrl, forceCheck = false) => syncAria(ctrl, false, forceCheck),
    validateAll: () => {
      const ctrls = [...formEl.querySelectorAll('input, select, textarea')].filter(c => !c.disabled);
      let firstBad = null;
      for (const c of ctrls){
        if (!c.checkValidity()){
          syncAria(c, false, true);
          if (!firstBad) firstBad = c;
        } else {
          syncAria(c, false, false);
        }
      }
      if (firstBad){
        firstBad.focus();
        announceFieldError(firstBad);
        return false;
      }
      return true;
    },
  };
}

/* ---------- Shared Pixel Avatar & Chrome Helpers ---------- */
const charSpriteImgs = {};
function spriteThumbCanvas(r){
  const spr = r.sprite || r.actor;
  const src = `/web/img/house/chars/${encodeURIComponent(spr)}.png`;
  if (!r.hasSprite) return null;
  const cv = el('canvas', {width: 32, height: 32, class: 'px', 'aria-hidden': 'true'});
  const draw = im => {
    const ctx = cv.getContext('2d');
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, 32, 32);
    ctx.drawImage(im, 0, 2, 32, 32, 0, 0, 32, 32);
  };
  let im = charSpriteImgs[spr];
  if (!im){
    im = new Image();
    im.onload = () => draw(im);
    im.src = src;
    charSpriteImgs[spr] = im;
  } else if (im.complete && im.naturalWidth){
    draw(im);
  }
  return cv;
}

export function ava(r, size = '', extraCls = ''){
  if (!r) return el('span', {class: `ava ${size} ${extraCls}`.trim()}, icon('users'));
  const px = r.hasSprite ? spriteThumbCanvas(r) : null;
  return el('span', {
    class: `ava ${size} ${extraCls}`.trim(),
    style: toneStyle(r),
    title: r.name,
  }, px || glyph(r.glyph));
}

export function pageHead(title, sub, side){
  return el('div', {class: 'phead'},
    el('div', {class: 'phead-main'}, el('h1', {}, title), sub ? el('p', {}, sub) : null),
    side ? el('div', {class: 'side'}, side) : null);
}

export function enhanceScroll(strip){
  if (!strip || strip._enhanced) return strip;
  strip._enhanced = true;
  strip.classList.add('hscroll');
  const sync = (center = false) => {
    if (!strip.isConnected) return;
    if (center){
      const on = strip.querySelector('.on,[aria-selected="true"],[aria-pressed="true"]');
      if (on && strip.scrollWidth > strip.clientWidth + 2){
        on.scrollIntoView({block: 'nearest', inline: 'nearest'});
      }
    }
    const max = strip.scrollWidth - strip.clientWidth;
    const start = max > 4 && strip.scrollLeft > 4;
    const end = max > 4 && strip.scrollLeft < max - 4;
    strip.style.setProperty('--fade-start', start ? '20px' : '0px');
    strip.style.setProperty('--fade-end', end ? '20px' : '0px');
  };
  strip.addEventListener('scroll', () => sync(false), {passive: true});
  queueMicrotask(() => sync(true));
  return strip;
}

function wireTabKeys(strip){
  strip.addEventListener('keydown', e => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    const btns = [...strip.querySelectorAll('[role="tab"]:not([disabled])')];
    const i = btns.indexOf(document.activeElement);
    if (i < 0 || !btns.length) return;
    e.preventDefault();
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? btns.length - 1
      : (i + (e.key === 'ArrowRight' ? 1 : -1) + btns.length) % btns.length;
    btns[next].focus();
    btns[next].scrollIntoView({block: 'nearest', inline: 'nearest'});
  });
  return strip;
}

export function tabs(items, on, cls = 'ptabs'){
  const strip = el('div', {class: cls, role: 'tablist'}, items.map(([k, label, target]) =>
    el('button', {
      type: 'button',
      role: 'tab',
      class: k === on ? 'on' : '',
      'aria-selected': String(k === on),
      tabindex: k === on ? '0' : '-1',
      onclick: () => typeof target === 'function' ? target(k) : go(target),
    }, label)));
  wireTabKeys(strip);
  return enhanceScroll(strip);
}

export function pageTabs(items, on, side = null, cls = ''){
  const strip = tabs(items, on, 'tabs');
  return el('div', {class: ('pagetabs ' + cls).trim()}, strip, side ? [el('span', {class: 'grow'}), side] : null);
}

export function whoStrip(on, onPick, {field = 'actor', allLabel = false} = {}){
  const strip = el('div', {class: 'who-strip filters', role: 'group', 'aria-label': '选择住客'},
    allLabel ? el('button', {
      type: 'button',
      class: 'f txt' + (!on ? ' on' : ''),
      'aria-pressed': String(!on),
      onclick: () => onPick(''),
    }, allLabel === true ? '全部住客' : allLabel) : null,
    RESIDENTS.map(r => el('button', {
      type: 'button',
      style: toneStyle(r),
      class: 'f rchip' + (r[field] === on ? ' on' : ''),
      'aria-pressed': String(r[field] === on),
      onclick: () => onPick(r),
    }, ava(r, 'sm'), el('span', {}, r.name))));
  return enhanceScroll(strip);
}

export function residentStrip(on, field, hashOf){
  return enhanceScroll(el('div', {class: 'rstrip', role: 'group', 'aria-label': '选择住客'},
    RESIDENTS.map(r => el('button', {
      type: 'button',
      style: toneStyle(r),
      class: r[field] === on ? 'on' : '',
      'aria-pressed': String(r[field] === on),
      onclick: () => { const h = hashOf(r); if (typeof h === 'string') go(h); else if (typeof hashOf === 'function') hashOf(r); },
    }, ava(r, 'sm'), el('span', {class: 'n'}, r.name)))));
}

export function residentFilter(on, onPick, {field = 'actor', allLabel = '全部住客'} = {}){
  return enhanceScroll(el('div', {class: 'rfilter people-filter', role: 'group', 'aria-label': '筛选住客'},
    allLabel !== false ? el('button', {type: 'button', class: !on ? 'on' : '', 'aria-pressed': String(!on), onclick: () => onPick('')}, allLabel) : null,
    RESIDENTS.map(r => el('button', {
      type: 'button',
      style: toneStyle(r),
      class: r[field] === on ? 'on' : '',
      'aria-pressed': String(r[field] === on),
      onclick: () => onPick(r[field]),
    }, ava(r, 'xs'), el('span', {}, r.name)))));
}

export function helpFold(summary, ...kids){
  return el('details', {class: 'help-fold'},
    el('summary', {}, icon('chevR', 'fold-ic'), el('span', {}, summary)),
    el('div', {class: 'fold-body'}, ...kids));
}

export function emptyState({kind = 'empty', title, hint, actionLabel, onAction, iconName} = {}){
  const ic = iconName || (kind === 'filtered' ? 'search' : 'box');
  return el('div', {class: 'empty empty-state ' + kind},
    el('span', {class: 'empty-ic'}, icon(ic)),
    el('b', {class: 'empty-title'}, title || (kind === 'filtered' ? '没有符合筛选条件的内容' : '这里还没有记录')),
    hint ? el('p', {class: 'small muted empty-hint'}, hint) : null,
    actionLabel && onAction ? el('button', {type: 'button', class: 'btn sm', onclick: onAction}, actionLabel) : null);
}

export const empty = text => (text && typeof text === 'object' && !(text instanceof Node)) ? emptyState(text) : el('div', {class: 'empty'}, text);

export function errState(e, {onRetry, staleAt, prefix = '读取失败'} = {}){
  const msg = (e && e.message) || e;
  return el('div', {class: 'err row wrap', style: 'gap:10px'},
    el('span', {class: 'grow'}, icon('alert'), ` ${prefix}：${msg}`, staleAt ? `（当前仍显示 ${fmtTime(staleAt)} 的数据）` : ''),
    onRetry ? el('button', {type: 'button', class: 'btn sm', onclick: onRetry}, icon('refresh'), '重试') : null);
}
export const errBox = (e, opts) => opts ? errState(e, opts) : el('div', {class: 'err'}, '读取失败：' + ((e && e.message) || e));
export const loading = () => el('div', {class: 'loading'}, '读取中…');

export function meter(label, ic, value, cls){
  const v = Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
  return el('div', {class: 'meter ' + (cls || '')},
    el('span', {class: 'lb'}, ic ? icon(ic) : null, label),
    el('span', {class: 'tr', role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': v, 'aria-label': label},
      el('i', {style: `width:${Math.max(2, v)}%`})),
    el('span', {class: 'v'}, v + '%'));
}

/* ---------- Navigation & Dirty Guard ---------- */
let guard = null;
export const setDirty = message => { guard = message ? {message} : null; };
export const isDirty = () => !!guard;
export async function leaveOk(){
  if (!guard) return true;
  const ok = await confirmBox('还没保存', guard.message, '不保存，离开', 'danger');
  if (ok) guard = null;
  return !!ok;
}
export async function go(hash){
  if (location.hash === hash){
    window.dispatchEvent(new Event('aihc:render'));
    return;
  }
  if (!(await leaveOk())) return;
  location.hash = hash;
}
