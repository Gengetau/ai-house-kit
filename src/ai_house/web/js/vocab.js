// Labels for world-kernel states, rooms, meals, scopes, and activities.
import {RESIDENTS, byActor} from './residents.js';

export const AVAIL = {
  free: ['有空', 'ok'],
  limited: ['在忙别的', 'sage'],
  occupied: ['很忙', 'warn'],
  sleeping: ['睡着了', 'info'],
};
export const lifeState = r => r ? (AVAIL[r.availability] || [r.availability || '空闲', '']) : ['状态未知', ''];

export const ACTIVITIES = {
  idle: '发呆', resting: '休息', sleeping: '睡觉', eating: '吃东西', cooking: '做饭',
  chores: '做家务', reading: '看书', working: '做事', gaming: '打游戏', hobby: '做喜欢的事',
  phone: '看消息', bathing: '洗澡', socializing: '和大家聊天', outing: '出门逛逛',
  walking: '散步', browsing: '刷动态', collecting: '整理收藏', feeling_wind: '吹风',
  stargazing: '看星星', sunbathing: '晒太阳', tinkering: '捣鼓设备', tea_time: '喝茶',
  shopping: '采买', stroll: '散步', flower_arranging: '插花', kitchen_help: '在厨房帮忙',
};

export const PUBLIC_ROOMS = [
  ['living', '客厅', 'sofa', '聊天、弹琴、一起玩耍'],
  ['kitchen', '厨房', 'pot', '料理、泡茶、共用三餐'],
  ['study', '书房', 'book', '阅读、整理笔记与工作'],
  ['bath', '浴室', 'drop', '洗漱、泡澡与洗衣'],
  ['yard', '院子', 'tree', '晒太阳、赏花与纳凉'],
];

export const OUTSIDE = {
  street: '商店街', station: '车站附近', river: '河岸', shop: '便利店/超市',
  cafe: '咖啡馆', away: '出门在外',
};

export function roomLabel(key){
  if (!key) return '未知位置';
  const pub = PUBLIC_ROOMS.find(([k]) => k === key);
  if (pub) return pub[1];
  if (key.startsWith('room_')){
    const r = byActor(key.slice(5));
    return r ? `${r.name}的房间` : '卧室';
  }
  if (key.startsWith('bedroom_')){
    const idx = Number(key.slice(8));
    const r = RESIDENTS[idx];
    return r ? `${r.name}的卧室` : `卧室 ${idx + 1}`;
  }
  return OUTSIDE[key] || key;
}

export function resolveBedroomAsset(roomKey){
  if (roomKey && roomKey.startsWith('room_')){
    const r = byActor(roomKey.slice(5));
    return r ? r.bed : 'bedroom_0';
  }
  return roomKey || 'living';
}

export const MOODS = {
  neutral: '平静', relaxed: '放松', happy: '开心', excited: '兴奋', tired: '累了', sleepy: '有点困',
  stressed: '烦躁', sad: '低落', annoyed: '不爽', bored: '无聊', cozy: '惬意', touched: '感动',
  proud: '得意', shy: '害羞', lonely: '孤单', worried: '担心', jealous: '吃醋',
};
export const POSITIVE_MOODS = new Set(['happy', 'excited', 'relaxed', 'cozy', 'touched', 'proud', 'shy']);
export const DEGREE = {1: '有点', 2: '', 3: '很'};

export function feelOf(r){
  if (!r) return {};
  if (r.feel && typeof r.feel === 'object') return r.feel;
  try { return JSON.parse(r.feel || '{}') || {}; } catch { return {}; }
}

export function moodWord(f, r){
  const key = (f && f.mood) || (r && r.mood) || 'neutral';
  return (key !== 'neutral' ? (DEGREE[f && f.degree] || '') : '') + (MOODS[key] || key);
}
export const moodTone = key => POSITIVE_MOODS.has(key) ? 'ok' : ['neutral', 'sleepy'].includes(key) ? 'info' : key === 'tired' ? 'warn' : 'terra';

export const MEM_SCOPES = {world: '世界公共', group: '群聊记忆', private: '房主私密'};
export const MEM_KINDS = {
  rule: '规矩', person: '人物', preference: '喜好', event: '事件',
  promise: '约定', diary: '日记', fact: '事实', note: '便签',
};
export const SHELVES = {
  fridge: ['冰箱', 'fridge'],
  pantry: ['食品柜', 'cabinet'],
  snacks: ['零食箱', 'basket'],
  room: ['住客房间', 'bed'],
};
