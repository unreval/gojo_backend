// app/explore/index.tsx
// 探店地图 v3:
//   ★ 修复瓦片水印 → OSM + CSS 反色滤镜(免费暗色)
//   ★ 角色头像标记 44px + 发光边框 + 点击 flyTo 放大
//   ★ 角色筛选
//   ★ 日程时间进度(没到的时间不显示)
//   ★ 品类扩展:景点/神社/公园/活动/温泉/购物/美术馆
//   ★ 暗色 popup、分类中文标签、角色碎碎念
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

const CITY_TABS = [
  { id: 'tokyo', label: '东京', lat: 35.6762, lng: 139.6503, zoom: 12 },
  { id: 'kyoto', label: '京都', lat: 35.0116, lng: 135.7681, zoom: 13 },
  { id: 'osaka', label: '大阪', lat: 34.6937, lng: 135.5023, zoom: 13 },
  { id: 'all',   label: '全部', lat: 36.2, lng: 138.0, zoom: 6 },
];

// ── 所有品类(含新增非餐饮) ──
const CAT_META: Record<string, { label: string; color: string; emoji: string }> = {
  // 餐饮
  cafe:          { label: '咖啡厅', color: '#F59E0B', emoji: '☕' },
  restaurant:    { label: '餐厅',   color: '#EF4444', emoji: '🍽' },
  sweets:        { label: '甜品店', color: '#EC4899', emoji: '🍰' },
  bakery:        { label: '面包店', color: '#F97316', emoji: '🥐' },
  ramen:         { label: '拉面店', color: '#DC2626', emoji: '🍜' },
  fashion:       { label: '服装店', color: '#8B5CF6', emoji: '👗' },
  bookstore:     { label: '书店',   color: '#3B82F6', emoji: '📚' },
  convenience:   { label: '便利店', color: '#10B981', emoji: '🏪' },
  // 景点/活动
  shrine:        { label: '神社',   color: '#EF4444', emoji: '⛩️' },
  temple:        { label: '寺庙',   color: '#B45309', emoji: '🏯' },
  park:          { label: '公园',   color: '#22C55E', emoji: '🌳' },
  landmark:      { label: '景点',   color: '#06B6D4', emoji: '🗼' },
  museum:        { label: '美术馆', color: '#A855F7', emoji: '🖼' },
  entertainment: { label: '娱乐',   color: '#F43F5E', emoji: '🎮' },
  shopping:      { label: '购物',   color: '#D946EF', emoji: '🛍' },
  onsen:         { label: '温泉',   color: '#0EA5E9', emoji: '♨️' },
  event:         { label: '活动',   color: '#F97316', emoji: '🎆' },
};

// 角色颜色
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

function toMin(hhmm: string): number {
  const [h, m] = hhmm.split(':').map(Number);
  return h * 60 + m;
}

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

  const [chars, setChars] = useState<CharMeta[]>([]);
  const [filterChar, setFilterChar] = useState<string | null>(null);
  const [schedMap, setSchedMap] = useState<Record<string, SchedItem[]>>({});
  const [serverNow, setServerNow] = useState('');

  const loadChars = async () => {
    try {
      const res = await axios.get(`${SERVER_URL}/characters_all`, { timeout: 8000 });
      const list: CharMeta[] = (res.data?.characters || []).map((c: any) => ({
        id: c.id, name: c.name || c.id, avatar_url: c.avatar_url || null,
      }));
      setChars(list);
      return list;
    } catch { return []; }
  };

  const loadSchedules = async (charList: CharMeta[]) => {
    const map: Record<string, SchedItem[]> = {};
    let now = '';
    for (const c of charList) {
      try {
        const res = await axios.get(`${SERVER_URL}/schedule`, {
          params: { character_id: c.id, user_id: UID }, timeout: 15000,
        });
        map[c.id] = (res.data?.items || []).map((it: any) => ({
          start_time: it.start_time, end_time: it.end_time,
          title: it.title || '', location: it.location || '',
        }));
        if (res.data?.now && !now) now = res.data.now;
      } catch { map[c.id] = []; }
    }
    setSchedMap(map);
    setServerNow(now);
  };

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

  // ★ 时间进度过滤
  const filteredPlaces = (() => {
    const today = todayCN();
    const nowMin = serverNow ? toMin(serverNow) : -1;
    if (nowMin < 0) return places;
    return places.filter(p => {
      if (p.visit_date !== today) return true;
      const charSched = schedMap[p.character_id] || [];
      if (charSched.length === 0) return true;
      const matched = charSched.find(
        s => s.title.includes(p.place_name) || s.location.includes(p.place_name)
          || p.place_name.includes(s.location)
          || (s.location && s.location.length > 2 && p.place_address.includes(s.location))
      );
      if (!matched) return true;
      return toMin(matched.start_time) <= nowMin;
    });
  })();

  const charNameMap: Record<string, string> = {};
  const charAvatarMap: Record<string, string> = {};
  for (const c of chars) {
    charNameMap[c.id] = c.name;
    if (c.avatar_url) charAvatarMap[c.id] = c.avatar_url;
  }

  const mapHtml = buildMapHtml(filteredPlaces, cityInfo, charNameMap, charAvatarMap);
  const activeName = filterChar
    ? (chars.find(c => c.id === filterChar)?.name || filterChar) : null;

  // 统计:合并小品类
  const catStats = filteredPlaces.reduce((acc, p) => {
    acc[p.category] = (acc[p.category] || 0) + 1;
    return acc;
  }, {} as Record<string, number>);

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      {/* 顶栏 */}
      <View style={s.header}>
        <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
          <Text style={s.backText}>‹</Text>
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={s.title}>
            {activeName ? `${activeName}的足迹` : '探索地图'}
          </Text>
          <Text style={s.sub}>
            {filteredPlaces.length === total
              ? `${total} 个打卡点`
              : `显示 ${filteredPlaces.length} / ${total}`}
            {serverNow ? ` · ${serverNow}` : ''}
          </Text>
        </View>
      </View>

      {/* 角色筛选 */}
      {chars.length > 0 && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false}
          style={s.charBar} contentContainerStyle={{ paddingHorizontal: 12, gap: 8 }}>
          <TouchableOpacity
            style={[s.charChip, !filterChar && s.charChipActive]}
            onPress={() => setFilterChar(null)} activeOpacity={0.8}>
            <View style={[s.charDot, { backgroundColor: C.accent }]} />
            <Text style={[s.charChipText, !filterChar && s.charChipTextActive]}>全部</Text>
          </TouchableOpacity>
          {chars.map(c => {
            const on = filterChar === c.id;
            const col = CHAR_COLORS[c.id] || DEFAULT_CHAR_COLOR;
            return (
              <TouchableOpacity key={c.id}
                style={[s.charChip, on && { ...s.charChipActive, borderColor: col }]}
                onPress={() => setFilterChar(on ? null : c.id)} activeOpacity={0.8}>
                <View style={[s.charAvatar, { borderColor: col, backgroundColor: col + '33' }]}>
                  <Text style={s.charAvatarText}>{c.name?.[0] || '?'}</Text>
                </View>
                <Text style={[s.charChipText, on && { color: col }]}>{c.name}</Text>
              </TouchableOpacity>
            );
          })}
        </ScrollView>
      )}

      {/* 城市 */}
      <View style={s.tabs}>
        {CITY_TABS.map(ct => (
          <TouchableOpacity key={ct.id}
            style={[s.tab, city === ct.id && s.tabActive]}
            onPress={() => setCity(ct.id)}>
            <Text style={[s.tabText, city === ct.id && s.tabTextActive]}>{ct.label}</Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* 地图 */}
      <View style={{ flex: 1 }}>
        {loading ? (
          <View style={s.center}>
            <ActivityIndicator color={C.accent} />
            <Text style={s.loadingText}>加载中...</Text>
          </View>
        ) : filteredPlaces.length === 0 ? (
          <View style={s.center}>
            <Text style={s.emptyIcon}>🗺️</Text>
            <Text style={s.emptyText}>
              {filterChar ? `${activeName}还没有足迹` : '还没有打卡记录'}
            </Text>
            <Text style={s.emptySub}>
              日程生成后,TA 去过的地方会自动出现在这里
            </Text>
          </View>
        ) : (
          <WebView ref={webRef} source={{ html: mapHtml }}
            style={{ flex: 1, backgroundColor: C.bg }}
            scrollEnabled javaScriptEnabled domStorageEnabled
            originWhitelist={['*']} />
        )}
      </View>

      {/* 底部统计 */}
      {filteredPlaces.length > 0 && (
        <View style={s.footer}>
          <ScrollView horizontal showsHorizontalScrollIndicator={false}
            contentContainerStyle={{ paddingHorizontal: 12, gap: 12 }}>
            {Object.entries(catStats)
              .sort((a, b) => b[1] - a[1])
              .slice(0, 8)
              .map(([cat, count]) => {
                const meta = CAT_META[cat] || { label: cat, color: '#5BC4FF', emoji: '📍' };
                return (
                  <View key={cat} style={s.footerItem}>
                    <Text style={s.footerEmoji}>{meta.emoji}</Text>
                    <Text style={s.footerLabel}>{meta.label}</Text>
                    <View style={[s.footerBadge, { backgroundColor: meta.color + '33' }]}>
                      <Text style={[s.footerCount, { color: meta.color }]}>{count}</Text>
                    </View>
                  </View>
                );
              })}
          </ScrollView>
        </View>
      )}
    </View>
  );
}


// ══════════════════════════════════════════════════════════════
//  Leaflet HTML
// ══════════════════════════════════════════════════════════════

function buildMapHtml(
  places: Place[],
  cityInfo: { lat: number; lng: number; zoom: number },
  charNameMap: Record<string, string>,
  charAvatarMap: Record<string, string>,
) {
  return `<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"><\/script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#070d1a}
#map{width:100vw;height:100vh;background:#070d1a}

/* ★ OSM + CSS 反色 = 免费暗色地图,不需要 API Key */
.leaflet-tile-pane{
  filter:invert(1) hue-rotate(190deg) brightness(0.65) saturate(0.35) contrast(1.6);
}
.leaflet-control-attribution{display:none!important}

/* ── 头像标记 44px ── */
.am{
  width:44px;height:44px;border-radius:50%;
  border:3px solid #3b82f6;
  overflow:hidden;
  box-shadow:0 0 10px rgba(59,130,246,0.5),0 3px 10px rgba(0,0,0,0.6);
  background:#0d1a2e;
  display:flex;align-items:center;justify-content:center;
  transition:transform 0.2s;
}
.am:active{transform:scale(1.15)}
.am img{width:100%;height:100%;object-fit:cover;display:block}
.am .init{
  color:#e8f4ff;font-size:17px;font-weight:800;
  line-height:38px;text-align:center;width:100%;
}

/* ── 分类小角标 ── */
.cat-badge{
  position:absolute;bottom:-2px;right:-2px;
  width:18px;height:18px;border-radius:50%;
  background:#0d1a2e;border:2px solid #1a3a5c;
  display:flex;align-items:center;justify-content:center;
  font-size:10px;line-height:1;
}

/* ── popup ── */
.leaflet-popup-content-wrapper{
  background:#0d1a2e!important;
  border:1px solid #1a3a5c!important;
  border-radius:16px!important;
  box-shadow:0 8px 32px rgba(0,0,0,0.6)!important;
  padding:0!important;
}
.leaflet-popup-tip{background:#0d1a2e!important;border:1px solid #1a3a5c!important}
.leaflet-popup-content{margin:0!important;width:250px!important}
.leaflet-popup-close-button{color:#7ba8d0!important;font-size:20px!important;top:8px!important;right:10px!important}

.sp{padding:16px;font-family:-apple-system,sans-serif}
.sp .sh{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.sp .sa{
  width:34px;height:34px;border-radius:50%;overflow:hidden;flex-shrink:0;
  border:2px solid #1a3a5c;background:#0f2040;
}
.sp .sa img{width:100%;height:100%;object-fit:cover}
.sp .sa .si{color:#7ba8d0;font-size:14px;font-weight:700;line-height:30px;text-align:center}
.sp .sn{font-size:15px;font-weight:700;color:#e8f4ff;line-height:1.3;flex:1}
.sp .sm{display:flex;align-items:center;gap:6px;margin-bottom:2px;flex-wrap:wrap}
.sp .ct{font-size:11px;padding:3px 10px;border-radius:10px;font-weight:600}
.sp .sa2{font-size:11px;color:#3d6080}
.sp .sr{
  font-size:13px;color:#7ba8d0;line-height:1.5;
  padding:10px 0;margin-top:8px;
  border-top:1px solid #1a3a5c;font-style:italic;
}
.sp .sr::before{content:'\\201C';color:#3b82f6;font-size:18px;margin-right:3px}
.sp .sr::after{content:'\\201D';color:#3b82f6;font-size:18px;margin-left:3px}
.sp .sf{
  display:flex;justify-content:space-between;align-items:center;
  margin-top:8px;padding-top:8px;border-top:1px solid #1a3a5c;
}
.sp .sc{font-size:11px;font-weight:600}
.sp .sd{font-size:10px;color:#3d6080}
</style>
</head>
<body>
<div id="map"></div>
<script>
var map=L.map('map',{center:[${cityInfo.lat},${cityInfo.lng}],zoom:${cityInfo.zoom},zoomControl:false,attributionControl:false});

// ★ 用 OpenStreetMap 标准瓦片(免费,不要 API Key)
// 暗色效果靠 CSS filter 实现
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
  maxZoom:19, subdomains:'abc'
}).addTo(map);

var places=${JSON.stringify(places)};
var catMeta=${JSON.stringify(CAT_META)};
var charColors=${JSON.stringify(CHAR_COLORS)};
var avatarMap=${JSON.stringify(charAvatarMap)};
var nameMap=${JSON.stringify(charNameMap)};

function esc(s){if(!s)return '';var d=document.createElement('div');d.textContent=s;return d.innerHTML}

places.forEach(function(p){
  var cm=catMeta[p.category]||{label:p.category,color:'#5BC4FF',emoji:'📍'};
  var cc=charColors[p.character_id]||'#5BC4FF';
  var cn=nameMap[p.character_id]||p.character_id;
  var av=avatarMap[p.character_id];
  var ci=cn?cn[0]:'?';

  // ★ 头像标记 + 分类小角标
  var inner;
  if(av){
    inner='<img src="'+av+'" onerror="this.style.display=\\'none\\';this.nextElementSibling.style.display=\\'block\\'"/>'
      +'<div class="init" style="display:none">'+esc(ci)+'</div>';
  }else{
    inner='<div class="init">'+esc(ci)+'</div>';
  }
  var mhtml='<div style="position:relative">'
    +'<div class="am" style="border-color:'+cc+'">'+inner+'</div>'
    +'<div class="cat-badge">'+cm.emoji+'</div>'
    +'</div>';

  var icon=L.divIcon({
    html:mhtml, className:'',
    iconSize:[48,48], iconAnchor:[24,24], popupAnchor:[0,-28],
  });

  // popup
  var avh=av?'<img src="'+av+'"/>':'<div class="si">'+esc(ci)+'</div>';
  var popup='<div class="sp">'
    +'<div class="sh">'
    +'<div class="sa" style="border-color:'+cc+'">'+avh+'</div>'
    +'<div class="sn">'+esc(p.place_name)+'</div>'
    +'</div>'
    +'<div class="sm">'
    +'<span class="ct" style="background:'+cm.color+'22;color:'+cm.color+'">'+cm.emoji+' '+cm.label+'</span>'
    +'<span class="sa2">'+esc(p.place_address||p.city||'')+'</span>'
    +'</div>'
    +(p.char_review?'<div class="sr">'+esc(p.char_review)+'</div>':'')
    +'<div class="sf">'
    +'<span class="sc" style="color:'+cc+'">'+esc(cn)+'</span>'
    +'<span class="sd">'+esc(p.visit_date||'')+'</span>'
    +'</div></div>';

  var marker=L.marker([p.lat,p.lng],{icon:icon}).addTo(map).bindPopup(popup,{maxWidth:260,minWidth:250});

  // ★ 点击 → flyTo 放大 + 弹 popup
  marker.on('click',function(e){
    map.flyTo(e.latlng,Math.max(map.getZoom(),16),{duration:0.6});
  });
});

// 自动适配视野
if(places.length>1&&places.length<200){
  try{
    var b=L.latLngBounds(places.map(function(p){return[p.lat,p.lng]}));
    map.fitBounds(b,{padding:[50,50],maxZoom:15});
  }catch(e){}
}else if(places.length===1){
  map.setView([places[0].lat,places[0].lng],15);
}
<\/script>
</body>
</html>`;
}


// ══════════════════════════════════════════════════════════════
const s = StyleSheet.create({
  header:{
    flexDirection:'row',alignItems:'center',paddingHorizontal:12,
    paddingTop:Platform.OS==='ios'?50:40,paddingBottom:10,
    borderBottomWidth:1,borderBottomColor:C.border,backgroundColor:C.card,
  },
  backBtn:{paddingHorizontal:6,paddingVertical:4},
  backText:{color:C.text,fontSize:30,lineHeight:32,fontWeight:'300'},
  title:{color:C.text,fontSize:17,fontWeight:'700'},
  sub:{color:C.textMute,fontSize:11,marginTop:2},

  charBar:{flexGrow:0,paddingVertical:10,backgroundColor:C.card,borderBottomWidth:1,borderBottomColor:C.border},
  charChip:{
    flexDirection:'row',alignItems:'center',gap:6,
    paddingHorizontal:12,paddingVertical:6,borderRadius:20,
    borderWidth:1,borderColor:C.border,backgroundColor:C.bg,
  },
  charChipActive:{borderColor:C.accent,backgroundColor:C.accent+'18'},
  charChipText:{color:C.textMute,fontSize:13},
  charChipTextActive:{color:C.accent2,fontWeight:'600'},
  charDot:{width:8,height:8,borderRadius:4},
  charAvatar:{
    width:22,height:22,borderRadius:11,borderWidth:1.5,
    alignItems:'center',justifyContent:'center',overflow:'hidden',
  },
  charAvatarText:{color:C.text,fontSize:10,fontWeight:'700'},

  tabs:{
    flexDirection:'row',backgroundColor:C.card,
    paddingHorizontal:12,paddingVertical:8,gap:8,
    borderBottomWidth:1,borderBottomColor:C.border,
  },
  tab:{paddingHorizontal:14,paddingVertical:6,borderRadius:16,backgroundColor:C.bg},
  tabActive:{backgroundColor:C.accent},
  tabText:{color:C.textMute,fontSize:13},
  tabTextActive:{color:'#fff',fontWeight:'600'},

  center:{flex:1,justifyContent:'center',alignItems:'center',padding:32},
  loadingText:{color:C.textMute,fontSize:13,marginTop:12},
  emptyIcon:{fontSize:48,marginBottom:12},
  emptyText:{color:C.text,fontSize:16,marginBottom:6},
  emptySub:{color:C.textMute,fontSize:12,textAlign:'center',lineHeight:18},

  footer:{
    backgroundColor:C.card,paddingVertical:10,
    borderTopWidth:1,borderTopColor:C.border,
  },
  footerItem:{flexDirection:'row',alignItems:'center',gap:4},
  footerEmoji:{fontSize:13},
  footerLabel:{color:C.textMute,fontSize:11},
  footerBadge:{paddingHorizontal:6,paddingVertical:1,borderRadius:8,marginLeft:2},
  footerCount:{fontSize:10,fontWeight:'700'},
});