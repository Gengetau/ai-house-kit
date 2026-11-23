// 珍藏页 (#/keep/<tab>): 精装日记书架与翻页器 / 小屋相册 / 长期记忆筛选与遗忘 / 书架便签。
import {el, put, api, icon, ava, go, setCurrentActor, pageHead, pageTabs, residentFilter,
  emptyState, errState, ownerOnly, loading, confirmBox, dialog, fmtTime, jstDate, hasTier, isOnboarding, toast, LoginNeeded, bindMediaBlob} from '../core.js';
import {RESIDENTS, byActor, nameOf, toneStyle} from '../residents.js';
import {MEM_SCOPES, MEM_KINDS} from '../vocab.js';

const TABS = [['diary', '日记书架'], ['album', '小屋相册'], ['memory', '长期记忆'], ['nook', '书架便签']];

export async function render(view, route){
  if (isOnboarding()){
    go('#/setup');
    return {};
  }
  const tab = TABS.some(([k]) => k === route.parts[1]) ? route.parts[1] : 'diary';
  const slot = el('div');
  view.append(
    pageHead('珍藏与手记', '每晚写下的日记配图、分域隔离的小屋相册、长期记忆与书架便签。'),
    pageTabs(TABS.map(([k, l]) => [k, l, `#/keep/${k}`]), tab),
    slot);

  if (tab === 'diary') return renderDiary(slot, route.parts[2] || '');
  if (tab === 'album') return renderAlbum(slot, route.q || {});
  if (tab === 'memory') return renderMemory(slot, route.q || {});
  return renderNook(slot);
}

/* ---------------------------------------------------------------- 日记书架与双页翻书 */
async function renderDiary(view, actorArg){
  if (!RESIDENTS.length){
    view.append(emptyState({title: '还没有住客入住', hint: '迎接第一位住客后，她的日记本会放在这里。', actionLabel: '去迎接第一位住客', onAction: () => go('#/setup')}));
    return {};
  }
  if (actorArg && byActor(actorArg)){
    setCurrentActor(actorArg);
    return diaryReader(view, byActor(actorArg));
  }
  const box = el('div', {}, loading());
  view.append(box);

  const load = async () => {
    const d = await api('/api/diaries?limit=200');
    const all = d.diaries || [];
    put(box, el('div', {class: 'shelf'}, RESIDENTS.map(r => {
      const mine = all.filter(x => x.actor === r.actor);
      const latest = mine[0];
      const open = () => go('#/keep/diary/' + r.actor);
      return el('div', {class: 'bookcard', style: toneStyle(r)},
        el('button', {class: 'cover' + (mine.length ? '' : ' empty-book'), onclick: open, 'aria-label': `打开 ${r.name} 的日记`},
          el('span', {class: 'emb'}, ava(r)),
          el('span', {class: 'ttl'}, r.name, el('small', {}, r.latin))),
        el('div', {class: 'info'},
          el('h3', {}, r.name, ava(r, 'xs')),
          el('div', {class: 'meta'}, r.bio || '每晚 22:30 后整理今日点滴写入日记'),
          el('div', {class: 'meta'}, latest
            ? [icon('calendar'), ` 最近：${latest.day}`, el('br'), `共 ${mine.length} 篇`]
            : '这本还没有日记'),
          el('button', {type: 'button', class: 'btn primary', onclick: open}, mine.length ? '翻开阅读' : '查看详情', icon('arrowR'))));
    })));
  };

  await load();
  return {refresh: load};
}

async function diaryReader(view, R){
  const backBar = el('div', {class: 'row between wrap', style: 'margin-bottom:14px'},
    el('button', {type: 'button', class: 'btn sm', onclick: () => go('#/keep/diary')}, icon('chevL'), '返回书架'),
    el('span', {class: 'small muted'}, '点击书页角落或按左右方向键翻页'));
  const strip = el('div', {class: 'rstrip', style: 'margin-bottom:14px'},
    RESIDENTS.map(res => el('button', {
      type: 'button', style: toneStyle(res), class: res.actor === R.actor ? 'on' : '',
      onclick: () => go('#/keep/diary/' + res.actor),
    }, ava(res, 'sm'), el('span', {class: 'n'}, res.name))));
  const stage = el('div', {}, loading());
  view.append(backBar, strip, stage);

  let idx = 0;
  const load = async () => {
    const d = await api(`/api/diaries?actor=${encodeURIComponent(R.actor)}&limit=100`);
    const rows = (d.diaries || []).slice().reverse();
    if (!rows.length){
      put(stage, el('div', {class: 'dread'},
        el('div', {class: 'toc'}, el('div', {class: 'mon'}, el('b', {}, R.name + '的目录')),
          el('p', {class: 'small muted', style: 'padding:8px'}, '还没有日记记录，把管家时钟快进到 22:30 后再推进 1 步即可生成。')),
        el('div', {class: 'book single', style: toneStyle(R)},
          el('div', {class: 'pages'},
            el('div', {class: 'page blank'}, el('div', {class: 'flow'},
              emptyState({title: `${R.name}的日记本还是空的`, hint: '每天晚上 22:30–03:00（东京时间）会写下今日见闻。', iconName: 'bookOpen'})))))));
      return;
    }
    idx = rows.length - 1;
    const drawBook = () => {
      const e = rows[idx];
      const prev = idx > 0 ? rows[idx - 1] : null;
      const single = window.matchMedia('(max-width: 900px)').matches;
      const renderPage = (item, pno, cornerSide) => {
        if (!item) return el('div', {class: 'page blank'}, el('div', {class: 'flow'}, '— 翻至最新一页 —'));
        const paras = String(item.text || '').replace(/\r\n/g, '\n').split(/\n+/).map(s => s.trim()).filter(Boolean);
        const isHeroArt = Boolean(item === e && (single || cornerSide === 'r'));
        const artAttrs = {
          alt: item.image_title || `${item.day} 日记配图`,
          class: 'dimg',
          width: 480,
          height: 360,
        };
        if (isHeroArt) artAttrs.fetchpriority = 'high';
        else artAttrs.loading = 'lazy';
        // The list no longer embeds pictures: each one is fetched (with the token) only when its page is drawn.
        let artNode = null;
        if (item.image_data_uri || item.image_media_rel){
          if (item.image_data_uri) artAttrs.src = item.image_data_uri;
          const artImg = el('img', artAttrs);
          if (!item.image_data_uri) bindMediaBlob(artImg, item.image_media_rel);
          artNode = el('figure', {class: 'diary-art'}, artImg);
        }
        return el('div', {class: 'page'},
          el('div', {class: 'flow'},
            el('h3', {}, item.day + ' 的日记'),
            el('div', {class: 'dmeta'},
              fmtTime(item.created_at, true),
              el('span', {class: 'tag'}, item.mode === 'live_api' ? 'LLM 亲笔' : '内置模板')),
            artNode,
            paras.map(p => el('p', {}, p))),
          el('div', {class: 'pno'}, `P.${pno}`),
          cornerSide === 'l' && idx > 0 ? el('button', {class: 'corner l', 'aria-label': '上一篇', onclick: () => { idx--; drawBook(); }}) : null,
          cornerSide === 'r' && idx < rows.length - 1 ? el('button', {class: 'corner r', 'aria-label': '下一篇', onclick: () => { idx++; drawBook(); }}) : null);
      };

      const toc = el('div', {class: 'toc'},
        el('div', {class: 'mon'}, el('b', {}, `${rows.length} 篇日记`)),
        rows.slice().reverse().map(item => {
          const realIdx = rows.indexOf(item);
          return el('button', {
            type: 'button',
            class: 'day' + (realIdx === idx ? ' on' : ''),
            onclick: () => { idx = realIdx; drawBook(); },
          }, el('b', {}, item.day), el('small', {}, item.text));
        }));

      const pagesNode = single
        ? el('div', {class: 'pages'}, renderPage(e, idx + 1, idx > 0 ? 'l' : 'r'))
        : el('div', {class: 'pages'},
            renderPage(prev, idx, 'l'),
            renderPage(e, idx + 1, 'r'));

      const bookWrap = el('div', {},
        el('div', {
          class: 'book' + (single ? ' single' : ''),
          style: toneStyle(R),
          tabindex: '0',
          onkeydown: ev => {
            if (ev.key === 'ArrowLeft' && idx > 0){ idx--; drawBook(); }
            if (ev.key === 'ArrowRight' && idx < rows.length - 1){ idx++; drawBook(); }
          },
        }, pagesNode, el('span', {class: 'ribbon'})),
        el('div', {class: 'dnav'},
          el('button', {type: 'button', class: 'btn sm', disabled: idx <= 0, onclick: () => { idx--; drawBook(); }}, icon('chevL'), '上一篇'),
          el('span', {class: 'pos'}, `${idx + 1} / ${rows.length} · ${e.day}`),
          el('button', {type: 'button', class: 'btn sm', disabled: idx >= rows.length - 1, onclick: () => { idx++; drawBook(); }}, '下一篇', icon('chevR'))));

      put(stage, el('div', {class: 'dread'}, toc, bookWrap));
    };
    drawBook();
  };

  await load();
  return {};
}

/* ---------------------------------------------------------------- 小屋相册 */
async function renderAlbum(view, q){
  let scope = 'world', actor = (q.who && byActor(q.who)) ? q.who : '', alive = true;
  const filterCard = el('div', {class: 'card', style: 'margin-bottom:14px'});
  const stage = el('div', {}, loading());
  view.append(filterCard, stage);

  function drawFilters(){
    const canPriv = hasTier('private');
    const scSeg = el('div', {class: 'seg'},
      [['world', '公共生活'], ['group', '群聊相册'], ['private', '私密相册']].map(([k, lb]) =>
        el('button', {
          type: 'button',
          class: scope === k ? 'on' : '',
          onclick: () => {
            scope = k;
            if (scope === 'private' && !actor) actor = RESIDENTS[0]?.actor || '';
            load();
          },
        }, k === 'private' && !canPriv ? [icon('lock'), ' ' + lb] : lb)));

    put(filterCard,
      residentFilter(actor, a => {
        actor = a;
        load();
      }, {allLabel: scope === 'private' ? false : '全部住客'}),
      el('div', {class: 'row wrap', style: 'gap:10px'}, scSeg));
  }

  function previewItem(it){
    const res = byActor(it.actor);
    let fullImg = null;
    if (it.data_uri || it.media_rel){
      fullImg = el('img', {alt: it.title || '相册照片'});
      if (it.data_uri) fullImg.src = it.data_uri;
      else bindMediaBlob(fullImg, it.media_rel);
    }
    const body = el('div', {class: 'stack'},
      fullImg ? el('div', {class: 'album-full'}, fullImg) : null,
      el('div', {class: 'row wrap', style: 'gap:8px'},
        el('span', {class: 'tag'}, MEM_SCOPES[it.scope] || it.scope),
        it.actor ? el('span', {class: 'tag'}, nameOf(it.actor)) : null,
        it.diary_day ? el('span', {class: 'tag'}, it.diary_day) : null,
        el('span', {class: 'tiny muted'}, fmtTime(it.created_at, true))),
      it.caption ? el('p', {class: 'small', style: 'margin:0;line-height:1.7'}, it.caption) : null);
    dialog({
      title: (res ? nameOf(res.actor) + ' · ' : '') + (it.title || '相册记录'),
      body,
      buttons: [['关闭', true, 'primary']],
      wide: true,
    });
  }

  async function load(){
    drawFilters();
    if (!alive) return;
    if (scope === 'private' && !hasTier('private')){
      put(stage, ownerOnly('房主私密相册', '私聊分享的照片仅存放在私密作用域，需要 Owner 或 Private 令牌才能查看。'));
      return;
    }
    put(stage, loading());
    const qs = new URLSearchParams({scope, limit: '24', inline: '0'});
    if (actor) qs.set('actor', actor);
    try {
      const d = await api('/api/album?' + qs.toString());
      if (!alive) return;
      if (!d.enabled){
        put(stage, emptyState({
          kind: 'empty',
          title: '相册与日记配图插件尚未开启',
          hint: '此功能默认关闭，可在插件配置中心按需开启；私聊照片与公开相册完全分域隔离。',
          actionLabel: hasTier('owner') ? '前往插件设置' : '',
          onAction: hasTier('owner') ? () => go('#/settings') : null,
          iconName: 'image',
        }));
        return;
      }
      const items = d.items || [];
      put(stage, items.length
        ? el('div', {class: 'album-grid'}, items.map((it, idx) => {
            const res = byActor(it.actor);
            let thumbNode = icon('image');
            if (it.data_uri || it.media_rel){
              const imgAttrs = {
                alt: it.title || '小屋照片',
                width: 480,
                height: 360,
              };
              if (it.data_uri) imgAttrs.src = it.data_uri;
              if (idx === 0) imgAttrs.fetchpriority = 'high';
              else imgAttrs.loading = 'lazy';
              const imgEl = el('img', imgAttrs);
              if (!it.data_uri && it.media_rel) bindMediaBlob(imgEl, it.media_rel);
              thumbNode = imgEl;
            }
            return el('article', {class: 'card album-card', style: res ? toneStyle(res) : ''},
              el('button', {
                type: 'button',
                class: 'album-thumb',
                'aria-label': `查看大图：${it.title || '小屋照片'}`,
                onclick: () => previewItem(it),
              }, thumbNode),
              el('div', {class: 'album-meta'},
                el('div', {class: 'row between wrap', style: 'gap:6px'},
                  el('b', {}, it.title || '瞬间留念'),
                  el('span', {class: 'tag'}, MEM_SCOPES[it.scope] || it.scope)),
                el('div', {class: 'row wrap', style: 'gap:6px'},
                  res ? ava(res, 'xs') : null,
                  el('span', {class: 'tiny muted'}, it.actor ? nameOf(it.actor) : '合租屋'),
                  el('span', {class: 'tiny muted'}, fmtTime(it.created_at, true))),
                it.caption ? el('p', {class: 'small muted album-cap'}, it.caption) : null));
          }))
        : emptyState({
            kind: 'empty',
            title: '这个分域下还没有照片',
            hint: scope === 'private'
              ? '房主私聊分享的照片会自动归入这里，绝不流出到群聊或公开相册。'
              : '开启文生图并在每晚生成日记后，公开插画会按天保存在这里。',
            iconName: 'image',
          }));
    } catch (e){
      if (e instanceof LoginNeeded) throw e;
      put(stage, errState(e, {onRetry: load}));
    }
  }

  await load();
  return {refresh: load, cleanup: () => { alive = false; }};
}

/* ---------------------------------------------------------------- 长期记忆 */
async function renderMemory(view, q){
  let scope = 'world', actor = (q.who && byActor(q.who)) ? q.who : '', queryText = '';
  let memories = [], alive = true;
  const filterCard = el('div', {class: 'card', style: 'margin-bottom:14px'});
  const listBox = el('div', {}, loading());
  view.append(filterCard, listBox);

  const searchInp = el('input', {
    type: 'search',
    placeholder: '在当前记忆中按关键词过滤……',
    style: 'flex:1',
    oninput: e => { queryText = e.target.value.trim().toLowerCase(); drawList(); },
  });

  function drawFilters(){
    const canPriv = hasTier('private');
    const scSeg = el('div', {class: 'seg'},
      [['world', '世界公共'], ['group', '群聊记忆'], ['private', '房主私密']].map(([k, lb]) =>
        el('button', {
          type: 'button',
          class: scope === k ? 'on' : '',
          onclick: () => {
            scope = k;
            if (scope === 'private' && !actor) actor = RESIDENTS[0]?.actor || 'nell';
            load();
          },
        }, k === 'private' && !canPriv ? [icon('lock'), ' ' + lb] : lb)));

    put(filterCard,
      residentFilter(actor, a => {
        if (scope === 'private' && !a) return;
        actor = a;
        load();
      }, {allLabel: scope === 'private' ? false : '全部住客'}),
      el('div', {class: 'row wrap', style: 'gap:10px'}, scSeg, searchInp));
  }

  async function forgetMem(m){
    const ok = await confirmBox('遗忘这条记忆', `确定要让 ${nameOf(m.owner)} 忘掉这条记忆吗？\n\n「${m.text}」`, '确定遗忘', 'danger');
    if (!ok) return;
    try {
      await api('/api/memories/forget', {
        actor: m.owner === 'shared' ? (actor || RESIDENTS[0].actor) : m.owner,
        scope,
        id: m.id,
        channel: m.channel || '',
      });
      toast('已从有效记忆中遗忘');
      await load();
    } catch (e){ toast(e.message, true); }
  }

  function drawList(){
    if (!alive) return;
    const shown = memories.filter(m => !queryText || (m.text || '').toLowerCase().includes(queryText) || (m.about || '').toLowerCase().includes(queryText));
    const canForget = scope === 'private' ? hasTier('private') : hasTier('write');
    put(listBox,
      shown.length ? el('div', {class: 'stack'}, shown.map(m => {
        const res = byActor(m.owner);
        return el('article', {class: 'card', style: res ? toneStyle(res) : ''},
          el('div', {class: 'row between wrap', style: 'gap:8px;margin-bottom:6px'},
            el('div', {class: 'row wrap', style: 'gap:6px'},
              ava(res, 'xs'),
              el('b', {}, nameOf(m.owner)),
              el('span', {class: 'tag'}, MEM_SCOPES[scope] || scope),
              el('span', {class: 'tag'}, MEM_KINDS[m.kind] || m.kind || '事实'),
              m.channel ? el('span', {class: 'tiny muted'}, m.channel) : null),
            el('div', {class: 'row', style: 'gap:8px'},
              el('span', {class: 'tiny muted'}, fmtTime(m.updated || m.created, true)),
              canForget ? el('button', {type: 'button', class: 'btn sm danger', onclick: () => forgetMem(m)}, icon('trash'), '忘掉') : null)),
          el('div', {style: 'white-space:pre-wrap;line-height:1.7'}, m.text));
      }))
        : emptyState({kind: memories.length ? 'filtered' : 'empty', title: '当前筛选下没有记忆记录'}));
  }

  async function load(){
    drawFilters();
    if (scope === 'private' && !hasTier('private')){
      put(listBox, ownerOnly('房主私密记忆', '私密作用域与聊天对话需要 Owner 或 Private 令牌才能查看。'));
      return;
    }
    put(listBox, loading());
    const qs = new URLSearchParams({scope, limit: '100'});
    if (actor) qs.set('actor', actor);
    try {
      const d = await api('/api/memories?' + qs.toString());
      memories = d.memories || [];
      drawList();
    } catch (e){
      if (e instanceof LoginNeeded) throw e;
      put(listBox, errState(e, {onRetry: load}));
    }
  }

  await load();
  return {refresh: load, cleanup: () => { alive = false; }};
}

/* ---------------------------------------------------------------- 书架便签 */
async function renderNook(view){
  const st = await api('/api/state');
  const notes = st.bookshelf_notes || [];
  view.append(notes.length
    ? el('div', {class: 'nook-grid'}, notes.map(n => el('article', {class: 'nook-card'},
        el('div', {class: 'row between'}, el('b', {}, n.title || n.author || '小屋便签'),
          n.created_at ? el('span', {class: 'tiny muted'}, fmtTime(n.created_at)) : null),
        el('p', {class: 'small', style: 'margin:0;white-space:pre-wrap;line-height:1.7'}, n.text || n.content || n.summary || ''))))
    : emptyState({title: '书架旁还没有便签', hint: '住客在讨论书籍或留下留言后会贴在这里。', iconName: 'book'}));
  return {};
}
