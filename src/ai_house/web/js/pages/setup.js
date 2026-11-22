// 住客入驻向导 (#/setup): 首次配置第一位 AI 住客、邀请新住客、编辑档案与可选大模型驱动。
import {
  el, put, api, icon, go, pageHead, emptyState, errState, ownerOnly, loading,
  hasTier, isOnboarding, setOnboarding, setCurrentActor, toast, wireFormValidation,
} from '../core.js';
import {setResidentsFromState} from '../residents.js';

const INSPIRATIONS = [
  {
    tag: '温柔料理系 · 小满',
    name: '小满',
    actor: '',
    sprite: 'sora',
    accent: '#B4532D',
    schedule_preset: 'early',
    cook_preset: 'chef',
    bio: '喜欢早起熬汤、随身带着采购手账的温柔室友。',
    persona: '你是小满，住在 AI 小屋里的合租室友。性格温柔细心，喜欢在清晨煮热汤、整理厨房货架，并在群里关心大家的日常冷暖。',
  },
  {
    tag: '安静书虫系 · 知夏',
    name: '知夏',
    actor: '',
    sprite: 'elsa',
    accent: '#0F766E',
    schedule_preset: 'regular',
    cook_preset: 'helper',
    bio: '常泡在书房看旧文库本，每晚认真记日记的室友。',
    persona: '你是知夏，住在 AI 小屋里的合租室友。喜欢安静阅读、手写笔记和泡红茶，说话温和克制，总能在关键时刻给出体贴建议。',
  },
  {
    tag: '夜猫创客 · 零',
    name: '零',
    actor: '',
    sprite: 'kuro',
    accent: '#7C3AED',
    schedule_preset: 'night_owl',
    cook_preset: 'foodie',
    bio: '深夜灵感爆发的创客兼游戏玩家，零食重度爱好者。',
    persona: '你是零，住在 AI 小屋里的夜猫子创客。偏爱傍晚后清醒干活，喜欢待在客厅和书房折腾新点子，话不多但对朋友特别讲义气。',
  },
];

const LLM_ENDPOINTS = [
  ['OpenAI', 'https://api.openai.com', 'gpt-4o-mini'],
  ['DeepSeek', 'https://api.deepseek.com', 'deepseek-chat'],
  ['SiliconFlow', 'https://api.siliconflow.cn/v1', 'Qwen/Qwen2.5-72B-Instruct'],
  ['本地 Ollama', 'http://127.0.0.1:11434/v1', 'qwen2.5:7b'],
];

const spriteImgCache = {};
function drawPixelPreview(canvas, spriteId){
  const spr = spriteId || 'sora';
  const ctx = canvas.getContext('2d');
  const draw = im => {
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(im, 0, 0, 32, 48, 0, 0, canvas.width, canvas.height);
  };
  let im = spriteImgCache[spr];
  if (!im){
    im = new Image();
    im.onload = () => draw(im);
    im.src = `/web/img/house/chars/${encodeURIComponent(spr)}.png`;
    spriteImgCache[spr] = im;
  } else if (im.complete && im.naturalWidth){
    draw(im);
  }
}

function spriteMiniCanvas(spriteId){
  const cv = el('canvas', {width: 32, height: 32, class: 'px', 'aria-hidden': 'true'});
  const im = spriteImgCache[spriteId] || (() => {
    const x = new Image();
    x.src = `/web/img/house/chars/${encodeURIComponent(spriteId)}.png`;
    spriteImgCache[spriteId] = x;
    return x;
  })();
  const paint = () => {
    const ctx = cv.getContext('2d');
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, 32, 32);
    ctx.drawImage(im, 0, 2, 32, 32, 0, 0, 32, 32);
  };
  if (im.complete && im.naturalWidth) paint();
  else im.addEventListener('load', paint, {once: true});
  return cv;
}

export async function render(view, route, ctx){
  if (!hasTier('owner')){
    view.append(ownerOnly('住客入驻与向导', '创建或编辑 AI 住客与大模型接入需要房主令牌 (AI_HOUSE_OWNER_TOKEN)。'));
    return {};
  }

  const editActor = (route?.q?.edit || '').trim();
  const isAdd = route?.q?.add === '1' || (!isOnboarding() && !editActor);
  const mode = editActor ? 'edit' : (isAdd && !isOnboarding()) ? 'add' : 'onboarding';

  const title = mode === 'edit' ? '编辑住客档案与人设'
    : mode === 'add' ? '邀请新住客搬进小屋'
    : '迎接你的第一位 AI 住客';
  const sub = mode === 'edit' ? '调整她的名字、简介、Prompt 人设、形象与作息偏好（不影响既有对话与记忆）。'
    : mode === 'add' ? '新室友会立刻带着自己的行囊和零花钱搬进空卧室，参与共同起居与对话。'
    : '先为小屋配置第 1 位一起生活的室友，之后随时可在小屋里继续邀请新住客或更换大模型。';

  const backHref = editActor ? `#/people/${encodeURIComponent(editActor)}/profile` : '#/home';
  const slot = el('div', {}, loading());
  view.append(
    pageHead(title, sub, mode !== 'onboarding' ? el('a', {class: 'btn sm', href: backHref}, icon('chevL'), '返回') : null),
    slot);

  let setupDoc;
  try {
    setupDoc = await api('/api/setup');
  } catch (e){
    put(slot, errState(e, {onRetry: () => go(location.hash || '#/setup')}));
    return {};
  }

  const existing = editActor ? (setupDoc.residents || []).find(r => r.actor === editActor) : null;
  if (mode === 'edit' && !existing){
    put(slot, emptyState({title: `找不到住客 ${editActor}`, actionLabel: '返回小屋', onAction: () => go('#/home')}));
    return {};
  }

  let selectedSprite = existing?.sprite || 'sora';
  let selectedAccent = existing?.accent || (setupDoc.accents?.[0] || '#B4532D');
  let selectedSched = existing?.schedule_preset || 'regular';
  let selectedCook = existing?.cook_preset || 'helper';
  let llmChoice = setupDoc.llm?.mode === 'live_api' ? 'api' : 'demo';

  // Form controls (ordered cleanly to match DOM tab order)
  const nameInp = el('input', {
    id: 'setup-name',
    name: 'name',
    type: 'text',
    required: true,
    maxlength: 24,
    autocomplete: 'off',
    value: existing?.name || '',
    'aria-describedby': 'setup-name-hint',
    'data-error': 'setup-name-err',
    placeholder: '例如：小满、星野',
  });
  const actorInp = el('input', {
    id: 'setup-actor',
    name: 'actor',
    type: 'text',
    pattern: '[a-z][a-z0-9_]{1,15}',
    maxlength: 16,
    readonly: mode === 'edit',
    autocomplete: 'off',
    autocorrect: 'off',
    autocapitalize: 'none',
    spellcheck: false,
    value: existing?.actor || '',
    'aria-describedby': 'setup-actor-hint',
    'data-error': 'setup-actor-err',
    placeholder: '留空自动生成（如 bot1）',
  });
  const bioInp = el('input', {
    id: 'setup-bio',
    name: 'bio',
    type: 'text',
    maxlength: 120,
    value: existing?.bio || '',
    'aria-describedby': 'setup-bio-hint',
    placeholder: '用一句话介绍她在小屋里的特点',
  });
  const personaInp = el('textarea', {
    id: 'setup-persona',
    name: 'persona',
    rows: 3,
    maxlength: 600,
    'aria-describedby': 'setup-persona-hint',
    placeholder: '描述她的性格、说话语气和兴趣爱好；留空将根据昵称与简介自动生成。',
  });
  personaInp.value = existing?.persona || '';

  const previewCv = el('canvas', {width: 96, height: 144, class: 'setup-sprite-cv', 'aria-hidden': 'true'});
  const previewName = el('b', {class: 'setup-preview-name'}, existing?.name || '第一位新住客');
  const previewCard = el('div', {class: 'setup-preview-card', style: `--c:${selectedAccent};--tone:${selectedAccent};`},
    previewCv,
    previewName,
    el('span', {class: 'tiny muted'}, '小屋像素实景预览'));

  nameInp.addEventListener('input', () => {
    previewName.textContent = nameInp.value.trim() || (mode === 'edit' ? existing.name : '新住客');
  });

  function updateSpritePreview(spr){
    selectedSprite = spr;
    drawPixelPreview(previewCv, spr);
  }
  function updateAccentPreview(col){
    selectedAccent = col;
    previewCard.style.setProperty('--c', col);
    previewCard.style.setProperty('--tone', col);
  }
  updateSpritePreview(selectedSprite);

  const spriteRadios = el('div', {class: 'sprite-grid'},
    (setupDoc.sprites || []).map(sp => {
      const rid = 'spr-' + sp.id;
      const radio = el('input', {
        type: 'radio',
        id: rid,
        name: 'sprite',
        value: sp.id,
        class: 'visually-hidden',
        checked: sp.id === selectedSprite,
        onchange: () => { if (radio.checked) updateSpritePreview(sp.id); },
      });
      return el('div', {class: 'rc-opt'},
        radio,
        el('label', {for: rid, class: 'sprite-opt'}, spriteMiniCanvas(sp.id), el('span', {}, sp.label)));
    }));

  const accentRadios = el('div', {class: 'accent-grid'},
    (setupDoc.accents || []).map((col, idx) => {
      const aid = 'acc-' + idx;
      const radio = el('input', {
        type: 'radio',
        id: aid,
        name: 'accent',
        value: col,
        class: 'visually-hidden',
        checked: col.toUpperCase() === selectedAccent.toUpperCase(),
        onchange: () => { if (radio.checked) updateAccentPreview(col); },
      });
      return el('div', {class: 'rc-opt'},
        radio,
        el('label', {for: aid, class: 'accent-opt', 'aria-label': `主题色 ${col}`},
          el('span', {class: 'swatch', style: `background:${col}`}),
          el('span', {class: 'tiny'}, col)));
    }));

  const schedRadios = el('div', {class: 'radio-cards'},
    (setupDoc.schedule_presets || []).map(p => {
      const pid = 'sched-' + p.id;
      const radio = el('input', {
        type: 'radio',
        id: pid,
        name: 'schedule_preset',
        value: p.id,
        class: 'visually-hidden',
        checked: p.id === selectedSched,
        onchange: () => { if (radio.checked) selectedSched = p.id; },
      });
      return el('div', {class: 'rc-opt'},
        radio,
        el('label', {for: pid, class: 'rc-card'}, el('b', {}, p.label)));
    }));

  const cookRadios = el('div', {class: 'radio-cards'},
    (setupDoc.cook_presets || []).map(p => {
      const pid = 'cook-' + p.id;
      const radio = el('input', {
        type: 'radio',
        id: pid,
        name: 'cook_preset',
        value: p.id,
        class: 'visually-hidden',
        checked: p.id === selectedCook,
        onchange: () => { if (radio.checked) selectedCook = p.id; },
      });
      return el('div', {class: 'rc-opt'},
        radio,
        el('label', {for: pid, class: 'rc-card'}, el('b', {}, p.label)));
    }));

  // Optional LLM configuration (shown cleanly in onboarding mode)
  const urlInp = el('input', {
    id: 'setup-llm-url',
    name: 'base_url',
    type: 'url',
    required: true,
    autocomplete: 'off',
    autocorrect: 'off',
    autocapitalize: 'none',
    spellcheck: false,
    value: setupDoc.llm?.base_url || 'https://api.openai.com',
    'aria-describedby': 'setup-llm-url-hint',
    'data-error': 'setup-llm-url-err',
  });
  const modelInp = el('input', {
    id: 'setup-llm-model',
    name: 'model',
    type: 'text',
    required: true,
    autocomplete: 'off',
    autocorrect: 'off',
    autocapitalize: 'none',
    spellcheck: false,
    value: setupDoc.llm?.model || 'gpt-4o-mini',
    'aria-describedby': 'setup-llm-model-hint',
    'data-error': 'setup-llm-model-err',
  });
  const keyInp = el('input', {
    id: 'setup-llm-key',
    name: 'api_key',
    type: 'password',
    required: !setupDoc.llm?.api_key_set,
    autocomplete: 'off',
    'aria-describedby': 'setup-llm-key-hint',
    'data-error': 'setup-llm-key-err',
    placeholder: setupDoc.llm?.api_key_set ? `已配置（${setupDoc.llm.api_key_masked}），留空保留原 Key` : 'sk-...',
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
  const llmTestStatus = el('div', {class: 'small muted', role: 'status'});
  const testLlmBtn = el('button', {
    type: 'button',
    class: 'btn sm',
    onclick: async () => {
      testLlmBtn.disabled = true;
      llmTestStatus.textContent = '正在测试模型连通性…';
      llmTestStatus.style.color = 'var(--ink-2)';
      try {
        const res = await api('/api/setup/test-llm', {
          base_url: urlInp.value.trim(),
          model: modelInp.value.trim(),
          api_key: keyInp.value.trim(),
        });
        if (res.ok){
          llmTestStatus.textContent = `连通成功（${res.model}，回复：${res.reply || 'OK'}）`;
          llmTestStatus.style.color = 'var(--ok)';
        } else {
          llmTestStatus.textContent = '连通失败：' + (res.error || '未知错误');
          llmTestStatus.style.color = 'var(--bad)';
        }
      } catch (e){
        llmTestStatus.textContent = '测试出错：' + e.message;
        llmTestStatus.style.color = 'var(--bad)';
      } finally {
        testLlmBtn.disabled = false;
      }
    },
  }, icon('bolt'), '测试连通性');

  const apiPanel = el('div', {class: 'llm-subpanel' + (llmChoice === 'api' ? '' : ' hidden')},
    el('div', {class: 'row wrap', style: 'gap:6px;margin-bottom:10px'},
      el('span', {class: 'tiny muted'}, '常用服务商：'),
      LLM_ENDPOINTS.map(([lb, url, mdl]) => el('button', {
        type: 'button',
        class: 'chip',
        onclick: () => { urlInp.value = url; modelInp.value = mdl; validator?.syncField?.(urlInp); validator?.syncField?.(modelInp); },
      }, lb))),
    el('div', {class: 'g2'},
      el('div', {class: 'field-blk'},
        el('label', {for: 'setup-llm-url'}, 'Base URL ', el('span', {'aria-hidden': 'true'}, '*')),
        el('span', {id: 'setup-llm-url-hint', class: 'field-hint'}, '以 http:// 或 https:// 开头的 OpenAI 兼容接口地址。'),
        urlInp,
        el('div', {id: 'setup-llm-url-err', class: 'error-msg'}, el('span', {role: 'img', 'aria-label': '错误'}, '⚠'), ' 请输入有效的 http:// 或 https:// 接口地址。')),
      el('div', {class: 'field-blk'},
        el('label', {for: 'setup-llm-model'}, '模型名称 ', el('span', {'aria-hidden': 'true'}, '*')),
        el('span', {id: 'setup-llm-model-hint', class: 'field-hint'}, '例如 gpt-4o-mini、deepseek-chat 或本地 Ollama 模型名。'),
        modelInp,
        el('div', {id: 'setup-llm-model-err', class: 'error-msg'}, el('span', {role: 'img', 'aria-label': '错误'}, '⚠'), ' 请填写模型名称。'))),
    el('div', {class: 'field-blk'},
      el('label', {for: 'setup-llm-key'}, 'API Key ', !setupDoc.llm?.api_key_set ? el('span', {'aria-hidden': 'true'}, '*') : null),
      el('span', {id: 'setup-llm-key-hint', class: 'field-hint'}, '保存在本地 .env 的 AI_HOUSE_API_KEY，从不传给浏览器以外的第三方。'),
      el('div', {class: 'pwrow'}, icon('key'), keyInp, eyeBtn),
      el('div', {id: 'setup-llm-key-err', class: 'error-msg'}, el('span', {role: 'img', 'aria-label': '错误'}, '⚠'), ' 请填写模型 API Key（或切回免密钥演示模式）。')),
    el('div', {class: 'row wrap', style: 'gap:10px;align-items:center'}, testLlmBtn, llmTestStatus));

  function syncLlmInputs(){
    const isApi = llmChoice === 'api';
    apiPanel.classList.toggle('hidden', !isApi);
    urlInp.disabled = !isApi;
    modelInp.disabled = !isApi;
    keyInp.disabled = !isApi;
  }
  syncLlmInputs();

  const llmFieldset = mode === 'onboarding' ? el('fieldset', {class: 'card fset'},
    el('legend', {}, '3. 大模型驱动（可选，随时可在「管家」修改）'),
    el('div', {class: 'radio-cards two'},
      [['demo', '先用内置离线演示体验', '免 API Key、立即可玩，支持日常起居与演示对话。'],
       ['api', '接入 OpenAI 兼容大模型 API', '填入 DeepSeek / OpenAI / Ollama 等接口即刻唤醒真人感对话与晚间日记。']].map(([val, t, d]) => {
        const rid = 'llm-mode-' + val;
        const radio = el('input', {
          type: 'radio',
          id: rid,
          name: 'llm_mode',
          value: val,
          class: 'visually-hidden',
          checked: llmChoice === val,
          onchange: () => { if (radio.checked){ llmChoice = val; syncLlmInputs(); } },
        });
        return el('div', {class: 'rc-opt'},
          radio,
          el('label', {for: rid, class: 'rc-card col'}, el('b', {}, t), el('span', {class: 'tiny muted'}, d)));
      })),
    apiPanel) : null;

  const submitBtn = el('button', {type: 'submit', class: 'btn primary lg'},
    icon(mode === 'edit' ? 'check' : 'sparkle'),
    mode === 'edit' ? '保存档案修改' : mode === 'add' ? '邀请入住小屋' : '迎接这位住客入住小屋');
  const formErr = el('div', {class: 'err hidden', role: 'alert'});

  const inspirationBar = mode !== 'edit' ? el('div', {class: 'row wrap', style: 'gap:8px;margin-bottom:14px'},
    el('span', {class: 'small muted'}, '快速填入模板灵感：'),
    INSPIRATIONS.map(tpl => el('button', {
      type: 'button',
      class: 'chip',
      onclick: () => {
        nameInp.value = tpl.name;
        actorInp.value = tpl.actor;
        bioInp.value = tpl.bio;
        personaInp.value = tpl.persona;
        previewName.textContent = tpl.name;
        const spR = form.querySelector(`input[name="sprite"][value="${tpl.sprite}"]`);
        if (spR){ spR.checked = true; updateSpritePreview(tpl.sprite); }
        const acR = form.querySelector(`input[name="accent"][value="${tpl.accent}"]`);
        if (acR){ acR.checked = true; updateAccentPreview(tpl.accent); }
        const scR = form.querySelector(`input[name="schedule_preset"][value="${tpl.schedule_preset}"]`);
        if (scR){ scR.checked = true; selectedSched = tpl.schedule_preset; }
        const ckR = form.querySelector(`input[name="cook_preset"][value="${tpl.cook_preset}"]`);
        if (ckR){ ckR.checked = true; selectedCook = tpl.cook_preset; }
        validator.syncField(nameInp);
        validator.syncField(actorInp);
      },
    }, tpl.tag))) : null;

  const form = el('form', {
    method: 'post',
    action: mode === 'edit' ? '/api/residents/update' : mode === 'add' ? '/api/residents' : '/api/setup',
    novalidate: true,
    class: 'stack',
    onsubmit: async e => {
      e.preventDefault();
      formErr.classList.add('hidden');
      formErr.textContent = '';
      if (!validator.validateAll()) return;
      submitBtn.disabled = true;
      try {
        const resPayload = {
          name: nameInp.value.trim(),
          bio: bioInp.value.trim(),
          persona: personaInp.value.trim(),
          sprite: selectedSprite,
          accent: selectedAccent,
          schedule_preset: selectedSched,
          cook_preset: selectedCook,
        };
        const rawActor = actorInp.value.trim().toLowerCase();
        if (mode === 'edit') resPayload.actor = existing.actor;
        else if (rawActor) resPayload.actor = rawActor;

        let res;
        if (mode === 'edit'){
          res = await api('/api/residents/update', resPayload);
        } else if (mode === 'add'){
          res = await api('/api/residents', resPayload);
        } else {
          const setupBody = {
            mode: 'first_bot',
            resident: resPayload,
            llm: llmChoice === 'api'
              ? {mode: 'api', base_url: urlInp.value.trim(), model: modelInp.value.trim(), api_key: keyInp.value.trim()}
              : {mode: 'demo'},
          };
          res = await api('/api/setup', setupBody);
        }
        setOnboarding(false);
        if (res.state) setResidentsFromState(res.state);
        const destActor = res.actor || resPayload.actor || (res.state?.roster?.[0]) || 'bot1';
        setCurrentActor(destActor);
        ctx?.redrawNav?.();
        toast(mode === 'edit' ? `已更新 ${resPayload.name} 的档案` : `欢迎 ${resPayload.name} 搬进 AI 小屋！`);
        await go(mode === 'edit' ? `#/people/${encodeURIComponent(destActor)}/profile` : '#/home');
      } catch (err){
        formErr.textContent = err.message;
        formErr.classList.remove('hidden');
        submitBtn.disabled = false;
      }
    },
  },
    el('fieldset', {class: 'card fset'},
      el('legend', {}, '1. 住客资料与人设'),
      inspirationBar,
      el('div', {class: 'g2'},
        el('div', {class: 'field-blk'},
          el('label', {for: 'setup-name'}, '住客昵称 ', el('span', {'aria-hidden': 'true'}, '*')),
          el('span', {id: 'setup-name-hint', class: 'field-hint'}, '1–24 个字符，将在像素小屋、群聊与日记里显示。'),
          nameInp,
          el('div', {id: 'setup-name-err', class: 'error-msg'}, el('span', {role: 'img', 'aria-label': '错误'}, '⚠'), ' 请填写 1–24 字的住客昵称。')),
        el('div', {class: 'field-blk'},
          el('label', {for: 'setup-actor'}, '英文代号（可选）'),
          el('span', {id: 'setup-actor-hint', class: 'field-hint'}, mode === 'edit' ? '英文代号创建后不可修改。' : '2–16 位小写字母、数字或下划线；留空将自动生成。'),
          actorInp,
          el('div', {id: 'setup-actor-err', class: 'error-msg'}, el('span', {role: 'img', 'aria-label': '错误'}, '⚠'), ' 需以小写字母开头，由 2–16 位小写字母、数字或下划线组成。'))),
      el('div', {class: 'field-blk'},
        el('label', {for: 'setup-bio'}, '一句话简介'),
        el('span', {id: 'setup-bio-hint', class: 'field-hint'}, '最多 120 字，展示在状态卡与书架上。'),
        bioInp),
      el('div', {class: 'field-blk'},
        el('label', {for: 'setup-persona'}, 'AI 人设与说话风格'),
        el('span', {id: 'setup-persona-hint', class: 'field-hint'}, '直接发给大模型作为扮演设定；留空则按昵称与简介自动生成。'),
        personaInp)),

    el('fieldset', {class: 'card fset'},
      el('legend', {}, '2. 像素形象、主题色与生活作息'),
      el('div', {class: 'setup-visual'},
        previewCard,
        el('div', {class: 'stack', style: 'gap:14px;min-width:0;flex:1'},
          el('div', {},
            el('div', {class: 'small muted', style: 'margin-bottom:8px;font-weight:600;color:var(--ink)'}, '像素小人外观'),
            spriteRadios),
          el('div', {},
            el('div', {class: 'small muted', style: 'margin-bottom:8px;font-weight:600;color:var(--ink)'}, '专属强调色'),
            accentRadios))),
      el('hr', {class: 'soft'}),
      el('div', {class: 'g2'},
        el('div', {},
          el('div', {class: 'small muted', style: 'margin-bottom:8px;font-weight:600;color:var(--ink)'}, '作息习惯'),
          schedRadios),
        el('div', {},
          el('div', {class: 'small muted', style: 'margin-bottom:8px;font-weight:600;color:var(--ink)'}, '厨房分工'),
          cookRadios))),

    llmFieldset,
    formErr,
    el('div', {class: 'row wrap', style: 'gap:10px;justify-content:flex-end'},
      mode !== 'onboarding' ? el('a', {class: 'btn', href: backHref}, '取消') : null,
      submitBtn));

  const validator = wireFormValidation(form);

  const presetFooter = mode === 'onboarding' ? el('section', {class: 'card flat mt'},
    el('div', {class: 'chead'},
      el('h2', {}, '想先体验现成的多人合租屋示例？'),
      el('span', {class: 'small muted'}, '可一键载入预置世界包，之后同样可以继续邀请新人或重置')),
    el('div', {class: 'row wrap', style: 'gap:10px'},
      (setupDoc.presets || []).map(p => el('button', {
        type: 'button',
        class: 'btn',
        onclick: async e => {
          const btn = e.currentTarget;
          btn.disabled = true;
          try {
            const res = await api('/api/setup', {mode: 'preset', preset: p.id});
            setOnboarding(false);
            if (res.state) setResidentsFromState(res.state);
            setCurrentActor(res.state?.roster?.[0] || '');
            ctx?.redrawNav?.();
            toast(`已载入示例「${p.label}」`);
            await go('#/home');
          } catch (err){
            btn.disabled = false;
            toast(err.message, true);
          }
        },
      }, icon('users'), `载入${p.label}`)))) : null;

  put(slot, form, presetFooter);
  return {};
}
