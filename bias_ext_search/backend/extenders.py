from __future__ import annotations

from bias_core.extensions import ApiRoutesExtender, ForumCapabilitiesExtender, LifecycleExtender, ServiceProviderExtender

from bias_ext_search.backend.admin_api import router as search_admin_router
from bias_ext_search.backend.api import router as search_router
from bias_ext_search.backend.frontend import frontend_extender
from bias_ext_search.backend.runtime import search_service_provider
from bias_ext_search.backend.search_contracts import search_filter_definitions


def frontend_extenders():
    return (frontend_extender(),)


def route_extenders():
    return (
        ApiRoutesExtender(
            mounts=(("", search_router), ("/admin", search_admin_router)),
            tags=("Search",),
        ),
    )


def forum_extenders():
    return (
        ForumCapabilitiesExtender(
            search_filters=search_filter_definitions(),
        ),
    )


def service_extenders():
    return (
        ServiceProviderExtender(
            key="search.service",
            provider=search_service_provider,
        ),
        LifecycleExtender(),
    )
