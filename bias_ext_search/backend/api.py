from __future__ import annotations

from django.http import JsonResponse
from ninja import Router

from bias_core.extensions.platform import api_error
from bias_core.extensions.platform import get_optional_user
from bias_core.extensions.runtime import get_runtime_resource_registry
from bias_core.extensions.platform import has_forum_permission
from bias_core.extensions.platform import ResourceQueryOptions, apply_resource_preloads, parse_resource_query_options
from bias_ext_search.backend.schemas import SearchFilterCatalogSchema, SearchSuggestionSchema
from bias_ext_search.backend.services import SearchService


router = Router()


def _get_resource_registry():
    return get_runtime_resource_registry()


def serialize_discussion_search_result(discussion, resource_options=None):
    resource_options = resource_options or ResourceQueryOptions()
    return _get_resource_registry().serialize(
        "search_discussion",
        discussion,
        only=resource_options.fields,
        include=resource_options.includes,
    )


def serialize_post_search_result(post, resource_options=None):
    resource_options = resource_options or ResourceQueryOptions()
    return _get_resource_registry().serialize(
        "search_post",
        post,
        only=resource_options.fields,
        include=resource_options.includes,
    )


def serialize_user_search_result(user, resource_options=None):
    resource_options = resource_options or ResourceQueryOptions()
    return _get_resource_registry().serialize(
        "search_user",
        user,
        only=resource_options.fields,
    )


def serialize_extra_search_result(target: str, item, resource_options=None):
    resource = SearchService.get_extra_search_target_config(target).get("resource") or target
    resource_options = resource_options or ResourceQueryOptions()
    return _get_resource_registry().serialize(
        resource,
        item,
        only=resource_options.fields,
        include=resource_options.includes,
    )


def serialize_search_filter(definition):
    return {
        "code": definition.code,
        "label": definition.label,
        "module_id": definition.module_id,
        "target": definition.target,
        "syntax": definition.syntax,
        "description": definition.description,
    }


@router.get("/search", tags=["Search"])
def search(
    request,
    q: str,
    type: str = "all",
    page: int = 1,
    limit: int = 20,
    preview: bool = False,
):
    page = SearchService.normalize_page(page)
    limit = SearchService.normalize_limit(limit)

    if not q or len(q.strip()) == 0:
        payload = {
            "total": 0,
            "page": page,
            "limit": limit,
            "type": type,
            "discussion_total": 0,
            "post_total": 0,
            "user_total": 0,
            "discussions": [],
            "posts": [],
            "users": [],
        }
        for target in SearchService.get_extra_search_targets():
            payload[f"{target}_total"] = 0
            payload[SearchService.get_extra_search_results_key(target)] = []
        return payload

    query = q.strip()
    user = get_optional_user(request)

    if user and user.is_authenticated and not has_forum_permission(user, "viewForum"):
        return api_error("没有权限访问搜索", status=403)

    can_search_users = user and has_forum_permission(user, "searchUsers")
    discussion_resource_options = parse_resource_query_options(request, "search_discussion")
    post_resource_options = parse_resource_query_options(request, "search_post")
    user_resource_options = parse_resource_query_options(request, "search_user")
    extra_targets = SearchService.get_extra_search_targets()
    extra_resource_options = {
        target: parse_resource_query_options(
            request,
            SearchService.get_extra_search_target_config(target).get("resource") or target,
        )
        for target in extra_targets
    }

    if preview and type == "all":
        result = SearchService.search_preview(
            query,
            limit=limit,
            user=user,
            include_users=can_search_users,
        )
        response_payload = {
            **result,
            "discussions": [
                serialize_discussion_search_result(item, resource_options=discussion_resource_options)
                for item in result["discussions"]
            ],
            "posts": [
                serialize_post_search_result(item, resource_options=post_resource_options)
                for item in result["posts"]
            ],
            "users": [
                serialize_user_search_result(item, resource_options=user_resource_options)
                for item in result["users"]
            ],
        }
        _serialize_extra_search_sections(response_payload, result, extra_resource_options)
        return JsonResponse(response_payload)

    context = SearchService.build_search_context(query, user=user, include_users=can_search_users)

    if type == "all":
        result = SearchService.search_all(
            query,
            page,
            limit,
            user=user,
            include_users=can_search_users,
            context=context,
        )
        response_payload = {
            **result,
            "discussions": [
                serialize_discussion_search_result(item, resource_options=discussion_resource_options)
                for item in result["discussions"]
            ],
            "posts": [
                serialize_post_search_result(item, resource_options=post_resource_options)
                for item in result["posts"]
            ],
            "users": [
                serialize_user_search_result(item, resource_options=user_resource_options)
                for item in result["users"]
            ],
        }
        _serialize_extra_search_sections(response_payload, result, extra_resource_options)
        return JsonResponse(response_payload)

    if type == "discussions":
        if not SearchService.has_search_target("discussion"):
            return api_error("搜索目标不可用", status=400)

        discussions, total = SearchService.search_discussions(
            query,
            page,
            limit,
            user=user,
            context=context,
            preload=lambda queryset: apply_resource_preloads(
                _get_resource_registry(),
                queryset,
                "search_discussion",
                resource_options=discussion_resource_options,
                default_includes=("user",),
            ),
        )
        discussion_data = [
            serialize_discussion_search_result(discussion, resource_options=discussion_resource_options)
            for discussion in discussions
        ]

        return JsonResponse({
            "total": total,
            "page": page,
            "limit": limit,
            "type": type,
            "discussion_total": total,
            "post_total": context.post_total,
            "user_total": context.user_total,
            "discussions": discussion_data,
            "posts": [],
            "users": [],
        })

    if type == "posts":
        if not SearchService.has_search_target("post"):
            return api_error("搜索目标不可用", status=400)

        posts, total = SearchService.search_posts(
            query,
            page,
            limit,
            user=user,
            context=context,
            preload=lambda queryset: apply_resource_preloads(
                _get_resource_registry(),
                queryset,
                "search_post",
                resource_options=post_resource_options,
                default_includes=("user",),
            ),
        )
        post_data = [
            serialize_post_search_result(post, resource_options=post_resource_options)
            for post in posts
        ]

        return JsonResponse({
            "total": total,
            "page": page,
            "limit": limit,
            "type": type,
            "discussion_total": context.discussion_total,
            "post_total": total,
            "user_total": context.user_total,
            "discussions": [],
            "posts": post_data,
            "users": [],
        })

    if type == "users":
        if not SearchService.has_search_target("user"):
            return api_error("搜索目标不可用", status=400)

        if not can_search_users:
            return api_error("没有权限搜索用户", status=403)

        users, total = SearchService.search_users(
            query,
            page,
            limit,
            context=context,
            preload=lambda queryset: apply_resource_preloads(
                _get_resource_registry(),
                queryset,
                "search_user",
                resource_options=user_resource_options,
            ),
        )

        return JsonResponse({
            "total": total,
            "page": page,
            "limit": limit,
            "type": type,
            "discussion_total": context.discussion_total,
            "post_total": context.post_total,
            "user_total": total,
            "discussions": [],
            "posts": [],
            "users": [serialize_user_search_result(item, resource_options=user_resource_options) for item in users],
        })

    if type in extra_targets:
        items, total = SearchService.search_extra_target(
            type,
            query,
            page,
            limit,
            user=user,
            context=context,
            preload=lambda queryset: apply_resource_preloads(
                _get_resource_registry(),
                queryset,
                SearchService.get_extra_search_target_config(type).get("resource") or type,
                resource_options=extra_resource_options[type],
            ),
        )
        results_key = SearchService.get_extra_search_results_key(type)
        payload = {
            "total": total,
            "page": page,
            "limit": limit,
            "type": type,
            "discussion_total": context.discussion_total,
            "post_total": context.post_total,
            "user_total": context.user_total,
            "discussions": [],
            "posts": [],
            "users": [],
        }
        for target in extra_targets:
            payload[f"{target}_total"] = total if target == type else context.extra_totals.get(target, 0)
            payload[SearchService.get_extra_search_results_key(target)] = (
                [
                    serialize_extra_search_result(target, item, resource_options=extra_resource_options[target])
                    for item in items
                ]
                if target == type
                else []
            )
        payload[results_key] = payload.get(results_key, [])
        return JsonResponse(payload)

    return api_error("无效的搜索类型", status=400)


def _serialize_extra_search_sections(response_payload: dict, result: dict, extra_resource_options: dict) -> None:
    extra_results = result.get("extra_results") or {}
    for target, items in extra_results.items():
        results_key = SearchService.get_extra_search_results_key(target)
        options = extra_resource_options.get(target)
        response_payload[results_key] = [
            serialize_extra_search_result(target, item, resource_options=options)
            for item in items
        ]
    response_payload["extra_results"] = {
        target: response_payload.get(SearchService.get_extra_search_results_key(target), [])
        for target in extra_results
    }


@router.get("/search/suggestions", response=SearchSuggestionSchema, tags=["Search"])
def get_search_suggestions(request, q: str, limit: int = 5):
    if not q or len(q.strip()) == 0:
        return {"suggestions": []}

    user = get_optional_user(request)

    if user and user.is_authenticated and not has_forum_permission(user, "viewForum"):
        return api_error("没有权限访问搜索", status=403)

    suggestions = SearchService.get_search_suggestions(q.strip(), limit, user=user)
    return {"suggestions": suggestions}


@router.get("/search/filters", response=SearchFilterCatalogSchema, tags=["Search"])
def get_search_filters(request, target: str = "all"):
    normalized_target = (target or "all").strip().lower()
    builtin_targets = SearchService.get_builtin_search_targets()
    target_map = {
        "all": tuple(item for item in ("discussion", "post") if item in builtin_targets),
        "discussions": ("discussion",) if "discussion" in builtin_targets else (),
        "discussion": ("discussion",) if "discussion" in builtin_targets else (),
        "posts": ("post",) if "post" in builtin_targets else (),
        "post": ("post",) if "post" in builtin_targets else (),
    }
    targets = target_map.get(normalized_target)
    if targets is None and normalized_target in SearchService.get_extra_search_targets():
        targets = (normalized_target,)
    if targets is None:
        return api_error("无效的搜索过滤目标", status=400)

    return {
        "target": target,
        "filters": [
            serialize_search_filter(item)
            for item in SearchService.get_public_search_filters(targets=targets)
        ],
    }
