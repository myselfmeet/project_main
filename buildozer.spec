[app]
# --- EDIT THESE 3 ---
title = MyApp
package.name = myapp
package.domain = org.example
# --------------------

source.dir = .
source.include_exts = py,kv,png,jpg,jpeg,webp,mp4,txt,csv,json
version = 0.1.0
orientation = portrait
fullscreen = 0
log_level = 2
android.archs = arm64-v8a, armeabi-v7a

# KivyMD (latest, Android-friendly), ffpyplayer for video, camera4kivy for camera,
# plus common deps your code uses.
requirements = python3==3.13.7,hostpython3==3.13.7,kivy==2.3.0,kivymd@git+https://github.com/kivymd/KivyMD.git,ffpyplayer,kivy_garden.camera4kivy,numpy,pillow,urllib3,certifi

# If you DON'T use NumPy on-device, you can remove "numpy" to shrink APK size.

# Ensure ffpyplayer/ffmpeg is bundled
android.allow_backup = 0

# Min/target SDKs (Android 7.0+ min; target per Play requirements)
android.minapi = 24
android.api = 35
android.ndk = 25b

# Permissions your app likely needs (camera, mic, media, network, optional location)
android.permissions = CAMERA, RECORD_AUDIO, INTERNET, WAKE_LOCK, ACCESS_COARSE_LOCATION, ACCESS_FINE_LOCATION, READ_MEDIA_IMAGES, READ_MEDIA_VIDEO, READ_MEDIA_AUDIO, POST_NOTIFICATIONS

# If you still need legacy external storage on older devices (pre-Android 10), you can add:
# android.permissions = READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE
# but prefer the new scoped storage (the READ_MEDIA_* above) for Android 13+.

# App icons / presplash (optional – set your files if you have them)
# icon.filename = resources/icon.png
# presplash.filename = resources/presplash.png

# Keep Python bytecode for faster load
# (disable to reduce size slightly)
# use this default behavior

# If you hit dependency resolver issues, uncomment next line to use the latest p4a:
# p4a.branch = master

# Speed up build by skipping source inclusion checks (optional)
# ignore_path = venv, .git, build, bin, __pycache__, .mypy_cache__, .pytest_cache__

# If you need to pass custom gradle args (rare), you can set:
# android.gradle_dependencies = 
# android.gradle_options = 

# If Video white-screen persists on some devices, force SDL2/ffpyplayer provider in code
# via os.environ["KIVY_VIDEO"] = "ffpyplayer" before importing kivy video modules (already in your code).

[buildozer]
log_level = 2
warn_on_root = 0

[python]
# Optional: include/glob extra assets
# android_add_assets = assets/*.ttf

# If you use OpenCV (cv2) on Android (heavy!), add opencv to requirements AND:
# android.permissions = CAMERA, RECORD_AUDIO, ... (already present)
# Note: opencv massively increases size; prefer camera4kivy if possible.

[android]
# For modern Play Store compliance
android.compile_sdk = 35
# If you need a custom Keystore:
# android.release_keystore = mykeystore.jks
# android.release_keystore_alias = myalias

# Enable View Binding/Jetifier if you include modern libs (not needed here)
# android.enable_androidx = 1
# android.enable_jetifier = 1

# Handle file associations (optional)
# android.manifest_intent_filters = 

# Foreground service if you record video long-running (optional)
# android.permissions = FOREGROUND_SERVICE

# Workaround rare packaging conflicts (keep empty unless you see "duplicate class" errors)
# android.packaging_options = 

# If using GPS/location updates in background, you'll also need proper foreground service setup in code.



