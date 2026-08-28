// app/explore/index.tsx
// 探店地图 v2:
//   ★ 角色头像做地图标记(替代圆点)
//   ★ 角色筛选(点某个角色只看他去过的)
//   ★ 按日程时间进度显示(没到的时间不显示)
//   ★ 店铺卡片质量提升(更好的 popup、分类中文标签、角色碎碎念)
//
// 技术:WebView + Leaflet.js,不需要 Google API Key
// 头像:avatar_url 是 base64 data URI,在 WebView 里用 <img> 渲染没问题
//       (RN 的 <Image> 才会崩,WebView 内的 HTML img 不受影响)
import axios from 'axios';
import { useFocusEffect, useRouter } from 'expo-router';
import React, { useCallback, useRef, useState } from 'react';
import {
  ActivityIndicator, Platform,
  ScrollView,
  StatusBar, StyleSheet,
  Text, TouchableOpacity, View,
} from 'react-native';
import { WebView } from 'react-native-webview';
import { C, SERVER_URL } from '../../constants/theme';

const UID = 'user_mofpiyd7442ia7';

// ── 城市 ──
const CITY_TABS = [
  { id: 'tokyo', label: '东京', lat: 35.6762, lng: 139.6503, zoom: 12 },
  { id: 'kyoto', label: '京都', lat: 35.0116, lng: 135.7681, zoom: 13 },
  { id: 'osaka', label: '大阪', lat: 34.6937, lng: 135.5023, zoom: 13 },
  { id: 'all',   label: '全部', lat: 36.2, lng: 138.0, zoom: 6 },
];

// ── 分类:中文标签 + 颜色 + emoji ──
const CAT_META: Record<string, { label: string; color: string; emoji: string }> = {
  cafe:        { label: '咖啡厅', color: '#F59E0B', emoji: '☕' },
  restaurant:  { label: '餐厅',   color: '#EF4444', emoji: '🍽' },
  sweets:      { label: '甜品店', color: '#EC4899', emoji: '🍰' },
  bakery:      { label: '面包店', color: '#F97316', emoji: '🥐' },
  ramen:       { label: '拉面店', color: '#DC2626', emoji: '🍜' },
  fashion:     { label: '服装店', color: '#8B5CF6', emoji: '👗' },
  bookstore:   { label: '书店',   color: '#3B82F6', emoji: '📚' },
  convenience: { label: '便利店', color: '#10B981', emoji: '🏪' },
};

// ── 角色颜色(地图标记边框 + 筛选高亮) ──
const CHAR_COLORS: Record<string, string> = {
  gojo:   '#3b82f6',
  geto:   '#8B5CF6',
  minato: '#EF4444',
};
const DEFAULT_CHAR_COLOR = '#5BC4FF';

interface Place {
  id: number;
  character_id: string;
  place_name: string;
  place_address: string;
  lat: number;
  lng: number;
  category: string;
  city: string;
  char_review: string;
  visit_date: string | null;
}

interface CharMeta {
  id: string;
  name: string;
  avatar_url: string | null;
}

interface SchedItem {
  start_time: string;
  end_time: string;
  title: string;
  location: string;
}

/** 'HH:MM' → 分钟数 */
function toMin(hhmm: string): number {
  const [h, m] = hhmm.split(':').map(Number);
  return h * 60 + m;
}

/** 今天的日期字符串 YYYY-MM-DD (北京时间) */
function todayCN(): string {
  const now = new Date();
  const cn = new Date(now.getTime() + 8 * 3600 * 1000);
  return cn.toISOString().slice(0, 10);
}

export default function ExploreScreen() {
  const router = useRouter();
  const [city, setCity] = useState('tokyo');
  const [places, setPlaces] = useState<Place[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const webRef = useRef<WebView>(null);

  // ★ 角色列表 + 筛选
  const [chars, setChars] = useState<CharMeta[]>([]);
  const [filterChar, setFilterChar] = useState<string | null>(null); // null = 全部

  // ★ 日程数据(按角色 → 今天的时间段列表)
  const [schedMap, setSchedMap] = useState<Record<string, SchedItem[]>>({});
  const [serverNow, setServerNow] = useState('');

  // ── 加载角色列表 ──
  const loadChars = async () => {
    try {
      const res = await axios.get(`${SERVER_URL}/characters_all`, { timeout: 8000 });
      const list: CharMeta[] = (res.data?.characters || []).map((c: any) => ({
        id: c.id,
        name: c.name || c.id,
        avatar_url: c.avatar_url || null,
      }));
      setChars(list);
      return list;
    } catch (e: any) {
      console.warn('[explore] 拉角色失败', e?.message);
      return [];
    }
  };

  // ── 加载今天所有角色的日程(用于时间进度过滤) ──
  const loadSchedules = async (charList: CharMeta[]) => {
    const map: Record<string, SchedItem[]> = {};
    let now = '';
    for (const c of charList) {
      try {
        const res = await axios.get(`${SERVER_URL}/schedule`, {
          params: { character_id: c.id, user_id: UID },
          timeout: 15000,
        });
        map[c.id] = (res.data?.items || []).map((it: any) => ({
          start_time: it.start_time,
          end_time: it.end_time,
          title: it.title || '',
          location: it.location || '',
        }));
        if (res.data?.now && !now) now = res.data.now;
      } catch {
        map[c.id] = [];
      }
    }
    setSchedMap(map);
    setServerNow(now);
    return { map, now };
  };

  // ── 加载探店数据 ──
  const loadPlaces = async () => {
    try {
      const res = await axios.get(`${SERVER_URL}/explore/visited`, {
        params: {
          user_id: UID,
          character_id: filterChar || undefined,
          city: city === 'all' ? undefined : city,
        },
        timeout: 10000,
      });
      setPlaces(res.data?.places || []);
      setTotal(res.data?.total || 0);
    } catch (e: any) {
      console.warn('[explore] load failed:', e?.message);
    }
  };

  // ── 主加载流程 ──
  useFocusEffect(useCallback(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      const charList = await loadChars();
      await loadPlaces();
      await loadSchedules(charList);
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [city, filterChar]));

  const cityInfo = CITY_TABS.find(c => c.id === city) || CITY_TABS[0];

  // ★ 时间进度过滤:今天还没到的日程对应的店不显示
  const filteredPlaces = (() => {
    const today = todayCN();
    const nowMin = serverNow ? toMin(serverNow) : -1;
    if (nowMin < 0) return places; // 没拿到服务器时间就全显示

    return places.filter(p => {
      // 不是今天的记录 → 永远显示
      if (p.visit_date !== today) return true;

      // 今天的记录 → 检查日程时间
      const charSched = schedMap[p.character_id] || [];
      if (charSched.length === 0) return true; // 没日程就全显示

      // 尝试匹配:地点名出现在日程的 title 或 location 里
      const matched = charSched.find(
        s => s.title.includes(p.place_name) || s.location.includes(p.place_name)
          || p.place_name.includes(s.location)
          || (s.location && s.location.length > 2 && p.place_address.includes(s.location))
      );

      if (!matched) return true; // 没匹配上就显示
      return toMin(matched.start_time) <= nowMin; // 开始时间到了才显示
    });
  })();

  // ── 角色名字 / 头像映射 ──
  const charNameMap: Record<string, string> = {};
  const charAvatarMap: Record<string, string> = {};
  for (const c of chars) {
    charNameMap[c.id] = c.name;
    if (c.avatar_url) charAvatarMap[c.id] = c.avatar_url;
  }

  // ── Leaflet HTML ──
  const mapHtml = buildMapHtml(filteredPlaces, cityInfo, charNameMap, charAvatarMap);

  const activeName = filterChar
    ? (chars.find(c => c.id === filterChar)?.name || filterChar)
    : null;

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      {/* ── 顶栏 ── */}
      <View style={s.header}>
        <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
          <Text style={s.backText}>‹</Text>
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={s.title}>
            {activeName ? `${activeName}的探店地图` : '探店地图'}
          </Text>
          <Text style={s.sub}>
            {filteredPlaces.length === total
              ? `已探 ${total} 家`
              : `显示 ${filteredPlaces.length} / ${total} 家`}
            {serverNow ? `  ·  ${serverNow}` : ''}
          </Text>
        </View>
      </View>

      {/* ── 角色筛选 ── */}
      {chars.length > 0 && (
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          style={s.charBar}
          contentContainerStyle={{ paddingHorizontal: 12, gap: 8 }}
        >
          <TouchableOpacity
            style={[s.charChip, !filterChar && s.charChipActive]}
            onPress={() => setFilterChar(null)}
            activeOpacity={0.8}
          >
            <View style={[s.charDot, { backgroundColor: C.accent }]} />
            <Text style={[s.charChipText, !filterChar && s.charChipTextActive]}>全部</Text>
          </TouchableOpacity>
          {chars.map(c => {
            const on = filterChar === c.id;
            const col = CHAR_COLORS[c.id] || DEFAULT_CHAR_COLOR;
            return (
              <TouchableOpacity
                key={c.id}
                style={[s.charChip, on && { ...s.charChipActive, borderColor: col }]}
                onPress={() => setFilterChar(on ? null : c.id)}
                activeOpacity={0.8}
              >
                <View style={[s.charAvatar, { borderColor: col, backgroundColor: col + '33' }]}>
                  <Text style={s.charAvatarText}>{c.name?.[0] || '?'}</Text>
                </View>
                <Text style={[s.charChipText, on && { color: col }]}>{c.name}</Text>
              </TouchableOpacity>
            );
          })}
        </ScrollView>
      )}

      {/* ── 城市切换 ── */}
      <View style={s.tabs}>
        {CITY_TABS.map(ct => (
          <TouchableOpacity
            key={ct.id}
            style={[s.tab, city === ct.id && s.tabActive]}
            onPress={() => setCity(ct.id)}
          >
            <Text style={[s.tabText, city === ct.id && s.tabTextActive]}>
              {ct.label}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* ── 地图 ── */}
      <View style={{ flex: 1 }}>
        {loading ? (
          <View style={s.center}>
            <ActivityIndicator color={C.accent} />
            <Text style={s.loadingText}>加载探店记录...</Text>
          </View>
        ) : filteredPlaces.length === 0 ? (
          <View style={s.center}>
            <Text style={s.emptyIcon}>🗺️</Text>
            <Text style={s.emptyText}>
              {filterChar ? `${activeName}还没有探店记录` : '还没有探店记录'}
            </Text>
            <Text style={s.emptySub}>
              {filterChar
                ? '换个角色看看?日程生成后探店会自动记录'
                : '日程生成后,TA 去过的店会自动出现在这里'}
            </Text>
          </View>
        ) : (
          <WebView
            ref={webRef}
            source={{ html: mapHtml }}
            style={{ flex: 1, backgroundColor: C.bg }}
            scrollEnabled={true}
            javaScriptEnabled={true}
            domStorageEnabled={true}
            originWhitelist={['*']}
          />
        )}
      </View>

      {/* ── 底部:分类统计 ── */}
      {filteredPlaces.length > 0 && (
        <View style={s.footer}>
          {Object.entries(
            filteredPlaces.reduce((acc, p) => {
              acc[p.category] = (acc[p.category] || 0) + 1;
              return acc;
            }, {} as Record<string, number>)
          ).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([cat, count]) => {
            const meta = CAT_META[cat] || { label: cat, color: '#5BC4FF', emoji: '📍' };
            return (
              <View key={cat} style={s.footerItem}>
                <Text style={s.footerEmoji}>{meta.emoji}</Text>
                <Text style={s.footerText}>{meta.label}</Text>
                <View style={[s.footerBadge, { backgroundColor: meta.color + '33' }]}>
                  <Text style={[s.footerCount, { color: meta.color }]}>{count}</Text>
                </View>
              </View>
            );
          })}
        </View>
      )}
    </View>
  );
}


// ══════════════════════════════════════════════════════════════
//  Leaflet HTML 生成
// ══════════════════════════════════════════════════════════════

function buildMapHtml(
  places: Place[],
  cityInfo: { lat: number; lng: number; zoom: number },
  charNameMap: Record<string, string>,
  charAvatarMap: Record<string, string>,
) {
  const catMetaJson = JSON.stringify(CAT_META);
  const charColorsJson = JSON.stringify(CHAR_COLORS);
  const avatarMapJson = JSON.stringify(charAvatarMap);
  const nameMapJson = JSON.stringify(charNameMap);

  return `<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"><\/script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #070d1a; }
  #map { width: 100vw; height: 100vh; background: #070d1a; }

  /* 暗色地图风格 */
  .leaflet-tile-pane { filter: brightness(0.7) contrast(1.1) saturate(0.8); }
  .leaflet-control-attribution { display: none !important; }

  /* ── 角色头像标记 ── */
  .avatar-marker {
    width: 36px; height: 36px; border-radius: 50%;
    border: 3px solid #3b82f6;
    overflow: hidden;
    box-shadow: 0 2px 8px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.1);
    background: #0d1a2e;
    display: flex; align-items: center; justify-content: center;
  }
  .avatar-marker img {
    width: 100%; height: 100%; object-fit: cover; display: block;
  }
  .avatar-marker .initial {
    color: #e8f4ff; font-size: 14px; font-weight: 700;
    line-height: 30px; text-align: center; width: 100%;
  }

  /* ── 升级版 popup ── */
  .leaflet-popup-content-wrapper {
    background: #0d1a2e !important;
    border: 1px solid #1a3a5c !important;
    border-radius: 16px !important;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6) !important;
    padding: 0 !important;
  }
  .leaflet-popup-tip { background: #0d1a2e !important; border: 1px solid #1a3a5c !important; }
  .leaflet-popup-content { margin: 0 !important; width: 240px !important; }
  .leaflet-popup-close-button {
    color: #7ba8d0 !important; font-size: 18px !important;
    top: 8px !important; right: 10px !important;
  }

  .shop-popup { padding: 16px; font-family: -apple-system, sans-serif; }
  .shop-popup .shop-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .shop-popup .shop-avatar {
    width: 32px; height: 32px; border-radius: 50%;
    overflow: hidden; flex-shrink: 0;
    border: 2px solid #1a3a5c; background: #0f2040;
  }
  .shop-popup .shop-avatar img { width: 100%; height: 100%; object-fit: cover; }
  .shop-popup .shop-avatar .init {
    color: #7ba8d0; font-size: 13px; font-weight: 700;
    line-height: 28px; text-align: center;
  }
  .shop-popup .shop-name {
    font-size: 15px; font-weight: 700; color: #e8f4ff;
    line-height: 1.3; flex: 1;
  }
  .shop-popup .shop-meta {
    display: flex; align-items: center; gap: 6px;
    margin-bottom: 2px; flex-wrap: wrap;
  }
  .shop-popup .cat-tag {
    font-size: 11px; padding: 2px 8px; border-radius: 10px;
    font-weight: 600;
  }
  .shop-popup .shop-addr { font-size: 11px; color: #3d6080; }
  .shop-popup .shop-review {
    font-size: 13px; color: #7ba8d0; line-height: 1.5;
    padding: 10px 0; margin-top: 8px;
    border-top: 1px solid #1a3a5c;
    font-style: italic;
  }
  .shop-popup .shop-review::before { content: '\\201C'; color: #3b82f6; font-size: 16px; margin-right: 2px; }
  .shop-popup .shop-review::after { content: '\\201D'; color: #3b82f6; font-size: 16px; margin-left: 2px; }
  .shop-popup .shop-footer {
    display: flex; justify-content: space-between; align-items: center;
    margin-top: 8px; padding-top: 8px; border-top: 1px solid #1a3a5c;
  }
  .shop-popup .shop-char { font-size: 11px; font-weight: 600; }
  .shop-popup .shop-date { font-size: 10px; color: #3d6080; }
</style>
</head>
<body>
<div id="map"></div>
<script>
var map = L.map('map', {
  center: [${cityInfo.lat}, ${cityInfo.lng}],
  zoom: ${cityInfo.zoom},
  zoomControl: false,
  attributionControl: false,
});

// ★ 暗色瓷砖(CartoDB Dark Matter,配合 app 深蓝主题)
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  maxZoom: 19,
  subdomains: 'abcd',
}).addTo(map);

var places = ${JSON.stringify(places)};
var catMeta = ${catMetaJson};
var charColors = ${charColorsJson};
var avatarMap = ${avatarMapJson};
var nameMap = ${nameMapJson};

// 转义 HTML 特殊字符,防止 XSS / 破坏 popup
function esc(s) {
  if (!s) return '';
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

places.forEach(function(p) {
  var cm = catMeta[p.category] || { label: p.category, color: '#5BC4FF', emoji: '📍' };
  var charColor = charColors[p.character_id] || '#5BC4FF';
  var charName = nameMap[p.character_id] || p.character_id;
  var avatar = avatarMap[p.character_id];
  var charInitial = charName ? charName[0] : '?';

  // ★ 角色头像标记(替代圆点)
  var markerHtml;
  if (avatar) {
    markerHtml = '<div class="avatar-marker" style="border-color:' + charColor + '">'
      + '<img src="' + avatar + '" onerror="this.style.display=\\'none\\';this.nextElementSibling.style.display=\\'block\\'" />'
      + '<div class="initial" style="display:none">' + esc(charInitial) + '</div>'
      + '</div>';
  } else {
    markerHtml = '<div class="avatar-marker" style="border-color:' + charColor + ';background:' + charColor + '33">'
      + '<div class="initial">' + esc(charInitial) + '</div>'
      + '</div>';
  }

  var icon = L.divIcon({
    html: markerHtml,
    className: '',
    iconSize: [36, 36],
    iconAnchor: [18, 18],
    popupAnchor: [0, -22],
  });

  // ★ 升级版 popup:暗色主题 + 角色头像 + 分类标签
  var avatarHtml = avatar
    ? '<img src="' + avatar + '" />'
    : '<div class="init">' + esc(charInitial) + '</div>';

  var popup = '<div class="shop-popup">'
    + '<div class="shop-head">'
    +   '<div class="shop-avatar" style="border-color:' + charColor + '">' + avatarHtml + '</div>'
    +   '<div class="shop-name">' + esc(p.place_name) + '</div>'
    + '</div>'
    + '<div class="shop-meta">'
    +   '<span class="cat-tag" style="background:' + cm.color + '22;color:' + cm.color + '">'
    +     cm.emoji + ' ' + cm.label + '</span>'
    +   '<span class="shop-addr">' + esc(p.place_address || p.city || '') + '</span>'
    + '</div>'
    + (p.char_review
      ? '<div class="shop-review">' + esc(p.char_review) + '</div>'
      : '')
    + '<div class="shop-footer">'
    +   '<span class="shop-char" style="color:' + charColor + '">' + esc(charName) + '</span>'
    +   '<span class="shop-date">' + esc(p.visit_date || '') + '</span>'
    + '</div>'
    + '</div>';

  L.marker([p.lat, p.lng], { icon: icon }).addTo(map).bindPopup(popup, {
    maxWidth: 260,
    minWidth: 240,
    closeButton: true,
  });
});

// 多个标记时自动适配视野
if (places.length > 1 && places.length < 200) {
  try {
    var bounds = L.latLngBounds(places.map(function(p) { return [p.lat, p.lng]; }));
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
  } catch(e) {}
} else if (places.length === 1) {
  map.setView([places[0].lat, places[0].lng], 15);
}
<\/script>
</body>
</html>`;
}


// ══════════════════════════════════════════════════════════════
//  样式
// ══════════════════════════════════════════════════════════════

const s = StyleSheet.create({
  // ── 顶栏 ──
  header: {
    flexDirection: 'row', alignItems: 'center',
    paddingHorizontal: 12,
    paddingTop: Platform.OS === 'ios' ? 50 : 40,
    paddingBottom: 10,
    borderBottomWidth: 1, borderBottomColor: C.border,
    backgroundColor: C.card,
  },
  backBtn: { paddingHorizontal: 6, paddingVertical: 4 },
  backText: { color: C.text, fontSize: 30, lineHeight: 32, fontWeight: '300' },
  title: { color: C.text, fontSize: 17, fontWeight: '700' },
  sub: { color: C.textMute, fontSize: 11, marginTop: 2 },

  // ── 角色筛选条 ──
  charBar: {
    flexGrow: 0, paddingVertical: 10,
    backgroundColor: C.card,
    borderBottomWidth: 1, borderBottomColor: C.border,
  },
  charChip: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    paddingHorizontal: 12, paddingVertical: 6,
    borderRadius: 20, borderWidth: 1,
    borderColor: C.border, backgroundColor: C.bg,
  },
  charChipActive: {
    borderColor: C.accent, backgroundColor: C.accent + '18',
  },
  charChipText: { color: C.textMute, fontSize: 13 },
  charChipTextActive: { color: C.accent2, fontWeight: '600' },
  charDot: { width: 8, height: 8, borderRadius: 4 },
  charAvatar: {
    width: 22, height: 22, borderRadius: 11,
    borderWidth: 1.5, alignItems: 'center', justifyContent: 'center',
    overflow: 'hidden',
  },
  charAvatarText: { color: C.text, fontSize: 10, fontWeight: '700' },

  // ── 城市切换 ──
  tabs: {
    flexDirection: 'row', backgroundColor: C.card,
    paddingHorizontal: 12, paddingVertical: 8, gap: 8,
    borderBottomWidth: 1, borderBottomColor: C.border,
  },
  tab: {
    paddingHorizontal: 14, paddingVertical: 6,
    borderRadius: 16, backgroundColor: C.bg,
  },
  tabActive: { backgroundColor: C.accent },
  tabText: { color: C.textMute, fontSize: 13 },
  tabTextActive: { color: '#fff', fontWeight: '600' },

  // ── 空状态 / 加载 ──
  center: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 32 },
  loadingText: { color: C.textMute, fontSize: 13, marginTop: 12 },
  emptyIcon: { fontSize: 48, marginBottom: 12 },
  emptyText: { color: C.text, fontSize: 16, marginBottom: 6 },
  emptySub: { color: C.textMute, fontSize: 12, textAlign: 'center', lineHeight: 18 },

  // ── 底部统计 ──
  footer: {
    flexDirection: 'row', backgroundColor: C.card,
    paddingHorizontal: 12, paddingVertical: 10,
    gap: 10, borderTopWidth: 1, borderTopColor: C.border,
    flexWrap: 'wrap',
  },
  footerItem: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  footerEmoji: { fontSize: 12 },
  footerText: { color: C.textMute, fontSize: 11 },
  footerBadge: {
    paddingHorizontal: 6, paddingVertical: 1,
    borderRadius: 8, marginLeft: 2,
  },
  footerCount: { fontSize: 10, fontWeight: '700' },
});