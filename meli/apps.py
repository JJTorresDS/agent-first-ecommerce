from django.apps import AppConfig


class MeliConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "meli"
    verbose_name = "Mercado Libre"

    def ready(self):
        from notifications.registry import register

        from .alerts import presales_questions

        register(presales_questions)
