// 模型用量账本 (#/usage): 仅 read 权限可用，展示多维 Token 聚合及最近调用明细（响应式无障碍数据表）。
import {el, put, api, icon, pageHead, toast} from '../core.js';

const PERIODS = [
  ['today', '今日'],
  ['7d', '近 7 天'],
  ['30d', '近 30 天'],
  ['all', '全部'],
];

const PURPOSE_LABELS = {
  chat: '对话回复',
  life: '生活决策',
  memory_extract: '记忆整理',
  diary: '睡前日记',
  proactive: '主动发言',
  ping: '连通探测',
};

const fmtNum = v => Number(v || 0).toLocaleString('zh-CN');

function fmtJst(ts){
  if (!ts) return '—';
  const d = new Date(Number(ts) * 1000 + 9 * 3600 * 1000);
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mi = String(d.getUTCMinutes()).padStart(2, '0');
  const ss = String(d.getUTCSeconds()).padStart(2, '0');
  return `${mm}-${dd} ${hh}:${mi}:${ss}`;
}

function buildUsageTable({id, caption, headers, rows, emptyText}){
  const capId = `usage-cap-${id}`;
  const styleVars = headers
    .slice(1)
    .map((h, i) => `--label-${i + 2}: "${String(h).replace(/"/g, '\\"')}"`)
    .join('; ');

  const tbody = el('tbody', {},
    rows.length
      ? rows.map(cells => el('tr', {},
          el('th', {scope: 'row'}, cells[0]),
          cells.slice(1).map(c => el('td', {}, c))))
      : el('tr', {}, el('td', {colspan: headers.length, class: 'muted small'}, emptyText || '当前时间范围暂无调用记录')));

  return el('section', {class: 'card mt'},
    el('div', {
      class: 'table-wrapper',
      tabindex: '0',
      role: 'region',
      'aria-labelledby': capId,
    },
    el('table', {class: 't usage-table', style: styleVars},
      el('caption', {id: capId}, caption),
      el('thead', {}, el('tr', {}, headers.map(h => el('th', {scope: 'col'}, h)))),
      tbody)));
}

export async function render(view){
  const box = el('div');
  view.append(pageHead('模型用量账本', '按用途、住客、模型与近期流水核对本地大模型 Token 消耗，所有数据均来自本地 usage.db。'), box);

  let period = '7d';
  let data = null;
  let alive = true;

  async function load(quiet = false){
    try {
      const doc = await api(`/api/usage?period=${encodeURIComponent(period)}&recent_limit=40`);
      if (!alive) return;
      data = doc;
      draw();
    } catch (e){
      if (!quiet) toast(e.message || '读取用量失败', true);
    }
  }

  function draw(){
    if (!alive || !data) return;
    const tot = data.totals || {};
    const periodBar = el('div', {class: 'seg usage-periods', role: 'group', 'aria-label': '统计周期'},
      PERIODS.map(([k, lb]) => el('button', {
        type: 'button',
        class: period === k ? 'on' : '',
        onclick: () => { period = k; load(); },
      }, lb)));

    const refreshBtn = el('button', {
      type: 'button',
      class: 'btn sm',
      onclick: () => load(),
    }, icon('refresh'), '刷新');

    const kpis = el('div', {class: 'usage-kpis mt'},
      [
        ['调用总次数', fmtNum(tot.calls), `成功 ${fmtNum(tot.ok_calls)} / 失败 ${fmtNum(tot.failed_calls)}`],
        ['输入 Token', fmtNum(tot.prompt_tokens), 'Prompt Tokens'],
        ['输出 Token', fmtNum(tot.completion_tokens), 'Completion Tokens'],
        ['合计 Token', fmtNum(tot.total_tokens), `故障转移 ${fmtNum(tot.fallback_calls)} 次`],
      ].map(([title, num, sub]) => el('div', {class: 'card usage-kpi'},
        el('div', {class: 'small muted'}, title),
        el('div', {class: 'usage-num'}, num),
        el('div', {class: 'tiny muted'}, sub))));

    const purposeTable = buildUsageTable({
      id: 'purpose',
      caption: '按任务用途统计',
      headers: ['用途', '调用', '失败', '输入 Token', '输出 Token', '总 Token'],
      rows: (data.by_purpose || []).map(r => [
        PURPOSE_LABELS[r.purpose] ? `${PURPOSE_LABELS[r.purpose]} (${r.purpose})` : (r.purpose || '—'),
        fmtNum(r.calls),
        fmtNum(r.failed_calls),
        fmtNum(r.prompt_tokens),
        fmtNum(r.completion_tokens),
        fmtNum(r.total_tokens),
      ]),
    });

    const actorTable = buildUsageTable({
      id: 'actor',
      caption: '按住客角色统计',
      headers: ['住客 ID', '调用', '失败', '输入 Token', '输出 Token', '总 Token'],
      rows: (data.by_actor || []).map(r => [
        r.actor || '—',
        fmtNum(r.calls),
        fmtNum(r.failed_calls),
        fmtNum(r.prompt_tokens),
        fmtNum(r.completion_tokens),
        fmtNum(r.total_tokens),
      ]),
    });

    const modelTable = buildUsageTable({
      id: 'model',
      caption: '按模型与接口主机统计',
      headers: ['模型', '接口主机', '调用', '输入 Token', '输出 Token', '总 Token'],
      rows: (data.by_model || []).map(r => [
        r.model || '—',
        r.base_url_host || '—',
        fmtNum(r.calls),
        fmtNum(r.prompt_tokens),
        fmtNum(r.completion_tokens),
        fmtNum(r.total_tokens),
      ]),
    });

    const recentTable = buildUsageTable({
      id: 'recent',
      caption: '最近 40 次模型调用明细',
      headers: ['时间 (JST)', '用途', '住客', '模型', '总 Token', '耗时 / 状态'],
      rows: (data.recent || []).map(r => [
        fmtJst(r.ts),
        PURPOSE_LABELS[r.purpose] || r.purpose || '—',
        r.actor || '—',
        r.model || '—',
        fmtNum(r.total_tokens),
        `${fmtNum(r.latency_ms)} ms · ${r.ok ? (r.fallback_used ? '备线成功' : '成功') : (r.error_code || '失败')}`,
      ]),
    });

    put(box,
      el('div', {class: 'row between wrap', style: 'gap:10px;align-items:center'},
        periodBar,
        el('div', {class: 'row wrap', style: 'gap:8px;align-items:center'},
          el('a', {class: 'btn sm', href: '#/ops'}, icon('gear'), '返回管家'),
          refreshBtn)),
      kpis,
      tot.calls === 0
        ? el('div', {class: 'card mt small muted'}, '当前处于零配置演示模式或本周期尚无外部模型调用记录；在「管家」配置 OpenAI 兼容模型后，每次对话、日记与记忆整理均会自动入账。')
        : null,
      purposeTable,
      actorTable,
      modelTable,
      recentTable);
  }

  await load();
  return {refresh: () => load(true), cleanup: () => { alive = false; }};
}
