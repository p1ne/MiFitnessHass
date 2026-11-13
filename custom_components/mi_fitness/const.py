"""Constants for Mi Fitness integration."""

DOMAIN = "mi_fitness"

CONF_USER_ID       = "user_id"
CONF_C_USER_ID     = "c_user_id"
CONF_SSECURITY     = "ssecurity"
CONF_SERVICE_TOKEN = "service_token"
CONF_REGION        = "region"
# Stable client fingerprint for the data-API User-Agent ("<hex32>-<hex32>")
CONF_API_DEVICE_SUFFIX = "api_device_suffix"

# Path B — auto login
CONF_USERNAME      = "username"
CONF_PASSWORD      = "password"
CONF_AUTH_METHOD   = "auth_method"
CONF_PASS_TOKEN    = "pass_token"    # passToken — used for silent serviceToken refresh
# Whether the password is kept in the entry (used only when passToken expires)
CONF_SAVE_PASSWORD = "save_password"
# Persisted login deviceId cookie (an_<32 hex>). Must stay STABLE across logins and
# silent refreshes so Xiaomi remembers the device after one trusted 2FA (trust=true).
CONF_LOGIN_DEVICE_ID = "login_device_id"

AUTH_METHOD_TOKENS   = "tokens"    # Path A: manual token entry
AUTH_METHOD_PASSWORD = "password"  # Path B: username + password auto-login

REGIONS = ["us", "de", "sg", "cn", "ru", "i2"]
DEFAULT_REGION = "de"

SCAN_INTERVAL_MINUTES = 15

# Known watermark (~2026-04-30). Xiaomi watermarks are global sequential IDs
# growing by roughly WATERMARK_PER_DAY. On first install the start point is
# extrapolated from this anchor (conservatively, see coordinator); if the
# estimate turns out to be too far ahead, the anchor itself is used.
DEFAULT_START_WATERMARK = 157_500_000_000_000
DEFAULT_START_WATERMARK_TS = 1_777_507_200   # 2026-04-30T00:00:00Z
WATERMARK_PER_DAY = 78_000_000_000

STORAGE_KEY = "mi_fitness_watermarks"
STORAGE_VERSION = 1

# Sensor keys in the API response
KEY_STEPS              = "steps"
KEY_HEART_RATE         = "heart_rate"
KEY_RESTING_HEART_RATE = "resting_heart_rate"
KEY_SLEEP              = "sleep"
KEY_CALORIES           = "calories"
KEY_WEIGHT             = "weight"
KEY_VITALITY           = "vitality"
KEY_VALID_STAND        = "valid_stand"
KEY_SPO2               = "spo2"
KEY_STRESS             = "stress"

# Device info
DEVICE_MANUFACTURER = "Xiaomi"
DEVICE_MODEL        = "Mi Fitness Account"
