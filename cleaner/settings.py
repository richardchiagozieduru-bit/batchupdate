"""
Django settings for cleaner project.
"""

from pathlib import Path
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured
import os

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


# Quick-start development settings - unsuitable for production
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "SECRET_KEY environment variable is not set. "
        "Add it to your .env file or set it in the environment."
    )

DEBUG = os.getenv('DEBUG', 'False') == 'True'

ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_q',
    'acctmgt',
    'update',
    'extraction',
    'unupdated',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'cleaner.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'update.context_processors.drop_folder_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'cleaner.wsgi.application'


# Database - MSSQL for Django App State & Migrations (Local/Server A)
DATABASES = {
    'default': {
        'ENGINE': 'mssql',
        'NAME': os.getenv('APP_DB_NAME', os.getenv('BATCHUPDATE_DB', os.getenv('BATCHUPDATE_NAME', 'BatchUpdate'))),
        'HOST': os.getenv('APP_DB_SERVER', os.getenv('APP_DB_HOST', os.getenv('BATCHUPDATE_SERVER', os.getenv('BATCHUPDATE_HOST', '')))),
        'USER': os.getenv('APP_DB_USER', ''),
        'PASSWORD': os.getenv('APP_DB_PASSWORD', ''),
        'PORT': os.getenv('APP_DB_PORT', ''),
        'OPTIONS': {
            'driver': os.getenv('APP_DB_DRIVER', os.getenv('BATCHUPDATE_DRIVER', 'ODBC Driver 17 for SQL Server')),
            'trusted_connection': os.getenv('APP_DB_TRUSTED_CONNECTION', 'no' if os.getenv('APP_DB_USER') else 'yes'),
        },
    }
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# Static files
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

# Media files (uploads)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'


# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Cache — in-memory for default app state, plus a dedicated cross-process file-based
# cache for async task progress (shared between web server & Q-cluster workers).
CACHE_DIR = BASE_DIR / 'media' / 'cache'
os.makedirs(CACHE_DIR, exist_ok=True)

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'dataclean-cache',
    },
    'progress': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': str(CACHE_DIR),
        'TIMEOUT': 3600,
        'OPTIONS': {
            'MAX_ENTRIES': 2000,
        }
    }
}
SUBSCRIBER_CACHE_TTL = 300   # seconds
SESSION_RETENTION_DAYS = 30  # days before uploaded files and sessions are purged
DROPPED_FILE_RETENTION_DAYS = 2  # days before dropped files in media/drop_folder are cleared


# Login settings
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'upload'
LOGOUT_REDIRECT_URL = 'login'

# Email settings
# Development: prints emails to the console (terminal running the dev server)
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = 'no-reply@firstcentral.com'
# Production: switch to SMTP and set the vars below (e.g. via .env):
# EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
# EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
# EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
# EMAIL_USE_TLS = True
# EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER')
# EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')


# Django-Q2 Configuration (uses Django ORM as broker - no Redis needed)
Q_CLUSTER = {
    'name': 'DataClean',
    'workers': 4,
    'timeout': 10800,     # 3 hours max per task (prevents killing long extractions with 1M+ rows)
    'retry': 11400,       # retry window = timeout + 600s — prevents thrashing if a task fails near timeout
    'max_attempts': 1,    # do not automatically retry failed file-processing tasks
    'queue_limit': 50,
    'orm': 'default',     # Uses Django's default database as broker
    'save_limit': 250,    # Keep last 250 task results
    'catch_up': False,
}


# Logging Configuration
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} [{levelname}] {name}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(LOG_DIR, 'dataclean.log'),
            'maxBytes': 5 * 1024 * 1024,  # 5MB per file
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
    },
    'loggers': {
        'update': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'extraction': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'unupdated': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django_q': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django': {
            'handlers': ['console', 'file'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

# BatchUpdate SQL Server settings (Target Upload DB — Bentley Server)
BATCHUPDATE_SERVER = os.getenv('BATCHUPDATE_SERVER', os.getenv('BATCHUPDATE_HOST', ''))
BATCHUPDATE_DB = os.getenv('BATCHUPDATE_DB', os.getenv('BATCHUPDATE_NAME', 'BatchUpdate'))
BATCHUPDATE_DRIVER = os.getenv('BATCHUPDATE_DRIVER', 'ODBC Driver 17 for SQL Server')
BATCHUPDATE_TRUSTED_CONNECTION = os.getenv('BATCHUPDATE_TRUSTED_CONNECTION', 'no')
BATCHUPDATE_USER = os.getenv('BATCHUPDATE_USER', os.getenv('BUREAU_DB_USERNAME', ''))
BATCHUPDATE_PASSWORD = os.getenv('BATCHUPDATE_PASSWORD', os.getenv('BUREAU_DB_PASSWORD', ''))

# SQL template settings
SQL_TEMPLATE_PATH = os.path.join(MEDIA_ROOT, 'sql_template', 'template.sql')
SQL_TEMPLATE_BASE_NAME = os.getenv('SQL_TEMPLATE_BASE_NAME', '446_13042026_gtb')
SQL_TEMPLATE_BASE_SUBID = os.getenv('SQL_TEMPLATE_BASE_SUBID', '446')

# Bureau SQL Server connection (read-only extraction — remote server)
BUREAU_DB_SERVER = os.getenv('BUREAU_DB_SERVER', '')
BUREAU_DB_NAME = os.getenv('BUREAU_DB_NAME', 'XDSBureauAdmin')
BUREAU_DB_DRIVER = os.getenv('BUREAU_DB_DRIVER', 'ODBC Driver 17 for SQL Server')
BUREAU_DB_TRUSTED_CONNECTION = os.getenv('BUREAU_DB_TRUSTED_CONNECTION', 'no')
BUREAU_DB_USERNAME = os.getenv('BUREAU_DB_USERNAME', '')
BUREAU_DB_PASSWORD = os.getenv('BUREAU_DB_PASSWORD', '')

# Extraction settings
EXTRACTION_MAX_ROWS_PER_CSV = 1_000_000
EXTRACTION_FETCH_BATCH_SIZE = 100_000
EXTRACTION_PARALLEL_WORKERS = 3
EXTRACTION_ZIP_RETENTION_DAYS = 7
GENERATED_SCRIPTS_DIR = os.path.join(MEDIA_ROOT, 'generated_scripts')

# Unupdated settings
UNUPDATED_RETENTION_DAYS = 7

