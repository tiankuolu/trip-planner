import { useEffect, useRef, useState } from 'react';
import AMapLoader from '@amap/amap-jsapi-loader';
import { LoaderCircle, MapPin } from 'lucide-react';

export type Place = { name: string; address: string; description?: string; location?: { longitude: number; latitude: number } | null; source?: string; poi_id?: string; rating?: number; ticket_price?: number; visit_duration?: number };
export default function TripMap({ points, mapKey, securityCode, onSelect }: { points: Place[]; mapKey: string; securityCode: string; onSelect: (place: Place) => void }) {
  const container = useRef<HTMLDivElement>(null);
  const select = useRef(onSelect);
  select.current = onSelect;
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    let map: any = null;
    if (!mapKey) { setError('请在 frontend/.env 配置高德地图 JS API Key。'); setLoading(false); return; }
    const locations = points.filter(place => place.location && Number.isFinite(place.location.longitude) && Number.isFinite(place.location.latitude));
    if (!locations.length) { setLoading(false); return; }
    setLoading(true); setError('');
    if (securityCode) (window as Window & { _AMapSecurityConfig?: { securityJsCode: string } })._AMapSecurityConfig = { securityJsCode: securityCode };
    AMapLoader.load({ key: mapKey, version: '2.0', plugins: ['AMap.Scale', 'AMap.ToolBar'] }).then(AMap => {
      if (!active || !container.current) return;
      map = new AMap.Map(container.current, { zoom: 12, center: [locations[0].location!.longitude, locations[0].location!.latitude], mapStyle: 'amap://styles/normal' });
      map.addControl(new AMap.Scale()); map.addControl(new AMap.ToolBar());
      const markers = locations.map((place, index) => {
        const marker = new AMap.Marker({ position: [place.location!.longitude, place.location!.latitude], title: place.name, label: { content: String(index + 1), direction: 'top' } });
        marker.on('click', () => select.current(place));
        return marker;
      });
      map.add(markers);
      if (locations.length > 1) map.add(new AMap.Polyline({ path: locations.map(place => [place.location!.longitude, place.location!.latitude]), strokeColor: '#3a8b93', strokeWeight: 4, strokeOpacity: 0.65 }));
      map.setFitView(markers, false, [45, 35, 45, 35], 15);
      setLoading(false);
    }).catch(reason => { if (active) { setError(String(reason.message || reason)); setLoading(false); } });
    return () => { active = false; map?.destroy(); };
  }, [points, mapKey, securityCode]);
  return <div className="map-shell"><div ref={container} className="amap-container"/>{loading && <div className="map-overlay"><LoaderCircle className="spin" size={25}/><span>加载高德地图</span></div>}{error && <div className="map-overlay map-error"><MapPin size={25}/><span>{error}</span></div>}{!points.some(place => place.location) && !loading && !error && <div className="map-overlay"><MapPin size={32}/><span>该日没有已记录坐标的景点</span></div>}<span className="map-legend">景点位置 · 按行程顺序连线</span></div>;
}
