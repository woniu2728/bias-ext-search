from django.apps import AppConfig

class SearchExtensionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bias_ext_search.backend"
    label = "search"
