// Minimal Leaflet stand-in for offline smoke tests of index.html logic.
// Implements only the surface index.html touches; geometry math is real
// enough for bounds/zoom checks. NOT a renderer.
(function () {
  const L = {};
  class Evented {
    constructor() { this._ev = {}; }
    on(n, f) { (this._ev[n] = this._ev[n] || []).push(f); return this; }
    fire(n, e) { (this._ev[n] || []).forEach(f => f(e || {})); return this; }
  }
  class LatLng { constructor(lat, lng) { this.lat = lat; this.lng = lng; } }
  L.latLng = (a, b) => a instanceof LatLng ? a : Array.isArray(a) ? new LatLng(a[0], a[1]) : new LatLng(a, b);
  class LatLngBounds {
    constructor(a, b) {
      const pts = b ? [a, b] : a;
      this.s = Math.min(...pts.map(p => L.latLng(p).lat)); this.n = Math.max(...pts.map(p => L.latLng(p).lat));
      this.w = Math.min(...pts.map(p => L.latLng(p).lng)); this.e = Math.max(...pts.map(p => L.latLng(p).lng));
    }
    pad(r) { const dh = (this.n - this.s) * r, dw = (this.e - this.w) * r; return new LatLngBounds([[this.s - dh, this.w - dw], [this.n + dh, this.e + dw]]); }
    intersects(o) { return o.s <= this.n && o.n >= this.s && o.w <= this.e && o.e >= this.w; }
    getCenter() { return new LatLng((this.s + this.n) / 2, (this.w + this.e) / 2); }
  }
  L.latLngBounds = (a, b) => new LatLngBounds(a, b);
  class Layer extends Evented {
    addTo(m) { m.addLayer(this); return this; }
    bindTooltip(html, o) { this._tip = html; return this; }
  }
  class TileLayer extends Layer { constructor(url, o) { super(); this.url = url; this.options = o || {}; } }
  L.tileLayer = (u, o) => new TileLayer(u, o);
  class Marker extends Layer {
    constructor(ll, o) { super(); this._latlng = L.latLng(ll); this.options = o || {}; this._el = null; }
    setIcon(i) { this.options.icon = i; if (this._el) this._el.innerHTML = i.options.html || ""; return this; }
    getLatLng() { return this._latlng; }
    _render(m) {
      if (!this._el) { this._el = document.createElement("div"); this._el.className = "leaflet-marker-icon " + (this.options.icon?.options?.className || ""); this._el.innerHTML = this.options.icon?.options?.html || ""; }
      m._pane.appendChild(this._el);
    }
    _unrender() { this._el?.remove(); }
  }
  L.marker = (ll, o) => new Marker(ll, o);
  class DivIcon { constructor(o) { this.options = o || {}; } }
  L.divIcon = o => new DivIcon(o);
  class Path extends Layer { constructor(o) { super(); this.options = o || {}; } _render() {} _unrender() {} }
  class LayerGroup extends Layer {
    constructor() { super(); this._layers = []; }
    addLayer(l) { this._layers.push(l); if (this._map) l._render?.(this._map); return this; }
    clearLayers() { this._layers.forEach(l => l._unrender?.()); this._layers = []; return this; }
    eachLayer(f) { this._layers.forEach(f); }
    _render(m) { this._map = m; this._layers.forEach(l => l._render?.(m)); }
    _unrender() { this._layers.forEach(l => l._unrender?.()); this._map = null; }
    getLayers() { return this._layers; }
  }
  L.featureGroup = () => new LayerGroup();
  L.markerClusterGroup = () => new LayerGroup();
  L.geoJSON = (fc, o) => {
    const g = new LayerGroup();
    for (const f of fc.features) {
      let layer;
      if (f.geometry.type === "Point") {
        const [lng, lat] = f.geometry.coordinates;
        layer = o.pointToLayer ? o.pointToLayer(f, L.latLng(lat, lng)) : L.marker([lat, lng]);
      } else { layer = new Path(o.style ? o.style(f) : {}); }
      layer.feature = f;
      o.onEachFeature && o.onEachFeature(f, layer);
      g.addLayer(layer);
    }
    return g;
  };
  L.Control = class { constructor(o) { this.options = o || {}; } };
  L.Control.extend = proto => class extends L.Control { constructor(o) { super(o); Object.assign(this, proto); } };
  L.DomUtil = { create(tag, cls, parent) { const e = document.createElement(tag); if (cls) e.className = cls; parent && parent.appendChild(e); return e; } };
  L.DomEvent = { on(el, ev, f) { el.addEventListener(ev, f); }, stop() {}, disableClickPropagation() {} };
  class Map extends Evented {
    constructor(id, o) {
      super(); this.options = o || {}; this._zoom = o.zoom; this._center = L.latLng(o.center);
      this._layers = new Set(); this._pane = document.createElement("div"); this._pane.className = "leaflet-marker-pane";
      document.getElementById(id).appendChild(this._pane);
    }
    getZoom() { return this._zoom; }
    getCenter() { return this._center; }
    getBounds() { const span = 360 / Math.pow(2, this._zoom) * 3.5; const spanLat = span / 2; return new LatLngBounds([[this._center.lat - spanLat, this._center.lng - span], [this._center.lat + spanLat, this._center.lng + span]]); }
    setView(c, z) { this._center = L.latLng(c); if (z != null) { const zc = z !== this._zoom; this._zoom = z; if (zc) this.fire("zoomend"); } this.fire("moveend"); return this; }
    fitBounds(b, o) { const z = Math.min((o && o.maxZoom) || 18, 14); return this.setView(b.getCenter(), z); }
    addLayer(l) { this._layers.add(l); l._map = this; l._render?.(this); return this; }
    removeLayer(l) { this._layers.delete(l); l._unrender?.(); l._map = null; return this; }
    hasLayer(l) { return this._layers.has(l); }
    addControl(c) { const el = c.onAdd(this); document.getElementById("map").appendChild(el); return this; }
  }
  L.map = (id, o) => new Map(id, o);
  window.L = L;
})();
