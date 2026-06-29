from __future__ import annotations

from bias_core.extensions import FrontendExtender


def frontend_extender():
    return (
        FrontendExtender(
            admin_entry="extensions/search/frontend/admin/index.js",
            forum_entry="extensions/search/frontend/forum/index.js",
        )
        .route(
            "/search",
            "search",
            "./SearchResultsView.vue",
            title="搜索",
            description="搜索论坛中的讨论、回复和用户。",
            order=40,
        )
    )
