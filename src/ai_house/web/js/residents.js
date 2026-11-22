// Dynamic WorldPack resident registry: populates from GET /api/state or GET /api/chat/roster.
const TONES = ['--t-0', '--t-1', '--t-2', '--t-3', '--t-4', '--t-5'];
const GLYPHS = ['cup', 'book', 'bolt', 'star', 'paw', 'leaf'];
const KNOWN_ACCOUNTS = {
  nell: 'cup', elsa: 'book', alma: 'bolt', mina: 'star', yuki: 'paw', wanda: 'leaf',
  sora: 'cup', rin: 'star', kuro: 'bolt',
};
const BUILTIN_ACTORS = new Set(['nell', 'elsa', 'alma', 'mina', 'yuki', 'wanda', 'sora', 'rin', 'kuro']);

export const RESIDENTS = [];

export function setResidentsFromState(state){
  if (state?.needs_onboarding || (Array.isArray(state?.roster) && state.roster.length === 0)){
    RESIDENTS.splice(0, RESIDENTS.length);
    return RESIDENTS;
  }
  const order = Array.isArray(state?.roster) && state.roster.length
    ? state.roster
    : Object.keys(state?.residents || {});
  if (!order.length){
    RESIDENTS.splice(0, RESIDENTS.length);
    return RESIDENTS;
  }
  const next = order.map((actor, i) => {
    const info = (state?.residents || {})[actor] || {};
    const accent = info.accent || '';
    const sprite = info.sprite || (BUILTIN_ACTORS.has(actor) ? actor : 'sora');
    return {
      actor,
      name: info.name || actor,
      latin: actor.slice(0, 1).toUpperCase() + actor.slice(1),
      glyph: KNOWN_ACCOUNTS[actor] || KNOWN_ACCOUNTS[sprite] || GLYPHS[i % GLYPHS.length],
      accent,
      tone: accent || `var(${TONES[i % TONES.length]})`,
      bed: `bedroom_${i % 6}`,
      idx: i,
      bio: info.bio || '',
      persona: info.persona || '',
      sprite,
      schedule_preset: info.schedule_preset || 'regular',
      cook_preset: info.cook_preset || 'helper',
      hasSprite: BUILTIN_ACTORS.has(sprite),
    };
  });
  RESIDENTS.splice(0, RESIDENTS.length, ...next);
  return RESIDENTS;
}

export function setResidentsFromRoster(rows){
  if (!Array.isArray(rows) || !rows.length){
    RESIDENTS.splice(0, RESIDENTS.length);
    return RESIDENTS;
  }
  const next = rows.map((row, i) => {
    const actor = row.actor;
    const sprite = row.sprite || (BUILTIN_ACTORS.has(actor) ? actor : 'sora');
    return {
      actor,
      name: row.name || actor,
      latin: actor.slice(0, 1).toUpperCase() + actor.slice(1),
      glyph: KNOWN_ACCOUNTS[actor] || KNOWN_ACCOUNTS[sprite] || GLYPHS[i % GLYPHS.length],
      accent: row.accent || '',
      tone: row.accent || `var(${TONES[i % TONES.length]})`,
      bed: `bedroom_${i % 6}`,
      idx: i,
      bio: '',
      persona: '',
      sprite,
      hasSprite: BUILTIN_ACTORS.has(sprite),
    };
  });
  RESIDENTS.splice(0, RESIDENTS.length, ...next);
  return RESIDENTS;
}

export const byActor = a => RESIDENTS.find(r => r.actor === a) || null;
export const anyKey = k => RESIDENTS.find(r => r.actor === k || r.name === k) || null;
export const nameOf = a => a === 'owner' ? '主人' : a === 'shared' ? '小屋公共' : ((byActor(a) || {}).name || a);

export function toneStyle(r){
  if (!r) return '';
  const col = r.tone || r.accent || 'var(--accent)';
  return `--c:${col};--tone:${col};--s:color-mix(in srgb,${col} 15%,var(--card));`;
}
