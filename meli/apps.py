from django.apps import AppConfig


class MeliConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "meli"
    verbose_name = "Mercado Libre"

    def ready(self):
        from inbox.channels import (
            register_list_action,
            register_opener,
            register_reconciler,
            register_sender,
        )
        from notifications.registry import register

        from .alerts import presales_questions
        from .projection import (
            questions_sync_action,
            reconcile_item_questions,
            refresh_item_questions,
        )
        from .replies import send_item_question

        register(presales_questions)
        register_sender("meli-itemquestions", send_item_question)
        register_opener(refresh_item_questions)
        register_reconciler(reconcile_item_questions)
        register_list_action(questions_sync_action)
