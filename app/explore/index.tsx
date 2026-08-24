// app/explore/index.tsx
// 探店地图:用 WebView + Leaflet.js 显示角色去过的店铺
// 不需要 react-native-maps,不需要 Google API Key,完全免费
import axios from 'axios';
import { useFocusEffect, useRouter } from 'expo-router';
import React, { useCallback, useRef, useState } from 'react';
import {
  ActivityIndicator, Platform, StatusBar, StyleSheet,
  Text, TouchableOpacity, View,
} from 'react-native';
import { WebView } from 'react-native-webview';
import { C, SERVER_URL } from '../../constants/theme';

const UID = 'user_mofpiyd7442ia7';

// 城市切换按钮
const CITY_TABS = [
  { id: 'tokyo', label: '东京', lat: 35.6762, lng: 139.6503, zoom: 12 },
  { id: 'kyoto', label: '京都', lat: 35.0116, lng: 135.7681, zoom: 13 },
  { id: 'osaka', label: '大阪', lat: 34.6937, lng: 135.5023, zoom: 13 },
  { id: 'all',   label: '全部', lat: 36.5, lng: 138.0, zoom: 6 },
];

// 类别颜色
const CAT_COLORS: Record<string, string> = {
  cafe: '#F59E0B',
  restaurant: '#EF4444',
  sweets: '#EC4899',
  bakery: '#F97316',
  ramen: '#DC2626',
  fashion: '#8B5CF6',
  bookstore: '#3B82F6',
  convenience: '#10B981',
};

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

export default function ExploreScreen() {
  const router = useRouter();
  const [city, setCity] = useState('tokyo');
  const [places, setPlaces] = useState<Place[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const webRef = useRef<WebView>(null);

  const load = async () => {
    try {
      const res = await axios.get(`${SERVER_URL}/explore/visited`, {
        params: { user_id: UID, city: city === 'all' ? undefined : city },
        timeout: 10000,
      });
      setPlaces(res.data?.places || []);
      setTotal(res.data?.total || 0);
    } catch (e: any) {
      console.warn('[explore] load failed:', e?.message);
    }
  };

  useFocusEffect(useCallback(() => {
    let c = false;
    (async () => {
      setLoading(true);
      await load();
      if (!c) setLoading(false);
    })();
    return () => { c = true; };
  }, [city]));

  const cityInfo = CITY_TABS.find(c => c.id === city) || CITY_TABS[0];

  // 生成 Leaflet HTML
  const mapHtml = `
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * { margin: 0; padding: 0; }
  #map { width: 100vw; height: 100vh; background: #1a1a2e; }
  .custom-popup { font-size: 13px; line-height: 1.5; max-width: 220px; }
  .custom-popup .name { font-weight: 700; font-size: 14px; margin-bottom: 4px; color: #1a1a2e; }
  .custom-popup .cat { color: #666; font-size: 11px; margin-bottom: 4px; }
  .custom-popup .review { color: #333; font-style: italic; margin-top: 6px; padding-top: 6px; border-top: 1px solid #eee; }
  .custom-popup .date { color: #999; font-size: 10px; margin-top: 4px; }
  .leaflet-tile-pane { filter: saturate(0.85) brightness(0.95); }
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

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 18,
}).addTo(map);

var places = ${JSON.stringify(places)};
var catColors = ${JSON.stringify(CAT_COLORS)};

places.forEach(function(p) {
  var color = catColors[p.category] || '#5BC4FF';
  var icon = L.divIcon({
    html: '<div style="width:14px;height:14px;border-radius:50%;background:' + color + ';border:2px solid white;box-shadow:0 2px 6px rgba(0,0,0,0.3);"></div>',
    className: '',
    iconSize: [14, 14],
    iconAnchor: [7, 7],
    popupAnchor: [0, -10],
  });
  var popup = '<div class="custom-popup">'
    + '<div class="name">' + (p.place_name || '') + '</div>'
    + '<div class="cat">' + (p.category || '') + ' · ' + (p.place_address || p.city || '') + '</div>'
    + (p.char_review ? '<div class="review">"' + p.char_review + '"</div>' : '')
    + (p.visit_date ? '<div class="date">' + p.visit_date + ' 去的</div>' : '')
    + '</div>';
  L.marker([p.lat, p.lng], { icon: icon }).addTo(map).bindPopup(popup);
});
</script>
</body>
</html>`;

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      <View style={s.header}>
        <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
          <Text style={s.backText}>‹</Text>
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={s.title}>探店地图</Text>
          <Text style={s.sub}>已探 {total} 家</Text>
        </View>
      </View>

      {/* 城市切换 */}
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

      {/* 地图 */}
      <View style={{ flex: 1 }}>
        {loading ? (
          <View style={s.center}>
            <ActivityIndicator color={C.accent} />
            <Text style={s.loadingText}>加载中...</Text>
          </View>
        ) : places.length === 0 ? (
          <View style={s.center}>
            <Text style={s.emptyIcon}>🗺️</Text>
            <Text style={s.emptyText}>还没有探店记录</Text>
            <Text style={s.emptySub}>日程生成后,TA 去过的店会自动出现在这里</Text>
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

      {/* 底部统计 */}
      {places.length > 0 && (
        <View style={s.footer}>
          {Object.entries(
            places.reduce((acc, p) => {
              acc[p.category] = (acc[p.category] || 0) + 1;
              return acc;
            }, {} as Record<string, number>)
          ).slice(0, 5).map(([cat, count]) => (
            <View key={cat} style={s.footerItem}>
              <View style={[s.footerDot, { backgroundColor: CAT_COLORS[cat] || '#5BC4FF' }]} />
              <Text style={s.footerText}>{cat} {count}</Text>
            </View>
          ))}
        </View>
      )}
    </View>
  );
}

const s = StyleSheet.create({
  header: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: Platform.OS === 'ios' ? 50 : 40, paddingBottom: 10, borderBottomWidth: 1, borderBottomColor: C.border, backgroundColor: C.card },
  backBtn: { paddingHorizontal: 6, paddingVertical: 4 },
  backText: { color: C.text, fontSize: 30, lineHeight: 32, fontWeight: '300' },
  title: { color: C.text, fontSize: 17, fontWeight: '700' },
  sub: { color: C.textMute, fontSize: 11, marginTop: 2 },
  tabs: { flexDirection: 'row', backgroundColor: C.card, paddingHorizontal: 12, paddingVertical: 8, gap: 8, borderBottomWidth: 1, borderBottomColor: C.border },
  tab: { paddingHorizontal: 14, paddingVertical: 6, borderRadius: 16, backgroundColor: C.bg },
  tabActive: { backgroundColor: C.accent },
  tabText: { color: C.textMute, fontSize: 13 },
  tabTextActive: { color: '#fff', fontWeight: '600' },
  center: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 32 },
  loadingText: { color: C.textMute, fontSize: 13, marginTop: 12 },
  emptyIcon: { fontSize: 48, marginBottom: 12 },
  emptyText: { color: C.text, fontSize: 16, marginBottom: 6 },
  emptySub: { color: C.textMute, fontSize: 12, textAlign: 'center' },
  footer: { flexDirection: 'row', backgroundColor: C.card, paddingHorizontal: 16, paddingVertical: 10, gap: 14, borderTopWidth: 1, borderTopColor: C.border, flexWrap: 'wrap' },
  footerItem: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  footerDot: { width: 8, height: 8, borderRadius: 4 },
  footerText: { color: C.textMute, fontSize: 11 },
});
