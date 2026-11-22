// 住客详情 (#/people/<actor>/<tab>): 此刻 / 衣柜 / 档案 / 对话（支持多世界包与私密/只读档位）。
import {el, put, api, icon, ava, glyph, go, pickActor, setCurrentActor, pageTabs, tabs, whoStrip,
  emptyState, errState, ownerOnly, loading, fmtTime, num, hasTier, isPrivateOnly, isOnboarding, toast, store, LoginNeeded} from '../core.js';
import {RESIDENTS, byActor, nameOf, toneStyle} from '../residents.js';
import {ACTIVITIES, MOODS, feelOf, moodWord, moodTone, lifeState, roomLabel} from '../vocab.js';

const TABS = [['now', '此刻'], ['wardrobe', '衣柜'], ['profile', '档案'], ['chat', '对话']];
const CATEGORY_LIST = [
  ['dress', '裙装'], ['top', '上装'], ['bottom', '下装'], ['outer', '外套'], ['shoes', '鞋'],
  ['loungewear', '居家服'], ['sleepwear', '睡衣'], ['sportswear', '运动服'], ['swimwear', '泳衣'],
  ['yukata', '浴衣'], ['accessory', '配饰'],
];
const SOURCE_LABEL = {initial: '自带', bought: '新买', shelf: '取自衣架', gift: '获赠', unknown: '未记录'};
const TZ = {timeZone: 'Asia/Tokyo'};
const hm = t => t ? new Date(t * 1000).toLocaleTimeString('zh-CN', {...TZ, hour: '2-digit', minute: '2-digit', hour12: false}) : '—';

export async function render(view, route){
  if (isOnboarding() || !RESIDENTS.length){
    go('#/setup');
    return {};
  }
  const actor = pickActor(route, route.parts[1]);
  const r = byActor(actor) || RESIDENTS[0];
  setCurrentActor(r.actor);
  const defaultTab = isPrivateOnly() ? 'chat' : 'now';
  const tab = TABS.some(([k]) => k === route.parts[2]) ? route.parts[2] : defaultTab;

  const strip = whoStrip(r.actor, x => x && go(`#/people/${x.actor}/${tab}`), {field: 'actor'});
  const shortcuts = !isPrivateOnly() ? el('div', {class: 'row wrap', style: 'gap:8px;align-items:center'},
    el('span', {class: 'row', style: 'gap:6px;margin-right:4px;' + toneStyle(r)}, ava(r, 'sm'), el('b', {}, r.name)),
    el('a', {class: 'btn sm', href: `#/keep/diary/${r.actor}`}, icon('bookOpen'), '她的日记'),
    el('a', {class: 'btn sm', href: `#/keep/memory?who=${r.actor}`}, icon('brain'), '她的记忆')) : null;
  const pt = pageTabs(TABS.map(([k, l]) => [k, l, `#/people/${r.actor}/${k}`]), tab, shortcuts);
  const slot = el('div');
  view.append(strip, pt, slot);

  if (tab === 'now') return renderNow(slot, r);
  if (tab === 'wardrobe') return renderWardrobe(slot, r);
  if (tab === 'profile') return renderProfile(slot, r);
  return renderChat(slot, r);
}

/* ---------------------------------------------------------------- 此刻 (Status Card) */
function bigSpriteFrame(r){
  const frame = el('div', {class: 'frame contain q'});
  if (!r.hasSprite){
    put(frame, el('div', {class: 'ph'}, glyph(r.glyph), el('b', {}, r.name)));
    return frame;
  }
  const cv = el('canvas', {width: 128, height: 192, style: 'width:160px;height:240px;image-rendering:pixelated;margin:18px auto'});
  const im = new Image();
  im.onload = () => {
    const ctx = cv.getContext('2d');
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.drawImage(im, 0, 0, 32, 48, 0, 0, 128, 192);
  };
  im.onerror = () => put(frame, el('div', {class: 'ph'}, glyph(r.glyph), el('b', {}, r.name)));
  im.src = `/web/img/house/chars/${encodeURIComponent(r.sprite || r.actor)}.png`;
  put(frame, cv);
  return frame;
}

async function renderNow(view, R){
  if (!hasTier('read')){
    view.append(ownerOnly(`${R.name}的此刻`, '当前钥匙仅限私聊权限，切到「对话」标签即可与她私聊。'));
    return {};
  }
  const box = el('div', {}, loading());
  view.append(box);
  let state = null, alive = true;
  const load = async () => {
    state = await api('/api/state');
    draw();
  };

  function draw(){
    if (!alive || !state) return;
    const s = (state.residents || {})[R.actor];
    if (!s){ put(box, errState('状态列表里找不到该住客')); return; }
    const real = s.reality || {};
    const feel = feelOf(real);
    const [availTxt, availCls] = lifeState(real);
    const moodTxt = moodWord(feel, real);
    const o = s.outfit || {};
    const garments = o.garments || [];
    const meals = s.meals || [];
    const changes = s.changes || [];
    const nextPlans = s.next || [];
    const curRoom = real.location === 'room' ? `room_${R.actor}`
      : ['living', 'kitchen', 'study', 'bath', 'yard'].includes(real.location) ? real.location : null;

    const left = el('div', {class: 'portrait'},
      el('div', {class: 'sc-plabel'},
        el('span', {}, R.latin.toUpperCase()),
        el('span', {class: 'pill ok'}, o.occasion_label || '日常')),
      bigSpriteFrame(R),
      el('div', {class: 'sc-cap'}, o.labels && o.labels.length ? o.labels.join(' · ') : '日常穿搭'),
      el('div', {class: 'tools', style: 'justify-content:center'},
        curRoom ? el('a', {class: 'btn sm', href: `#/house/${curRoom}`}, icon('home'), '去她在的房间') : null,
        hasTier('private') ? el('a', {class: 'btn sm primary', href: `#/people/${R.actor}/chat`}, icon('msg'), '和她私聊') : null));

    const right = el('div', {class: 'card sc-info'},
      el('div', {class: 'sc-head'},
        el('h2', {}, R.name),
        el('div', {class: 'row wrap', style: 'gap:6px'},
          el('span', {class: 'pill ' + availCls}, availTxt),
          el('span', {class: 'pill ' + moodTone(feel.mood || real.mood)}, moodTxt),
          el('span', {class: 'pill'}, `¥${num(s.wallet_yen || 0)}`))),

      el('section', {class: 'sc-sec'},
        el('div', {class: 'sc-label'}, el('span', {}, '现在在做什么'),
          el('small', {}, s.kernel?.action?.due ? `预计到 ${hm(s.kernel.action.due)}` : state.now_jst || '')),
        el('div', {class: 'sc-place'}, icon('home'), real.place || roomLabel(real.location)),
        el('div', {class: 'sc-doing'}, real.doing || ACTIVITIES[real.activity] || '在屋里休息'),
        nextPlans.length ? el('div', {class: 'sc-next small muted'}, '接下来：' + nextPlans.map(p => p.label || p.activity || p).join(' → ')) : null),

      el('section', {class: 'sc-sec'},
        el('div', {class: 'sc-label'}, el('span', {}, '今日衣装'),
          el('a', {class: 'small', href: `#/people/${R.actor}/wardrobe`}, '查看整本衣柜 →')),
        el('p', {class: 'sc-outfit'}, o.labels && o.labels.length ? o.labels.join('、') : '今日尚未记录穿搭'),
        garments.length ? el('ul', {class: 'sc-garments'}, garments.map(g =>
          el('li', {}, el('b', {}, g.label), el('span', {}, g.kind || '单品')))) : null),

      el('section', {class: 'sc-sec'},
        el('div', {class: 'sc-label'}, el('span', {}, '今日三餐')),
        el('div', {class: 'sc-meals'}, meals.map(m =>
          el('div', {class: 'sc-meal ' + (m.state || 'unknown')},
            el('small', {class: 'muted'}, m.label),
            el('b', {}, m.text))))),

      changes.length ? el('section', {class: 'sc-sec'},
        el('div', {class: 'sc-label'}, el('span', {}, `今日换衣（${changes.length} 次）`)),
        el('ul', {class: 'sc-changes'}, changes.map((c, idx) =>
          el('li', {class: idx === changes.length - 1 ? 'now' : ''},
            el('time', {}, hm(c.at)),
            el('div', {}, el('b', {}, c.occasion_label || '换衣'), el('p', {class: 'small'}, (c.labels || []).join('、'))))))) : null,

      feel.why ? el('section', {class: 'sc-sec'},
        el('div', {class: 'sc-label'}, el('span', {}, '心情来源')),
        el('p', {class: 'sc-mood small'}, feel.why, feel.at ? el('small', {class: 'muted'}, `· ${fmtTime(feel.at)}`) : null)) : null);

    put(box, el('div', {class: 'prof sc', style: toneStyle(R)}, left, right));
  }

  await load();
  return {refresh: load, cleanup: () => { alive = false; }};
}

/* ---------------------------------------------------------------- 衣柜 (Wardrobe) */
async function renderWardrobe(view, R){
  if (!hasTier('read')){
    view.append(ownerOnly(`${R.name}的衣柜`, '需要 Read 或房主令牌才能查看衣柜清单。'));
    return {};
  }
  const ui = {days: '30', sub: 'garments'};
  const box = el('div', {}, loading());
  view.append(box);
  let doc = null, alive = true;

  const load = async () => {
    doc = await api(`/api/wardrobe?actor=${encodeURIComponent(R.actor)}&days=${encodeURIComponent(ui.days)}`);
    draw();
  };

  const likingDots = n => {
    const val = Math.max(0, Math.min(5, Number(n) || 0));
    return el('span', {class: 'wd-liking', title: `喜好 ${val}/5`},
      Array.from({length: 5}, (_, i) => el('span', {class: 'wd-dot' + (i < val ? ' on' : '')})));
  };

  function draw(){
    if (!alive || !doc) return;
    const sum = doc.summary || {};
    const cur = doc.current;

    const winSeg = el('div', {class: 'seg'}, [['30', '最近 30 天'], ['90', '90 天'], ['all', '全部']].map(([k, lb]) =>
      el('button', {type: 'button', class: ui.days === k ? 'on' : '', onclick: () => { ui.days = k; load(); }}, lb)));

    const curCard = cur ? el('section', {class: 'card wd-now'},
      el('div', {class: 'wd-thumb ph'}, glyph(R.glyph), el('small', {}, cur.occasion_label || '日常')),
      el('div', {class: 'wd-now-body'},
        el('div', {class: 'wd-now-head'},
          el('h2', {}, '现在穿着'),
          el('span', {class: 'pill ok'}, `${cur.occasion_label || '日常'} · ${hm(cur.since)} 换上`)),
        el('p', {class: 'wd-now-labels'}, (cur.labels || []).join('、') || '暂无搭配')))
      : emptyState({title: '尚未记录此刻穿着'});

    const subTabEl = tabs([['garments', '单品衣柜', k => { ui.sub = k; draw(); }],
      ['outfits', '常穿搭配', k => { ui.sub = k; draw(); }],
      ['log', '换衣记录', k => { ui.sub = k; draw(); }]], ui.sub, 'wd-tabs');

    let panel;
    if (ui.sub === 'garments'){
      const gs = doc.garments || [];
      if (!gs.length){
        panel = emptyState({title: `${R.name}的衣柜里还没有衣物`});
      } else {
        const groups = [];
        for (const [ck, clabel] of CATEGORY_LIST){
          const sub = gs.filter(g => g.category === ck);
          if (!sub.length) continue;
          groups.push(el('section', {class: 'wd-cat-group'},
            el('h3', {class: 'wd-cat-title'}, `${clabel}（${sub.length}）`),
            el('div', {class: 'wd-grid'}, sub.map(g => el('article', {class: 'card wd-card' + (g.wearing ? ' wearing' : '')},
              el('div', {class: 'wd-card-top'},
                el('h4', {class: 'wd-card-title'}, g.label),
                el('div', {class: 'wd-badges'},
                  g.wearing ? el('span', {class: 'pill ok'}, '正在穿') : null,
                  el('span', {class: 'pill' + (g.state === 'worn' ? ' warn' : '')}, g.state_label || '干净'))),
              el('p', {class: 'wd-card-sub'},
                [g.color, g.material].filter(Boolean).join(' · ') || '日常面料',
                Array.isArray(g.temp) && g.temp[0] != null ? ` · 适温 ${g.temp[0]}～${g.temp[1]}℃` : ''),
              el('div', {class: 'wd-card-tags'},
                (g.occasion_labels || []).map(l => el('span', {class: 'pill'}, l)),
                likingDots(g.liking)),
              el('div', {class: 'wd-card-meta'},
                el('div', {class: 'wd-meta-row muted'}, `来源：${SOURCE_LABEL[g.source?.type] || '自带'} · 已穿 ${g.worn?.count_all || 0} 次`)))))));
        }
        panel = el('div', {class: 'wd-panel-inner'}, ...groups);
      }
    } else if (ui.sub === 'outfits'){
      const os = doc.outfits || [];
      panel = os.length ? el('div', {class: 'wd-outfit-grid'}, os.map(o =>
        el('article', {class: 'card wd-outfit-card' + (o.current ? ' current' : '')},
          el('div', {class: 'wd-outfit-body'},
            el('div', {class: 'row between wrap'},
              el('b', {}, o.current ? '当前搭配' : '历史套装'),
              el('span', {class: 'pill'}, `穿过 ${o.count_all} 次`)),
            el('div', {class: 'small'}, (o.garments || []).map(g => g.label).join(' + ')),
            el('div', {class: 'tiny muted'}, Object.keys(o.occasions || {}).join(' · '))))))
        : emptyState({title: '还没有套装搭配记录'});
    } else {
      const rows = (doc.log && doc.log.rows) || [];
      panel = rows.length ? el('ul', {class: 'wd-log'}, rows.map(row =>
        el('li', {class: 'card wd-log-row'},
          el('div', {class: 'wd-log-head'},
            el('b', {}, `${row.day || ''} ${hm(row.at)}`),
            el('span', {class: 'pill'}, row.occasion_label || '换衣')),
          el('div', {class: 'wd-log-items'}, (row.labels || []).join('、')))))
        : emptyState({title: '这个时间段还没有换衣记录'});
    }

    put(box, el('div', {class: 'wd-page', style: toneStyle(R)},
      el('div', {class: 'wd-winbar'},
        el('span', {class: 'wd-sum-text'}, `共 ${sum.garments ?? 0} 件单品 · ${sum.outfits ?? 0} 套常穿搭配 · 待洗 ${sum.worn_waiting ?? 0} 件`),
        winSeg),
      curCard,
      subTabEl,
      panel));
  }

  await load();
  return {refresh: load, cleanup: () => { alive = false; }};
}

/* ---------------------------------------------------------------- 档案 (Profile) */
async function renderProfile(view, R){
  if (!hasTier('read')){
    view.append(ownerOnly(`${R.name}的档案`, '当前钥匙仅限私聊权限。'));
    return {};
  }
  const st = await api('/api/state');
  const info = (st.residents || {})[R.actor] || {};
  view.append(
    el('div', {class: 'card', style: toneStyle(R)},
      el('div', {class: 'chead'},
        el('div', {class: 'row', style: 'gap:12px'},
          ava(R, 'lg'),
          el('div', {},
            el('h2', {}, R.name),
            el('div', {class: 'small muted'}, `${R.actor} · 世界包 ${st.pack_profile || 'life1'}`))),
        el('div', {class: 'acts'},
          hasTier('owner') ? el('a', {class: 'btn sm primary', href: `#/setup?edit=${encodeURIComponent(R.actor)}`}, icon('edit'), '编辑档案与人设') : null,
          el('a', {class: 'btn sm', href: `#/house/room_${R.actor}`}, icon('home'), '她的房间'),
          el('a', {class: 'btn sm', href: `#/keep/diary/${R.actor}`}, icon('bookOpen'), '她的日记'))),
      el('p', {style: 'line-height:1.8;font-size:15px'}, info.bio || R.bio || '来自 AI 小屋的室友。'),
      el('div', {class: 'g3 mt'},
        el('div', {class: 'mini'}, el('b', {}, `¥${num(info.wallet_yen || 0)}`), el('span', {}, '随身零花钱')),
        el('div', {class: 'mini'}, el('b', {}, info.reality?.place || roomLabel(info.reality?.location)), el('span', {}, '此刻位置')),
        el('div', {class: 'mini'}, el('b', {}, moodWord(feelOf(info.reality), info.reality)), el('span', {}, '当前心情')))));
  return {};
}

/* ---------------------------------------------------------------- 对话 (Private + Group Chat) */
async function renderChat(view, R){
  const canPriv = hasTier('private');
  const canRead = hasTier('read');
  const canWrite = hasTier('write');
  let mode = store.get('chatMode', canPriv ? 'private' : 'group');
  if (mode === 'private' && !canPriv && canRead) mode = 'group';
  if (mode === 'group' && !canRead && canPriv) mode = 'private';

  const channelOf = () => mode === 'private' ? `private:${R.actor}` : 'group';
  const listEl = el('div', {class: 'chat'});
  const topRow = el('div', {class: 'chead'});
  const statusHint = el('div', {class: 'tiny muted mt'});
  const inp = el('input', {type: 'text', style: 'flex:1'});
  const sendBtn = el('button', {type: 'submit', class: 'btn primary'}, icon('send'), '发送');

  const box = el('section', {class: 'card', style: toneStyle(R)}, topRow, listEl,
    el('form', {
      class: 'row mt',
      style: 'gap:8px',
      onsubmit: async e => {
        e.preventDefault();
        const txt = inp.value.trim();
        if (!txt || sendBtn.disabled) return;
        sendBtn.disabled = true;
        statusHint.textContent = mode === 'private' ? `${R.name} 正在输入…` : '屋里的住客们正在回应…';
        try {
          const res = await api('/api/chat', {channel: channelOf(), text: txt, sender_name: '房主'});
          inp.value = '';
          drawMsgs(res.messages || []);
          statusHint.textContent = '';
        } catch (err){
          statusHint.textContent = '';
          toast(err.message, true);
        } finally {
          syncComposer();
        }
      },
    }, inp, sendBtn),
    statusHint);
  view.append(box);

  function syncComposer(){
    const writable = mode === 'private' ? canPriv : canWrite;
    inp.disabled = !writable;
    sendBtn.disabled = !writable;
    inp.placeholder = writable
      ? (mode === 'private' ? `和 ${R.name} 私聊一句……` : '在小屋群聊发一句话（室友们会接话）……')
      : (mode === 'private' ? '私聊需要 Owner 或 Private 令牌' : '群聊发言需要 Owner 或 Write 令牌');
  }

  function drawTop(){
    const btns = [];
    if (canPriv) btns.push(['private', `与 ${R.name} 私聊`]);
    if (canRead) btns.push(['group', '小屋群聊']);
    put(topRow,
      el('h2', {}, mode === 'private' ? `与 ${R.name} 的私聊` : '小屋群聊'),
      btns.length > 1 ? el('div', {class: 'seg'}, btns.map(([k, lb]) =>
        el('button', {type: 'button', class: mode === k ? 'on' : '', onclick: () => {
          mode = k;
          store.set('chatMode', k);
          load();
        }}, lb))) : null);
    syncComposer();
  }

  function drawMsgs(msgs){
    put(listEl, msgs.length ? msgs.map(m =>
      el('div', {class: 'msg' + (m.sender === 'owner' ? ' me owner' : '')},
        el('div', {class: 'from'},
          el('b', {}, m.sender_name || nameOf(m.sender)),
          m.badge ? el('span', {class: 'tag'}, m.badge) : null,
          el('span', {class: 'tiny muted'}, fmtTime(m.created_at))),
        el('div', {class: 'txt'}, m.text)))
      : emptyState({title: mode === 'private' ? `还没有和 ${R.name} 私聊过` : '群聊里还安静着', iconName: 'msg'}));
    listEl.scrollTop = listEl.scrollHeight;
  }

  async function load(){
    drawTop();
    if ((mode === 'private' && !canPriv) || (mode === 'group' && !canRead)){
      put(listEl, ownerOnly('聊天记录', '当前钥匙档位无法读取该频道对话。'));
      return;
    }
    try {
      const d = await api('/api/chat?channel=' + encodeURIComponent(channelOf()));
      drawMsgs(d.messages || []);
    } catch (e){
      if (e instanceof LoginNeeded) throw e;
      put(listEl, errState(e, {onRetry: load}));
    }
  }

  await load();
  return {refresh: async () => {
    try {
      const d = await api('/api/chat?channel=' + encodeURIComponent(channelOf()));
      drawMsgs(d.messages || []);
    } catch { /* ignore */ }
  }};
}
