from django.db import models


class Synonym(models.Model):
    """Words that mean the same thing to customers, e.g. "tap" ~ "faucet", "GI" ~ "galvanised".

    A search for any term in a group also searches the others. Staff maintain
    these in admin, including Swahili and trade terms.
    """

    id = models.BigAutoField(primary_key=True)
    terms = models.CharField(max_length=255, unique=True, help_text="Comma-separated, e.g. tap, faucet, mfereji")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["terms"]

    def __str__(self):
        return self.terms

    @property
    def term_list(self) -> list[str]:
        return [t.strip().lower() for t in self.terms.split(",") if t.strip()]
