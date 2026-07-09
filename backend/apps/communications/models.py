from django.db import models
from apps.common.models import TimeStampedModel

class WhatsAppLog(TimeStampedModel):
    recipient_phone = models.CharField(max_length=50)
    message_body = models.TextField()
    status = models.CharField(max_length=50, default='Sent')
    response_metadata = models.JSONField(null=True, blank=True)

    def __str__(self):
        return f"WhatsApp to {self.recipient_phone} - Status: {self.status}"
