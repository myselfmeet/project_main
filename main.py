from __future__ import annotations

# =========================
# main.py (mobile-only backend)
# =========================
# - Uses MOBILE NUMBER as the ONLY identifier.
# - Works with updated auth_store.py (mobile primary key) and local_store.py (mobile-scoped uploads).
# - Preserves your original structure & camera logic.

import os
# Prefer ffpyplayer before any Kivy video imports.
os.environ.setdefault("KIVY_VIDEO", "ffpyplayer")

import sys
import time
import json
import re
import uuid
import glob
import shutil
import threading
import csv
from typing import Optional

from kivy.lang import Builder
from kivy.core.window import Window
from kivy.resources import resource_add_path
from kivy.logger import Logger
from kivy.clock import Clock
from kivy.utils import platform
from kivy.cache import Cache
from kivy.uix.image import AsyncImage
from kivy.uix.video import Video

from kivymd.app import MDApp
from kivymd.uix.dialog import MDDialog
from kivymd.uix.button import MDFlatButton
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.label import MDLabel
from kivymd.uix.card import MDCard
from kivymd.uix.menu import MDDropdownMenu  # <-- proper import here

# ---- camera4kivy import with safe fallback ----
try:
    from camera4kivy import Preview          # PyPI name (desktop)
except Exception:
    try:
        from kivy_garden.camera4kivy import Preview  # Garden name (Android/buildozer)
    except Exception:
        from kivy.uix.widget import Widget
        class Preview(Widget):
            """Fallback so KV loads even if camera4kivy is missing."""
            pass
# -----------------------------------------------

from local_store import LocalStore  # per-user profile + uploads
from auth_store import AuthStore    # local auth (mobile-only)

# optional pickers
try:
    from plyer import filechooser
except Exception:
    filechooser = None
try:
    from plyer import camera as plyer_camera
except Exception:
    plyer_camera = None

# OpenCV fallback (desktop video)
try:
    import cv2  # noqa: F401
    _HAS_CV2 = True
except Exception:
    _HAS_CV2 = False


def _fmt_bytes(n):
    try:
        for u in ["B", "KB", "MB", "GB", "TB"]:
            if n < 1024.0:
                return f"{n:.1f} {u}"
            n /= 1024.0
    except Exception:
        pass
    return "?"


def _fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return "?"


# ---------- crash logger ----------
import traceback
def install_crashlog(path: str):
    """Capture uncaught exceptions to a file and logcat."""
    def hook(exctype, value, tb):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                traceback.print_exception(exctype, value, tb, file=f)
        except Exception:
            pass
        traceback.print_exception(exctype, value, tb)
    sys.excepthook = hook


# Cache config (raise limits slightly for smoother gallery previews)
Cache.register('asyncimage', limit=64)
Cache.register('preview_image', limit=4)


class PhotoApp(MDApp):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Auth + data
        self.auth: Optional[AuthStore] = None
        self.store: Optional[LocalStore] = None
        self.profile_data: dict = {}  # ACTIVE user's profile only (per-mobile JSON)

        # Gallery paging
        self._gallery_loaded = False
        self._current_chunk_index = 0
        self._chunk_size = 8

        # Shutter/recording state
        self._press_evt = None
        self._is_pressing = False
        self._is_recording = False
        self._record_path: Optional[str] = None
        self.LONG_PRESS_MS = 300

        # Stopwatch
        self._rec_start_ts = 0.0
        self._timer_ev = None

        # Preview state: "image" or "video"
        self._preview_mode = "image"

        # OpenCV thread state
        self._cv_thread = None
        self._cv_stop_flag = False

        # Camera attrs
        self._cam_widget: Optional[Preview] = None
        self._cam_connected = False
        self._last_capture_path: Optional[str] = None

        # Theme dropdown menu handle
        self._theme_menu: Optional[MDDropdownMenu] = None

    # ---------- Helpers ----------
    def _as_text(self, v) -> str:
        """Coerce a KV widget or any object to a clean string."""
        try:
            if hasattr(v, "text"):
                return (v.text or "").strip()
            return (str(v) if v is not None else "").strip()
        except Exception:
            return ""

    # --- Theme prefs ---
    def _prefs_path(self):
        return os.path.join(self.user_data_dir, "ui_prefs.json")

    def _load_ui_prefs(self):
        try:
            with open(self._prefs_path(), "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _save_ui_prefs(self, prefs: dict):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self._prefs_path(), "w", encoding="utf-8") as f:
                json.dump(prefs, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def set_theme(self, mode: str):
        """mode: 'Light' or 'Dark' (case-insensitive)."""
        mode = (mode or "").strip().lower()
        self.theme_cls.theme_style = "Dark" if mode == "dark" else "Light"
        prefs = self._load_ui_prefs()
        prefs["theme_style"] = self.theme_cls.theme_style
        self._save_ui_prefs(prefs)
        self._notify(f"Theme: {self.theme_cls.theme_style}")

    def toggle_theme(self):
        self.set_theme("Dark" if self.theme_cls.theme_style == "Light" else "Light")

    def open_theme_menu(self, caller):
        """Open a small dropdown anchored to the theme icon in the top bar."""
        try:
            if self._theme_menu:
                self._theme_menu.dismiss()
        except Exception:
            pass

        items = [
            {"text": "Light",  "on_release": lambda: (self.set_theme("Light"),  self._theme_menu.dismiss())},
            {"text": "Dark",   "on_release": lambda: (self.set_theme("Dark"),   self._theme_menu.dismiss())},
            {"text": "Toggle", "on_release": lambda: (self.toggle_theme(),      self._theme_menu.dismiss())},
        ]
        self._theme_menu = MDDropdownMenu(caller=caller, items=items, width_mult=3)
        self._theme_menu.open()

    # main.py (add this method in PhotoApp)
    def refresh_uploads_for_active_user(self, *_):
        mobile = (self.profile_data.get("mobile") or "").strip()
        uploads = self.store.list_uploads_for_mobile(mobile) if mobile else []
        grid = getattr(self.root, "ids", {}).get("uploads_grid")
        if not grid:
            return
        grid.clear_widgets()
        self._current_chunk_index = 0
        self._all_uploads = [row for row in uploads if os.path.exists(row.path)]
        self._gallery_loaded = False
        self._load_next_chunk()

    def open_upload_detail(self, filepath: str):
        """Open a modal dialog with big preview + description + file info."""
        try:
            # read meta sidecar (added earlier when saving)
            meta = {}
            sidecar = filepath + ".json"
            if os.path.exists(sidecar):
                with open(sidecar, "r", encoding="utf-8") as f:
                    meta = json.load(f) or {}

            desc = (meta.get("description") or "").strip()
            mobile = (meta.get("mobile") or "").strip()

            st = os.stat(filepath)
            fname = os.path.basename(filepath)
            size_s = _fmt_bytes(st.st_size)
            mtime_s = _fmt_time(st.st_mtime)

            # content layout
            content = MDBoxLayout(orientation="vertical", spacing="8dp", padding=("8dp", "8dp", "8dp", "8dp"))
            # big preview
            img = AsyncImage(source=filepath, allow_stretch=True, keep_ratio=True)
            img.size_hint_y = 0.78
            content.add_widget(img)

            # description (if any)
            if desc:
                content.add_widget(MDLabel(text=desc, halign="center", theme_text_color="Secondary"))

            # footer info
            info = f"File: {fname}\nSize: {size_s}\nModified: {mtime_s}"
            if mobile:
                info += f"\nUser: {mobile}"
            footer = MDLabel(text=info, halign="center", theme_text_color="Secondary")
            content.add_widget(footer)

            # dialog
            self._detail_dialog = MDDialog(
                title="Preview",
                type="custom",
                content_cls=content,
                buttons=[MDFlatButton(text="Close", on_release=lambda *_: self._detail_dialog.dismiss())],
                auto_dismiss=True,
            )
            self._detail_dialog.open()
        except Exception as e:
            self._notify(f"Preview failed: {e}")

    # ---------- Validators (MOBILE ONLY) ----------
    def _is_valid_identifier(self, s: str):
        """
        Backward-compatible signature, but MOBILE-ONLY.
        Returns (ok, 'mobile', digits or error_msg)
        """
        s = self._as_text(s)
        digits = re.sub(r"\D", "", s)
        if len(digits) != 10:
            return False, "mobile", "Mobile must be 10 digits"
        return True, "mobile", digits

    def _is_valid_pin(self, pin: str) -> bool:
        return bool(re.fullmatch(r"\d{4,6}", self._as_text(pin)))

    # ---------- KV loading ----------
    def _load_kv_files(self):
        """
        Load auth.kv as root only if app.kv/main.kv are missing.
        If app.kv exists, preload auth.kv for widget rules then load app.kv as root
        to avoid double-root warnings.
        """
        app_dir = os.path.dirname(os.path.abspath(__file__))
        resource_add_path(app_dir)

        auth_kv = os.path.join(app_dir, "auth.kv")
        app_kv = os.path.join(app_dir, "app.kv")
        main_kv = os.path.join(app_dir, "main.kv")  # legacy name

        # If app.kv exists, preload auth rules, then load app as root
        if os.path.exists(app_kv):
            if os.path.exists(auth_kv):
                try:
                    Builder.load_file(auth_kv)
                    Logger.info("KV: loaded rules from auth.kv (preload)")
                except Exception as e:
                    Logger.warning(f"KV: failed loading auth.kv rules: {e}")
            try:
                root = Builder.load_file(app_kv)
                Logger.info("KV: root from app.kv")
                return root
            except Exception as e:
                Logger.warning(f"KV: app.kv failed as root: {e}")

        # If no app.kv but auth.kv exists, use it as root
        if os.path.exists(auth_kv):
            try:
                root = Builder.load_file(auth_kv)
                Logger.info("KV: root from auth.kv")
                return root
            except Exception as e:
                Logger.warning(f"KV: auth.kv failed as root: {e}")

        # Try legacy main.kv
        if os.path.exists(main_kv):
            try:
                root = Builder.load_file(main_kv)
                Logger.info("KV: root from main.kv")
                return root
            except Exception as e:
                Logger.warning(f"KV: main.kv failed as root: {e}")

        raise RuntimeError("No suitable KV file found. Ensure app.kv or auth.kv is present.")

    # ---------- Build ----------
    def build(self):
        # Install crash logger FIRST so KV parse/runtime errors are captured
        os.makedirs(self.user_data_dir, exist_ok=True)
        install_crashlog(os.path.join(self.user_data_dir, "last_crash.txt"))

        # Theme: default then restore saved choice
        self.theme_cls.theme_style = "Light"
        saved = self._load_ui_prefs()
        if isinstance(saved, dict) and saved.get("theme_style") in ("Light", "Dark"):
            self.theme_cls.theme_style = saved["theme_style"]

        self.title = "Photo App (Local Only)"
        if platform not in ('android', 'ios'):
            Window.minimum_width = max(480, int(getattr(Window, 'minimum_width', 0) or 0))
            Window.minimum_height = max(800, int(getattr(Window, 'minimum_height', 0) or 0))

        from kivy.config import Config
        Config.set('kivy', 'log_level', 'info')

        self.store = LocalStore(self.user_data_dir)
        self.auth = AuthStore(self.user_data_dir)

        root = self._load_kv_files()

        try:
            u = self.auth.current_user()
            sm = getattr(root, "ids", {}).get("screen_manager")
            if not sm:
                Logger.warning("No ScreenManager found in root.ids; check your KV.")
            if not u:
                if sm:
                    sm.current = "login"
            else:
                self._set_active_user(u)  # u contains {'mobile': 'XXXXXXXXXX'}
                if sm:
                    sm.current = "profile"
        except Exception as e:
            Logger.warning(f"Initial screen selection failed: {e}")
            try:
                root.ids.screen_manager.current = "login"
            except Exception:
                pass

        Clock.schedule_once(lambda *_: self._delayed_gallery_init(), 0.8)
        return root

    def _delayed_gallery_init(self):
        if not self._gallery_loaded:
            self._bootstrap_gallery_for_mobile()

    # ---------- Drawer / nav ----------
    def open_nav_drawer(self):
        nav = getattr(self.root, "ids", {}).get("nav_drawer")
        if nav:
            nav.set_state("open")

    def close_nav_drawer(self):
        nav = getattr(self.root, "ids", {}).get("nav_drawer")
        if nav:
            nav.set_state("close")

    def change_screen(self, name: str):
        ids = getattr(self.root, "ids", {})
        sm = ids.get("screen_manager")
        if self.auth and not self.auth.current_user() and name not in ("login", "register"):
            self._notify("Please sign in")
            name = "login"
        try:
            if sm and getattr(sm, "current", None) == "camera" and name != "camera":
                self.stop_camera()
        except Exception:
            pass

        # After setting sm.current, handle screen-specific bootstraps
        if sm and name in {s.name for s in sm.screens}:
            sm.current = name
            if name == "camera":
                Clock.schedule_once(lambda dt: self.start_camera(), 0.1)
                self._cancel_press_timer()
                self._is_pressing = False
                self._update_video_button_text()
            elif name == "uploads":
                Clock.schedule_once(self.refresh_uploads_for_active_user, 0.05)

        self.close_nav_drawer()

    # ---------- Auth (MOBILE ONLY) ----------
    def auth_register(self, identifier, pin, pin2="", name=""):
        # identifier is expected to be the mobile field; keeping name for KV compatibility
        ok, _id_type, mobile_or_err = self._is_valid_identifier(identifier)
        if not ok:
            self._notify(mobile_or_err); return
        if not self._is_valid_pin(pin):
            self._notify("PIN must be 4-6 digits"); return
        if self._as_text(pin2) and self._as_text(pin) != self._as_text(pin2):
            self._notify("PINs do not match"); return

        try:
            # New auth_store API: register(mobile, pin)
            user = self.auth.register(mobile_or_err, self._as_text(pin))
            self._set_active_user(user)
            name_text = self._as_text(name)
            if name_text:
                self.profile_data["name"] = name_text
            self.profile_data["mobile"] = mobile_or_err
            self._save_user_profile()

            # Optional: import legacy global profile.json on first save
            try:
                legacy = os.path.join(self.user_data_dir, 'profile.json')
                if os.path.exists(legacy) and all(not (self.profile_data.get(k) or "").strip()
                                                  for k in ('name', 'email', 'mobile')):
                    with open(legacy, 'r', encoding='utf-8') as f:
                        legacy_data = json.load(f)
                    for k in ('name', 'mobile', 'email', 'state', 'district', 'address'):
                        v = legacy_data.get(k)
                        if isinstance(v, str) and v.strip():
                            self.profile_data[k] = v.strip()
                    self._save_user_profile()
            except Exception:
                pass

            self._bind_profile_to_ui()
            self._notify("Account created. You can sign in now.")
            self.change_screen("login")
        except Exception as e:
            self._notify(str(e))

    def auth_login(self, identifier, pin):
        ok, _id_type, mobile_or_err = self._is_valid_identifier(identifier)
        if not ok:
            self._notify(mobile_or_err); return
        if not self._is_valid_pin(pin):
            self._notify("PIN must be 4-6 digits"); return

        try:
            # New auth_store API: login(mobile, pin)
            user = self.auth.login(mobile_or_err, self._as_text(pin))
            self._set_active_user(user)
            self._write_login_history(user, mobile_or_err)
            self._notify(f"Welcome {user.get('mobile')}")
            self.change_screen("profile")
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ("not found", "no account", "unknown")):
                self._notify("No account found. Create one or recheck your mobile.")
            elif "too many" in msg:
                self._notify("Too many attempts. Try again in a few minutes.")
            elif any(k in msg for k in ("wrong", "pin", "password", "mismatch", "invalid")):
                self._notify("Wrong PIN. Try again.")
            else:
                self._notify(str(e))

    def auth_logout(self):
        try:
            self.auth.logout()
            self.profile_data = {}
            self._bind_profile_to_ui()
        finally:
            self._notify("Logged out")
            self.change_screen("login")

    # ---------- Shutter (tap/hold) ----------
    def on_shutter_press(self):
        self._is_pressing = True
        delay = self.LONG_PRESS_MS / 1000.0
        self._press_evt = Clock.schedule_once(lambda *_: self._maybe_start_recording(), delay)

    def on_shutter_release(self):
        was_recording = self._is_recording
        self._is_pressing = False
        self._cancel_press_timer()
        if was_recording:
            self._stop_video_recording(); return
        self.capture_frame()

    def _cancel_press_timer(self):
        if self._press_evt is not None:
            try:
                self._press_evt.cancel()
            except Exception:
                pass
            self._press_evt = None

    def _maybe_start_recording(self):
        self._press_evt = None
        if not self._is_pressing:
            return
        self._start_video_recording()

    # ---------- Video button ----------
    def toggle_video_recording(self):
        if self._is_recording:
            self._stop_video_recording()
        else:
            self._start_video_recording()

    def _update_video_button_text(self):
        btn = getattr(self.root, "ids", {}).get("video_btn")
        if not btn:
            return
        btn.text = "Stop Video" if self._is_recording else "Start Video"

    # ---------- Pick / Android camera ----------
    def pick_image(self):
        def _do_pick(dt):
            path = None
            try:
                if filechooser:
                    paths = filechooser.open_file(
                        title="Choose an image",
                        filters=[("Images", "*.png;*.jpg;*.jpeg")],
                        multiple=False
                    )
                    if paths:
                        path = paths[0]
            except Exception as e:
                self._notify(f"Picker error: {e}")
            if not path:
                self._notify("No file chosen"); return
            self._show_image_preview(path)
        Clock.schedule_once(_do_pick, 0.1)

    # ---------- Save to gallery (per-user) ----------
    def save_current_to_gallery(self):
        if not (self._last_capture_path and os.path.exists(self._last_capture_path)):
            self._notify("No media to save"); return
        mobile = (self.profile_data.get("mobile") or "").strip()
        if not re.fullmatch(r"[0-9]{10}", mobile):
            self._notify("Your profile mobile is missing/invalid"); return

        ids = getattr(self.root, "ids", {})
        desc = ""
        try:
            desc = (ids.get("desc_input").text or "").strip()
        except Exception:
            pass

        def _do_save(dt):
            try:
                # LocalStore handles naming <mobile>_<YYYYMMDD>_<digit>.<ext>
                row = self.store.add_upload(mobile, self._last_capture_path)

                # write sidecar metadata next to the saved file
                sidecar = row.path + ".json"
                meta = {
                    "filename": row.filename,
                    "description": desc,
                    "created_at": time.time(),
                    "mobile": mobile,
                    "media_type": row.media_type,
                }
                try:
                    with open(sidecar, "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    self._notify(f"Meta save warn: {e}")

                # optional: clear the text field for next time
                try:
                    if ids.get("desc_input"):
                        ids["desc_input"].text = ""
                except Exception:
                    pass

                Clock.schedule_once(lambda dt2: self._add_upload_tile(row.path), 0)
                self.change_screen("uploads")
                self._notify(f"Saved: {row.filename}")
            except Exception as e:
                self._notify(f"Save failed: {e}")
        Clock.schedule_once(_do_save, 0.1)

    # ---------- Gallery (mobile-scoped) ----------
    def _bootstrap_gallery_for_mobile(self):
        if self._gallery_loaded:
            return
        mobile = (self.profile_data.get("mobile") or "").strip()
        uploads = self.store.list_uploads_for_mobile(mobile) if mobile else []
        grid = getattr(self.root, "ids", {}).get("uploads_grid")
        if not grid:
            self._gallery_loaded = True
            return
        grid.clear_widgets()
        self._current_chunk_index = 0
        self._all_uploads = [row for row in uploads if os.path.exists(row.path)]
        self._load_next_chunk()

    def _load_next_chunk(self):
        if not hasattr(self, '_all_uploads'):
            return
        grid = getattr(self.root, "ids", {}).get("uploads_grid")
        if not grid:
            return
        start_idx = self._current_chunk_index
        end_idx = min(start_idx + self._chunk_size, len(self._all_uploads))
        for i in range(start_idx, end_idx):
            self._add_upload_tile(self._all_uploads[i].path)
        self._current_chunk_index = end_idx
        if end_idx < len(self._all_uploads):
            Clock.schedule_once(lambda dt: self._load_next_chunk(), 0.05)
        else:
            self._gallery_loaded = True

    def _add_upload_tile(self, filepath):
        from kivymd.uix.card import MDCard
        from kivymd.uix.label import MDLabel
        from kivymd.uix.boxlayout import MDBoxLayout

        grid = getattr(self.root, "ids", {}).get("uploads_grid")
        if not grid:
            return

        ext = os.path.splitext(filepath)[1].lower()
        is_video = ext in (".mp4", ".mov", ".mkv", ".3gp", ".webm", ".avi")

        # read sidecar description if available
        desc_text = ""
        try:
            sidecar = filepath + ".json"
            if os.path.exists(sidecar):
                with open(sidecar, "r", encoding="utf-8") as f:
                    meta = json.load(f) or {}
                desc_text = (meta.get("description") or "").strip()
        except Exception:
            pass

        card = MDCard(orientation="vertical", radius=[8], elevation=1,
                      size_hint_y=None, height="180dp", padding="4dp")

        inner = MDBoxLayout(orientation="vertical", spacing="4dp")
        card.add_widget(inner)

        if is_video:
            label = MDLabel(text=f"▶ {os.path.basename(filepath)}",
                            halign="center", theme_text_color="Secondary")
            inner.add_widget(label)
        else:
            img = AsyncImage(source=filepath, allow_stretch=True, keep_ratio=True,
                             mipmap=True, nocache=False, anim_delay=0.1)
            inner.add_widget(img)

        if desc_text:
            subtitle = MDLabel(text=desc_text, halign="center",
                               theme_text_color="Secondary")
            inner.add_widget(subtitle)

        grid.add_widget(card)

    def open_uploads_folder(self):
        # open this user's uploads folder
        mobile = (self.profile_data.get("mobile") or "").strip()
        path = self.store.user_uploads_dir(mobile) if mobile else self.user_data_dir
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore
            elif sys.platform == "darwin":
                import subprocess; subprocess.call(["open", path])
            else:
                import subprocess; subprocess.call(["xdg-open", path])
        except Exception as e:
            self._notify(f"Open folder failed: {e}")

    def show_csv_path(self):
        # kept for compatibility – now shows the uploads directory instead of a CSV
        mobile = (self.profile_data.get("mobile") or "").strip()
        self._notify(f"Uploads dir: {self.store.user_uploads_dir(mobile) if mobile else '(no user)'}")

    # ---------- Webcam preview (camera4kivy) ----------
    def _ensure_cam_widget(self):
        holder = getattr(self.root, "ids", {}).get("cam_holder")
        if not holder:
            return None
        if self._cam_widget is not None:
            return self._cam_widget
        try:
            w = Preview()
            holder.clear_widgets()
            holder.add_widget(w)
            self._cam_widget = w
            return w
        except Exception as e:
            self._notify(f"Camera widget create failed: {e}")
            self._cam_widget = None
            return None

    def _get_cam_widget(self):
        if hasattr(self, '_cam_widget') and self._cam_widget is not None:
            return self._cam_widget
        return None

    def start_camera(self):
        prev = self._ensure_cam_widget()
        if prev is None:
            self._notify("Camera not available"); return
        if not hasattr(prev, "connect_camera"):
            self._notify("camera4kivy not available"); return
        if self._cam_connected:
            return
        try:
            camera_config = {
                'index': 0,
                'enable_analyze': False,
                'enable_video': True,
                'resolution': (640, 480),
            }
            prev.connect_camera(**camera_config)
            self._cam_connected = True
            self._notify("Camera started")
        except Exception as e:
            self._notify(f"Start camera failed: {e}")
            self._cam_connected = False

    def stop_camera(self):
        if not hasattr(self, '_cam_connected'):
            return
        prev = self._get_cam_widget()
        if prev is None:
            self._cam_connected = False
            return
        try:
            if hasattr(prev, "disconnect_camera"):
                prev.disconnect_camera()
        except Exception as e:
            Logger.warning(f"Stop camera failed: {e}")
        finally:
            self._cam_connected = False

    # ---------- Photo capture ----------
    def capture_frame(self, *_):
        prev = self._get_cam_widget()
        if prev is None:
            self._notify("Camera not ready"); return
        temp_dir = os.path.join(self.user_data_dir, "temp_captures")
        os.makedirs(temp_dir, exist_ok=True)
        self._cleanup_temp_files(temp_dir)
        out = os.path.join(temp_dir, f"capture_{int(time.time())}.png")
        def _do_capture(dt):
            try:
                prev.export_to_png(out)
                Clock.schedule_once(lambda dt2: self._verify_capture(out), 0.1)
            except Exception as e:
                self._notify(f"Capture error: {e}")
        Clock.schedule_once(_do_capture, 0)

    def _verify_capture(self, filepath):
        if os.path.exists(filepath) and os.path.getsize(filepath) > 2000:
            self._show_image_preview(filepath)
        else:
            self._notify("Capture failed - please try again")
            if os.path.exists(filepath):
                os.remove(filepath)

    # ---------- Video recording ----------
    def _start_video_recording(self):
        prev = self._get_cam_widget()
        if prev and hasattr(prev, "start_recording"):
            vid_dir = os.path.join(self.user_data_dir, "temp_captures")
            os.makedirs(vid_dir, exist_ok=True)
            ts = int(time.time())
            self._record_path = os.path.join(vid_dir, f"rec_{ts}.mp4")
            try:
                try:
                    prev.start_recording(self._record_path)
                except TypeError:
                    prev.start_recording(filename=self._record_path)
                self._is_recording = True
                self._start_stopwatch()
                self._notify("Recording…")
            except Exception as e:
                self._notify(f"Video start failed: {e}")
                self._is_recording = False
                self._record_path = None
            self._update_video_button_text()
            return

        if not _HAS_CV2:
            self._notify("OpenCV not available; video recording unsupported on this backend.")
            return

        try:
            self.stop_camera()  # free the device
            vid_dir = os.path.join(self.user_data_dir, "temp_captures")
            os.makedirs(vid_dir, exist_ok=True)
            ts = int(time.time())
            self._record_path = os.path.join(vid_dir, f"rec_{ts}.mp4")

            self._cv_stop_flag = False
            self._is_recording = True
            self._start_stopwatch()
            self._cv_thread = threading.Thread(target=self._cv_record_worker, daemon=True)
            self._cv_thread.start()
            self._notify("Recording (OpenCV)…")
            self._update_video_button_text()
        except Exception as e:
            self._notify(f"OpenCV video start failed: {e}")
            self._is_recording = False
            self._record_path = None
            self._update_video_button_text()

    def _cv_record_worker(self):
        import cv2
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            self._notify("OpenCV could not open camera")
            self._cv_stop_flag = True

        fps = 20.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 640)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(self._record_path, fourcc, fps, (w, h))

        t_last = time.time()
        frame_interval = 1.0 / fps

        while not self._cv_stop_flag:
            now = time.time()
            if now - t_last < frame_interval:
                time.sleep(0.002); continue
            t_last = now
            ok, frame = cap.read()
            if not ok:
                break
            writer.write(frame)

        try:
            writer.release()
        except Exception:
            pass
        try:
            cap.release()
        except Exception:
            pass

    def _stop_video_recording(self):
        prev = self._get_cam_widget()
        if self._is_recording and prev and hasattr(prev, "stop_recording") and self._record_path:
            try:
                prev.stop_recording()
            except Exception as e:
                self._notify(f"Video stop error: {e}")
            self._finish_recording_common()
            return

        if self._is_recording and self._cv_thread is not None:
            self._cv_stop_flag = True
            self._cv_thread.join(timeout=3.0)
            self._cv_thread = None
            self._finish_recording_common()
            Clock.schedule_once(lambda dt: self.start_camera(), 0.2)

    def _finish_recording_common(self):
        self._is_recording = False
        self._stop_stopwatch()
        path = self._record_path
        self._record_path = None
        self._update_video_button_text()
        if not (path and os.path.exists(path) and os.path.getsize(path) > 0):
            self._notify("Recording produced no file"); return
        self._show_video_preview(path)

    # ---------- Stopwatch ----------
    def _start_stopwatch(self):
        self._rec_start_ts = time.time()
        self._update_timer_label(0)
        if self._timer_ev is not None:
            try:
                self._timer_ev.cancel()
            except Exception:
                pass
        self._timer_ev = Clock.schedule_interval(self._tick_stopwatch, 0.2)

    def _stop_stopwatch(self):
        if self._timer_ev is not None:
            try:
                self._timer_ev.cancel()
            except Exception:
                pass
        self._timer_ev = None
        self._update_timer_label(0)

    def _tick_stopwatch(self, dt):
        elapsed = int(time.time() - self._rec_start_ts)
        self._update_timer_label(elapsed)

    def _update_timer_label(self, secs):
        m = secs // 60
        s = secs % 60
        lbl = getattr(self.root, "ids", {}).get("timer_lbl")
        if lbl:
            lbl.text = f"{m:02d}:{s:02d}"

    # ---------- Preview helpers ----------
    def _replace_preview_widget(self, new_widget):
        container = getattr(self.root, "ids", {}).get("preview_container")
        if not container:
            self._notify("Preview container not found"); return
        container.clear_widgets()
        container.add_widget(new_widget)

    def _show_image_preview(self, path):
        self._last_capture_path = path
        self._preview_mode = "image"
        img = getattr(self.root, "ids", {}).get("preview_image")
        if img is None:
            img = AsyncImage(source=path, mipmap=True, nocache=False, allow_stretch=True, keep_ratio=True)
            self._replace_preview_widget(img)
        else:
            img.source = path
        self.change_screen("preview")

    def _show_video_preview(self, video_path):
        if not os.path.exists(video_path):
            self._notify("Video file not found"); return
        file_size = os.path.getsize(video_path)
        if file_size == 0:
            self._notify("Video file is empty"); return

        self._last_capture_path = video_path
        self._preview_mode = "video"

        player = Video(
            source=video_path,
            state='play',
            options={'eos': 'loop'},
            allow_stretch=True,
            keep_ratio=True
        )
        player.bind(loaded=self._on_video_loaded)
        player.bind(on_error=self._on_video_error)
        player.bind(on_eos=self._on_video_eos)

        self._replace_preview_widget(player)
        self.change_screen("preview")

    def _on_video_loaded(self, instance, value):
        if value:
            self._notify("Video loaded")
            instance.state = 'play'
        else:
            self._notify("Video failed to load")
            self._show_video_fallback(instance.source)

    def _on_video_error(self, instance, error):
        self._notify(f"Video error: {error}")
        self._show_video_fallback(instance.source)

    def _on_video_eos(self, instance, *args):
        instance.state = 'stop'
        instance.position = 0
        instance.state = 'play'

    def _show_video_fallback(self, video_path):
        from kivymd.uix.card import MDCard
        from kivymd.uix.boxlayout import MDBoxLayout
        from kivymd.uix.label import MDLabel
        from kivymd.uix.button import MDRaisedButton
        try:
            from kivymd.uix.label import MDIcon
        except Exception:
            MDIcon = None

        container = getattr(self.root, "ids", {}).get("preview_container")
        if not container:
            return

        container.clear_widgets()

        card = MDCard(
            orientation="vertical",
            padding="20dp",
            spacing="10dp",
            size_hint=(0.8, 0.6),
            pos_hint={"center_x": 0.5, "center_y": 0.5}
        )

        layout = MDBoxLayout(orientation="vertical", spacing="10dp")

        if MDIcon:
            icon = MDIcon(
                icon="video",
                font_size="64sp",
                halign="center",
                theme_text_color="Primary"
            )
            icon.size_hint_y = None
            icon.height = "80dp"
            layout.add_widget(icon)

        filename = os.path.basename(video_path)
        file_size = os.path.getsize(video_path)
        size_mb = file_size / (1024 * 1024)

        info_label = MDLabel(
            text=f"Video: {filename}\nSize: {size_mb:.1f} MB",
            halign="center",
            theme_text_color="Secondary"
        )
        layout.add_widget(info_label)

        play_btn = MDRaisedButton(
            text="Try External Player",
            on_release=lambda x: self._play_video_external(video_path)
        )
        layout.add_widget(play_btn)

        card.add_widget(layout)
        container.add_widget(card)

    def _play_video_external(self, video_path):
        try:
            if platform == 'android':
                from android import mActivity
                from android.content import Intent
                from android.net import Uri
                intent = Intent(Intent.ACTION_VIEW)
                uri = Uri.parse("file://" + video_path)
                intent.setDataAndType(uri, "video/*")
                mActivity.startActivity(intent)
            else:
                import subprocess
                if sys.platform == "win32":
                    os.startfile(video_path)  # type: ignore
                elif sys.platform == "darwin":
                    subprocess.call(["open", video_path])
                else:
                    subprocess.call(["xdg-open", video_path])
        except Exception as e:
            self._notify(f"Cannot open video: {e}")

    def on_pause(self):
        try:
            self.stop_camera()
            if hasattr(self, '_is_recording') and self._is_recording:
                self._stop_video_recording()
        except Exception as e:
            Logger.error(f"App pause error: {e}")
        return True

    def on_resume(self):
        try:
            sm = getattr(self.root, "ids", {}).get("screen_manager")
            if sm and sm.current == "camera":
                Clock.schedule_once(lambda dt: self.start_camera(), 0.5)
        except Exception as e:
            Logger.error(f"App resume error: {e}")

    def on_stop(self):
        # stop playback if any
        try:
            container = getattr(self.root, "ids", {}).get("preview_container")
            if container and container.children:
                for child in container.children:
                    if hasattr(child, 'state'):
                        child.state = 'stop'
        except Exception as e:
            Logger.warning(f"Video stop error: {e}")

        if hasattr(self, '_is_recording') and self._is_recording:
            try:
                self._stop_video_recording()
            except Exception as e:
                Logger.warning(f"Recording stop error: {e}")

        try:
            self.stop_camera()
        except Exception as e:
            Logger.warning(f"Camera stop error: {e}")

        # Avoid misuse of Cache.remove(category) — if needed, let GC handle it.
        try:
            Cache.print_usage()
        except Exception:
            pass

    # ---------- Temp files housekeeping ----------
    def _cleanup_temp_files(self, directory, keep_count=3):
        try:
            patterns = [os.path.join(directory, "capture_*.png"),
                        os.path.join(directory, "capture_*.jpg"),
                        os.path.join(directory, "rec_*.mp4")]
            files = []
            for pat in patterns:
                files.extend(glob.glob(pat))
            files.sort(key=os.path.getmtime, reverse=True)
            for old_file in files[keep_count:]:
                try:
                    os.remove(old_file); Logger.info(f"Cleaned: {old_file}")
                except Exception as e:
                    Logger.warning(f"Cleanup failed {old_file}: {e}")
        except Exception as e:
            Logger.warning(f"Temp cleanup error: {e}")

    # ---------- Profile (per-mobile storage via LocalStore) ----------
    def _collect_profile_from_ui(self):
        ids = getattr(self.root, "ids", {})
        tf = lambda k: (ids.get(k).text.strip() if ids.get(k) and hasattr(ids.get(k), 'text') else "")
        return {
            "name": tf("tf_name"),
            "mobile": tf("tf_mobile"),
            "email": tf("tf_email"),
            "state": tf("tf_state"),
            "district": tf("tf_district"),
            "address": tf("tf_address"),
        }

    def _validate_profile(self, p: dict):
        if not p["name"]:
            return False, "Name is required"
        if p["mobile"] and not re.fullmatch(r"[0-9]{10}", p["mobile"]):
            return False, "Mobile must be 10 digits"
        if p["email"] and not re.fullmatch(r"[^@]+@[^@]+\.[^@]+", p["email"]):
            return False, "Email format invalid"
        return True, ""

    def save_profile(self):
        p = self._collect_profile_from_ui()
        ok, msg = self._validate_profile(p)
        if not ok:
            self._notify(msg); return
        def _do_save(dt):
            try:
                self.profile_data.update(p)
                mobile = (self.profile_data.get("mobile") or "").strip()
                if re.fullmatch(r"[0-9]{10}", mobile):
                    self.store.save_profile(mobile, self.profile_data)
                self._bind_profile_to_ui()
                self._notify("Profile saved")
            except Exception as e:
                self._notify(f"Profile save failed: {e}")
        Clock.schedule_once(_do_save, 0.1)

    def _save_user_profile(self):
        """Persist current profile into users/<mobile>/profile.json."""
        mobile = (self.profile_data.get("mobile") or "").strip()
        if not re.fullmatch(r"[0-9]{10}", mobile):
            self._notify("Profile save skipped: invalid/missing mobile")
            return
        try:
            self.store.save_profile(mobile, self.profile_data)
            # optional UI refresh
            self._bind_profile_to_ui()
        except Exception as e:
            self._notify(f"Profile save failed: {e}")

    def reset_profile_view(self):
        p = self.profile_data; ids = getattr(self.root, "ids", {})
        if ids.get("tf_name"):     ids["tf_name"].text = p.get("name","")
        if ids.get("tf_mobile"):   ids["tf_mobile"].text = p.get("mobile","")
        if ids.get("tf_email"):    ids["tf_email"].text = p.get("email","")
        if ids.get("tf_state"):    ids["tf_state"].text = p.get("state","")
        if ids.get("tf_district"): ids["tf_district"].text = p.get("district","")
        if ids.get("tf_address"):  ids["tf_address"].text = p.get("address","")
        self._notify("Form reset")

    def _hash_text(self, text: str) -> str:
        try:
            import hashlib
            return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]
        except Exception:
            return "masked"

    def _write_login_history(self, user: dict, mobile_value: str):
        auth_dir = os.path.join(self.user_data_dir, "auth")
        os.makedirs(auth_dir, exist_ok=True)
        path = os.path.join(auth_dir, "login_history.csv")
        is_new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["ts_iso", "mobile_masked"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), self._hash_text(mobile_value)])

    def _bind_profile_to_ui(self):
        try:
            ids = getattr(self.root, "ids", {})
            p = self.profile_data
            if ids.get("tf_name"):     ids["tf_name"].text = p.get("name", "")
            if ids.get("tf_mobile"):   ids["tf_mobile"].text = p.get("mobile", "")
            if ids.get("tf_email"):    ids["tf_email"].text = p.get("email", "")
            if ids.get("tf_state"):    ids["tf_state"].text = p.get("state", "")
            if ids.get("tf_district"): ids["tf_district"].text = p.get("district", "")
            if ids.get("tf_address"):  ids["tf_address"].text = p.get("address", "")
            if ids.get("lbl_name"):    ids["lbl_name"].text = p.get("name", "Your Name") or "Your Name"
            if ids.get("lbl_mobile"):  ids["lbl_mobile"].text = p.get("mobile", "Add mobile") or "Add mobile"
        except Exception:
            pass

    
    def _set_active_user(self, user: dict | None):
        if not user:
            return
        mobile = (user.get("mobile") or "").strip()
        if not re.fullmatch(r"[0-9]{10}", mobile):
            return
        # Load/save profile.json INSIDE this user's folder via LocalStore
        self.profile_data = self.store.load_profile(mobile)
        self._bind_profile_to_ui()

    # ---------- Utils ----------
    def _notify(self, msg: str):
        Logger.info(f"PhotoApp: {msg}")
        try:
            if platform not in ('android', 'ios') and hasattr(Window, "set_title"):
                if len(msg) < 60:
                    Window.set_title(f"Photo App — {msg}")
        except Exception:
            pass


if __name__ == "__main__":
    PhotoApp().run()
