import json, sys
src, out = sys.argv[1], sys.argv[2]
XY = lambda lon, lat: ((lon - 124) * 72 + 20, (43.2 - lat) * 84 + 16)
def dp(pts, tol):
    if len(pts) < 3: return pts
    a, b = pts[0], pts[-1]; dx, dy = b[0]-a[0], b[1]-a[1]; L = (dx*dx+dy*dy) ** .5 or 1e-9
    i, dm = 0, -1
    for k in range(1, len(pts)-1):
        p = pts[k]; d = abs(dy*(p[0]-a[0]) - dx*(p[1]-a[1])) / L
        if d > dm: i, dm = k, d
    if dm > tol: return dp(pts[:i+1], tol)[:-1] + dp(pts[i:], tol)
    return [a, b]
def area(r): return abs(sum(r[i][0]*r[i-1][1]-r[i-1][0]*r[i][1] for i in range(len(r))))/2
d = json.load(open(src, encoding='utf-8'))
res = []
for f in d['features']:
    g = f['geometry']; polys = g['coordinates'] if g['type'] == 'MultiPolygon' else [g['coordinates']]
    main, far, big, bb = [], [], (0, None), [1e9, 1e9, -1e9, -1e9]
    for poly in polys:
        ring = [XY(*p[:2]) for p in poly[0]]
        a = area(ring); isfar = poly[0][0][0] > 130.2
        if a < 1.2 and not isfar: continue
        h = len(ring)//2
        s = dp(ring[:h+1], 0.45)[:-1] + dp(ring[h:], 0.45)
        if len(s) < 3: s = ring[:: max(1, len(ring)//6)]
        path = 'M' + 'L'.join(f'{x:.1f} {y:.1f}' for x, y in s) + 'Z'
        (far if isfar else main).append(path)
        if not isfar and not (f['properties']['code'] == '23' and poly[0][0][0] < 126.0):
            for x, y in s: bb = [min(bb[0], x), min(bb[1], y), max(bb[2], x), max(bb[3], y)]
            if a > big[0]:
                cx = sum(p[0] for p in ring)/len(ring); cy = sum(p[1] for p in ring)/len(ring); big = (a, (round(cx, 1), round(cy, 1)))
    res.append({'code': f['properties']['code'], 'name': f['properties']['name'], 'd': ''.join(main), 'far': ''.join(far), 'bb': [round(v, 1) for v in bb], 'c': big[1]})
open(out, 'w', encoding='utf-8').write('  const PROV = ' + json.dumps(res, ensure_ascii=False, separators=(',', ':')) + ';\n')
print(len(open(out, encoding='utf-8').read()), 'bytes', sum(r['d'].count('L') for r in res), 'pts')
