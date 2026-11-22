// 主人化身走进小屋: walk in, say a line in the room, or hand a snack from the kitchen stock.
import {el, put, api, hasTier, ownerOnly} from './core.js';
import {byActor, nameOf, toneStyle} from './residents.js';
import {roomLabel} from './vocab.js';

const ON_KEY = 'aihk-avatar-on', SID_KEY = 'aihk-avatar-sid';
const store = {
  get(k, s = localStorage){ try { return s.getItem(k); } catch { return null; } },
  set(k, v, s = localStorage){ try { v === null ? s.removeItem(k) : s.setItem(k, v); } catch { /* ignore */ } },
};
function session(){
  let s = store.get(SID_KEY, sessionStorage);
  if (!s){
    s = 'web_' + Math.random().toString(36).slice(2, 12);
    store.set(SID_KEY, s, sessionStorage);
  }
  return s;
}
const newId = () => 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
const REASONS = {
  not_in_house: '你还没进屋，先点一下走进房间',
  not_here: '她现在不在这个房间',
  asleep: '她睡着了，等醒来再给吧',
  not_a_house_snack: '这样东西不能直接递',
  none_left: '这样零食已经没有了',
  owner_only: '当前令牌没有写入权限',
  cmd_conflict: '指令重复',
  no_route: '走不过去',
  bad_location: '没有这个房间',
};

export function drawOwner(ctx, x, y, t = 0){
  const bob = Math.round(Math.sin(t / 6) * 0.6);
  ctx.save();
  ctx.fillStyle = 'rgba(25,15,10,0.3)';
  ctx.beginPath(); ctx.ellipse(x, y, 8, 2.4, 0, 0, Math.PI * 2); ctx.fill();
  const p = (c, dx, dy, w, h) => { ctx.fillStyle = c; ctx.fillRect(Math.round(x + dx), Math.round(y + dy + bob), w, h); };
  p('#2b2f3a', -5, -14, 4, 14); p('#2b2f3a', 1, -14, 4, 14);
  p('#34405a', -7, -32, 14, 19);
  p('#46557a', -7, -32, 14, 3);
  p('#f1d3bd', -5, -44, 10, 12);
  p('#1e1a1d', -6, -47, 12, 5); p('#1e1a1d', -6, -44, 2, 5);
  p('#1e1a1d', -2, -39, 1, 2); p('#1e1a1d', 2, -39, 1, 2);
  ctx.restore();
  return {x: x - 8, y: y - 48, w: 16, h: 48};
}

export function avatarRoom(o){
  if (!hasTier('write')){
    return {
      el: el('div', {class: 'av-panel'}, el('h3', {}, '主人在小屋'),
        ownerOnly('走进房间说话与递零食', '当前是只读模式，换用房主或写入令牌后就能进屋互动。')),
      stage: () => null, update(){}, stop(){},
    };
  }
  let on = store.get(ON_KEY) === '1', stage = null, alive = true, entered = false;
  const log = el('div', {class: 'av-log'}), status = el('div', {class: 'muted small'}), note = el('div', {class: 'small av-note'});
  const box = el('div', {class: 'av-box'});
  const say = el('textarea', {class: 'av-say', rows: 2, maxlength: 200, placeholder: '在这个房间里说点什么……'});
  const snack = el('select', {class: 'av-snack'});
  const flash = (t, bad) => { note.textContent = t; note.classList.toggle('bad', !!bad); };

  async function act(op, extra, ok){
    try {
      const d = await api('/api/avatar', {op, cmd_id: newId(), session: session(), ...extra});
      if (!alive) return false;
      const rec = (d && d.receipt) || d || {};
      if (rec.status === 'failed' || rec.ok === false){
        flash(REASONS[rec.reason] || ('没成：' + (rec.reason || rec.status)), true);
        await refresh();
        return false;
      }
      flash(ok || '已完成');
      await refresh();
      o.onAction && o.onAction();
      return true;
    } catch (e){
      flash(e.message, true);
      return false;
    }
  }

  async function walkIn(){
    on = true; store.set(ON_KEY, '1'); entered = true;
    await act('enter', {location: o.room}, '你走进了' + o.roomName);
    panel();
  }
  async function walkOut(){
    on = false; store.set(ON_KEY, null); entered = false;
    await act('leave', {}, '你离开了小屋');
    panel();
  }

  function panel(){
    if (!on){
      put(box, el('button', {type: 'button', class: 'btn primary', onclick: walkIn}, '以房主身份走进这个房间'),
        el('p', {class: 'muted small'}, '同室的住客会看到你、听到你说的话并作出反应。'), note);
      return;
    }
    const people = o.here();
    const snacks = (stage && stage.snacks) || [];
    const picked = snack.value;
    put(snack, snacks.length ? snacks.map(s => el('option', {value: s.resource_id}, `${s.label} ×${s.qty}`))
      : el('option', {value: ''}, '厨房里没有可递的零食'));
    if (picked && snacks.some(s => s.resource_id === picked)) snack.value = picked;
    put(box, status,
      el('div', {class: 'av-row'}, say, el('button', {type: 'button', class: 'btn primary', onclick: async () => {
        const t = say.value.trim();
        if (!t) return;
        if (await act('say', {text: t}, '她们听到了')) say.value = '';
      }}, '说')),
      el('div', {class: 'av-row'}, snack, people.length
        ? people.map(a => el('button', {
            type: 'button', class: 'chip', style: toneStyle(byActor(a)), disabled: !snacks.length,
            onclick: () => snack.value && act('give', {to: a, resource_id: snack.value}, '递给了' + nameOf(a)),
          }, '递给' + nameOf(a)))
        : el('span', {class: 'muted small'}, '这间屋子现在没人在')),
      el('div', {class: 'av-row'}, el('button', {type: 'button', class: 'btn', onclick: walkOut}, '离开小屋')),
      note);
  }

  function show(){
    const me = stage && stage.owner;
    status.textContent = !on ? '' : me ? (me.location === o.room ? '你就在这间房里' : `你现在在${roomLabel(me.location)}`)
      : entered ? '正在进屋…' : '你还没走进屋里';
    const lines = ((stage && stage.events) || []).filter(e => e.room === o.room).slice(-30);
    put(log, lines.length ? lines.map(e => {
      const t = new Date(e.at * 1000).toLocaleTimeString('zh-CN', {timeZone: 'Asia/Tokyo', hour: '2-digit', minute: '2-digit'});
      const who = nameOf(e.who), res = byActor(e.who);
      const text = e.kind === 'owner_entered' ? '走了进来' : e.kind === 'owner_left_room' ? '去了别的房间'
        : e.kind === 'owner_exited_avatar' ? '离开了小屋' : e.kind === 'owner_gave' ? `把${e.label}递给了${nameOf(e.to)}`
        : (e.to ? `（对${nameOf(e.to)}）` : '') + (e.text || '');
      return el('div', {class: 'av-line', style: res ? toneStyle(res) : ''},
        el('span', {class: 'muted small'}, t + ' '), el('b', {}, who), ' ', text);
    }) : el('p', {class: 'muted small'}, '最近三小时这间屋子很安静。'));
  }

  async function refresh(){
    try { stage = await api('/api/house/stage'); }
    catch (e){ status.textContent = e.message; return; }
    if (!alive) return;
    if (stage && stage.ok === false){
      put(box, el('p', {class: 'muted small'}, '生活内核尚在准备中，稍等一会儿就能互动。'));
      return;
    }
    o.onStage && o.onStage(stage);
    panel();
    show();
  }

  const el0 = el('div', {class: 'av-panel'}, el('h3', {}, '房主在小屋'), box, el('h3', {}, '房内动态'), log);
  if (on){ entered = true; act('enter', {location: o.room}, '你走进了' + o.roomName); }
  else refresh();
  const poll = setInterval(refresh, 8000);
  const beat = setInterval(() => { if (on) api('/api/avatar', {op: 'heartbeat', cmd_id: newId(), session: session()}).catch(() => {}); }, 60000);
  return {
    el: el0,
    stage: () => stage,
    update(){ if (alive && stage && stage.ok !== false){ panel(); show(); } },
    stop(){
      alive = false; clearInterval(poll); clearInterval(beat);
      setTimeout(() => {
        if (on && !location.hash.startsWith('#/house/')){
          api('/api/avatar', {op: 'leave', cmd_id: newId(), session: session()}).catch(() => {});
        }
      }, 0);
    },
  };
}

export function bubbles(layer, stage, room, spots){
  const refNow = (stage && stage.now) ? stage.now : Date.now() / 1000;
  const out = [], last = {};
  for (const e of (stage && stage.events) || []){
    if (e.room !== room || refNow - e.at > 180) continue;
    if (e.kind === 'said' || e.kind === 'owner_input_received') last[e.who] = e.text;
    if (e.kind === 'owner_gave') last.owner = `（递给${nameOf(e.to)}：${e.label}）`;
  }
  for (const [who, text] of Object.entries(last)){
    const p = spots[who];
    if (!p || !text) continue;
    out.push(el('div', {class: 'av-bubble', style: `left:${p.x}%;top:${p.y}%;` + (byActor(who) ? toneStyle(byActor(who)) : '')}, text));
  }
  put(layer, out);
}
