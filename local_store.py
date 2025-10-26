import os, re, json, time, shutil, glob
from dataclasses import dataclass
from typing import List, Dict, Optional

ALLOWED_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp'}
ALLOWED_VIDEO_EXTS = {'.mp4', '.avi', '.mov', '.mkv', '.webm', '.3gp'}

def _date_key(ts: Optional[float] = None) -> str:
    return time.strftime("%Y%m%d", time.localtime(ts or time.time()))

@dataclass
class UploadRow:
    path: str
    filename: str
    media_type: str   # 'image' or 'video'
    created_at: float

class LocalStore:
    """
    Per-user storage keyed by mobile number (10 digits).

    Layout:
      base_dir/
        users/
          <mobile>/
            profile.json
            uploads/
              <mobile>_<YYYYMMDD>_<digit>.<ext>
    """
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.users_root = os.path.join(base_dir, "users")
        os.makedirs(self.users_root, exist_ok=True)

    # ---------- helpers ----------
    def _norm_mobile(self, mobile: str) -> str:
        m = re.sub(r"\D", "", (mobile or "").strip())
        if len(m) != 10:
            raise ValueError("mobile must be a 10-digit string")
        return m

    def _user_dir(self, mobile: str) -> str:
        return os.path.join(self.users_root, self._norm_mobile(mobile))

    def _profile_path(self, mobile: str) -> str:
        return os.path.join(self._user_dir(mobile), "profile.json")

    def _uploads_dir(self, mobile: str) -> str:
        return os.path.join(self._user_dir(mobile), "uploads")

    # ---------- profile ----------
    def load_profile(self, mobile: str) -> Dict[str, str]:
        mob = self._norm_mobile(mobile)
        p = self._profile_path(mob)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        else:
            data = {}

        data.setdefault("mobile", mob)
        data.setdefault("name", "")
        data.setdefault("email", "")
        data.setdefault("state", "")
        data.setdefault("district", "")
        data.setdefault("address", "")

        # ensure file exists
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return data

    def save_profile(self, mobile: str, data: Dict[str, str]) -> None:
        mob = self._norm_mobile(mobile)
        p = self._profile_path(mob)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---------- uploads ----------
    def _detect_media_type(self, ext: str) -> str:
        ext = (ext or "").lower()
        if ext in ALLOWED_VIDEO_EXTS:
            return "video"
        return "image"

    def _next_digit_for_day(self, mobile: str, date_key: str) -> int:
        """Scan user's uploads dir for existing files for that day and return next digit."""
        mob = self._norm_mobile(mobile)
        udir = self._uploads_dir(mob)
        os.makedirs(udir, exist_ok=True)
        pattern = os.path.join(udir, f"{mob}_{date_key}_*")
        digits = []
        for p in glob.glob(pattern):
            base = os.path.basename(p)
            # expected: <mobile>_<YYYYMMDD>_<digit>.<ext>
            try:
                part = base.split("_", 2)[2]           # "<digit>.<ext>"
                d = int(os.path.splitext(part)[0])     # strip extension
                digits.append(d)
            except Exception:
                continue
        return (max(digits) + 1) if digits else 1

    def add_upload(self, owner_mobile: str, src_fullpath: str, *, date_key: Optional[str] = None) -> UploadRow:
        """Copy file into user's uploads dir with name <mobile>_<YYYYMMDD>_<digit><ext>."""
        mob = self._norm_mobile(owner_mobile)
        if not os.path.isfile(src_fullpath):
            raise FileNotFoundError(src_fullpath)

        udir = self._uploads_dir(mob)
        os.makedirs(udir, exist_ok=True)

        ext = os.path.splitext(src_fullpath)[1].lower()
        date_key = date_key or _date_key()
        digit = self._next_digit_for_day(mob, date_key)
        dest_name = f"{mob}_{date_key}_{digit}{ext}"
        dst = os.path.join(udir, dest_name)

        shutil.copy2(src_fullpath, dst)
        created = time.time()
        return UploadRow(path=dst, filename=dest_name, media_type=self._detect_media_type(ext), created_at=created)

    def list_uploads_for_mobile(self, owner_mobile: str) -> List[UploadRow]:
        mob = self._norm_mobile(owner_mobile)
        udir = self._uploads_dir(mob)
        out: List[UploadRow] = []
        if not os.path.isdir(udir):
            return out
        for base in sorted(os.listdir(udir)):
            p = os.path.join(udir, base)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(base)[1].lower()
            try:
                created = os.path.getmtime(p)
            except Exception:
                created = time.time()
            out.append(UploadRow(path=p, filename=base, media_type=self._detect_media_type(ext), created_at=created))
        return out

    def user_uploads_dir(self, mobile: str) -> str:
        return self._uploads_dir(mobile)

    # ---- legacy compatibility (no-op CSV) ----
    def export_uploads_csv(self) -> str:
        # Kept only so older UI hooks don't crash if called.
        return "CSV not used: uploads are per-user in users/<mobile>/uploads/"
        # local_store.py  (inside list_uploads_for_mobile)
    
    
    def list_uploads_for_mobile(self, owner_mobile: str) -> List[UploadRow]:
        mob = self._norm_mobile(owner_mobile)
        udir = self._uploads_dir(mob)
        out: List[UploadRow] = []
        if not os.path.isdir(udir):
            return out
        prefix = f"{mob}_"
        for base in sorted(os.listdir(udir)):
            if not base.startswith(prefix):      # <— enforce "mobile_" prefix
                continue
            p = os.path.join(udir, base)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(base)[1].lower()
            try:
                created = os.path.getmtime(p)
            except Exception:
                created = time.time()
            out.append(UploadRow(path=p, filename=base,
                                media_type=self._detect_media_type(ext),
                                created_at=created))
        return out
