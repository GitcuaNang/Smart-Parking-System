
import re
from datetime import datetime
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, db


# ===== CAU HINH FIREBASE =====
BASE_DIR = Path(r"D:\SmartParking")

SERVICE_ACCOUNT_FILE = BASE_DIR / "serviceAccountKey.json"

# Thay bằng Database URL trong Firebase Console.

FIREBASE_DATABASE_URL = (
    "https://smart-parking-78632-default-rtdb.firebaseio.com/"
)


def init_firebase():
    """Khoi tao Firebase Admin SDK mot lan."""
    if not SERVICE_ACCOUNT_FILE.is_file():
        raise FileNotFoundError(
            f"Khong tim thay file khoa Firebase: "
            f"{SERVICE_ACCOUNT_FILE}"
        )

    if not firebase_admin._apps:
        credential = credentials.Certificate(
            str(SERVICE_ACCOUNT_FILE)
        )

        firebase_admin.initialize_app(
            credential,
            {
                "databaseURL": FIREBASE_DATABASE_URL,
                "httpTimeout": 5,
            },
        )

    return db.reference("/")


# Khoi tao khi import module.
root_ref = init_firebase()


def normalize_plate(plate):
    """Tao khoa bien so, bo dau cach va ky tu phan cach."""
    return re.sub(r"[^A-Z0-9]", "", plate.upper())


def upload_vehicle_event(plate, gate, status):
    """
    Ghi mot su kien vao/ra va cap nhat trang thai hien tai.
    status nhan 'VÀO' hoac 'RA'.
    gate nhan 'CONG_VAO' hoac 'CONG_RA'.
    """
    plate = plate.strip().upper()
    plate_key = normalize_plate(plate)

    if not plate_key:
        print("[FIREBASE] Bien so rong, bo qua.")
        return False

    if status not in ("VÀO", "RA"):
        print(f"[FIREBASE] Trang thai khong hop le: {status}")
        return False

    if gate not in ("CONG_VAO", "CONG_RA"):
        print(f"[FIREBASE] Cong khong hop le: {gate}")
        return False

    # Kiem tra logic vao/ra co khop voi cong khong.
    if (status == "VÀO" and gate != "CONG_VAO") or (
        status == "RA" and gate != "CONG_RA"
    ):
        print("[FIREBASE] Trang thai va cong khong khop.")
        return False

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current_status = "IN" if status == "VÀO" else "OUT"

    try:
        # Tao ID duy nhat cho su kien lich su.
        event_ref = root_ref.child("vehicle_events").push()
        event_id = event_ref.key

        # Cap nhat ca lich su va trang thai hien tai
        # trong cung mot lenh update o nut goc.
        updates = {
            f"vehicle_events/{event_id}": {
                "plate": plate,
                "plate_key": plate_key,
                "gate": gate,
                "status": status,
                "timestamp": now,
            },
            f"vehicles/{plate_key}": {
                "plate": plate,
                "status": current_status,
                "last_gate": gate,
                "last_update": now,
            },
        }

        root_ref.update(updates)

        print(
            f"[FIREBASE] OK | {plate} | {gate} | "
            f"{status} | {now}",
            flush=True,
        )
        return True

    except Exception as exc:
        print(
            f"[FIREBASE] LOI GHI DU LIEU: {exc}",
            flush=True,
        )
        return False


def test_firebase():
    """Test ket noi bang mot su kien thu."""
    result = upload_vehicle_event(
        plate="99E1-7777",
        gate="CONG_VAO",
        status="VÀO",
    )
    print("Ket qua test:", result)


if __name__ == "__main__":
    test_firebase()
