import sqlite3

conn = sqlite3.connect('central_server/data/cognetra.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute('''SELECT i.event_id, i.camera_id, i.status, i.event_type, i.source_video,
               i.candidate_timestamp, i.edge_confidence,
               v.is_accident, v.is_near_miss, v.heavy_vehicle_involved,
               v.severity, v.vehicles_involved, v.confidence, v.rationale
               FROM incidents i LEFT JOIN verifications v ON i.event_id = v.event_id
               ORDER BY i.created_at ASC''')
rows = cur.fetchall()

# Count stats
accidents = sum(1 for r in rows if r['is_accident'])
near_miss = sum(1 for r in rows if r['is_near_miss'])
heavy = sum(1 for r in rows if r['heavy_vehicle_involved'])
dispatched = sum(1 for r in rows if r['status'] in ('DISPATCHED', 'VERIFIED_ACCIDENT'))
pending = sum(1 for r in rows if r['status'] == 'PENDING_VERIFICATION')

print(f"{'='*80}")
print(f"  COGNETRA 3.0 - FINAL VERDICT SUMMARY ({len(rows)} incidents)")
print(f"{'='*80}")
print(f"  Confirmed Accidents : {accidents}")
print(f"  Near-Miss           : {near_miss}")
print(f"  Heavy Vehicle       : {heavy}")
print(f"  Dispatched          : {dispatched}")
print(f"  Still Pending       : {pending}")
print(f"{'='*80}")

for i, r in enumerate(rows, 1):
    accident  = r['is_accident']
    near      = r['is_near_miss']
    hv        = r['heavy_vehicle_involved']
    sev       = r['severity'] or 'PENDING'
    veh       = (r['vehicles_involved'] or '?')[:40]
    conf      = r['confidence'] or 0.0
    status    = r['status']
    ttype     = (r['event_type'] or '?')[:22]
    src       = (r['source_video'] or '?')[-30:]
    ts        = r['candidate_timestamp'] or 0
    econf     = r['edge_confidence'] or 0
    icon = 'ACCIDENT' if accident else ('NEAR-MISS' if near else 'PENDING..')
    print(f"  V{i:02d} | {icon:9s} | T={ts:5.1f}s | econf={econf:.2f} | {status:22s} | sev={sev:9s} | heavy={str(hv):5s} | gconf={conf:.2f} | {src}")

conn.close()
