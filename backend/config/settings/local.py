from .base import *

DEBUG = True

ALLOWED_HOSTS = ['*']

CORS_ALLOW_ALL_ORIGINS = True

# Simple Console email backend for testing
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
