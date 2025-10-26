import os, json, re, secrets, hashlib, time, hmac, tempfile
from typing import Optional

MOBILE_RE = re.compile(r"^[0-9]{10}$")

def _now() -> int:
    return int(time.time())

def _atomic_write_json(path: str, data: dict) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd = None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d, text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

def _normalize_mobile(mobile: str) -> str:
    x = re.sub(r"\D", "", (mobile or "").strip())
    if not MOBILE_RE.fullmatch(x):
        raise ValueError("Enter a valid 10-digit mobile number.")
    return x

def _hash_pin(pin: str, *, salt: Optional[str] = None) -> tuple[str, str]:
    if not re.fullmatch(r"[0-9]{4,6}", pin or ""):
        raise ValueError("PIN must be 4–6 digits.")
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), 120_000)
    return salt, dk.hex()

class AuthStore:
    """
    Per-user auth layout (mobile is the ONLY primary key):

      base_dir/
        users/
          <mobile>/
            auth.json     # credentials ONLY (mobile, pin_salt, pin_hash, timestamps, throttle)
            profile.json  # user data (LocalStore handles this)
            uploads/      # media (LocalStore handles this)
        session.json      # current signed-in mobile

    Also migrates legacy auth/users.json (mobile map) if present.
    """
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)
        self.users_root = os.path.join(self.base_dir, "users")
        os.makedirs(self.users_root, exist_ok=True)
        self.session_path = os.path.join(self.base_dir, "session.json")

        # ---- optional migration from legacy map ----
        legacy_dir = os.path.join(base_dir, "auth")
        legacy_map = os.path.join(legacy_dir, "users.json")
        if os.path.exists(legacy_map):
            try:
                with open(legacy_map, "r", encoding="utf-8") as f:
                    db = json.load(f) or {}
                users_map = db.get("users_by_mobile") or {}
                if isinstance(users_map, dict):
                    for mob, rec in users_map.items():
                        mobn = re.sub(r"\D", "", (mob or ""))
                        if not MOBILE_RE.fullmatch(mobn):
                            continue
                        udir = os.path.join(self.users_root, mobn)
                        os.makedirs(udir, exist_ok=True)
                        payload = {
                            "mobile": mobn,
                            "pin_salt": rec.get("pin_salt", ""),
                            "pin_hash": rec.get("pin_hash", ""),
                            "created_at": rec.get("created_at", _now()),
                            "updated_at": rec.get("updated_at", _now()),
                        }
                        _atomic_write_json(os.path.join(udir, "auth.json"), payload)
                # Keep legacy file as-is; comment next line to preserve it.
                # os.rename(legacy_map, legacy_map + ".migrated")
            except Exception:
                pass

    # ---- paths ----
    def _user_dir(self, mobile: str) -> str:
        return os.path.join(self.users_root, _normalize_mobile(mobile))

    def _auth_path(self, mobile: str) -> str:
        return os.path.join(self._user_dir(mobile), "auth.json")

    # ---- public API ----
    def register(self, mobile: str, pin: str) -> dict:
        """Create/update user keyed ONLY by mobile."""
        mob = _normalize_mobile(mobile)
        udir = self._user_dir(mob)
        os.makedirs(udir, exist_ok=True)
        ap = self._auth_path(mob)

        now = _now()
        if os.path.exists(ap):
            try:
                with open(ap, "r", encoding="utf-8") as f:
                    u = json.load(f) or {}
            except Exception:
                u = {}
        else:
            u = {"mobile": mob, "created_at": now}

        salt, pin_hash = _hash_pin(pin)
        u.update({
            "mobile": mob,
            "pin_salt": salt,
            "pin_hash": pin_hash,
            "updated_at": now,
        })
        # Clear throttle info on reset
        u.pop("failed_attempts", None)
        u.pop("last_failed_at", None)

        _atomic_write_json(ap, u)
        return {"mobile": mob}

    def login(self, mobile: str, pin: str) -> dict:
        mob = _normalize_mobile(mobile)
        ap = self._auth_path(mob)
        if not os.path.exists(ap):
            raise ValueError("Account not found. Please register.")

        try:
            with open(ap, "r", encoding="utf-8") as f:
                u = json.load(f) or {}
        except Exception:
            raise ValueError("Corrupted account. Recreate the user.")

        # Throttle (5 failures -> 5 min cool-off)
        now = _now()
        fails = int(u.get("failed_attempts", 0))
        last = int(u.get("last_failed_at", 0))
        if fails >= 5 and now - last < 300:
            raise ValueError("Too many attempts. Try again in a few minutes.")

        salt = u.get("pin_salt", "")
        expect = u.get("pin_hash", "")
        if not (salt and expect):
            raise ValueError("Account not initialized properly.")

        _, got = _hash_pin(pin, salt=salt)
        if not hmac.compare_digest(got, expect):
            u["failed_attempts"] = fails + 1
            u["last_failed_at"] = now
            _atomic_write_json(ap, u)
            raise ValueError("Invalid PIN.")

        # Success -> clear throttling
        u.pop("failed_attempts", None)
        u.pop("last_failed_at", None)
        u["updated_at"] = now
        _atomic_write_json(ap, u)

        # Persist session as current mobile
        _atomic_write_json(self.session_path, {"mobile": mob, "login_at": now})
        return {"mobile": mob}

    def logout(self) -> None:
        try:
            if os.path.exists(self.session_path):
                os.remove(self.session_path)
        except Exception:
            pass

    def current_user(self) -> Optional[dict]:
        if not os.path.exists(self.session_path):
            return None
        try:
            with open(self.session_path, "r", encoding="utf-8") as f:
                sess = json.load(f) or {}
            mob = _normalize_mobile(sess.get("mobile", ""))
            # Return minimal user object (mobile only); main.py/LocalStore use this.
            return {"mobile": mob}
        except Exception:
            return None
    # --- add inside class AuthStore ---

    def user_exists(self, mobile: str) -> bool:
        try:
            return os.path.exists(self._auth_path(mobile))
        except Exception:
            return False

    def list_users(self) -> list[str]:
        out = []
        try:
            for name in os.listdir(self.users_root):
                if MOBILE_RE.fullmatch(name) and os.path.exists(os.path.join(self.users_root, name, "auth.json")):
                    out.append(name)
        except Exception:
            pass
        return sorted(out)

    def verify_pin(self, mobile: str, pin: str) -> bool:
        mob = _normalize_mobile(mobile)
        ap = self._auth_path(mob)
        if not os.path.exists(ap):
            return False
        with open(ap, "r", encoding="utf-8") as f:
            u = json.load(f) or {}
        salt, expect = u.get("pin_salt", ""), u.get("pin_hash", "")
        if not (salt and expect):
            return False
        _, got = _hash_pin(pin, salt=salt)
        return hmac.compare_digest(got, expect)

    def change_pin(self, mobile: str, old_pin: str, new_pin: str) -> None:
        if not self.verify_pin(mobile, old_pin):
            raise ValueError("Old PIN incorrect.")
        mob = _normalize_mobile(mobile)
        ap = self._auth_path(mob)
        with open(ap, "r", encoding="utf-8") as f:
            u = json.load(f) or {}
        salt, pin_hash = _hash_pin(new_pin)
        u.update({"pin_salt": salt, "pin_hash": pin_hash, "updated_at": _now()})
        _atomic_write_json(ap, u)

    def delete_user(self, mobile: str, *, archive: bool = True) -> bool:
        import shutil
        mob = _normalize_mobile(mobile)
        udir = self._user_dir(mob)
        if not os.path.isdir(udir):
            return False
        if archive:
            trash = os.path.join(self.base_dir, "trash")
            os.makedirs(trash, exist_ok=True)
            os.rename(udir, os.path.join(trash, f"{mob}_{_now()}"))
        else:
            shutil.rmtree(udir, ignore_errors=True)
        # clear session if it belonged to this user
        try:
            if os.path.exists(self.session_path):
                with open(self.session_path, "r", encoding="utf-8") as f:
                    sess = json.load(f) or {}
                if _normalize_mobile(sess.get("mobile", "")) == mob:
                    os.remove(self.session_path)
        except Exception:
            pass
        return True

    def set_current_user(self, mobile: str) -> dict:
        """Switch session to an existing user without re-entering PIN (e.g., quick account switch UI)."""
        mob = _normalize_mobile(mobile)
        if not self.user_exists(mob):
            raise ValueError("User not found.")
        _atomic_write_json(self.session_path, {"mobile": mob, "login_at": _now()})
        return {"mobile": mob}
