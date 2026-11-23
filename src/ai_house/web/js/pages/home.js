// 今天 (v2 暖灯小屋首页): 小屋实景与房间、住客一览、生活剧本片段与快速群聊。
import {el, put, api, icon, ava, jstHour, fmtTime, num, pageHead, pageTabs, empty, go, currentActor, hasTier, setOnboarding, toast, LoginNeeded, chatMediaNodes} from '../core.js';
import {RESIDENTS, byActor, nameOf, toneStyle, setResidentsFromState} from '../residents.js';
import {ACTIVITIES, PUBLIC_ROOMS, lifeState, moodWord, moodTone, feelOf, roomLabel} from '../vocab.js';

function greeting(){
  const h = jstHour();
  return h < 5 ? '夜深了，屋里静悄悄的。' : h < 11 ? '早上好，小屋新的一天开始了。' : h < 17 ? '午后的阳光落在窗边。' : h < 22 ? '欢迎回家，今天也辛苦了。' : '夜幕降临，大家准备休息了。';
}

export async function render(view, _route, ctx){
  const box = el('div');
  const h = jstHour();
  let state = null, chatMsgs = [], chatErr = '', alive = true;
  const headSlot = el('div', {class: 'row wrap', style: 'gap:8px'});
  view.append(
    pageHead('欢迎回家，主人', greeting(), headSlot),
    pageTabs([['today', '今天', '#/home'], ['house', '走进小屋', '#/house']], 'today'),
    box);

  async function stepWorld(mins = 0){
    try {
      const res = await api('/api/step', mins ? {minutes: mins} : {});
      if (!alive) return;
      if (res && res.state) state = res.state;
      toast(mins ? `小屋时间前进了 ${mins} 分钟` : '小屋推进了 1 步');
      draw();
    } catch (e){
      toast(e.message, true);
    }
  }

  async function load(){
    const [st, ch] = await Promise.all([
      api('/api/state'),
      api('/api/chat?channel=group').catch(e => {
        if (e instanceof LoginNeeded) throw e;
        chatErr = e.message;
        return null;
      }),
    ]);
    if (!alive) return;
    if (st?.needs_onboarding){
      setOnboarding(true);
      setResidentsFromState(st);
      ctx?.redrawNav?.();
      go('#/setup');
      return;
    }
    setOnboarding(false);
    setResidentsFromState(st);
    state = st;
    if (ch && Array.isArray(ch.messages)) chatMsgs = ch.messages;
    draw();
  }

  function drawHead(){
    if (!state){
      put(headSlot,
        el('span', {class: 'chip info'}, icon('clock'), `东京 ${h}:00`),
        el('span', {class: 'chip acc'}, h >= 18 || h < 6 ? '夜晚灯亮' : '日间光照'));
      return;
    }
    const w = state.weather || {};
    const temp = w.temp_c != null ? `${Math.round(w.temp_c)}℃` : '';
    const rain = w.rain ? ' · 下雨' : '';
    put(headSlot,
      el('span', {class: 'chip info'}, icon('clock'), `${state.now_jst || ''} 周${state.weekday || ''}`),
      temp ? el('span', {class: 'chip'}, `${temp}${rain}`) : null,
      el('span', {class: 'chip acc'}, state.pack_profile || 'life1'),
      el('span', {class: 'chip ' + (state.llm_mode === 'live_api' ? 'ok' : '')}, state.llm_mode === 'live_api' ? 'Live LLM' : '演示引擎'),
      hasTier('write') ? el('button', {type: 'button', class: 'btn sm primary', onclick: () => stepWorld(0)}, icon('play'), '+1 步') : null);
  }

  function drawHero(resList){
    const byRoom = {};
    const elsewhere = [];
    for (const r of resList){
      const loc = r.reality?.location;
      if (PUBLIC_ROOMS.some(([k]) => k === loc)) (byRoom[loc] = byRoom[loc] || []).push(r);
      else elsewhere.push(r);
    }
    const hour = state?.now_jst ? Number(state.now_jst.slice(11, 13)) : h;
    const night = hour >= 18 || hour < 6;
    const imgSrc = `/web/img/house/overview/house_${night ? 'lit' : 'day'}.png`;
    return el('div', {class: 'card'},
      el('div', {class: 'chead'},
        el('h2', {}, '小屋此刻'),
        el('div', {class: 'acts'},
          el('span', {class: 'tiny muted'}, `共同钱包 ¥${num(state.house_wallet_yen || 0)}`),
          el('a', {class: 'btn sm primary', href: '#/house'}, '走进像素小屋', icon('chevR')))),
      el('div', {class: 'hero'},
        el('a', {class: 'hero-thumb', href: '#/house', title: '走进像素小屋'},
          el('img', {src: imgSrc, alt: 'AI 小屋像素实景', loading: 'eager'})),
        el('div', {},
          el('div', {class: 'rooms'}, PUBLIC_ROOMS.slice(0, 4).map(([key, label, , desc]) => {
            const who = byRoom[key] || [];
            const summary = who.length
              ? who.map(x => `${x.name} · ${x.reality?.doing || ACTIVITIES[x.reality?.activity] || ''}`).join('；')
              : desc;
            return el('a', {class: 'room', href: `#/house/${key}`},
              el('div', {class: 'who'}, who.length ? who.map(x => ava(byActor(x.actor), 'sm')) : el('span', {class: 'small muted'}, '此刻没人')),
              el('span', {class: 'nm'}, label),
              el('span', {class: 'ds'}, summary));
          })),
          elsewhere.length ? el('p', {class: 'small muted mt', style: 'margin-bottom:0'}, '其他位置：',
            elsewhere.map(x => `${x.name}（${x.reality?.place || roomLabel(x.reality?.location)}）`).join('、')) : null)));
  }

  function drawResidentCard(r, info){
    const real = info?.reality || {};
    const [availTxt, availCls] = lifeState(real);
    const feel = feelOf(real);
    const mood = moodWord(feel, real);
    const meals = info?.meals || [];
    const outfitLabels = info?.outfit?.labels || [];
    return el('a', {
      class: 'card rcard',
      style: toneStyle(r),
      href: `#/people/${r.actor}/now`,
      'aria-label': `查看 ${r.name} 的状态卡`,
    },
      el('div', {class: 'row', style: 'gap:10px'},
        ava(r),
        el('div', {class: 'grow'},
          el('div', {class: 'row between wrap', style: 'gap:4px'},
            el('b', {style: 'font-size:16px'}, r.name),
            el('span', {class: 'tiny muted'}, `¥${num(info?.wallet_yen || 0)}`)),
          el('div', {class: 'small muted', style: 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis'},
            `${real.place || roomLabel(real.location)} · ${real.doing || ACTIVITIES[real.activity] || '发呆'}`))),
      el('div', {class: 'row wrap', style: 'gap:6px;margin:10px 0 6px'},
        el('span', {class: 'chip ' + (availCls || '')}, el('span', {class: 'dot'}), availTxt),
        el('span', {class: 'chip ' + moodTone(feel.mood || real.mood)}, mood)),
      outfitLabels.length ? el('div', {class: 'tiny muted', style: 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis'},
        icon('box'), ' ', outfitLabels.slice(0, 3).join(' · ')) : null,
      meals.length ? el('div', {class: 'row wrap', style: 'gap:6px;margin-top:8px'},
        meals.map(m => el('span', {
          class: 'tag',
          title: `${m.label}：${m.text}`,
          style: m.state === 'eaten' ? 'background:var(--sage-soft);color:var(--sage-ink);border-color:transparent' : '',
        }, m.label.slice(0, 1) + (m.state === 'eaten' ? ' ✓' : m.state === 'missed' ? ' ×' : '')))) : null);
  }

  function drawQuickChat(){
    const sec = el('section', {class: 'card home-chat'});
    const listEl = el('div', {class: 'chat'});
    const drawMsgs = () => {
      put(listEl, chatMsgs.length ? chatMsgs.slice(-15).map(m =>
        el('div', {class: 'msg' + (m.sender === 'owner' ? ' me owner' : '')},
          el('div', {class: 'from'},
            el('b', {}, m.sender_name || nameOf(m.sender)),
            m.badge ? el('span', {class: 'tag'}, m.badge) : null,
            el('span', {class: 'tiny muted'}, fmtTime(m.created_at))),
          el('div', {class: 'txt'}, m.text),
          chatMediaNodes(m.media)))) : empty(chatErr || '群里还没有对话，发一句和大家打个招呼吧。'));
      listEl.scrollTop = listEl.scrollHeight;
    };
    drawMsgs();

    const inp = el('input', {
      type: 'text',
      placeholder: hasTier('write') ? '在小屋群里说一句……' : '当前令牌无发言权限',
      disabled: !hasTier('write'),
      style: 'flex:1',
    });
    const sendBtn = el('button', {type: 'submit', class: 'btn primary', disabled: !hasTier('write')}, icon('send'), '发送');
    const form = el('form', {
      class: 'row mt',
      style: 'gap:8px',
      onsubmit: async e => {
        e.preventDefault();
        const t = inp.value.trim();
        if (!t || sendBtn.disabled) return;
        sendBtn.disabled = true;
        try {
          const res = await api('/api/chat', {channel: 'group', text: t, sender_name: '房主'});
          inp.value = '';
          if (res && Array.isArray(res.messages)) chatMsgs = res.messages;
          drawMsgs();
          load();
        } catch (err){
          toast(err.message, true);
        } finally {
          sendBtn.disabled = !hasTier('write');
        }
      },
    }, inp, sendBtn);

    put(sec,
      el('div', {class: 'chead'},
        el('h2', {}, '小屋群聊'),
        el('div', {class: 'acts'}, el('a', {class: 'btn sm', href: `#/people/${currentActor()}/chat`}, '去私聊 / 全屏', icon('chevR')))),
      listEl,
      form);
    return sec;
  }

  function drawSnippets(){
    const notes = state.day_notes || [];
    const rows = (state.recent_script || []).slice(0, 6);
    return el('section', {class: 'card'},
      el('div', {class: 'chead'},
        el('h2', {}, '今日动态'),
        el('div', {class: 'acts'}, el('a', {class: 'btn sm', href: '#/days'}, '日子与总结', icon('chevR')))),
      notes.length ? el('div', {class: 'note', style: 'margin-bottom:12px'},
        el('b', {}, '晚间小结：'), notes[0].content) : null,
      rows.length ? rows.map((s, i) => el('div', {class: 'tl-row', style: i === 0 ? 'padding-top:0' : ''},
        el('span', {class: 'tm'}, fmtTime(s.ts)),
        el('div', {},
          (s.actors || []).map(a => {
            const r = byActor(a);
            return r ? el('span', {class: 'chip rchip', style: toneStyle(r) + ';margin-right:4px'}, r.name) : null;
          }),
          ' ', s.text))) : empty('今天还没有生活片段。'));
  }

  function draw(){
    if (!alive || !state) return;
    drawHead();
    const resList = RESIDENTS.map(r => ({...r, ...(state.residents?.[r.actor] || {})}));
    const curRes = byActor(currentActor()) || RESIDENTS[0];
    put(box,
      el('div', {class: 'home-grid'},
        el('div', {class: 'stack'},
          drawHero(resList),
          el('section', {class: 'card'},
            el('div', {class: 'chead'},
              el('h2', {}, `住客一览（${RESIDENTS.length} 位）`),
              el('div', {class: 'acts'},
                hasTier('owner') ? el('a', {class: 'btn sm primary', href: '#/setup?add=1'}, icon('plus'), '邀请新住客') : null,
                curRes ? el('a', {class: 'btn sm', href: `#/people/${curRes.actor}/now`}, `去看${curRes.name}`, icon('chevR')) : null)),
            el('div', {class: 'g3 keep2', style: 'gap:12px'},
              RESIDENTS.map(r => drawResidentCard(r, state.residents?.[r.actor]))))),
        el('div', {class: 'stack'},
          drawSnippets(),
          drawQuickChat(),
          el('section', {class: 'card'},
            el('div', {class: 'chead'}, el('h2', {}, '快捷去处')),
            el('div', {class: 'g2', style: 'gap:10px'},
              el('a', {class: 'li', href: '#/days/stock'}, el('span', {class: 'ic sage'}, icon('fridge')),
                el('span', {class: 'grow'}, el('b', {class: 't'}, '家里的东西'), el('div', {class: 'd'}, `${(state.stock || []).length} 种食材与零食`))),
              el('a', {class: 'li', href: '#/keep/diary'}, el('span', {class: 'ic honey'}, icon('bookOpen')),
                el('span', {class: 'grow'}, el('b', {class: 't'}, '日记书架'), el('div', {class: 'd'}, `${(state.diaries || []).length} 篇近期日记`))),
              el('a', {class: 'li', href: '#/days/city'}, el('span', {class: 'ic sky'}, icon('globe')),
                el('span', {class: 'grow'}, el('b', {class: 't'}, '晴空都远足'), el('div', {class: 'd'}, state.city_enabled ? '城市连线已就绪' : '本地出行模式'))),
              el('a', {class: 'li', href: '#/ops'}, el('span', {class: 'ic acc'}, icon('gear')),
                el('span', {class: 'grow'}, el('b', {class: 't'}, '管家与插件'), el('div', {class: 'd'}, `${(state.tools || []).length} 个工具 · MCP`))))))));
  }

  await load();
  return {refresh: load, cleanup: () => { alive = false; }};
}
