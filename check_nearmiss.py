import sqlite3
conn = sqlite3.connect('central_server/data/cognetra.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute('''SELECT i.event_id, i.source_video, i.candidate_timestamp, i.edge_confidence,
               v.is_accident, v.is_near_miss, v.heavy_vehicle_involved,
               v.severity, v.vehicles_involved, v.confidence, v.rationale, v.collision_type
               FROM incidents i LEFT JOIN verifications v ON i.event_id = v.event_id
               ORDER BY i.created_at ASC''')
rows = cur.fetchall()
for i, r in enumerate(rows, 1):
    accident = r['is_accident']
    nm = r['is_near_miss']
    src = str(r['source_video'] or '')[-38:]
    ts  = r['candidate_timestamp'] or 0
    ec  = r['edge_confidence'] or 0
    hv  = r['heavy_vehicle_involved']
    sev = r['severity'] or 'N/A'
    veh = r['vehicles_involved'] or 'N/A'
    ct  = r['collision_type'] or 'N/A'
    gc  = r['confidence'] or 0
    rat = r['rationale'] or 'N/A'

    icon = 'ACCIDENT' if accident else ('NEAR-MISS' if nm else 'PENDING')
    print(f"V{i:02d} | {icon} | T={ts:.1f}s | econf={ec:.2f} | heavy={hv} | sev={sev} | gconf={gc:.2f}")
    if not accident:
        print(f"     Source    : {src}")
        print(f"     Vehicles  : {veh}")
        print(f"     CollType  : {ct}")
        print(f"     Rationale : {rat[:300]}")
        print()
conn.close()
