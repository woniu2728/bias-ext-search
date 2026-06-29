from __future__ import annotations

from bias_core.extensions import SearchFilterDefinition

from bias_ext_search.backend.constants import EXTENSION_ID
from bias_ext_search.backend.filters import (
    apply_discussion_author_search_filter,
    apply_discussion_created_month_search_filter,
    apply_discussion_locked_search_filter,
    apply_discussion_sticky_search_filter,
    apply_discussion_unread_search_filter,
    apply_post_author_search_filter,
    apply_post_created_month_search_filter,
    parse_author_search_filter,
    parse_created_month_search_filter,
    parse_locked_search_filter,
    parse_sticky_search_filter,
    parse_unread_search_filter,
)


def search_filter_definitions():
    return (
        SearchFilterDefinition(
            code="author",
            label="按作者过滤",
            module_id=EXTENSION_ID,
            target="discussion",
            parser=parse_author_search_filter,
            applier=apply_discussion_author_search_filter,
            syntax="author:<username>",
            description="按讨论作者用户名过滤搜索结果。",
        ),
        SearchFilterDefinition(
            code="author",
            label="按作者过滤",
            module_id=EXTENSION_ID,
            target="post",
            parser=parse_author_search_filter,
            applier=apply_post_author_search_filter,
            syntax="author:<username>",
            description="按回复作者用户名过滤搜索结果。",
        ),
        SearchFilterDefinition(
            code="is_sticky",
            label="仅置顶讨论",
            module_id=EXTENSION_ID,
            target="discussion",
            parser=parse_sticky_search_filter,
            applier=apply_discussion_sticky_search_filter,
            syntax="is:sticky",
            description="仅返回已置顶的讨论。",
        ),
        SearchFilterDefinition(
            code="is_locked",
            label="仅锁定讨论",
            module_id=EXTENSION_ID,
            target="discussion",
            parser=parse_locked_search_filter,
            applier=apply_discussion_locked_search_filter,
            syntax="is:locked",
            description="仅返回已锁定的讨论。",
        ),
        SearchFilterDefinition(
            code="is_unread",
            label="仅未读讨论",
            module_id=EXTENSION_ID,
            target="discussion",
            parser=parse_unread_search_filter,
            applier=apply_discussion_unread_search_filter,
            syntax="is:unread",
            description="仅返回当前用户还有未读回复的讨论。",
        ),
        SearchFilterDefinition(
            code="created",
            label="按创建月份过滤",
            module_id=EXTENSION_ID,
            target="discussion",
            parser=parse_created_month_search_filter,
            applier=apply_discussion_created_month_search_filter,
            syntax="created:YYYY-MM",
            description="按讨论创建月份过滤搜索结果。",
        ),
        SearchFilterDefinition(
            code="created",
            label="按创建月份过滤",
            module_id=EXTENSION_ID,
            target="post",
            parser=parse_created_month_search_filter,
            applier=apply_post_created_month_search_filter,
            syntax="created:YYYY-MM",
            description="按回复创建月份过滤搜索结果。",
        ),
    )
