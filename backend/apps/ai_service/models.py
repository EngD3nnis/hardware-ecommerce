from django.db import models
from django.conf import settings
from apps.common.models import TimeStampedModel

class AIInteractionLog(TimeStampedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    session_id = models.CharField(max_length=100, blank=True)
    model_provider = models.CharField(max_length=50)
    model_name = models.CharField(max_length=100)
    tokens_input = models.IntegerField(null=True, blank=True)
    tokens_output = models.IntegerField(null=True, blank=True)
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    prompt_purpose = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return f"{self.model_provider} - {self.prompt_purpose} - Cost: {self.estimated_cost or 0.0}"
