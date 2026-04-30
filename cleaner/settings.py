"""
Django settings for cleaner project.
"""

from pathlib import Path
from dotenv import load_dotenv
import os

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


# Quick-start development settings - unsuitable for production
SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-aur94vy4@nf07$0mq&b)imahjmqyggahdd!t*&g*4job1vj=0a')

DEBUG = os.getenv('DEBUG', 'True') == 'True'

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
            ],
        },
    },
]

WSGI_APPLICATION = 'cleaner.wsgi.application'


# Database - MSSQL for everything
DATABASES = {
    'default': {
        'ENGINE': 'mssql',
        'NAME': os.getenv('BATCHUPDATE_DB', 'BatchUpdate'),
        'HOST': os.getenv('BATCHUPDATE_SERVER', ''),
        'OPTIONS': {
            'driver': os.getenv('BATCHUPDATE_DRIVER', 'ODBC Driver 17 for SQL Server'),
            'trusted_connection': os.getenv('BATCHUPDATE_TRUSTED_CONNECTION', 'yes'),
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
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']

# Media files (uploads)
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'


# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


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
    'workers': 2,
    'timeout': 1800,  # 30 minutes max per task
    'retry': 1900,
    'queue_limit': 50,
    'orm': 'default',  # Uses Django's default database as broker
    'save_limit': 250,  # Keep last 250 task results
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

# BatchUpdate SQL Server settings
BATCHUPDATE_SERVER = os.getenv('BATCHUPDATE_SERVER', '')
BATCHUPDATE_DB = os.getenv('BATCHUPDATE_DB', 'BatchUpdate')
BATCHUPDATE_DRIVER = os.getenv('BATCHUPDATE_DRIVER', 'ODBC Driver 17 for SQL Server')
BATCHUPDATE_TRUSTED_CONNECTION = os.getenv('BATCHUPDATE_TRUSTED_CONNECTION', 'yes')

# SQL template settings
SQL_TEMPLATE_PATH = os.path.join(MEDIA_ROOT, 'sql_template', 'template.sql')
SQL_TEMPLATE_BASE_NAME = os.getenv('SQL_TEMPLATE_BASE_NAME', '446_13042026_gtb')
SQL_TEMPLATE_BASE_SUBID = os.getenv('SQL_TEMPLATE_BASE_SUBID', '446')
GENERATED_SCRIPTS_DIR = os.path.join(MEDIA_ROOT, 'generated_scripts')
