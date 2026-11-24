// 管家与运行态 (#/ops): 时间步进、住客增改与重置、大模型在线配置、4轴插件、Streamable MCP 接入与灯光锁门。
import {
  el, put, api, icon, ava, go, pageHead, confirmBox, toast, store,
  hasTier, isOnboarding, setOnboarding, wireFormValidation,
} from '../core.js';
import {RESIDENTS, setResidentsFromState, toneStyle} from '../residents.js';

const LLM_ENDPOINTS = [
  ['OpenAI', 'https://api.openai.com', 'gpt-4o-mini'],
  ['DeepSeek', 'https://api.deepseek.com', 'deepseek-chat'],
  ['SiliconFlow', 'https://api.siliconflow.cn/v1', 'Qwen/Qwen2.5-72B-Instruct'],
  ['本地 Ollama', 'http://127.0.0.1:11434/v1', 'qwen2.5:7b'],
];

export async function render(view, route, ctx){
  if (isOnboarding()){
    go('#/setup');
    return {};
  }
  const box = el('div');
  view.append(pageHead('小屋管家与运行态', '查看小屋时间、管理住客与大模型接入、查看插件运行态或复制 MCP 配置。'), box);
  let state = null, setupDoc = null, alive = true;

  async function load(){
    const [st, sd] = await Promise.all([
      api('/api/state'),
      hasTier('owner') ? api('/api/setup').catch(() => null) : Promise.resolve(null),
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
    setupDoc = sd;
    draw();
  }

  async function step(minutes){
    try {
      const res = await api('/api/step', minutes ? {minutes} : {});
      if (!alive) return;
      if (res && res.state){
        state = res.state;
        setResidentsFromState(state);
      }
      toast(minutes ? `时间快进 ${minutes} 分钟完成` : '推进 1 步完成');
      draw();
    } catch (e){
      toast(e.message, true);
    }
  }

  function copyText(text, label){
    if (navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(() => toast(`已复制${label}`)).catch(() => fallbackCopy(text, label));
    } else {
      fallbackCopy(text, label);
    }
  }
  function fallbackCopy(text, label){
    const ta = el('textarea', {readonly: '', style: 'position:fixed;top:-9999px;opacity:0'});
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try { document.execCommand('copy'); toast(`已复制${label}`); } catch { toast('请手动全选复制', true); }
    ta.remove();
  }

  function buildResidentsCard(){
    const canOwner = hasTier('owner');
    const resetBtn = canOwner ? el('button', {
      type: 'button',
      class: 'btn sm danger',
      onclick: async () => {
        const ok = await confirmBox(
          '重置小屋',
          '确定要清空当前住客、对话、长期记忆与日记数据库，并重新回到「迎接第一位住客」向导吗？（.env 中的令牌与大模型配置会保留）',
          '确定重置小屋',
          'danger',
        );
        if (!ok) return;
        try {
          const res = await api('/api/setup/reset', {});
          setOnboarding(true);
          if (res?.state) setResidentsFromState(res.state);
          ctx?.redrawNav?.();
          toast('小屋已重置，欢迎迎接新住客');
          await go('#/setup');
        } catch (err){
          toast(err.message, true);
        }
      },
    }, icon('trash'), '重置小屋') : null;

    return el('section', {class: 'card mt'},
      el('div', {class: 'chead'},
        el('h2', {}, icon('users'), `住客名册与入住管理（${RESIDENTS.length} 位）`),
        el('div', {class: 'acts'},
          canOwner ? el('a', {class: 'btn sm primary', href: '#/setup?add=1'}, icon('plus'), '邀请新住客') : null,
          resetBtn)),
      el('div', {class: 'g3 keep2', style: 'gap:10px'},
        RESIDENTS.map(r => el('div', {class: 'li', style: toneStyle(r)},
          ava(r, 'sm'),
          el('div', {class: 'grow'},
            el('b', {class: 't'}, r.name),
            el('div', {class: 'd'}, `@${r.actor} · ${r.bio || '小屋住客'}`)),
          canOwner ? el('a', {class: 'btn sm', href: `#/setup?edit=${encodeURIComponent(r.actor)}`}, icon('edit'), '编辑') : null))));
  }

  function buildLlmCard(){
    if (!hasTier('owner')) return null;
    const llm = setupDoc?.llm || {};
    const urlInp = el('input', {
      id: 'ops-llm-url',
      name: 'base_url',
      type: 'url',
      required: true,
      autocomplete: 'off',
      autocorrect: 'off',
      autocapitalize: 'none',
      spellcheck: false,
      value: llm.base_url || 'https://api.openai.com',
      'aria-describedby': 'ops-llm-url-hint',
      'data-error': 'ops-llm-url-err',
    });
    const modelInp = el('input', {
      id: 'ops-llm-model',
      name: 'model',
      type: 'text',
      required: true,
      autocomplete: 'off',
      autocorrect: 'off',
      autocapitalize: 'none',
      spellcheck: false,
      value: llm.model || 'gpt-4o-mini',
      'aria-describedby': 'ops-llm-model-hint',
      'data-error': 'ops-llm-model-err',
    });
    const keyInp = el('input', {
      id: 'ops-llm-key',
      name: 'api_key',
      type: 'password',
      autocomplete: 'off',
      'aria-describedby': 'ops-llm-key-hint',
      placeholder: llm.api_key_set ? `已保存密钥（${llm.api_key_masked}），留空保留原 Key` : 'sk-...',
    });
    const eyeBtn = el('button', {
      type: 'button',
      class: 'btn icon ghost',
      'aria-pressed': 'false',
      'aria-label': '显示或隐藏 API Key',
      onclick: () => {
        const show = keyInp.type === 'password';
        keyInp.type = show ? 'text' : 'password';
        eyeBtn.setAttribute('aria-pressed', String(show));
        eyeBtn.replaceChildren(icon(show ? 'eye' : 'eyeOff'));
      },
    }, icon('eyeOff'));

    const testStatus = el('div', {class: 'small muted', role: 'status'});
    const testBtn = el('button', {
      type: 'button',
      class: 'btn sm',
      onclick: async () => {
        testBtn.disabled = true;
        testStatus.textContent = '正在测试连通性…';
        testStatus.style.color = 'var(--ink-2)';
        try {
          const res = await api('/api/setup/test-llm', {
            base_url: urlInp.value.trim(),
            model: modelInp.value.trim(),
            api_key: keyInp.value.trim(),
          });
          if (res.ok){
            testStatus.textContent = `连通成功（${res.model}：${res.reply || 'OK'}）`;
            testStatus.style.color = 'var(--ok)';
          } else {
            testStatus.textContent = '测试失败：' + (res.error || '未知错误');
            testStatus.style.color = 'var(--bad)';
          }
        } catch (err){
          testStatus.textContent = '测试出错：' + err.message;
          testStatus.style.color = 'var(--bad)';
        } finally {
          testBtn.disabled = false;
        }
      },
    }, icon('bolt'), '测试连通性');

    const saveBtn = el('button', {type: 'submit', class: 'btn sm primary'}, icon('check'), '保存并生效');
    const clearBtn = llm.api_key_set ? el('button', {
      type: 'button',
      class: 'btn sm',
      onclick: async () => {
        try {
          const res = await api('/api/settings/llm', {base_url: urlInp.value.trim(), model: modelInp.value.trim(), clear_key: true});
          if (res.state) state = res.state;
          setupDoc = {...(setupDoc || {}), llm: res.llm};
          toast('已清空密钥并切回离线演示引擎');
          draw();
        } catch (err){
          toast(err.message, true);
        }
      },
    }, '切回离线演示') : null;

    let validator;
    const llmForm = el('form', {
      method: 'post',
      action: '/api/settings/llm',
      novalidate: true,
      class: 'stack',
      onsubmit: async e => {
        e.preventDefault();
        if (!validator.validateAll()) return;
        saveBtn.disabled = true;
        try {
          const res = await api('/api/settings/llm', {
            base_url: urlInp.value.trim(),
            model: modelInp.value.trim(),
            api_key: keyInp.value.trim(),
          });
          if (res.state) state = res.state;
          setupDoc = {...(setupDoc || {}), llm: res.llm};
          keyInp.value = '';
          toast(res.llm?.mode === 'live_api' ? `已切换为真实模型 (${res.llm.model})` : '模型地址与名称已保存');
          draw();
        } catch (err){
          toast(err.message, true);
          saveBtn.disabled = false;
        }
      },
    },
      el('div', {class: 'row wrap', style: 'gap:6px'},
        el('span', {class: 'tiny muted'}, '常用模板：'),
        LLM_ENDPOINTS.map(([lb, url, mdl]) => el('button', {
          type: 'button',
          class: 'chip',
          onclick: () => { urlInp.value = url; modelInp.value = mdl; validator?.syncField?.(urlInp); validator?.syncField?.(modelInp); },
        }, lb))),
      el('div', {class: 'g2'},
        el('div', {class: 'field-blk'},
          el('label', {for: 'ops-llm-url'}, 'Base URL ', el('span', {'aria-hidden': 'true'}, '*')),
          el('span', {id: 'ops-llm-url-hint', class: 'field-hint'}, 'OpenAI 兼容接口地址，例如 https://api.openai.com'),
          urlInp,
          el('div', {id: 'ops-llm-url-err', class: 'error-msg'}, el('span', {role: 'img', 'aria-label': '错误'}, '⚠'), ' 请输入合法的 http:// 或 https:// 地址。')),
        el('div', {class: 'field-blk'},
          el('label', {for: 'ops-llm-model'}, '模型名称 ', el('span', {'aria-hidden': 'true'}, '*')),
          el('span', {id: 'ops-llm-model-hint', class: 'field-hint'}, '例如 gpt-4o-mini、deepseek-chat。'),
          modelInp,
          el('div', {id: 'ops-llm-model-err', class: 'error-msg'}, el('span', {role: 'img', 'aria-label': '错误'}, '⚠'), ' 模型名称不能为空。'))),
      el('div', {class: 'field-blk'},
        el('label', {for: 'ops-llm-key'}, 'API Key（留空保留当前密钥）'),
        el('span', {id: 'ops-llm-key-hint', class: 'field-hint'}, '保存后将写入本地 .env 并立即热更新生效，无需重启服务端。'),
        el('div', {class: 'pwrow'}, icon('key'), keyInp, eyeBtn)),
      el('div', {class: 'row between wrap', style: 'gap:10px'},
        el('div', {class: 'row wrap', style: 'gap:8px;align-items:center'}, testBtn, testStatus),
        el('div', {class: 'row wrap', style: 'gap:8px'}, clearBtn, saveBtn)));
    validator = wireFormValidation(llmForm);

    return el('section', {class: 'card mt'},
      el('div', {class: 'chead'},
        el('h2', {}, icon('sparkle'), '大模型 API 接入配置'),
        el('div', {class: 'acts'},
          el('a', {class: 'btn sm', href: '#/usage'}, icon('activity'), '用量账本'),
          el('span', {class: 'chip ' + (state.llm_mode === 'live_api' ? 'ok' : '')},
            state.llm_mode === 'live_api' ? `在线：${state.model_name}` : '当前：零密钥离线演示'))),
      llmForm);
  }

  function draw(){
    if (!alive || !state) return;
    const mcpUrl = `${location.origin}/mcp`;
    const mcpJson = JSON.stringify({
      mcpServers: {
        'ai-house-kit': {
          url: mcpUrl,
          headers: {Authorization: 'Bearer <AI_HOUSE_OWNER_TOKEN>'},
        },
      },
    }, null, 2);
    const plugs = state.plugins || {};
    const curTheme = store.get('theme', 'auto');

    put(box,
      el('div', {class: 'g2'},
        el('section', {class: 'card'},
          el('div', {class: 'chead'},
            el('h2', {}, icon('clock'), '小屋时间'),
            el('span', {class: 'chip info'}, `${state.now_jst || '—'} 周${state.weekday || ''}`)),
          ...(state.manual_time_control ? [
            el('p', {class: 'small muted'}, '这是一栋用 --virtual-clock 启动的演示小屋：时间只在这里手动推进时才前进（用于演示、跑测日夜作息与晚间日记生成）。'),
            el('div', {class: 'row wrap', style: 'gap:8px;margin-top:12px'},
              el('button', {type: 'button', class: 'btn primary', disabled: !hasTier('owner'), onclick: () => step(0)}, icon('play'), '推进 1 步'),
              el('button', {type: 'button', class: 'btn', disabled: !hasTier('owner'), onclick: () => step(15)}, '+15 分钟'),
              el('button', {type: 'button', class: 'btn', disabled: !hasTier('owner'), onclick: () => step(60)}, '+60 分钟')),
            !hasTier('owner') ? el('p', {class: 'tiny muted mt'}, '只有房主令牌可以推进演示小屋的时间。') : null,
          ] : [
            el('p', {class: 'small muted'}, '小屋的时间由内核按真实时钟推进，这里不能补步或快进。想快速看完一天的作息，请另起一栋演示小屋：ai-house up --virtual-clock'),
          ])),

        el('section', {class: 'card'},
          el('div', {class: 'chead'},
            el('h2', {}, icon('bulb'), '主题灯光与入口'),
            el('span', {class: 'chip'}, 'v2 暖灯控制台')),
          el('div', {class: 'row wrap', style: 'gap:8px;margin-bottom:14px'},
            [['light', '日间浅色', 'sun'], ['dark', '暖夜深色', 'moon'], ['auto', '跟随系统', 'sun']].map(([k, lb, ic]) =>
              el('button', {
                type: 'button',
                class: 'btn ' + (curTheme === k ? 'primary' : ''),
                onclick: () => { ctx?.setTheme?.(k); draw(); },
              }, icon(ic), lb))),
          el('div', {class: 'row wrap', style: 'gap:8px'},
            el('a', {class: 'btn sm', href: '#/usage'}, icon('activity'), '模型用量账本'),
            el('a', {class: 'btn sm', href: '/chat', target: '_blank', rel: 'noopener'}, icon('msg'), '独立聊天页 (/chat)'),
            el('button', {type: 'button', class: 'btn sm danger', onclick: () => ctx?.logout?.()}, icon('lock'), '锁门并清除当前钥匙')))),

      buildResidentsCard(),
      buildLlmCard(),

      el('div', {class: 'g2 mt'},
        el('section', {class: 'card'},
          el('div', {class: 'chead'},
            el('h2', {}, icon('gear'), '引擎与四轴插件'),
            el('div', {class: 'acts'},
              hasTier('owner') ? el('a', {class: 'btn sm primary', href: '#/settings'}, icon('shield'), '打开插件配置中心') : null,
              el('span', {class: 'chip ' + (state.llm_mode === 'live_api' ? 'ok' : '')}, state.model_name || state.llm_mode))),
          el('div', {class: 'stack', style: 'gap:10px'},
            el('div', {class: 'row between'}, el('span', {class: 'muted'}, '当前世界包 (WorldPack)'), el('b', {}, state.pack_profile || 'life1')),
            el('div', {class: 'row between'}, el('span', {class: 'muted'}, '对话驱动'), el('b', {}, state.brain && state.brain !== 'direct' ? `agent 外壳（${state.brain}）` : (state.llm_mode === 'live_api' ? '真实 OpenAI 兼容模型' : '零密钥离线演示引擎'))),
            el('div', {class: 'row between'}, el('span', {class: 'muted'}, '发现的世界包'), el('span', {class: 'small'}, (plugs.packs || []).join('、') || 'default')),
            el('div', {class: 'row between'}, el('span', {class: 'muted'}, '消息通道 (Channels)'), el('span', {class: 'small'}, (plugs.channels || []).join('、') || 'webchat')),
            el('div', {class: 'row between'}, el('span', {class: 'muted'}, '扩展 (Extensions)'), el('span', {class: 'small'}, (plugs.extensions || []).join('、') || '无')),
            el('div', {class: 'row between'}, el('span', {class: 'muted'}, '大模型工具 (Tools)'), el('span', {class: 'small'}, (plugs.tools || []).join('、') || '默认内置工具')))),

        el('section', {class: 'card'},
          el('div', {class: 'chead'},
            el('h2', {}, icon('terminal'), 'Streamable MCP 接入'),
            el('button', {type: 'button', class: 'btn sm', onclick: () => copyText(mcpJson, ' MCP 配置')}, icon('fileText'), '复制配置')),
          el('p', {class: 'small muted', style: 'margin:0 0 10px'}, '把 AI 小屋直接作为标准 MCP Server 接入 Claude Desktop、Cursor 或其他智能体宿主：'),
          el('pre', {class: 'codebox'}, mcpJson))));
  }

  await load();
  return {refresh: load, cleanup: () => { alive = false; }};
}
