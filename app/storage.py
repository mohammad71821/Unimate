import os
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

# توی Termux از مسیر خانگی استفاده می‌کنیم؛ روی Railway (یا هر جای دیگه) با
# ENV var می‌شه یه مسیر ثابت (که یه Volume دائمی روش mount شده) مشخص کرد —
# وگرنه با هر ری‌دیپلوی، فایل‌های آپلودشده از بین می‌رن.
STORAGE_ROOT = Path(os.environ.get("STORAGE_ROOT_DIR") or (Path.home() / "unimate-ai" / "storage_data"))
STORAGE_ROOT.mkdir(parents=True, exist_ok=True)


class LocalStorageBackend:
    """
    Disk-based storage backend. Same interface as the future MinIO/S3 backend
    (save_file / get_file_path / delete_file), so swapping backends later
    requires no changes in routers or business logic.
    """

    def save_file(self, file_obj: BinaryIO, original_filename: str) -> str:
        ext = Path(original_filename).suffix
        key = f"{uuid.uuid4()}{ext}"
        destination = STORAGE_ROOT / key
        with open(destination, "wb") as f:
            shutil.copyfileobj(file_obj, f)
        return key

    def get_file_path(self, key: str) -> Path:
        return STORAGE_ROOT / key

    def delete_file(self, key: str) -> None:
        path = STORAGE_ROOT / key
        if path.exists():
            path.unlink()


storage_backend = LocalStorageBackend()
