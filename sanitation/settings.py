"""
街道环卫考核系统 —— Django 配置。

数据库默认指向本机用户态 PostgreSQL/PostGIS（可用环境变量覆盖，
docker-compose 部署时设置 DB_HOST=db DB_PORT=5432 即可）。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-key-change-in-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "django.contrib.postgres",
    "rest_framework",
    "rest_framework_gis",
    "django_filters",
    "drf_spectacular",
    "assessment",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sanitation.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "sanitation.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": os.environ.get("DB_NAME", "sanitation"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "/tmp/pgsock"),
        "PORT": os.environ.get("DB_PORT", "5433"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


def _first_existing(*candidates):
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


# GDAL/GEOS 动态库定位：优先环境变量，其次项目内 conda 环境（用户态安装场景），
# 最后回退到系统 ldconfig 搜索路径（Django 默认行为）。
_CONDA_LIB = Path(os.environ.get("CONDA_PREFIX", BASE_DIR / ".condaenv")) / "lib"
GDAL_LIBRARY_PATH = os.environ.get("GDAL_LIBRARY_PATH") or _first_existing(
    _CONDA_LIB / "libgdal.so", _CONDA_LIB / "libgdal.dylib"
)
GEOS_LIBRARY_PATH = os.environ.get("GEOS_LIBRARY_PATH") or _first_existing(
    _CONDA_LIB / "libgeos_c.so", _CONDA_LIB / "libgeos_c.dylib"
)

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "EXCEPTION_HANDLER": "assessment.exceptions.api_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "街道环卫考核 API",
    "DESCRIPTION": (
        "道路网格 / 保洁合同 / 问题事件 / 证据照片 / 处罚单元管理。\n\n"
        "核心规则：\n"
        "* 照片感知哈希(pHash)只用于生成**疑似重复候选**，是否同一问题由人工结合位置、时间判断；\n"
        "* 同一问题不同角度拍摄只计扣一次（照片挂接到已有事件）；\n"
        "* 已整改后同一位置复发 = 新事件、新处罚；\n"
        "* 扣分归属按**事件发生时**的合同责任区间，与录入时间无关；\n"
        "* 逾期升级基于可注入时钟；复核通过锁定处罚版本，更正只能追加新版本。\n"
        "* 证据封存包：对某笔扣分的全部证据（照片文件摘要/位置时间/候选判定/整改/当前处罚版本）\n"
        "  生成不可变清单；封存后的变化只能派生显式关联的补充/替代包；支持导出与离线校验。"
    ),
    "VERSION": "1.0.0",
    "ENUM_NAME_OVERRIDES": {
        "EventStatusEnum": "assessment.models.ProblemEvent.Status",
        "EventCategoryEnum": "assessment.models.ProblemEvent.Category",
        "CandidateStatusEnum": "assessment.models.DuplicateCandidate.Status",
        "PenaltyStatusEnum": "assessment.models.PenaltyUnit.Status",
        "PenaltyVersionKindEnum": "assessment.models.PenaltyVersion.Kind",
        "PackageStatusEnum": "assessment.models.EvidencePackage.Status",
        "PackageKindEnum": "assessment.models.EvidencePackage.Kind",
    },
}

# 业务参数
ASSESSMENT = {
    "PHASH_THRESHOLD": 5,        # pHash 汉明距离 <= 5 生成疑似重复候选
    "DEFAULT_SLA_HOURS": 24,     # 默认整改时限
    "ESCALATION_STEP_POINTS": "1",  # 每升一级加扣分值
    "MAX_ESCALATION_LEVEL": 5,
}
