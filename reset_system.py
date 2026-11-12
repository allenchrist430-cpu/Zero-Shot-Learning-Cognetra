"""
Cognetra 3.0 — System Reset Utility
Resets all database tables (incidents, verifications, dispatch logs)
and clears active clip directories so telemetry starts at all 0's.
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

# Add central_server to path
sys.path.insert(0, str(Path(__file__).parent / "central_server"))

from sqlalchemy import delete
from models.database import engine, init_db, Incident, Verification, DispatchLog, AsyncSessionLocal

async def reset_all():
    print("====================================================")
    print("COGNETRA 3.0 — RESET SYSTEM TELEMETRY TO ALL 0'S")
    print("====================================================")
    await init_db()
    async with AsyncSessionLocal() as session:
        print("[*] Purging dispatch_logs table...")
        await session.execute(delete(DispatchLog))
        print("[*] Purging verifications table...")
        await session.execute(delete(Verification))
        print("[*] Purging incidents table...")
        await session.execute(delete(Incident))
        await session.commit()
    print("[OK] Database cleared.")

    # Clear edge client queue if any
    edge_queue = Path("edge_client/queue")
    if edge_queue.exists():
        for item in edge_queue.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
            except Exception as e:
                print(f"[-] Could not remove {item}: {e}")
        print("[OK] Edge client queue cleared.")

    print("\nSUCCESS: All statistics, database records, and queues reset to 0!")
    print("Dashboard counters will show 0 incidents across all categories.")
    print("====================================================\n")

if __name__ == "__main__":
    asyncio.run(reset_all())
