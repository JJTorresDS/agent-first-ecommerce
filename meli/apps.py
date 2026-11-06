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

        from inbox.models import Channel

        from .alerts import postsale_messages, presales_questions
        from .postsale import postsale_sync_action, refresh_postsale
        from .projection import (
            questions_sync_action,
            reconcile_item_questions,
            refresh_item_questions,
        )
        from .replies import send_item_question, send_post_sale

        register(presales_questions)
        register(postsale_messages)
        register_sender(Channel.MELI_ITEM_QUESTIONS, send_item_question)
        register_sender(
            Channel.MELI_POSTSALES,
            send_post_sale,
            max_length=350,
            stays_open=True,
        )
        register_opener(refresh_item_questions)
        register_opener(refresh_postsale)
        register_reconciler(reconcile_item_questions)
        register_list_action(questions_sync_action)
        register_list_action(postsale_sync_action)

        from django.db.models.signals import post_save

        from catalog.models import Combo, MasterProduct

        from .listings import link_new_combo, link_new_product

        post_save.connect(link_new_product, sender=MasterProduct, dispatch_uid="meli-link-new-product")
        post_save.connect(link_new_combo, sender=Combo, dispatch_uid="meli-link-new-combo")

        from .jobs import start_postsale_worker

        start_postsale_worker()
