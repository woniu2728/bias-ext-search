from __future__ import annotations

from bias_core.extensions import (
    ApiRoutesExtender,
    ForumCapabilitiesExtender,
    LifecycleExtender,
    RuntimeServiceContractExtender,
    ServiceProviderExtender,
)

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
        RuntimeServiceContractExtender().service(
            "search.service",
            required_methods=(
                "apply_discussion_search",
                "apply_post_search",
                "apply_user_search",
                "build_search_context",
                "get_public_search_filters",
                "get_search_suggestions",
                "normalize_limit",
                "normalize_page",
                "search_all",
                "search_discussions",
                "search_posts",
                "search_preview",
                "search_users",
            ),
        ),
        LifecycleExtender(),
    )
