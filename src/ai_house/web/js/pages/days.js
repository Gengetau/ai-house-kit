// 日子页 (#/days/<tab>): 今天的小屋 / 约定 / 家里的东西 / 晴空都。
import {el, put, api, icon, ava, go, pageHead, pageTabs, residentFilter, emptyState, errState,
  ownerOnly, loading, dialog, fmtTime, jstDate, num, hasTier, isOnboarding, toast, LoginNeeded} from '../core.js';
import {RESIDENTS, byActor, nameOf, toneStyle} from '../residents.js';
import {SHELVES} from '../vocab.js';

const TABS = [['today', '今天的小屋'], ['promises', '约定'], ['stock', '家里的东西'], ['city', '晴空都']];
const hm = t => t ? new Date(t * 1000).toLocaleTimeString('zh-CN', {timeZone: 'Asia/Tokyo', hour: '2-digit', minute: '2-digit', hour12: false}) : '—';

export async function render(view, route){
  if (isOnboarding()){
    go('#/setup');
    return {};
  }
  const raw = route.parts[1] || '';
  const tab = raw === 'intents' ? 'promises' : TABS.some(([k]) => k === raw) ? raw : 'today';
  const slot = el('div');
  view.append(
    pageHead('小屋的日子', '每天的剧本与三餐、互相记下的约定、厨房货架和晴空都出行。'),
    pageTabs(TABS.map(([k, l]) => [k, l, k === 'today' ? '#/days' : `#/days/${k}`]), tab),
    slot);

  if (tab === 'today') return renderToday(slot);
  if (tab === 'promises') return renderPromises(slot);
  if (tab === 'stock') return renderStock(slot);
  return renderCity(slot);
}

/* ---------------------------------------------------------------- 今天的小屋 */
async function renderToday(view){
  let who = '', state = null, alive = true;
  const box = el('div', {}, loading());
  view.append(box);

  const load = async () => {
    state = await api('/api/state');
    draw();
  };

  function draw(){
    if (!alive || !state) return;
    const notes = state.day_notes || [];
    const rows = (state.recent_script || []).filter(s => !who || (s.actors || []).includes(who));

    put(box, el('div', {class: 'kweek'},
      residentFilter(who, a => { who = a; draw(); }),
      el('div', {class: 'week-layout'},
        el('section', {class: 'card timeline-panel'},
          el('div', {class: 'timeline-date'},
            el('h2', {}, `${state.now_jst || '今天'} 周${state.weekday || ''}`),
            el('span', {}, `最近 ${rows.length} 条生活记录`)),
          notes.length ? notes.slice(0, 2).map(n => el('div', {class: 'day-note'},
            el('h3', {}, icon('bulb'), '小屋总结'),
            el('p', {}, n.content),
            n.created_at ? el('p', {class: 'caption'}, fmtTime(n.created_at, true)) : null)) : null,
          rows.length ? rows.map(s => el('div', {class: 'event-row'},
            el('time', {}, hm(s.ts)),
            el('div', {class: 'event-dot'}, icon('sparkle')),
            el('div', {class: 'event-copy'},
              el('h3', {}, s.text),
              el('div', {class: 'event-people'}, (s.actors || []).map(a => {
                const r = byActor(a);
                return r ? el('span', {class: 'chip rchip', style: toneStyle(r)}, ava(r, 'xs'), r.name) : null;
              })))))
            : emptyState({title: '筛选条件下暂无剧本记录'})),

        el('aside', {class: 'card meals-panel'},
          el('h2', {}, '今日三餐表'),
          el('p', {}, '按内核真实餐桌记录显示'),
          RESIDENTS.map(r => {
            const meals = state.residents?.[r.actor]?.meals || [];
            return el('div', {class: 'meal-person'},
              el('div', {class: 'meal-person-head'}, ava(r, 'sm'), el('b', {}, r.name)),
              meals.length ? meals.map(m => el('div', {class: 'meal-line'},
                el('span', {}, m.label.slice(0, 2)),
                el('strong', {}, m.text))) : el('div', {class: 'tiny muted'}, '暂无记录'));
          })))));
  }

  await load();
  return {refresh: load, cleanup: () => { alive = false; }};
}

/* ---------------------------------------------------------------- 约定 (Intents) */
async function renderPromises(view){
  if (!hasTier('private')){
    view.append(ownerOnly('约定列表', '对话里记下的约定与待买清单包含私聊摘要，需要 Owner 或 Private 令牌查看。'));
    return {};
  }
  let who = '', stateFilter = 'all', items = [], alive = true;
  const box = el('div', {}, loading());
  view.append(box);

  const load = async () => {
    const d = await api('/api/intents?limit=100');
    items = d.intents || [];
    draw();
  };

  function draw(){
    if (!alive) return;
    const filtered = items.filter(it => (!who || it.actor === who) && (stateFilter === 'all' || it.state === stateFilter));
    const stName = {open: ['待办', 'warn'], completed: ['已履行', 'ok'], expired: ['已过期', '']};
    const stateChips = el('div', {class: 'seg', style: 'margin-bottom:14px'},
      [['all', '全部'], ['open', '待办'], ['completed', '已履行'], ['expired', '已过期']].map(([k, lb]) =>
        el('button', {type: 'button', class: stateFilter === k ? 'on' : '', onclick: () => { stateFilter = k; draw(); }}, lb)));

    put(box,
      residentFilter(who, a => { who = a; draw(); }),
      stateChips,
      filtered.length ? el('div', {class: 'card tblw'}, el('table', {class: 't mcards intents-table'},
        el('tr', {}, ['时间', '室友', '约定内容', '带买物品', '状态', '凭证'].map(h => el('th', {}, h))),
        filtered.map(it => {
          const r = byActor(it.actor);
          const [lb, cls] = stName[it.state] || [it.state, ''];
          return el('tr', {},
            el('td', {class: 'tm', 'data-label': '时间'}, fmtTime(it.created_at)),
            el('td', {class: 'k', 'data-label': '室友'},
              el('span', {class: 'row', style: 'gap:6px'}, ava(r, 'xs'), r ? r.name : it.actor)),
            el('td', {class: 'main'}, it.summary || '—'),
            el('td', {'data-label': '物品'}, it.item ? `${it.item} ×${it.qty || 1}` : '—'),
            el('td', {'data-label': '状态'}, el('span', {class: 'pill ' + cls}, lb)),
            el('td', {class: 'tiny muted', 'data-label': '凭证'}, it.evidence_key || '—'));
        })))
        : emptyState({kind: items.length ? 'filtered' : 'empty', title: items.length ? '当前筛选下没有约定' : '还没有记下约定',
          hint: '在聊天里和室友说“顺便帮家里买2个鸡蛋”之类的嘱咐后会出现在这里。'}));
  }

  try { await load(); } catch (e){
    if (e instanceof LoginNeeded) throw e;
    put(box, e.isForbidden ? ownerOnly('约定列表', '当前令牌没有 Private 档位权限。') : errState(e, {onRetry: load}));
  }
  return {refresh: load, cleanup: () => { alive = false; }};
}

/* ---------------------------------------------------------------- 家里的东西 (Kitchen Stock) */
async function renderStock(view){
  let data = null, ingredients = [], q = '', shelfPick = 'all', onlyHave = true, alive = true;
  const box = el('div', {}, loading());
  view.append(box);

  const load = async () => {
    const [st, ing] = await Promise.all([api('/api/stock'), api('/api/ingredients').catch(() => ({items: []}))]);
    data = st;
    ingredients = ing.items || [];
    draw();
  };

  async function changeQty(op, args, okMsg){
    try {
      const r = await api('/api/stock', {op, args});
      if (!r.ok){ toast('没调整成功：' + (r.reason || '操作被拒'), true); return; }
      toast(okMsg);
      await load();
    } catch (e){ toast(e.message, true); }
  }

  async function openAddModal(){
    const ingMap = Object.fromEntries(ingredients.map(i => [i.id, i]));
    const modeSel = el('select', {}, el('option', {value: 'catalog'}, '从目录选择食材/零食'), el('option', {value: 'custom'}, '自定义新品'));
    const itemSel = el('select', {}, ingredients.map(i => el('option', {value: i.id}, `${i.label} (${i.kind} · ¥${i.price_yen})`)));
    const nameInp = el('input', {type: 'text', maxlength: 40, placeholder: '例如：草莓大福、冰美式'});
    const qtyInp = el('input', {type: 'number', min: 1, max: 99, value: 4});
    const shelfSel = el('select', {}, [['kitchen', '厨房共用'], ['fridge', '冰箱'], ['pantry', '食品柜'], ['snacks', '零食架']].map(([k, l]) =>
      el('option', {value: k}, l)));
    const err = el('p', {class: 'dialog-error'});

    const catRow = el('label', {class: 'fld'}, el('span', {}, '品名'), itemSel);
    const custRow = el('label', {class: 'fld hidden'}, el('span', {}, '名称'), nameInp);
    modeSel.onchange = () => {
      const cust = modeSel.value === 'custom';
      catRow.classList.toggle('hidden', cust);
      custRow.classList.toggle('hidden', !cust);
    };

    const form = el('div', {class: 'kstock-form'},
      el('label', {class: 'fld'}, el('span', {}, '添置方式'), modeSel),
      catRow, custRow,
      el('div', {class: 'g2'},
        el('label', {class: 'fld'}, el('span', {}, '数量'), qtyInp),
        el('label', {class: 'fld'}, el('span', {}, '放哪里'), shelfSel)),
      err);

    const ok = await dialog({
      title: '给小屋添置食材与零食',
      body: form,
      buttons: [['取消', null], ['放进小屋', () => {
        const qty = Math.max(1, Math.min(99, parseInt(qtyInp.value || '1', 10) || 1));
        if (modeSel.value === 'custom'){
          const nm = nameInp.value.trim();
          if (!nm){ err.textContent = '请填写物品名称'; return null; }
          return {name: nm, qty, place: shelfSel.value === 'kitchen' ? 'pantry' : shelfSel.value};
        }
        const meta = ingMap[itemSel.value] || {};
        return {item: itemSel.value, name: meta.label || itemSel.value, qty,
          place: shelfSel.value === 'kitchen' ? 'pantry' : shelfSel.value};
      }, 'primary']],
    });
    if (ok) await changeQty('add', ok, `已添置 ${ok.name} ×${ok.qty}`);
  }

  function draw(){
    if (!alive || !data) return;
    const allRows = data.rows || [];
    const totalKinds = allRows.filter(r => r.qty > 0).length;
    const totalQty = allRows.reduce((a, r) => a + (r.qty || 0), 0);
    const todayUsed = allRows.reduce((a, r) => a + (r.eaten_today || 0) + (r.cooked_today || 0) + (r.given_today || 0), 0);

    const visible = allRows.filter(r => {
      if (onlyHave && r.qty <= 0) return false;
      if (shelfPick !== 'all' && r.shelf !== shelfPick) return false;
      if (q && !(r.name || '').toLowerCase().includes(q) && !(r.label || '').toLowerCase().includes(q)) return false;
      return true;
    });

    const grouped = Object.entries(SHELVES).map(([sk, [slabel, sic]]) => {
      const list = visible.filter(r => r.shelf === sk);
      if (!list.length) return null;
      return el('section', {class: 'card'},
        el('div', {class: 'stock-header'}, icon(sic), el('h2', {}, slabel), el('small', {}, `${list.length} 种`)),
        list.map(r => el('article', {class: 'stock-item' + (r.qty <= 0 ? ' zero' : '')},
          el('div', {class: 'stock-item-top'},
            el('div', {class: 'item-name'}, r.name + (r.holder ? `（${nameOf(r.holder)}）` : '')),
            el('div', {class: 'kqty'}, r.qty > 0 ? [r.qty, el('small', {}, r.unit || '份')] : el('small', {class: 'used-up'}, '已用完'))),
          el('div', {class: 'item-meta'},
            el('span', {}, `词典：${r.label}`),
            r.by === 'owner' ? el('span', {}, '房主添置') : r.by ? el('span', {}, `${nameOf(r.by)} 带来`) : null,
            r.held > 0 ? el('span', {}, `占着 ${r.held}`) : null),
          el('div', {class: 'item-footer'},
            el('span', {class: 'consume'},
              [r.eaten_today ? `吃掉 ${r.eaten_today}` : '', r.cooked_today ? `做菜 ${r.cooked_today}` : '',
               r.given_today ? `递过 ${r.given_today}` : ''].filter(Boolean).join(' · ') || '今日未动'),
            hasTier('write') ? [
              el('button', {type: 'button', class: 'btn sm', disabled: r.qty <= r.held,
                onclick: () => changeQty('set', {id: r.id, delta: -1}, `${r.name} -1`)}, '-1'),
              el('button', {type: 'button', class: 'btn sm',
                onclick: () => changeQty('set', {id: r.id, delta: 1}, `${r.name} +1`)}, '+1'),
              el('button', {type: 'button', class: 'btn sm',
                onclick: () => changeQty('set', {id: r.id, delta: 4}, `${r.name} +4`)}, '+4'),
              r.qty > 0 ? el('button', {type: 'button', class: 'btn sm delete', disabled: r.held > 0,
                onclick: () => changeQty('del', {id: r.id}, `已清零 ${r.name}`)}, '清零') : null,
            ] : null))));
    }).filter(Boolean);

    const byId = Object.fromEntries(allRows.map(r => [r.id, r]));
    const takes = data.takes || [];
    const searchInp = el('input', {
      type: 'search',
      placeholder: '搜索品名，比如 草莓、鸡蛋、饼干……',
      value: q,
      oninput: e => { q = e.target.value.trim().toLowerCase(); draw(); },
    });

    put(box, el('div', {class: 'kstock'},
      el('div', {class: 'stock-intro'},
        el('div', {class: 'stock-mark'}, icon('fridge')),
        el('div', {class: 'grow'},
          el('h2', {}, '小屋厨房与零食储藏'),
          el('p', {}, '由生活内核实时维护；投喂的零食可在「走进小屋」房间里亲自递给住客。')),
        el('div', {class: 'stock-metrics'},
          el('div', {}, el('b', {}, totalKinds), el('small', {}, '现有种类')),
          el('div', {}, el('b', {}, totalQty), el('small', {}, '总库存')),
          el('div', {}, el('b', {}, todayUsed), el('small', {}, '今日取用'))),
        hasTier('write') ? el('button', {type: 'button', class: 'btn primary', onclick: openAddModal}, icon('plus'), '添置食材') : null),

      el('div', {class: 'filters'},
        el('div', {class: 'search-box'}, icon('search'), searchInp),
        el('select', {value: shelfPick, onchange: e => { shelfPick = e.target.value; draw(); }},
          el('option', {value: 'all'}, '全部货架'),
          Object.entries(SHELVES).map(([k, [lb]]) => el('option', {value: k}, lb))),
        el('button', {type: 'button', class: 'btn sm ' + (!onlyHave ? 'primary' : ''),
          onclick: () => { onlyHave = !onlyHave; draw(); }}, onlyHave ? '只看有货' : '含已用完')),

      el('div', {class: 'inventory-layout'},
        grouped.length ? el('div', {class: 'stock-groups'}, grouped) : emptyState({title: '当前筛选没有库存项目'}),
        el('aside', {class: 'stock-aside'},
          el('section', {class: 'card'},
            el('h2', {}, '今日取用动态'),
            takes.length ? takes.slice(0, 12).map(t => el('div', {class: 'take'},
              el('time', {}, hm(t.at)),
              el('b', {}, t.kind === 'gift' ? `房主递给 ${nameOf(t.to)}`
                : `${nameOf(t.actor)} · ${t.kind === 'cook' ? '做饭取用' : '吃了'}`),
              el('p', {}, (t.items || []).map(x => `${(byId[x.id]?.name || x.id.split('@')[0])} ×${x.qty}`).join('、'))))
              : el('p', {class: 'small muted'}, '今天还没有取用食材的记录。'))))));
  }

  await load();
  return {refresh: load, cleanup: () => { alive = false; }};
}

/* ---------------------------------------------------------------- 晴空都出行 */
async function renderCity(view){
  let state = null, alive = true;
  const box = el('div', {}, loading());
  view.append(box);
  const DESTS = [
    ['ryokogai:kissa-tomoshibi', '棱光街 · 喫茶「灯」'],
    ['ryokogai:shiorido', '棱光街 · 书店「书签堂」'],
    ['ryokogai:minami-park', '棱光街 · 南琉璃公园'],
    ['kumominato:muginohi', '风港 · 面包店「麦穗」'],
    ['kumominato:kamitsuki', '风港 · 文具店「纸鸢」'],
  ];

  const load = async () => { state = await api('/api/state'); draw(); };

  function draw(){
    if (!alive || !state) return;
    if (!RESIDENTS.length){
      put(box, emptyState({title: '小屋里还没有住客', hint: '先迎接第一位住客后即可派出晴空都行程。', actionLabel: '迎接第一位住客', onAction: () => go('#/setup')}));
      return;
    }
    const trips = Object.entries(state.city_trips || {});
    const actorSel = el('select', {}, RESIDENTS.map(r => el('option', {value: r.actor}, r.name)));
    const destInp = el('input', {type: 'text', value: DESTS[0][0], style: 'flex:1'});
    const departBtn = el('button', {
      type: 'button', class: 'btn primary', disabled: !hasTier('write'),
      onclick: async () => {
        try {
          const res = await api('/api/city/depart', {actor_id: actorSel.value, destination: destInp.value.trim() || DESTS[0][0]});
          toast(`${nameOf(actorSel.value)} 出发了（${res.mode || 'outing'}）`);
          await load();
        } catch (e){ toast(e.message, true); }
      },
    }, icon('globe'), '出发去逛逛');

    put(box,
      el('section', {class: 'card'},
        el('div', {class: 'chead'},
          el('h2', {}, '晴空都 / 公共城市联动'),
          el('span', {class: 'pill ' + (state.city_enabled ? 'ok' : '')}, state.city_enabled ? '已连接晴空都 MCP' : '本地出行模式')),
        el('p', {class: 'small muted'}, state.city_enabled
          ? '住客可以通过 CityBridge / MCP 插件真实前往晴空都的各处地点，并在返回时把纪念品、新衣物和回忆带回小屋。'
          : '未挂载晴空都扩展时，派出行程会作为本地散步/外宿事件在生活内核内流转。')),

      el('section', {class: 'card mt'},
        el('div', {class: 'chead'}, el('h2', {}, '派出一名住客')),
        el('div', {class: 'row wrap', style: 'gap:10px'}, actorSel, destInp, departBtn),
        el('div', {class: 'chips mt'}, DESTS.map(([code, lb]) =>
          el('button', {type: 'button', class: 'chip', onclick: () => { destInp.value = code; }}, lb)))),

      el('section', {class: 'card mt'},
        el('div', {class: 'chead'}, el('h2', {}, `正在外出的行程（${trips.length}）`)),
        trips.length ? trips.map(([aid, sess]) => el('div', {class: 'li'},
          ava(byActor(aid), 'sm'),
          el('div', {class: 'grow'},
            el('b', {class: 't'}, `${nameOf(aid)} · ${sess.destination || sess.place || '晴空都'}`),
            el('div', {class: 'd'}, `状态：${sess.state || 'away'} · 会话 ${sess.session_id || '—'}`)),
          hasTier('write') ? [
            el('button', {type: 'button', class: 'btn sm', onclick: async () => {
              try { await api('/api/city/return', {actor_id: aid}); toast(`已叫 ${nameOf(aid)} 回家`); await load(); }
              catch (e){ toast(e.message, true); }
            }}, '叫她回家'),
            el('button', {type: 'button', class: 'btn sm warn', onclick: async () => {
              try { await api('/api/city/return', {actor_id: aid, force: true, abandon_remote: true}); toast(`已召回 ${nameOf(aid)}`); await load(); }
              catch (e){ toast(e.message, true); }
            }}, '强制召回'),
          ] : null))
          : emptyState({title: '目前大家都待在小屋里', iconName: 'home'})));
  }

  await load();
  return {refresh: load, cleanup: () => { alive = false; }};
}
