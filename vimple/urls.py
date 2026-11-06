from django.contrib import admin
from django.urls import include, path

from accounts.views import (
    access,
    home,
    impersonate,
    impersonate_exit,
    members,
    organization_delete,
    organization_settings,
    organization_setup,
    organization_switch,
    profile,
    profile_menu,
    profile_unread,
    set_app_access,
)
from inbox.views import inbox, inbox_reply
from meli.draft_views import (
    meli_combo_actions,
    meli_draft,
    meli_draft_ai,
    meli_draft_copy,
    meli_draft_from_combo,
    meli_draft_from_product,
    meli_drafts,
    meli_product_actions,
)
from meli.category_views import meli_categories
from meli.import_views import catalog_import
from meli.listing_views import meli_listings
from meli.views import (
    mercadolibre_callback,
    mercadolibre_connect,
    mercadolibre_disconnect,
    mercadolibre_question_reply,
    mercadolibre_questions,
    mercadolibre_postsale_sync,
    mercadolibre_questions_sync,
    mercadolibre_settings,
    mercadolibre_token,
    mercadolibre_webhook,
)
from tasks.api import api
from tasks.views import (
    cashflow_csv,
    cashflow_drive_token,
    cashflow_list,
    cashflow_parser_key,
    cashflow_parser_run,
    cashflow_settings,
    health,
)

admin.site.site_header = "Vimple"
admin.site.site_title = "Vimple"
admin.site.index_title = "Finances"

urlpatterns = [
    path("", home, name="home"),
    path("profile/", profile, name="profile"),
    path("profile/menu/", profile_menu, name="profile_menu"),
    path("profile/unread/", profile_unread, name="profile_unread"),
    path("organization/setup/", organization_setup, name="organization_setup"),
    path("organization/", organization_settings, name="organization"),
    path("organization/members/", members, name="members"),
    path("organization/switch/", organization_switch, name="organization_switch"),
    path("organization/delete/", organization_delete, name="organization_delete"),
    path("cashflow/", cashflow_list, name="cashflow"),
    path("cashflow/export.csv", cashflow_csv, name="cashflow_csv"),
    path("cashflow/settings/", cashflow_settings, name="cashflow_settings"),
    path("cashflow/settings/run/", cashflow_parser_run, name="cashflow_parser_run"),
    path("cashflow/settings/key/", cashflow_parser_key, name="cashflow_parser_key"),
    path("cashflow/settings/drive-token/", cashflow_drive_token, name="cashflow_drive_token"),
    path("catalog/meli/", meli_listings, name="meli_listings"),
    path("catalog/import/", catalog_import, name="catalog_import"),
    path("catalog/meli/new/", meli_drafts, name="meli_drafts"),
    path("catalog/meli/new/<int:pk>/", meli_draft, name="meli_draft"),
    path("catalog/meli/new/<int:pk>/ai/", meli_draft_ai, name="meli_draft_ai"),
    path("catalog/meli/new/<int:pk>/copy/", meli_draft_copy, name="meli_draft_copy"),
    path("catalog/meli/products/<uuid:pk>/", meli_product_actions, name="meli_product_actions"),
    path("catalog/meli/products/<uuid:pk>/draft/", meli_draft_from_product, name="meli_draft_from_product"),
    path("catalog/meli/combos/<int:pk>/", meli_combo_actions, name="meli_combo_actions"),
    path("catalog/meli/categories/", meli_categories, name="meli_categories"),
    path("catalog/meli/combos/<int:pk>/draft/", meli_draft_from_combo, name="meli_draft_from_combo"),
    path("catalog/", include("catalog.urls")),
    path("purchases/", include("purchasing.urls")),
    path("inventory/", include("inventory.urls")),
    path("access/", access, name="access"),
    path("access/<int:user_id>/", set_app_access, name="set_app_access"),
    path("accounts/", include("allauth.urls")),
    path("health/", health, name="health"),
    path("support/", include("support.urls")),
    path("inbox/", inbox, name="inbox"),
    path("inbox/<uuid:conversation_id>/reply/", inbox_reply, name="inbox_reply"),
    path("meli/", mercadolibre_settings, name="mercadolibre_settings"),
    path("meli/questions/", mercadolibre_questions, name="mercadolibre_questions"),
    path(
        "meli/questions/sync/",
        mercadolibre_questions_sync,
        name="mercadolibre_questions_sync",
    ),
    path(
        "meli/questions/<str:question_id>/reply/",
        mercadolibre_question_reply,
        name="mercadolibre_question_reply",
    ),
    path(
        "meli/postsale/sync/",
        mercadolibre_postsale_sync,
        name="mercadolibre_postsale_sync",
    ),
    path("meli/token/", mercadolibre_token, name="mercadolibre_token"),
    path("meli/connect/", mercadolibre_connect, name="mercadolibre_connect"),
    path("meli/callback/", mercadolibre_callback, name="mercadolibre_callback"),
    path("meli/disconnect/", mercadolibre_disconnect, name="mercadolibre_disconnect"),
    path("meli/webhooks/<str:secret>/", mercadolibre_webhook, name="mercadolibre_webhook"),
    path("admin/impersonate/exit/", impersonate_exit, name="impersonate_exit"),
    path("admin/impersonate/<int:user_id>/", impersonate, name="impersonate"),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
