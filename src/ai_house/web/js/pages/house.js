// 像素小屋: 整栋剖面预览 + 等轴测房间内景（支持任意世界包动态名册与昼夜灯光切换）。
import {el, put, api, icon, go, empty, pageHead, pageTabs, isOnboarding, store} from '../core.js';
import {RESIDENTS, byActor, toneStyle} from '../residents.js';
import {ACTIVITIES, OUTSIDE, roomLabel, resolveBedroomAsset, moodWord, feelOf, lifeState} from '../vocab.js';
import {avatarRoom, drawOwner, bubbles} from '../house_avatar.js';

const BASE = '/web/img/house/';
const ROOM_LIST = [
  'living', 'kitchen', 'study', 'bath', 'yard',
  ...Array.from({length: 6}, (_, i) => `bedroom_${i}`),
];

const imgs = {}, jsons = {};
const img = src => imgs[src] || (imgs[src] = new Promise((ok, bad) => {
  const i = new Image();
  i.onload = () => ok(i);
  i.onerror = () => bad(new Error('图没加载出来：' + src));
  i.src = BASE + src;
}));
const json = src => jsons[src] || (jsons[src] = fetch(BASE + src).then(r => {
  if (!r.ok) throw new Error('读不到 ' + src);
  return r.json();
}));

function worldHour(st){
  if (st && st.now_jst){
    const m = st.now_jst.match(/\b(\d{2}):\d{2}/);
    if (m) return Number(m[1]);
  }
  return Number(new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Tokyo', hour: '2-digit', hour12: false}).format(new Date()));
}
function isNightFor(st){
  const mode = store.get('houseLight', 'auto');
  if (mode === 'day') return false;
  if (mode === 'night') return true;
  const h = worldHour(st);
  return h >= 18 || h < 6;
}

// Procedural 32x48 sprite atlas fallback when a custom WorldPack introduces actors without pre-rendered PNGs.
function proceduralAtlas(r){
  const W = 32, H = 48, cols = 25;
  const cv = document.createElement('canvas');
  cv.width = cols * W; cv.height = 2 * H;
  const ctx = cv.getContext('2d');
  const col = r.accent || '#B4532D';
  const drawFrame = (ox, oy, dy) => {
    ctx.fillStyle = '#2a2035';
    ctx.fillRect(ox + 9, oy + 7 + dy, 14, 17);
    ctx.fillStyle = col;
    ctx.fillRect(ox + 10, oy + 8 + dy, 12, 8);
    ctx.fillStyle = '#f7d7c2';
    ctx.fillRect(ox + 10, oy + 14 + dy, 12, 9);
    ctx.fillStyle = '#2a2035';
    ctx.fillRect(ox + 12, oy + 17 + dy, 2, 2);
    ctx.fillRect(ox + 18, oy + 17 + dy, 2, 2);
    ctx.fillStyle = col;
    ctx.fillRect(ox + 10, oy + 24, 12, 14);
    ctx.fillStyle = '#3e3b4c';
    ctx.fillRect(ox + 12, oy + 38, 3, 9);
    ctx.fillRect(ox + 17, oy + 38, 3, 9);
  };
  const cells = {};
  for (let row = 0; row < 2; row++){
    const pose = row === 0 ? 'stand' : 'sit';
    for (const it of ['none', 'book', 'phone', 'pad', 'ladle', 'cup']){
      for (let f = 0; f < 4; f++){
        const idx = (['none', 'book', 'phone', 'pad', 'ladle', 'cup'].indexOf(it)) * 4 + f;
        drawFrame(idx * W, row * H, (f === 1 || f === 3) ? 1 : 0);
        const name = `${pose}_${it}_${f}`;
        cells[name] = {name, x: idx * W, y: row * H, w: W, h: H};
      }
    }
  }
  drawFrame(24 * W, 0, 0);
  cells.sleep = {name: 'sleep', x: 24 * W, y: 0, w: W, h: H};
  return {atlas: cv, cells};
}

async function loadChars(){
  const out = {};
  await Promise.all(RESIDENTS.map(async r => {
    const spr = r.sprite || r.actor;
    try {
      const [a, l] = await Promise.all([img(`chars/${spr}.png`), json(`chars/${spr}.json`)]);
      const cells = {};
      for (const c of l.cells || []) cells[c.name] = c;
      out[r.actor] = {atlas: a, cells};
    } catch {
      delete imgs[`chars/${spr}.png`];
      delete jsons[`chars/${spr}.json`];
      out[r.actor] = proceduralAtlas(r);
    }
  }));
  return out;
}

function roomKeyOf(r){
  if (!r || !r.reality) return null;
  const loc = r.reality.location;
  if (loc === 'room') return 'room_' + r.actor;
  if (['living', 'kitchen', 'study', 'bath', 'yard'].includes(loc)) return loc;
  if (loc && loc.startsWith('room_')) return loc;
  return null;
}

function actsOf(r){
  const real = r.reality || r;
  const a = real.activity || 'idle', d = real.doing || '';
  const has = (...w) => w.some(x => d.includes(x));
  const out = [];
  if (has('电影', '电视')) out.push('movie_night');
  if (has('桌游', '卡牌')) out.push('board_game');
  if (has('茶', '咖啡')) out.push('tea_time');
  if (has('洗碗')) out.push('chores_dishes');
  if (has('洗衣', '晾')) out.push('laundry');
  if (has('画')) out.push('drawing');
  if (has('琴', '乐器')) out.push('music');
  if (has('花', '浇水')) out.push('gardening');
  if (has('剧')) out.push('drama');
  if (has('泡澡', '洗澡')) out.push('bathing');
  if (a !== 'cooking' && has('吃', '饭')) out.push('eating');
  out.push({chores: 'chores_clean', hobby: 'hobby', socializing: 'socializing', walking: 'idle', browsing: 'browsing'}[a] || a);
  out.push('idle');
  return out;
}

const ITEM_FOR = {
  reading: 'book', working: 'book', phone: 'phone', browsing: 'phone', gaming: 'pad',
  cooking: 'ladle', tea_time: 'cup', eating: 'cup',
};
function itemOf(r){
  const real = r.reality || r;
  return ITEM_FOR[real.activity] || actsOf(r).map(a => ITEM_FOR[a]).find(Boolean) || 'none';
}
function postureOf(r){
  const d = ((r.reality || r).doing) || '';
  if (/坐|躺|靠在|窝在|趴/.test(d)) return 'sit';
  if (/站|走来走去|踱步|散步|晾/.test(d)) return 'stand';
  return null;
}

const SEQ = [0, 1, 0, 1, 0, 2, 0, 1, 3, 1, 0, 1, 0, 3];
const frameAt = (tick, i) => SEQ[(tick + i * 5) % SEQ.length];

function shadow(ctx, x, y, rx, ry, a){
  ctx.save();
  ctx.translate(x, y);
  ctx.scale(1, ry / rx);
  const g = ctx.createRadialGradient(0, 0, 0, 0, 0, rx);
  g.addColorStop(0, `rgba(25,15,10,${a})`);
  g.addColorStop(0.6, `rgba(25,15,10,${a * 0.6})`);
  g.addColorStop(1, 'rgba(25,15,10,0)');
  ctx.fillStyle = g;
  ctx.beginPath(); ctx.arc(0, 0, rx, 0, Math.PI * 2); ctx.fill();
  ctx.restore();
}

function drawChar(ctx, ch, x, y, pose, item, f, o = {}){
  if (!ch) return null;
  const dep = o.depth || 1;
  if (pose === 'stand') shadow(ctx, x, y, 9 * dep, 2.6 * dep, 0.32);
  else if (pose === 'sit') shadow(ctx, x, y + 1, 8 * dep, 2.2 * dep, 0.16);
  let name, rows, anchor;
  if (pose === 'sleep'){ name = 'sleep'; rows = 26; anchor = 25; }
  else if (pose === 'bath'){ name = `stand_none_${f}`; rows = 30; anchor = 29; }
  else if (pose === 'sit'){ name = `sit_${item}_${f}`; rows = 48; anchor = 41; }
  else { name = `stand_${item}_${f}`; rows = 48; anchor = 47; }
  const c = ch.cells[name] || ch.cells[(pose === 'sit' ? 'sit' : 'stand') + '_none_' + f] || ch.cells['stand_none_0'];
  if (!c) return null;
  const left = Math.round(x - 16), top = Math.round(y - anchor);
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  if (o.dim) ctx.filter = 'brightness(0.8) saturate(0.9)';
  ctx.drawImage(ch.atlas, c.x, c.y, 32, rows, left, top, 32, rows);
  ctx.restore();
  return {x: left, y: top, w: 32, h: rows};
}

function canvasBox(w, h, cls, K = 1){
  const c = el('canvas', {width: w * K, height: h * K, class: cls});
  c.getContext('2d').imageSmoothingEnabled = false;
  return c;
}
function begin(ctx, K, W, H){
  ctx.setTransform(K, 0, 0, K, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, W, H);
}
function startAnim(step){
  let dead = false, tid = 0;
  const run = () => {
    if (dead) return;
    step();
    if (!dead) tid = setTimeout(run, 110);
  };
  tid = setTimeout(run, 110);
  return () => { dead = true; clearTimeout(tid); };
}

function lightControl(onChange){
  const cur = store.get('houseLight', 'auto');
  return el('div', {class: 'seg', role: 'group', 'aria-label': '小屋光照'},
    [['auto', '自动'], ['day', '白天'], ['night', '夜晚亮灯']].map(([k, lb]) =>
      el('button', {
        type: 'button',
        class: cur === k ? 'on' : '',
        onclick: () => { store.set('houseLight', k); onChange(); },
      }, lb)));
}

function resolveCellRouteKey(cellKey){
  if (cellKey.startsWith('bedroom_')){
    const idx = Number(cellKey.slice(8));
    const r = RESIDENTS[idx];
    return r ? `room_${r.actor}` : cellKey;
  }
  return cellKey;
}

function resolveRoomStateKey(roomKey){
  if (roomKey.startsWith('bedroom_')){
    const idx = Number(roomKey.slice(8));
    return RESIDENTS[idx] ? `room_${RESIDENTS[idx].actor}` : roomKey;
  }
  return roomKey;
}

export async function render(view, route){
  if (isOnboarding()){
    go('#/setup');
    return {};
  }
  const key = route.parts[1];
  const valid = key && (ROOM_LIST.includes(key) || (key.startsWith('room_') && byActor(key.slice(5))));
  if (valid) return renderRoom(view, key);
  return renderHouse(view);
}

/* ---------------------------------------------------------------- Overview */
async function renderHouse(view){
  const wrap = el('div', {class: 'ph-stage'});
  const side = el('div', {class: 'ph-side'});
  const lightBox = lightControl(() => go('#/house'));
  view.append(
    pageHead('走进像素小屋', '看看每位住客此刻在屋里的哪个角落；点一下房间进去看内景。', lightBox),
    pageTabs([['today', '今天', '#/home'], ['house', '走进小屋', '#/house']], 'house'),
    el('div', {class: 'ph-house'}, wrap, side));

  const [cells, chars, state0] = await Promise.all([json('overview/cells.json'), loadChars(), api('/api/state')]);
  let state = state0, tick = 0;
  const night = isNightFor(state);
  const [bg, lit] = await Promise.all([
    img(`overview/house_${night ? 'night' : 'day'}.png`),
    night ? img('overview/house_lit.png') : null,
  ]);
  const [W, H] = cells.size;
  const KH = 3;
  const cv = canvasBox(W, H, 'ph-canvas', KH);
  const tags = el('div', {class: 'ph-tags'});
  const box = el('div', {class: 'ph-box'}, cv, tags);
  box.style.width = `min(100%, calc((100vh - 180px) * ${(W / H).toFixed(4)}))`;
  put(wrap, box);
  const ctx = cv.getContext('2d');
  let lastTagsKey = '';

  const cellLabel = c => c.key.startsWith('bedroom_') ? roomLabel(resolveCellRouteKey(c.key)) : roomLabel(c.key);

  cv.addEventListener('click', e => {
    const b = cv.getBoundingClientRect(), x = (e.clientX - b.left) * W / b.width, y = (e.clientY - b.top) * H / b.height;
    const c = cells.cells.find(c => x >= c.x && x < c.x + c.w && y >= c.y && y < c.y + c.h + 6);
    if (c) go('#/house/' + resolveCellRouteKey(c.key));
  });
  cv.addEventListener('mousemove', e => {
    const b = cv.getBoundingClientRect(), x = (e.clientX - b.left) * W / b.width, y = (e.clientY - b.top) * H / b.height;
    const c = cells.cells.find(c => x >= c.x && x < c.x + c.w && y >= c.y && y < c.y + c.h + 6);
    cv.style.cursor = c ? 'pointer' : 'default';
    cv.title = c ? cellLabel(c) + '（点击进入）' : '';
  });

  function draw(){
    begin(ctx, KH, W, H);
    ctx.drawImage(bg, 0, 0);
    const resMap = state.residents || {};
    const where = {};
    for (const res of RESIDENTS){
      const r = resMap[res.actor];
      if (!r) continue;
      const k = roomKeyOf(r);
      const cellKey = k && k.startsWith('room_') ? res.bed : k;
      if (cellKey) (where[cellKey] = where[cellKey] || []).push(r);
    }
    if (night){
      for (const c of cells.cells){
        if (where[c.key] && c.key !== 'yard') ctx.drawImage(lit, c.x, c.y, c.w, c.h, c.x, c.y, c.w, c.h);
      }
    }
    const tagEls = [];
    const ORDER = [2, 4, 1, 3, 0, 5];
    for (const c of cells.cells){
      const list = where[c.key] || [];
      list.forEach((r, i) => {
        const sx = c.spots[ORDER[i % ORDER.length] % c.spots.length] + (i >= c.spots.length ? 8 : 0);
        const sleeping = r.reality?.activity === 'sleeping';
        const dim = night && c.key === 'yard';
        const boxRect = sleeping
          ? drawChar(ctx, chars[r.actor], c.x + sx, c.floor - 6, 'sit', 'none', 0, {dim})
          : drawChar(ctx, chars[r.actor], c.x + sx, c.floor, 'stand', itemOf(r), frameAt(Math.floor(tick / 7), i), {dim});
        const top = boxRect ? boxRect.y - 2 : c.floor - 50;
        const meta = byActor(r.actor);
        tagEls.push(el('span', {
          class: 'ph-tag',
          style: `left:${(c.x + sx) / W * 100}%;top:${top / H * 100}%;` + toneStyle(meta),
        }, (meta ? meta.name : r.actor) + (sleeping ? ' 💤' : '')));
      });
    }
    const tagsKey = tagEls.map(n => n.textContent + '@' + n.getAttribute('style')).join('|');
    if (tagsKey !== lastTagsKey){ lastTagsKey = tagsKey; put(tags, tagEls); }
  }

  function sideList(){
    const resMap = state.residents || {};
    put(side,
      el('h3', {}, `${state.now_jst || ''} · ${night ? '夜色小屋' : '日间小屋'}`),
      RESIDENTS.map(meta => {
        const r = resMap[meta.actor];
        const k = r ? roomKeyOf(r) : null;
        const real = r?.reality || {};
        return el('button', {
          type: 'button',
          class: 'ph-row',
          style: toneStyle(meta),
          disabled: !k,
          onclick: () => k && go('#/house/' + k),
        },
          el('b', {}, meta.name),
          el('span', {class: 'muted'}, k ? roomLabel(k) : (OUTSIDE[real.location] || real.place || '出门在外')),
          el('span', {}, real.doing || ACTIVITIES[real.activity] || ''));
      }),
      el('h3', {}, '直达房间'),
      el('div', {class: 'ph-rooms'},
        ['living', 'kitchen', 'study', 'bath', 'yard'].map(rk =>
          el('button', {type: 'button', class: 'chip', onclick: () => go('#/house/' + rk)}, roomLabel(rk))),
        RESIDENTS.map(r =>
          el('button', {type: 'button', class: 'chip', onclick: () => go('#/house/room_' + r.actor)}, r.name + '的房间'))));
  }

  draw();
  sideList();
  const stopAnim = startAnim(() => { tick++; draw(); });
  return {
    refresh: async () => { state = await api('/api/state'); draw(); sideList(); },
    cleanup: stopAnim,
  };
}

/* ---------------------------------------------------------------- Room Interior */
async function renderRoom(view, key){
  const assetKey = resolveBedroomAsset(key);
  const stateKey = resolveRoomStateKey(key);
  const title = roomLabel(key);
  const stageEl = el('div', {class: 'ph-room'});
  const side = el('div', {class: 'ph-side'});
  const tip = el('div', {class: 'ph-tip hidden'});
  const lightBox = lightControl(() => go('#/house/' + key));

  view.append(
    pageHead(title, '把鼠标移到家具或人物上看看；可以用房主化身走进房间说话、递零食。',
      [lightBox, el('button', {type: 'button', class: 'btn', onclick: () => go('#/house')}, icon('home'), '回到整栋小屋')]),
    pageTabs([['today', '今天', '#/home'], ['house', '走进小屋', '#/house']], 'house'),
    el('div', {class: 'ph-house'}, el('div', {class: 'ph-stage'}, stageEl, tip), side));

  const state0 = await api('/api/state');
  let state = state0, tick = 0, hits = [], ownerBox = null;
  const night = isNightFor(state);
  const tags = night ? ['night', 'lit'] : ['day'];
  const [room, chars, ...pics] = await Promise.all([
    json(`rooms/${assetKey}/room.json`),
    loadChars(),
    ...tags.flatMap(t => [img(`rooms/${assetKey}/bg_${t}.png`), img(`rooms/${assetKey}/objs_${t}.png`)]),
  ]);

  const [W, H] = room.size;
  const KR = 4;
  const cv = canvasBox(W, H, 'ph-canvas ph-iso', KR);
  const talk = el('div', {class: 'ph-tags av-layer'});
  const frame = el('div', {class: 'ph-box'}, cv, talk);
  frame.style.width = `min(100%, calc((100vh - 210px) * ${(W / H).toFixed(4)}))`;
  cv.style.width = '100%';
  put(stageEl, frame);
  const ctx = cv.getContext('2d');

  const hereList = () => RESIDENTS.map(meta => state.residents?.[meta.actor]).filter(r => r && roomKeyOf(r) === stateKey);
  const av = avatarRoom({
    room: stateKey.startsWith('bedroom_') ? 'living' : stateKey,
    roomName: title,
    here: () => hereList().filter(r => r.reality?.activity !== 'sleeping').map(r => r.actor),
    onAction: async () => { state = await api('/api/state'); draw(); sideList(); },
  });

  function place(list){
    const used = new Set(), out = [];
    for (const r of list){
      const acts = actsOf(r), want = postureOf(r);
      const sleeping = r.reality?.activity === 'sleeping';
      const fits = x => !want || x.pose === want || (want === 'sit' && x.pose === 'sleep' && sleeping);
      let best = null;
      for (const a of acts){
        best = room.anchors.find(x => !used.has(x.id) && x.activities.includes(a) && fits(x) && (a !== 'sleeping' || sleeping));
        if (best) break;
      }
      if (!best && want) best = room.anchors.find(x => !used.has(x.id) && x.pose === want);
      if (!best) best = room.anchors.find(x => !used.has(x.id) && x.pose !== 'sleep' && x.pose !== 'bath');
      if (!best) best = {x: room.anchors[0].x + 10 * out.length, y: room.anchors[0].y + 4, depth: 999, pose: 'stand', id: 'extra'};
      used.add(best.id);
      out.push({r, a: best});
    }
    return out;
  }

  function draw(){
    const here = hereList();
    const st = av.stage();
    const ownerLoc = st?.owner?.location || state.owner_presence?.location;
    const ownerHere = !!(ownerLoc && ownerLoc === stateKey);
    const lit = night && (here.length > 0 || ownerHere);
    const [bg, objs] = (night && lit && pics.length >= 4) ? pics.slice(2, 4) : pics.slice(0, 2);
    begin(ctx, KR, W, H);
    ctx.drawImage(bg, 0, 0);
    const items = room.objects.map(o => ({d: o.depth, o}));
    const placed = place(here);
    for (const p of placed) items.push({d: p.a.depth, p});
    ownerBox = null;
    if (ownerHere){
      const used = new Set(placed.map(p => p.a.id));
      const spot = room.anchors.filter(x => !used.has(x.id) && x.pose === 'stand').sort((p, q) => q.y - p.y)[0]
        || {x: W * 0.5, y: H * 0.82, depth: 500};
      items.push({d: spot.depth, owner: spot});
    }
    items.sort((a, b) => a.d - b.d);
    hits = [];
    items.forEach((it, i) => {
      if (it.owner){
        ownerBox = drawOwner(ctx, it.owner.x, it.owner.y, tick);
        hits.push({...ownerBox, d: it.d + 1000, label: '房主（你）'});
      } else if (it.o){
        const o = it.o;
        ctx.drawImage(objs, o.ax, o.ay, o.w, o.h, o.x, o.y, o.w, o.h);
        hits.push({x: o.x, y: o.y, w: o.w, h: o.h, d: it.d, label: o.label});
      } else {
        const {r, a} = it.p;
        const asleep = a.pose === 'sleep' && r.reality?.activity === 'sleeping';
        const pose = a.pose === 'sleep' && !asleep ? 'sit' : a.pose;
        const yard = night && assetKey === 'yard';
        const boxRect = drawChar(ctx, chars[r.actor], a.x, a.y, pose,
          pose === 'sit' || pose === 'stand' ? itemOf(r) : 'none',
          frameAt(Math.floor(tick / (asleep ? 21 : 7)), i), {dim: yard});
        if (!boxRect) return;
        const meta = byActor(r.actor);
        hits.push({
          ...boxRect,
          d: it.d + 1000,
          label: (meta ? meta.name : r.actor) + '：' + (r.reality?.doing || ACTIVITIES[r.reality?.activity] || ''),
          who: r,
        });
      }
    });
  }

  cv.addEventListener('mousemove', e => {
    const b = cv.getBoundingClientRect(), x = (e.clientX - b.left) * W / b.width, y = (e.clientY - b.top) * H / b.height;
    const h = hits.filter(h => x >= h.x && x < h.x + h.w && y >= h.y && y < h.y + h.h).sort((p, q) => q.d - p.d)[0];
    if (!h){ tip.classList.add('hidden'); return; }
    tip.textContent = h.label;
    tip.style.left = (e.clientX - stageEl.getBoundingClientRect().left + 12) + 'px';
    tip.style.top = (e.clientY - stageEl.getBoundingClientRect().top + 12) + 'px';
    tip.classList.remove('hidden');
  });
  cv.addEventListener('mouseleave', () => tip.classList.add('hidden'));

  function talkLayer(){
    const spots = {};
    for (const h of hits) if (h.who) spots[h.who.actor] = {x: (h.x + h.w / 2) / W * 100, y: h.y / H * 100};
    if (ownerBox) spots.owner = {x: (ownerBox.x + ownerBox.w / 2) / W * 100, y: ownerBox.y / H * 100};
    bubbles(talk, av.stage(), stateKey, spots);
  }

  function sideList(){
    const here = hereList();
    av.update();
    const otherRooms = [
      ...['living', 'kitchen', 'study', 'bath', 'yard'],
      ...RESIDENTS.map(r => `room_${r.actor}`),
    ].filter(k => k !== key);
    put(side,
      av.el,
      el('h3', {}, '在这里的人'),
      here.length ? here.map(r => {
        const meta = byActor(r.actor);
        const real = r.reality || {};
        const [avail] = lifeState(real);
        return el('div', {class: 'ph-who', style: toneStyle(meta)},
          el('div', {class: 'ph-name'},
            el('b', {}, meta ? meta.name : r.actor),
            el('span', {class: 'muted small'}, `${avail} · ${moodWord(feelOf(real), real)}`)),
          el('div', {}, real.doing || ACTIVITIES[real.activity] || ''),
          el('a', {class: 'small', href: `#/people/${r.actor}/now`}, '去看她的此刻 →'));
      }) : el('p', {class: 'muted small'}, '现在这间屋子空着。'),
      el('h3', {}, '去别的房间'),
      el('div', {class: 'ph-rooms'}, otherRooms.map(rk => {
        const cnt = RESIDENTS.filter(res => {
          const r = state.residents?.[res.actor];
          return r && roomKeyOf(r) === rk;
        }).length;
        return el('button', {type: 'button', class: 'chip', onclick: () => go('#/house/' + rk)},
          roomLabel(rk) + (cnt ? ` · ${cnt}` : ''));
      })));
  }

  draw();
  sideList();
  const stopAnim = startAnim(() => {
    tick++;
    draw();
    if (tick % 10 === 0) talkLayer();
  });
  return {
    refresh: async () => { state = await api('/api/state'); draw(); sideList(); },
    cleanup: () => { stopAnim(); av.stop(); },
  };
}
