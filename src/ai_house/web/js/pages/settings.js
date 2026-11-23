// 通用插件与能力设置中心 (#/settings): 完全根据服务端 schema 动态生成表单并热重载生效。
import {
  el, put, api, icon, pageHead, toast, hasTier, isOnboarding, wireFormValidation,
} from '../core.js';

const KINDS = [
  ['all', '全部能力'],
  ['channel', '消息通道'],
  ['extension', '生活扩展'],
  ['tool', '对话工具'],
  ['provider', '渲染与记忆'],
  ['bundle', '综合套件'],
];

const KIND_LABELS = Object.fromEntries(KINDS);

function isIdentifierLike(fld){
  const k = String(fld.key || '').toLowerCase();
  return k.includes('url') || k.includes('actor') || k.includes('qq') || k.includes('group') || k.includes('token');
}

export async function render(view){
  const box = el('div');
  view.append(
    pageHead('插件与能力配置中心', '按插件声明的清单动态渲染配置表单，密码类密钥仅脱敏回显，保存后无需重启立即热生效。'),
    box,
  );
  let doc = null, filterKind = 'all', alive = true;

  async function load(){
    if (!hasTier('owner')){
      drawReadonly();
      return;
    }
    const res = await api('/api/settings/plugins');
    if (!alive) return;
    doc = res;
    draw();
  }

  function drawReadonly(){
    put(box,
      el('section', {class: 'card'},
        el('div', {class: 'chead'},
          el('h2', {}, icon('shield'), '仅限房主令牌配置'),
          el('span', {class: 'chip warn'}, '权限受限')),
        el('p', {class: 'small muted'}, '当前使用的令牌不具备房主（owner）权限；修改插件配置与凭据请使用 AI_HOUSE_OWNER_TOKEN 重新登录。')));
  }

  function renderPluginCard(plug){
    const pid = plug.plugin_id;
    const headId = `plug-head-${pid}`;
    const enId = `plug-en-${pid}`;
    const statusLive = el('div', {class: 'small muted', role: 'status'});
    const enBox = el('input', {
      id: enId,
      name: 'enabled',
      type: 'checkbox',
      checked: !!plug.enabled,
    });

    const clearSecrets = new Set();
    const fieldReaders = [];
    const reqBindings = [];
    const fieldNodes = [];

    for (const fld of plug.fields || []){
      const cid = `plug-${pid}-${fld.key}`;
      const hintId = `${cid}-hint`;
      const errId = `${cid}-err`;

      if (fld.field_type === 'bool'){
        const cb = el('input', {
          id: cid,
          name: fld.key,
          type: 'checkbox',
          checked: !!fld.value,
          'aria-describedby': fld.description ? hintId : false,
        });
        fieldReaders.push(() => [fld.key, !!cb.checked]);
        fieldNodes.push(
          el('div', {class: 'field-blk'},
            el('div', {class: 'row wrap', style: 'gap:8px;align-items:center'},
              cb,
              el('label', {for: cid, style: 'font-weight:600;cursor:pointer'}, fld.label)),
            fld.description ? el('span', {id: hintId, class: 'field-hint'}, fld.description) : null));
        continue;
      }

      const labelNode = el('label', {for: cid},
        fld.label,
        fld.required ? el('span', {'aria-hidden': 'true'}, ' *') : null);
      const hintTxt = fld.secret
        ? `${fld.description || '密钥类字段，保存后仅掩码回显。'}${fld.secret_set ? `（当前已保存：${fld.secret_masked}，留空保留原值）` : ''}`
        : (fld.description || (fld.env_var ? `对应环境变量：${fld.env_var}` : ''));
      const hintNode = hintTxt ? el('span', {id: hintId, class: 'field-hint'}, hintTxt) : null;

      let ctrl;
      if (fld.field_type === 'select'){
        ctrl = el('select', {
          id: cid,
          name: fld.key,
          required: !!(fld.required && plug.enabled),
          'aria-describedby': hintNode ? hintId : false,
          'data-error': errId,
        }, (fld.options || []).map(opt => el('option', {
          value: opt.value,
          selected: String(fld.value ?? fld.default ?? '') === String(opt.value),
        }, opt.label || opt.value)));
        fieldReaders.push(() => [fld.key, ctrl.value]);
      } else if (fld.field_type === 'int' || fld.field_type === 'float'){
        ctrl = el('input', {
          id: cid,
          name: fld.key,
          type: 'number',
          step: fld.field_type === 'int' ? '1' : 'any',
          min: fld.min_val != null ? String(fld.min_val) : false,
          max: fld.max_val != null ? String(fld.max_val) : false,
          required: !!(fld.required && plug.enabled),
          value: fld.value != null ? String(fld.value) : '',
          'aria-describedby': hintNode ? hintId : false,
          'data-error': errId,
        });
        fieldReaders.push(() => [fld.key, ctrl.value === '' ? null : Number(ctrl.value)]);
      } else if (fld.secret){
        ctrl = el('input', {
          id: cid,
          name: fld.key,
          type: 'password',
          autocomplete: 'off',
          autocorrect: 'off',
          autocapitalize: 'none',
          spellcheck: false,
          required: !!(fld.required && plug.enabled && !fld.secret_set),
          placeholder: fld.secret_set ? `已配置（${fld.secret_masked}），留空保留原密钥` : (fld.placeholder || '输入新密钥…'),
          'aria-describedby': hintNode ? hintId : false,
          'data-error': errId,
        });
        const eyeBtn = el('button', {
          type: 'button',
          class: 'btn icon ghost',
          'aria-pressed': 'false',
          'aria-label': `显示或隐藏${fld.label}`,
          onclick: () => {
            const show = ctrl.type === 'password';
            ctrl.type = show ? 'text' : 'password';
            eyeBtn.setAttribute('aria-pressed', String(show));
            eyeBtn.replaceChildren(icon(show ? 'eye' : 'eyeOff'));
          },
        }, icon('eyeOff'));
        const clrId = `${cid}-clear`;
        const clrBox = fld.secret_set ? el('input', {
          id: clrId,
          type: 'checkbox',
          onchange: () => {
            if (clrBox.checked) clearSecrets.add(fld.key);
            else clearSecrets.delete(fld.key);
          },
        }) : null;
        fieldReaders.push(() => [fld.key, ctrl.value]);
        reqBindings.push({ctrl, required: !!(fld.required && !fld.secret_set)});
        fieldNodes.push(
          el('div', {class: 'field-blk'},
            labelNode,
            hintNode,
            el('div', {class: 'pwrow'}, icon('key'), ctrl, eyeBtn),
            el('div', {id: errId, class: 'error-msg'}, el('span', {role: 'img', 'aria-label': '错误'}, '⚠'), ` 请检查「${fld.label}」输入是否合法。`),
            clrBox ? el('div', {class: 'row wrap', style: 'gap:6px;align-items:center;margin-top:4px'},
              clrBox,
              el('label', {for: clrId, class: 'tiny muted', style: 'cursor:pointer'}, '清空已保存的密钥')) : null));
        continue;
      } else {
        const valText = Array.isArray(fld.value) ? fld.value.join(', ') : String(fld.value ?? '');
        ctrl = el('input', {
          id: cid,
          name: fld.key,
          type: 'text',
          autocomplete: 'off',
          ...(isIdentifierLike(fld) ? {autocorrect: 'off', autocapitalize: 'none', spellcheck: false} : {}),
          required: !!(fld.required && plug.enabled),
          value: valText,
          placeholder: fld.placeholder || '',
          'aria-describedby': hintNode ? hintId : false,
          'data-error': errId,
        });
        fieldReaders.push(() => [fld.key, ctrl.value]);
      }

      reqBindings.push({ctrl, required: !!fld.required});
      const rangeHint = (fld.min_val != null || fld.max_val != null)
        ? `（范围：${fld.min_val ?? '-∞'} ～ ${fld.max_val ?? '+∞'}）` : '';
      fieldNodes.push(
        el('div', {class: 'field-blk'},
          labelNode,
          hintNode,
          ctrl,
          el('div', {id: errId, class: 'error-msg'}, el('span', {role: 'img', 'aria-label': '错误'}, '⚠'), ` 请检查「${fld.label}」格式或必填项${rangeHint}。`)));
    }

    const saveBtn = el('button', {type: 'submit', class: 'btn sm primary'}, icon('check'), '保存并热重载');
    let validator;
    enBox.addEventListener('change', () => {
      for (const b of reqBindings){
        b.ctrl.required = b.required && enBox.checked;
        validator?.syncField?.(b.ctrl);
      }
    });

    const form = el('form', {
      method: 'post',
      action: '/api/settings/plugins',
      'aria-labelledby': headId,
      novalidate: true,
      class: 'stack',
      onsubmit: async e => {
        e.preventDefault();
        if (!validator.validateAll()) return;
        saveBtn.disabled = true;
        statusLive.textContent = '正在校验并保存…';
        statusLive.style.color = 'var(--ink-2)';
        const values = {};
        for (const readPair of fieldReaders){
          const [k, v] = readPair();
          values[k] = v;
        }
        try {
          const res = await api('/api/settings/plugins', {
            plugin_id: pid,
            enabled: !!enBox.checked,
            values,
            clear_secrets: [...clearSecrets],
          });
          toast(`${plug.name} 配置已保存并生效`);
          await load();
          return res;
        } catch (err){
          statusLive.textContent = '保存失败：' + (err.message || err);
          statusLive.style.color = 'var(--bad)';
          toast(err.message || '保存失败', true);
          saveBtn.disabled = false;
        }
      },
    },
      el('div', {class: 'row between wrap', style: 'gap:8px;padding:6px 10px;border-radius:10px;background:var(--bg-2)'},
        el('div', {class: 'row wrap', style: 'gap:8px;align-items:center'},
          enBox,
          el('label', {for: enId, style: 'font-weight:600;cursor:pointer'}, '启用该插件')),
        el('span', {class: 'tiny muted'}, `ID: ${pid} · v${plug.version} · ${plug.source === 'entrypoint' ? '第三方扩展包' : '内置'}`)),
      fieldNodes.length ? el('div', {class: 'stack'}, fieldNodes) : el('p', {class: 'tiny muted'}, '该插件无需额外配置项。'),
      plug.missing_packages?.length ? el('p', {class: 'tiny', style: 'color:var(--honey-ink)'}, `缺少可选依赖包：${plug.missing_packages.join(', ')}`) : null,
      plug.error ? el('div', {class: 'err'}, icon('alert'), ' 最近错误：' + plug.error) : null,
      plug.config_warnings?.length ? el('div', {class: 'small', role: 'status', style: 'color:var(--honey-ink)'},
        icon('alert'), ' 配置提示：',
        el('ul', {style: 'margin:4px 0 0 18px'}, plug.config_warnings.map(w => el('li', {}, String(w))))) : null,
      el('div', {class: 'row between wrap', style: 'gap:8px;align-items:center'}, statusLive, saveBtn));
    validator = wireFormValidation(form);

    const badgeCls = plug.error ? 'bad' : (plug.active ? 'ok' : (plug.enabled ? 'warn' : ''));
    const badgeTxt = plug.error ? '加载异常' : (plug.active ? '运行中' : (plug.enabled ? '已启用（待满足条件）' : '已停用'));

    return el('section', {class: 'card'},
      el('div', {class: 'chead'},
        el('div', {},
          el('h2', {id: headId}, plug.name),
          el('div', {class: 'tiny muted'}, `${KIND_LABELS[plug.kind] || plug.kind} · ${plug.description || ''}`)),
        el('span', {class: 'chip ' + badgeCls}, badgeTxt)),
      form);
  }

  function draw(){
    if (!alive || !doc) return;
    const allPlugins = doc.plugins || [];
    const shown = filterKind === 'all' ? allPlugins : allPlugins.filter(p => p.kind === filterKind);
    const loadErrors = doc.load_errors || [];

    put(box,
      el('search', {class: 'card', 'aria-label': '按能力类别筛选插件'},
        el('div', {class: 'row between wrap', style: 'gap:10px;align-items:center'},
          el('div', {class: 'chips', role: 'group', 'aria-label': '插件类别筛选'},
            KINDS.map(([k, lb]) => el('button', {
              type: 'button',
              class: 'chip' + (filterKind === k ? ' on' : ''),
              onclick: () => { filterKind = k; draw(); },
            }, lb))),
          el('span', {class: 'tiny muted'}, `共 ${shown.length} / ${allPlugins.length} 项${isOnboarding() ? '（空屋向导前亦可预配置）' : ''}`))),
      loadErrors.length ? el('section', {class: 'card mt'},
        el('div', {class: 'chead'}, el('h2', {}, icon('alert'), `已隔离的插件异常（${loadErrors.length}）`)),
        el('div', {class: 'stack'}, loadErrors.map(e =>
          el('div', {class: 'tiny', style: 'color:var(--bad)'}, `[${e.group}] ${e.plugin_id}: ${e.error}`)))) : null,
      el('div', {class: 'g2 mt'}, shown.map(renderPluginCard)));
  }

  await load();
  return {refresh: load, cleanup: () => { alive = false; }};
}
